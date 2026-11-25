"""Namespace layout and transport of scoped Lake compiler artifacts."""

from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from lean_artifact_inventory import Module, PROJECT_BUILD
from lean_module_shards import (DEFAULT_CAPACITY, inspector_publication, lookup_shard, pack_shard,
                                plan_shards, report_paths, restore_shard)


class NamespaceLayout(unittest.TestCase):
    def test_split_stability_single_oversize_and_explicit_owner(self):
        sizes = {'D5.Foo': 1, 'D5.Foo.Big': 20, 'D5.Foo.Big.Small': 2,
                 'D5.Foobar': 3, 'D5.Else': 1, 'D5.Leaf.Only': 15}
        layout = plan_shards(sizes, 8)
        self.assertEqual(layout, {'D5': ['D5.Else', 'D5.Foobar'], 'D5.Foo': ['D5.Foo'],
                                  'D5.Foo.Big': ['D5.Foo.Big'], 'D5.Foo.Big.Small': ['D5.Foo.Big.Small'],
                                  'D5.Leaf.Only': ['D5.Leaf.Only']})
        owners = {n: shard for shard, names in layout.items() for n in names}
        for name, owner in owners.items():
            self.assertEqual(lookup_shard(name, owners, layout), owner)
        self.assertEqual(plan_shards(dict(reversed(sizes.items())), 8), layout)
        reduced = plan_shards({name: 1 for name in sizes}, 8, previous=layout)
        self.assertEqual(reduced, layout)
        layout['D5'].append('External.Small')
        owners['External.Small'] = 'D5'
        self.assertEqual(lookup_shard('External.Small', owners, layout), 'D5')
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            lookup_shard('D5.Foo.Big', dict(owners, **{'D5.Foo.Big': 'D5'}), layout)

    def test_default_capacity_splits_large_groups_and_keeps_indivisible_modules(self):
        self.assertEqual(DEFAULT_CAPACITY, 256 * 1024**2)
        sizes = {'D5.A': 150 * 1024**2, 'D5.B': 110 * 1024**2, 'Reg.Large': 300 * 1024**2}
        self.assertEqual(plan_shards(sizes), {'D5': ['D5.B'], 'D5.A': ['D5.A'], 'Reg.Large': ['Reg.Large']})

    def test_two_small_siblings_share_one_named_shard(self):
        self.assertEqual(plan_shards({'Math.A': 1, 'Math.B': 1}, 2), {'Math': ['Math.A', 'Math.B']})


class ShardCompilerTransport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copyfile(Path(__file__).resolve().parents[3] / 'lean-toolchain', self.root / 'lean-toolchain')
        (self.root / 'lakefile.toml').write_text('name = "ns_fixture"\n[[lean_lib]]\nname = "Demo"\n')
        (self.root / 'Demo').mkdir()
        self.modules = {}
        for name in ('A', 'B'):
            source = Path('Demo', name + '.lean')
            (self.root / source).write_text('def Demo.value' + name + ' : Nat := 3\n')
            self.modules['Demo.' + name] = Module('ns_fixture', source, Path('.'))
        subprocess.run(['lake', 'build', '+Demo.A:leanArts', '+Demo.B:leanArts'], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL)
        self.build = self.root / PROJECT_BUILD
        path = self.build / 'lean-inspector/modules/Demo.A.zip'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'excluded report')
        self.archive = self.root / 'rev-Demo.tar.gz'
        self.descriptor = pack_shard(self.root, self.modules, self.modules, self.archive, 'c'*64)

    def test_real_compiler_archives_are_stable_scoped_and_full(self):
        again = self.root / 'second.tar.gz'
        self.assertEqual(pack_shard(self.root, self.modules, reversed(self.modules), again, 'c'*64), self.descriptor)
        self.assertEqual(self.archive.read_bytes(), again.read_bytes())
        with self.archive.open('rb') as archive:
            header = archive.read(75)
        self.assertEqual(header[:4], b'\x1f\x8b\x08\x08')
        self.assertEqual(header[10:], b'c'*64 + b'\0')
        with tarfile.open(self.archive) as bundle:
            self.assertFalse(any('lean-inspector' in p for p in bundle.getnames()))
        for names in (['Demo.A'], ['Demo.B'], list(self.modules)):
            dest = self.root / ('consumer-' + ''.join(names))
            active = {n: self.modules[n] for n in names}
            self.assertEqual(restore_shard(self.archive, self.descriptor, names, active, dest), set(names))
            self.assertFalse((dest / 'lean-inspector').exists())
            self.assertEqual({p.stem for p in (dest / 'lib/lean/Demo').glob('*.olean')}, {n[-1] for n in names})
            for artifact in dest.rglob('*'):
                if artifact.is_file():
                    self.assertEqual(artifact.read_bytes(), (self.build / artifact.relative_to(dest)).read_bytes())
        self.assertTrue(all(self.descriptor['module_sizes'][n] > 0 for n in self.modules))

    def prepare_report(self, name, *, broken_material=False, utility=None):
        module = self.modules[name]
        public = inspector_publication()
        material = b'Type'
        type_id, statement_id = public.materials.material_identities(
            io.BytesIO(material), module.source.as_posix(), 'def', name)
        row = dict(module=name, source_path=module.source.as_posix(),
                   source_sha256='sha256:' + public.digest(self.root / module.source), imports=[],
                   declarations=[dict(axioms=[], include_in_statement=True, kind='def', name=name,
                                      name_key=name, statement_id=statement_id, type_sha256=type_id)])
        if utility:
            row['utility_refutation'] = dict(claim_gid=utility['claimGid'], claim_source_path=utility['claimSourcePath'],
                                             claim_source_sha256=utility['claimSourceSha256'], result_gid=utility['resultGid'],
                                             is_closed_negation=True)
        input_zip = self.root / (name + ".zip")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            report = Path(directory) / public.RAW
            report.write_bytes(public.materials.canonical_json(dict(modules=[row], schema=public.materials.REPORT_SCHEMA)))
            with zipfile.ZipFile(public.member(report, '.materials.zip'), 'w') as materials:
                materials.writestr('sha256/' + type_id[7:], b'wrong' if broken_material else material)
            public.write_origin(report, name, dict(inspector_executable_sha256='a'*64, producer_sources_sha256='b'*64),
                                dict(schema='stratalint-judge-input-projection-v1', module=name, inputs=[]))
            public.zip_files(input_zip, [(public.RAW + suffix, public.member(report, suffix))
                                       for suffix in ('', '.materials.zip', '.provenance.json')])
        inputs = self.build / 'lean-inspector/inputs'
        inputs.mkdir(parents=True, exist_ok=True)
        record = dict(utilities=[utility] if utility else [], claims=[utility['claimModule']] if utility else [],
                      source_path=module.source.as_posix())
        (inputs / (name + '.json')).write_bytes(public.materials.canonical_json(record))
        (inputs.parent / 'report-format').write_text(public.selection.REPORT_FORMAT + '\n')
        # Exercise the same Lake artifact primitive used by the Inspector's report facet.
        config = self.root / 'reports.lean'
        config.write_text('''import Lake
open Lake DSL
package ns_fixture
lean_lib Demo
module_facet report (mod : Module) : System.FilePath := withCurrPackage mod.pkg do
  let input := mod.pkg.dir / s!"{mod.name}.zip"
  let file := mod.pkg.buildDir / "lean-inspector/modules" / s!"{mod.name}.zip"
  let sources ← inputBinFile mod.leanFile
  let utility ← inputBinFile (mod.pkg.buildDir / "lean-inspector/inputs" / s!"{mod.name}.json")
  let format ← inputBinFile (mod.pkg.buildDir / "lean-inspector/report-format")
  let exports ← mod.exportInfo.fetch
  let zip ← inputBinFile input
  (Job.nil.mix sources |>.mix utility |>.add exports |>.add zip |>.add format).mapM fun _ => do
    let info ← exports.await
    addTrace (info.allArtsTrace.mix info.legacyTransTrace)
    addTrace zip.getTrace
    addTrace format.getTrace
    let _ ← buildArtifactUnlessUpToDate file (do
      IO.FS.createDirAll file.parent.get!
      IO.FS.writeBinFile file (← IO.FS.readBinFile input)) (ext := "zip") (restore := true)
    return file
''')
        subprocess.run(['lake', '-f', config.name, '--no-cache', 'build', '+' + name + ':report'],
                       cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        self.modules[name] = replace(module, report=True, report_utility=utility)

    def test_module_reports_restore_together_with_the_requested_compiler_files(self):
        name = 'Demo.A'
        self.prepare_report(name)
        self.prepare_report('Demo.B')
        with self.assertRaisesRegex(ValueError, 'report inventory differs'):
            restore_shard(self.archive, self.descriptor, [name], self.modules, self.root / 'legacy', root=self.root)
        self.descriptor = pack_shard(self.root, self.modules, self.modules, self.archive, 'c'*64)
        consumer = self.root / 'consumer'
        dest = consumer / PROJECT_BUILD
        archive, sidecar, trace = report_paths(name)
        # A matching warm Lake trace can record a different producer checkout path.
        (dest / trace).parent.mkdir(parents=True)
        local_trace = json.loads((self.build / trace).read_bytes())
        local_trace = json.loads(json.dumps(local_trace).replace(str(self.root.resolve()), str(consumer.resolve())))
        (dest / trace).write_text(json.dumps(local_trace))
        self.assertEqual(restore_shard(self.archive, self.descriptor, [name], self.modules, dest, root=self.root), {name})
        self.assertEqual(json.loads((dest / trace).read_bytes()), local_trace)
        for path in (archive, sidecar):
            self.assertEqual((dest / path).read_bytes(), (self.build / path).read_bytes())
        self.assertFalse((dest / report_paths('Demo.B')[0]).exists())
        self.assertTrue((dest / 'lib/lean/Demo/A.olean').is_file())
        for path in (Path('lean-toolchain'), Path('reports.lean'), Path(name + '.zip'), self.modules[name].source,
                     PROJECT_BUILD / 'lean-inspector' / 'report-format',
                     PROJECT_BUILD / 'lean-inspector' / 'inputs' / (name + '.json')):
            (consumer / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / path, consumer / path)
        subprocess.run(['lake', '-f', 'reports.lean', '--no-cache', '--no-build', 'build', '+' + name + ':report'],
                       cwd=consumer, check=True, stdout=subprocess.DEVNULL)

        conflict = self.root / 'conflict'
        (conflict / archive).parent.mkdir(parents=True)
        (conflict / archive).write_bytes(b'local report')
        self.assertEqual(restore_shard(self.archive, self.descriptor, [name], self.modules, conflict, root=self.root), set())
        self.assertEqual((conflict / archive).read_bytes(), b'local report')
        self.assertFalse((conflict / 'lib/lean/Demo/A.olean').exists())

    def test_compiler_only_restore_leaves_stale_reports_out_of_the_requested_outputs(self):
        names = ['Demo.A', 'Demo.B']
        for name in names:
            self.prepare_report(name)
        descriptor = pack_shard(self.root, self.modules, names, self.archive, 'c'*64)
        for local, report_eligible in ((False, True), (True, True), (True, False)):
            with self.subTest(local=local, report_eligible=report_eligible):
                dest = self.root / f'consumer-{local}-{report_eligible}'
                expected = {}
                if local:
                    for path in report_paths('Demo.A'):
                        (dest / path).parent.mkdir(parents=True, exist_ok=True)
                        expected[path] = ('previous local ' + path.name).encode()
                        (dest / path).write_bytes(expected[path])
                modules = dict(self.modules)
                modules['Demo.A'] = replace(modules['Demo.A'], report=report_eligible,
                                            conditional_backends=True, extra_builds=(Path('omega-strict'),))
                installed = restore_shard(self.archive, descriptor, names, modules, dest, root=self.root,
                                          compiler_only=['Demo.A'])
                self.assertEqual(modules['Demo.A'].extra_builds, ())
                self.assertEqual(modules['Demo.A'].report, report_eligible)
                self.assertEqual(installed, set(names))
                for name in names:
                    self.assertTrue((dest / f'lib/lean/Demo/{name[-1]}.olean').is_file())
                for path in report_paths('Demo.A'):
                    self.assertEqual((dest / path).read_bytes() if (dest / path).exists() else None,
                                     expected.get(path))
                for path in report_paths('Demo.B'):
                    self.assertEqual((dest / path).read_bytes(), (self.build / path).read_bytes())

    def test_current_claim_source_is_required_for_report_reuse(self):
        name = 'Demo.A'
        claim = self.modules['Demo.B'].source
        utility = dict(modulePath='Demo/A.lean', resultModule=name, resultGid='result', resultSelector='valueA',
                       claimGid='claim', claimModule='Demo.B', claimSelector='valueB', claimSourcePath=claim.as_posix(),
                       claimSourceSha256='sha256:' + inspector_publication().digest(self.root / claim))
        self.prepare_report(name, utility=utility)
        descriptor = pack_shard(self.root, self.modules, [name], self.archive, 'c'*64)
        for differing in (None, dict(utility, resultGid='wrong'), dict(utility, resultModule='Demo.B')):
            with self.subTest(utility=differing), self.assertRaisesRegex(ValueError, 'utility'):
                modules = {name: replace(self.modules[name], report_utility=differing)}
                restore_shard(self.archive, descriptor, [name], modules, self.root / 'unused', root=self.root)
        with (self.root / claim).open('a') as source:
            source.write('-- changed claim only\n')
        for action in (lambda: pack_shard(self.root, self.modules, [name], self.root / 'stale.tar.gz', 'c'*64),
                       lambda: restore_shard(self.archive, descriptor, [name], self.modules, self.root / 'unused', root=self.root)):
            with self.assertRaisesRegex(ValueError, 'stale or unsafe report utility claim'):
                action()
        self.assertFalse((self.root / 'unused').exists())

    def test_invalid_report_traces_and_compiler_fallback_with_local_report_conflict(self):
        name = 'Demo.A'
        self.prepare_report(name)
        descriptor = pack_shard(self.root, self.modules, [name], self.archive, 'c'*64)
        relative = report_paths(name)[2]
        path = self.build / relative
        native_trace = json.loads(path.read_bytes())
        for field, value in [('inputs', 'malformed'), ('inputs', [['source', False]]), ('log', [None]),
                             ('schemaVersion', 'unknown'), ('depHash', '0'*16)]:
            with self.subTest(field=field, value=value):
                broken = dict(native_trace, **{field: value})
                path.write_text(json.dumps(broken))
                with self.assertRaisesRegex(ValueError, 'Lake artifact or report trace'):
                    pack_shard(self.root, self.modules, [name], self.root/'bad.tar.gz', 'c'*64)
        path.write_text(json.dumps(dict(native_trace, synthetic=True, inputs=[])))
        pack_shard(self.root, self.modules, [name], self.root/'synthetic.tar.gz', 'd'*64)
        duplicate = '{"inputs":false,' + json.dumps(native_trace)[1:]
        path.write_text(duplicate)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
            pack_shard(self.root, self.modules, [name], self.root/'duplicate.tar.gz', 'c'*64)
        path.write_text(json.dumps(native_trace))
        for label, invalid_live in [('malformed', json.dumps(dict(native_trace, inputs='malformed'))),
                                    ('duplicate', duplicate)]:
            with self.subTest(local=label):
                dest = self.root / label
                (dest / relative).parent.mkdir(parents=True)
                (dest / relative).write_text(invalid_live)
                hits = set()
                self.assertEqual(restore_shard(self.archive, descriptor, [name], self.modules, dest,
                                               root=self.root, compiler_hits=hits), set())
                self.assertEqual(hits, {name})
                self.assertTrue((dest/'lib/lean/Demo/A.olean').is_file())
                self.assertEqual((dest / relative).read_text(), invalid_live)
                self.assertFalse((dest / report_paths(name)[0]).exists())
                self.assertFalse((dest / report_paths(name)[1]).exists())

    def test_crossed_missing_unsafe_and_corrupt_report_material(self):
        name = 'Demo.A'
        self.prepare_report(name)
        self.prepare_report('Demo.B')
        descriptor = pack_shard(self.root, self.modules, [name], self.archive, 'c'*64)
        archive, _, trace = report_paths(name)
        rejected = self.root / 'rejected.tar.gz'
        # Substitute all three artifacts from a different, internally consistent module.
        for left, right in zip(report_paths(name), report_paths('Demo.B'), strict=True):
            shutil.copyfile(self.build / right, self.build / left)
        with self.assertRaisesRegex(ValueError, 'current module or source'):
            pack_shard(self.root, self.modules, [name], rejected, 'c'*64)
        with (self.root / self.modules[name].source).open('a') as source:
            source.write('-- different current bytes\n')
        with self.assertRaisesRegex(ValueError, 'current module or source'):
            restore_shard(self.archive, descriptor, [name], self.modules, self.root / 'fresh', root=self.root)
        self.assertFalse((self.root / 'fresh').exists())

        self.prepare_report(name, broken_material=True)
        with self.assertRaisesRegex(ValueError, 'material or declaration identity mismatch'):
            pack_shard(self.root, self.modules, [name], rejected, 'c'*64)
        (self.build / trace).unlink()
        with self.assertRaisesRegex(ValueError, 'missing module report'):
            pack_shard(self.root, self.modules, [name], rejected, 'c'*64)
        (self.build / trace).symlink_to(self.build / archive)
        with self.assertRaisesRegex(ValueError, 'unsafe compiler artifact'):
            pack_shard(self.root, self.modules, [name], rejected, 'c'*64)
        self.assertFalse(rejected.exists())

    def test_conflicting_module_is_skipped_and_malformed_input_cannot_install(self):
        dest = self.root / 'consumer'
        existing = dest / 'lib/lean/Demo/A.olean'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'local')
        self.assertEqual(restore_shard(self.archive, self.descriptor, self.modules, self.modules, dest), {'Demo.B'})
        self.assertEqual(existing.read_bytes(), b'local')
        self.assertFalse(existing.with_suffix('.ilean').exists())
        self.assertTrue(existing.with_name('B.olean').is_file())
        for patch in ({'members': ['Demo.A']}, {'sha256': '0' * 64}):
            fresh = self.root / 'fresh'
            with self.assertRaises(ValueError):
                restore_shard(self.archive, {**self.descriptor, **patch}, ['Demo.A'], self.modules, fresh)
            self.assertFalse(fresh.exists())


if __name__ == '__main__':
    unittest.main()
