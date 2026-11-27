"""End-to-end compiler shard selection, incremental publication and restart with real Lake artifacts."""

from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import lean_module_cache as cache
from lean_artifact_inventory import Module, PROJECT_BUILD
from lean_module_shards import pack_shard, restore_shard


class MemoryRemote:
    def __init__(self):
        self.indexes = {}
        self.rows = {}
        self.assets = {}
        self.layouts = {}
        self.publications = {}
        self.saves = []
        self.fail = None

    def confirmed(self, rows):
        return rows

    def latest(self, version=None):
        return self.indexes.get(version)

    def partials(self, plan):
        return {n: self.rows[r['fingerprint']] for n, r in plan['shards'].items() if r['fingerprint'] in self.rows}

    def save(self, plan, name, path, row):
        if self.fail == name:
            raise InterruptedError(name)
        key = row['version'], row['asset']
        assert key not in self.assets
        self.assets[key] = path.read_bytes()
        self.rows[row['fingerprint']] = row
        self.saves.append(name)
        return row

    def publication_layout(self, version):
        return self.publications.get(version)

    def save_publication_layout(self, version, layout):
        assert self.publications.setdefault(version, layout) == layout

    def splits(self, plan):
        return {n: self.layouts[n, r['fingerprint']] for n, r in plan['shards'].items() if (n, r['fingerprint']) in self.layouts}

    def save_split(self, plan, name, layout):
        fingerprint = plan['shards'][name]['fingerprint']
        assert self.layouts.setdefault((name, fingerprint), layout) == layout

    def download(self, row, output):
        output.write_bytes(self.assets[row['version'], row['asset']])

    def publish(self, index):
        assert self.indexes.setdefault(index['version'], index) == index


class ColdReportShards(unittest.TestCase):
    def test_saved_parents_and_split_children_survive_replanning(self):
        # Warm is an unchanged report. Compiler has no report to extract.
        targets = {n: {'key': n, 'deps': [], 'report_inputs': [n]} for n in
                   ('D5.A.One', 'D5.A.Two', 'D5.B', 'D5.C', 'D5.Warm',
                    'Reg.A', 'Reg.B', 'Reg.C', 'Edge.A', 'Edge.B', 'Edge.Warm')}
        targets['D5.Compiler'] = {'key': 'compiler', 'deps': []}
        remote = MemoryRemote()

        def plan(source, inputs, base=None):
            return cache.make_plan(source, 'm', inputs, base, remote, cache.DEFAULT_CAPACITY, 2,
                                   mathlib_version='v1')

        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 100):
            original = plan('old', targets)
            for row in original['shards'].values():
                remote.rows[row['fingerprint']] = dict(row, bytes=128, version=original['version'])
            base = cache.finish(original, remote)
        current = {n: dict(row, key='changed-' + n) if 'report_inputs' in row and not n.endswith('.Warm')
                   else row for n, row in targets.items()}
        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 100):
            unsplit = plan('new', current, base)
        # Reg was already uploaded by a worker from the old plan.
        reg = dict(unsplit['shards']['Reg'], bytes=256, version=unsplit['version'])
        remote.rows[reg['fingerprint']] = reg
        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 2):
            first = plan('new', current, base)
            self.assertEqual(first['inherited'], {'Reg': reg})
            self.assertEqual(first['shards'].keys(), {'D5', 'D5.A', 'Reg', 'Edge'})
            self.assertEqual(first['shards']['Edge'], unsplit['shards']['Edge'])
            self.assertEqual(first['shards']['D5']['members'], ['D5.B', 'D5.C', 'D5.Compiler', 'D5.Warm'])
            self.assertEqual(len(first['lanes']), 2)
            self.assertEqual(first, plan('new', dict(reversed(current.items())), base))

            # The byte-size check further split D5.A. One child completed before interruption.
            members = first['shards']['D5.A']['members']
            remote.save_split(first, 'D5.A', {n: [n] for n in members})
            parts = cache.expanded(first, remote.splits(first))
            done = parts['shards']['D5.A.One']
            remote.rows[done['fingerprint']] = dict(done, bytes=128, version=first['version'])
            retry = plan('new', current, base)
            self.assertEqual(retry['shards'], parts['shards'])
            self.assertEqual(retry['inherited'].keys(), {'Reg', 'D5.A.One'})
            self.assertCountEqual([n for lane in retry['lanes'] for n in lane], ['Edge', 'D5', 'D5.A.Two'])
            with self.assertRaisesRegex(ValueError, 'planned shards unavailable'):
                cache.finish(first, remote)
            for row in retry['shards'].values():
                remote.rows.setdefault(row['fingerprint'], dict(row, bytes=128, version=first['version']))
            finished = cache.finish(first, remote)
            self.assertEqual(cache.finish(retry, remote), finished)
            self.assertCountEqual([n for r in finished['shards'].values() for n in r['members']], targets)
            self.assertEqual(plan('later', current, finished)['lanes'], [])


class ModuleReleases(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pin = '1' * 40
        self.version = 'v4.35.0'
        self.source = 'a' * 40
        self.remote = MemoryRemote()
        self.modules = {}
        shutil.copyfile(Path(__file__).resolve().parents[3] / 'lean-toolchain', self.root / 'lean-toolchain')
        self.cfg = self.root / 'lakefile.toml'
        self.cfg.write_text('name = "trureturing"\n[[lean_lib]]\nname = "D5"\n[[lean_lib]]\nname = "Other"\n')
        for name, body in [('D5.A', 'def valueA := 1'), ('D5.B', 'def valueB := 7'),
                           ('D5.C', 'import D5.A\ndef valueC := valueA'),
                           ('Other.Auditor', 'def irrelevant := 0')]:
            source = Path(*name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', source, Path('.'))
            path = self.root / source
            path.parent.mkdir(exist_ok=True)
            path.write_text(body + '\n')
        self.lake('update')
        self.enterContext(patch.object(cache.inventory, 'workspace_modules', return_value=self.modules))
        self.enterContext(patch.object(cache, 'seed_partitions', side_effect=lambda _: [self.pin + '/linux-arm64']))
        self.enterContext(patch.object(cache.compiler, 'checkout', side_effect=lambda _: self.source))
        self.enterContext(patch.object(cache.compiler, 'compile_roots', side_effect=self.compile))
        self.enterContext(patch.object(cache.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 8*1024**3)))
        self.compiled = []

    def lake(self, *args):
        return subprocess.run(['lake', *args], cwd=self.root, check=True, text=True, capture_output=True)

    def compile(self, _, modules, names):
        self.compiled.append(names)
        self.lake('build', *['+' + n + ':leanArts' for n in names])

    def plan(self, base=None, capacity=cache.DEFAULT_CAPACITY):
        _, targets = cache.checkout_targets(self.root)
        return cache.make_plan(self.source, self.pin, targets, base, self.remote, capacity, 2, mathlib_version=self.version)

    def workers(self, plan):
        return [cache.work(self.root, plan, i, self.remote, self.root / 'receipts') for i in range(len(plan['lanes']))]

    def test_reports_share_the_target_identity_and_compile_only_seeds(self):
        from lean_cache_release import report_selection
        import lean_generation_facets as facets

        old = self.plan()
        self.workers(old)
        index = cache.finish(old, self.remote)
        (self.root / 'lean-report-inputs.json').write_text('{}')
        selection = self.enterContext(patch.object(report_selection, 'Selection')).return_value
        selection.modules.return_value = {'D5.A': 'D5/A.lean'}
        utility = {'modulePath': 'D5/A.lean', 'claimModule': 'D5.B'}
        self.enterContext(patch.object(facets, 'utility_inputs', return_value={'D5/A.lean': utility}))

        modules, with_reports = cache.checkout_targets(self.root)
        self.assertTrue(modules['D5.A'].report)
        self.assertEqual(sorted(with_reports['D5.A']['report_inputs']), ['D5.A', 'D5.B'])
        self.assertEqual(index['targets']['D5.A']['key'], with_reports['D5.A']['compiler_input'])
        self.assertNotEqual(index['targets']['D5.A']['key'], with_reports['D5.A']['key'])
        # The old reader cannot open a mixed namespace archive, even for an ordinary sibling.
        self.assertNotEqual(index['targets']['D5.C']['key'], with_reports['D5.C']['key'])
        self.assertEqual(index['targets']['D5.C']['key'], with_reports['D5.C']['compiler_input'])
        destination = self.root / 'restored'
        result = cache.restore(self.root, index, ['D5.C'], self.remote, destination)
        self.assertEqual((result['requested'], result['restored'], result['compiler_restored']), (2, 1, 2))
        self.assertEqual((result['reports_requested'], result['reports_restored']), (1, 0))
        self.assertEqual((result['supporting_compilers_requested'], result['supporting_compilers_restored']), (1, 1))
        self.assertTrue((destination/'lib/lean/D5/B.olean').is_file())
        self.assertTrue((destination/'lib/lean/D5/A.olean').is_file())
        self.assertFalse((destination/'lean-inspector/modules/D5.A.zip').exists())

        # The example is a consumer of the requested source and cannot be found through its imports.
        def example_scope(root, subset, *, scope_inputs):
            scope_inputs['D5/A.lean'] = ['D5.C']
            return {'D5/A.lean': utility}
        with patch.object(facets, 'utility_inputs', side_effect=example_scope):
            expanded = cache.restore(self.root, index, ['D5.A'], self.remote, self.root / 'example-scope')
            self.assertEqual(expanded['requested'], 2)
            self.assertTrue((self.root / 'example-scope/lib/lean/D5/C.olean').is_file())

        with patch.object(facets, 'utility_inputs', side_effect=FileNotFoundError('dotnet')) as reader:
            plain = cache.restore(self.root, index, ['D5.B'], self.remote, self.root / 'ordinary')
            self.assertEqual(plain['restored'], 1)
            reader.assert_not_called()
            cold = cache.restore(self.root, index, ['D5.A'], self.remote, self.root / 'compiler-seed')
            self.assertEqual((cold['reports_restored'], cold['compiler_restored']), (0, 1))

        (self.root / 'D5/B.lean').write_text('def valueB := 8')
        _, changed = cache.checkout_targets(self.root)
        self.assertNotEqual(changed['D5.A']['key'], with_reports['D5.A']['key'])
        self.assertEqual(changed['D5.A']['compiler_input'], with_reports['D5.A']['compiler_input'])
        self.assertEqual(changed['D5.C']['key'], with_reports['D5.C']['key'])
        with patch.object(report_selection, 'REPORT_FORMAT', 'changed'):
            _, new_format = cache.checkout_targets(self.root)
        self.assertNotEqual(new_format['D5.A']['key'], changed['D5.A']['key'])
        self.assertEqual(new_format['D5.B']['key'], changed['D5.B']['key'])

    def test_incremental_releases_and_scoped_consumers(self):
        plan = self.plan()
        self.assertEqual(set(plan['targets']), {'D5.A', 'D5.B', 'D5.C'})
        self.workers(plan)
        first = cache.finish(plan, self.remote)
        self.assertEqual(self.remote.saves, ['D5'])
        old_archive = self.remote.assets[first['version'], self.version + '-D5.tar.gz']
        for subset in (None, ['D5.A'], ['D5.C']):
            destination = self.root / ('consumer-' + str(subset))
            result = cache.restore(self.root, first, subset, self.remote, destination)
            self.assertEqual(result['restored'], {None: 3, 'D5.A': 1, 'D5.C': 2}[subset[0] if subset else None])
            self.assertFalse((destination / 'lean-inspector/modules').exists())
        cfg = self.cfg.read_text()
        self.cfg.write_text(cfg + '[[lean_exe]]\nname = "auditor"\nroot = "Other.Auditor"\n')
        (self.root / self.modules['Other.Auditor'].source).write_text('def irrelevant := 99\n')
        self.source = 'b' * 40
        self.assertEqual(self.plan(first)['lanes'], [])
        (self.root / self.modules['D5.A'].source).write_text('def valueA := 9\n')
        second_plan = self.plan(first)
        self.assertEqual({n for n, row in second_plan['targets'].items()
                          if row['key'] != first['targets'][n]['key']}, {'D5.A', 'D5.C'})
        shutil.rmtree(self.root / PROJECT_BUILD)
        self.assertEqual(self.workers(second_plan), [{'compiled_modules': 2, 'saved_shards': 1}])
        second = cache.finish(second_plan, self.remote)
        self.assertEqual(first['shards']['D5']['asset'], second['shards']['D5']['asset'])
        self.assertEqual(old_archive, self.remote.assets[first['version'], first['shards']['D5']['asset']])
        self.assertNotEqual(first['shards']['D5']['sha256'], second['shards']['D5']['sha256'])
        self.assertEqual(self.workers(self.plan(second)), [])

    def omega_fixture(self):
        for name in ('OmegaInit', 'OmegaMathlib', 'OmegaChecks'):
            with self.cfg.open('a') as config:
                config.write(f'[[lean_lib]]\nname = "{name}"\nsrcDir = "src"\n')
        for name, code in [('OmegaInit', 'module\n@[expose] public def omegaBase := 1\n'),
                           ('OmegaMathlib', 'import OmegaInit\ndef omegaValue := omegaBase + 1\n'),
                           ('OmegaChecks.Portable.Client', 'import OmegaMathlib\ndef checked := omegaValue\n')]:
            path = Path('src', *name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', path, Path('.'))
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(code)
        strict = self.root / 'strict'
        strict.mkdir()
        (strict / 'OmegaInit.lean').write_text('module\n@[expose] public def omegaBase := 7\n')
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 1\n')
        (self.root / 'D5/A.lean').write_text('import OmegaMathlib\ndef valueA := omegaValue\n')
        checks = self.root / 'src/OmegaChecks'
        for helper in ('strict_lake.py', 'portable.py', 'Portable/Exports.lean'):
            (checks / helper).write_text('')
        (checks / 'Portable/ArtifactHashes.lean').write_text('''import Lake.Build.Trace

def main : IO Unit := do
  for path in (← (← IO.getStdin).readToEnd).splitOn "\\n" do
    IO.println (← Lake.computeBinFileHash path).hex
''')
        common = self.root / '.lake/omega-common-src'

        def layout(strict_backend, with_reg, common_dir=None):
            self.assertFalse(with_reg)
            if not strict_backend and common_dir is None:
                return ['lake'], {'': self.root / PROJECT_BUILD / 'lib/lean'}
            label = 'omega-common' if common_dir else 'omega-strict'
            source = common_dir or 'strict'
            text = self.cfg.read_text()
            for name in ('OmegaInit', 'OmegaMathlib'):
                text = text.replace(f'name = "{name}"\nsrcDir = "src"', f'name = "{name}"\nsrcDir = "{source}"')
            build = PROJECT_BUILD / label
            config = self.root / '.lake' / (label + '.toml')
            config.write_text(f'buildDir = "{build}"\n' + text)
            return ['lake', '-f', str(config)], {'': self.root / build / 'lib/lean'}

        def derived_facades(*args):
            names = args[2]
            normal = {n: (self.root / self.modules[n].source).read_bytes() for n in names}
            generated = dict(normal, OmegaInit=b'module\n@[expose] public def omegaBase := 11\n')
            return normal, generated

        strict_api = SimpleNamespace(ROOT=self.root.resolve(), COMMON_SRC=common, lake_layout=layout)
        portable_api = SimpleNamespace(ROOT=self.root.resolve(), derived_facades=derived_facades,
                                       output=lambda *args: self.lake(*args[1:]).stdout)
        self.enterContext(patch.object(cache.importlib, 'import_module', side_effect=lambda name: {
            'strict_lake': strict_api, 'portable': portable_api}[name]))
        return portable_api

    def test_omega_backends_share_module_identity_and_restore_together(self):
        self.omega_fixture()
        strict = self.root / 'strict'
        checks = self.root / 'src/OmegaChecks'
        plan = self.plan()
        modules, _ = cache.checkout_targets(self.root)
        self.assertEqual(modules['OmegaMathlib'].extra_builds, (Path('omega-strict'), Path('omega-common')))
        self.assertEqual(modules['D5.A'].extra_builds, (Path('omega-strict'),))
        self.assertEqual(modules['D5.B'].extra_builds, ())
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'OmegaChecks.Portable.Client'} <= plan['targets'].keys())
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        self.assertFalse(self.plan(index)['lanes'])
        build = self.root / PROJECT_BUILD
        paths = ['lib/lean/OmegaInit.olean', 'omega-strict/lib/lean/OmegaInit.olean',
                 'omega-common/lib/lean/OmegaInit.olean']
        outputs = {path: (build / path).read_bytes() for path in paths}
        self.assertEqual(len(set(outputs.values())), 3)
        shutil.rmtree(build)
        result = cache.restore(self.root, index, ['D5.C', 'OmegaChecks.Portable.Client'], self.remote, build)
        self.assertEqual(result['restored'], 5)
        self.assertEqual({path: (build / path).read_bytes() for path in paths}, outputs)
        self.assertFalse((build / 'lib/lean/D5/B.olean').exists())
        for backend in (None, 'omega-strict', 'omega-common'):
            options = ['-f', str(self.root / '.lake' / (backend + '.toml'))] if backend else []
            self.lake(*options, '--no-build', 'build', '+OmegaChecks.Portable.Client:olean')

        name = 'OmegaInit'
        row = index['shards'][index['targets'][name]['shard']]
        archive = self.root / 'omega.tar.gz'
        self.remote.download(row, archive)

        # An independent official build has the same outputs but local paths in its trace.
        warm = self.root / 'warm'
        (warm / 'src').mkdir(parents=True)
        for source in ('lean-toolchain', 'lakefile.toml', 'src/OmegaInit.lean'):
            shutil.copyfile(self.root / source, warm / source)
        subprocess.run(['lake', 'build', '+OmegaInit:olean'], cwd=warm, check=True, capture_output=True)
        warm_build = warm / PROJECT_BUILD
        official_trace = Path(paths[0]).with_suffix('.trace')
        local_trace = (warm_build / official_trace).read_bytes()
        self.assertNotEqual(local_trace, (build / official_trace).read_bytes())
        for missing_backends in (('omega-strict', 'omega-common'), ('omega-strict',), ('omega-common',)):
            with self.subTest(missing_backends=missing_backends):
                for backend in missing_backends:
                    if (warm_build / backend).exists():
                        shutil.rmtree(warm_build / backend)
                result = cache.restore(self.root, index, [name], self.remote, warm_build)
                self.assertEqual(result['restored'], 1)
                self.assertEqual((warm_build / official_trace).read_bytes(), local_trace)
                self.assertEqual({path: (warm_build / path).read_bytes() for path in paths}, outputs)

        # Wrong bytes with the original trace and sidecar cannot be published or restored.
        for crossed_path in paths:
            with self.subTest(crossed_path=crossed_path):
                path = build / crossed_path
                wrong_bytes = outputs[paths[1] if crossed_path == paths[0] else paths[0]]
                path.write_bytes(wrong_bytes)
                try:
                    rejected = self.root / 'unpublishable.tar.gz'
                    with self.assertRaisesRegex(ValueError, 'artifact differs from its Lake trace'):
                        pack_shard(self.root, modules, [name], rejected, 'a' * 64)
                    self.assertFalse(rejected.exists())
                finally:
                    path.write_bytes(outputs[crossed_path])

                # Give the altered archive a valid transport checksum so restoration reaches Lake verification.
                crossed_archive = self.root / 'crossed.tar.gz'
                with tarfile.open(archive) as original, tarfile.open(crossed_archive, 'w:gz') as tampered:
                    for entry in original:
                        data = original.extractfile(entry).read()
                        if entry.name == crossed_path:
                            data = wrong_bytes
                        entry.size = len(data)
                        tampered.addfile(entry, io.BytesIO(data))
                payload = crossed_archive.read_bytes()
                crossed_row = dict(row, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
                fresh = self.root / 'unrestorable'
                with self.assertRaisesRegex(ValueError, 'artifact differs from its Lake trace'):
                    restore_shard(crossed_archive, crossed_row, [name], modules, fresh, root=self.root)
                self.assertFalse(fresh.exists())

        # A module with a different backend inventory cannot consume this member.
        crossed = {name: replace(modules[name], extra_builds=tuple(reversed(modules[name].extra_builds)))}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], crossed, self.root / 'crossed', root=self.root)
        conflict = self.root / 'conflict'
        conflicting = conflict / paths[1]
        conflicting.parent.mkdir(parents=True)
        conflicting.write_bytes(b'local strict artifact')
        self.assertEqual(restore_shard(archive, row, [name], modules, conflict, root=self.root), set())
        self.assertEqual(conflicting.read_bytes(), b'local strict artifact')
        self.assertFalse((conflict / paths[0]).exists())
        self.assertFalse((conflict / paths[2]).exists())
        official_only = {name: replace(modules[name], extra_builds=())}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], official_only, self.root / 'legacy')
        legacy = self.root / 'legacy.tar.gz'
        legacy_row = pack_shard(self.root, official_only, [name], legacy, 'a' * 64)
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(legacy, legacy_row, [name], modules, self.root / 'missing-backends')
        (build / paths[1]).unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete compiler artifacts'):
            pack_shard(self.root, modules, [name], self.root / 'incomplete.tar.gz', 'a' * 64)

        def changed():
            _, current = cache.checkout_targets(self.root)
            return {n for n in current if current[n]['key'] != index['targets'][n]['key']}

        # A direct OmegaMathlib consumer changes even without importing OmegaMath.
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 2\n')
        self.assertTrue({'OmegaMathlib', 'D5.A', 'D5.C', 'OmegaChecks.Portable.Client'} <= changed())
        self.assertNotIn('D5.B', changed())
        stale = cache.restore(self.root, index, ['D5.C'], self.remote, self.root / 'stale')
        self.assertEqual(stale['restored'], 0)
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 1\n')
        self.assertFalse(changed())
        (checks / 'Portable/Exports.lean').write_text('-- changed common generator\n')
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'OmegaChecks.Portable.Client', 'D5.A', 'D5.C'} <= changed())
        self.assertNotIn('D5.B', changed())
        (checks / 'strict_lake.py').write_text('# changed strict configuration\n')
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'D5.A', 'D5.C'} <= changed())

    def test_compiled_backend_selection_survives_scoped_cache_and_marker_removal(self):
        api = self.omega_fixture()
        with self.cfg.open('a') as config:
            config.write('[[lean_lib]]\nname = "OmegaBuild"\nsrcDir = "src"\n')
        name = 'OmegaBuild.Commands'
        source = Path('src/OmegaBuild/Commands.lean')
        self.modules[name] = Module('trureturing', source, Path('.'))
        (self.root / source).parent.mkdir(parents=True)
        (self.root / source).write_text('def commandProvider := true\n')
        for reader_name, code in [('OmegaBuild.Plan', 'import OmegaBuild.Parts'),
                                  ('OmegaBuild.Parts', 'def planSetting := 1')]:
            source = Path('src', *reader_name.split('.')).with_suffix('.lean')
            self.modules[reader_name] = Module('trureturing', source, Path('.'))
            (self.root / source).write_text(code + '\n')
        marked = self.root / 'D5/A.lean'
        marked.write_text('import OmegaBuild.Commands\nimport OmegaMathlib\ndef valueA := omegaValue\n')
        (self.root / 'D5/B.lean').write_text('import OmegaBuild.Commands\ndef valueB := commandProvider\n')
        (self.root / 'src/OmegaChecks/Portable/Client.lean').write_text('import D5.A\ndef portable := valueA\n')
        reasons = {'D5.A': ['D5.A'], 'D5.C': ['D5.C', 'D5.A'],
                   'OmegaChecks.Portable.Client': ['OmegaChecks.Portable.Client', 'D5.A']}
        read = set()

        def backend_plan(search_path, names):
            for module in names:
                artifact = Path(*module.split('.')).with_suffix('.olean')
                self.assertTrue(any((Path(d) / artifact).is_file() for d in search_path.split(os.pathsep)))
            read.update(names)
            return {'mathlibOnly': {n: reasons[n] for n in names if n in reasons}}

        api.backend_plan = backend_plan
        plan = self.plan()
        self.assertFalse(read)
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        self.assertEqual(read, set(reasons) | {'D5.B'})
        build = self.root / PROJECT_BUILD
        shutil.rmtree(build)
        read.clear()
        result = cache.restore(self.root, index, ['D5.C', 'D5.B', 'OmegaChecks.Portable.Client'],
                               self.remote, build)
        self.assertGreater(result['restored'], 0)
        self.assertFalse(read)
        for n in reasons:
            base = Path('lib/lean', *n.split('.')).with_suffix('.olean')
            self.assertTrue((build / base).is_file())
            self.assertFalse((build / 'omega-strict' / base).exists())
            self.assertFalse((build / 'omega-common' / base).exists())
        self.assertTrue((build / 'omega-strict/lib/lean/D5/B.olean').is_file())
        for filename in ('src/OmegaBuild/Plan.lean', 'src/OmegaBuild/Parts.lean', 'src/OmegaChecks/portable.py'):
            reader_file = self.root / filename
            original = reader_file.read_bytes()
            reader_file.write_bytes(original + b'\n')
            changed = self.plan(index)
            for name in set(reasons) | {'D5.B'}:
                self.assertNotEqual(changed['targets'][name]['key'], plan['targets'][name]['key'])
            stale = self.root / ('stale-' + reader_file.name)
            cache.restore(self.root, index, ['D5.C'], self.remote, stale)
            self.assertFalse((stale / 'lib/lean/D5/C.olean').exists())
            reader_file.write_bytes(original)
        self.source = 'b' * 40
        marked.write_text(marked.read_text().replace('def valueA', '-- removed marker\ndef valueA'))
        reasons.clear()
        next_plan = self.plan(index)
        self.assertNotEqual(plan['targets']['D5.C']['key'], next_plan['targets']['D5.C']['key'])
        self.workers(next_plan)
        for n in ('D5.A', 'D5.C', 'OmegaChecks.Portable.Client'):
            self.assertTrue((build / 'omega-strict/lib/lean' / Path(*n.split('.')).with_suffix('.olean')).exists())
        self.assertTrue((build / 'omega-common/lib/lean/OmegaChecks/Portable/Client.olean').exists())

    def test_new_names_use_the_mathlib_release_and_inherit_original_addresses(self):
        manifest = self.root / 'lake-manifest.json'
        manifest.write_text(json.dumps({'packages': [
            {'name': 'mathlib', 'rev': self.pin, 'inputRev': self.version}]}))
        self.assertEqual(cache.publication_mathlib(self.root), (self.pin, 'v4.35.0'))
        manifest.write_text(json.dumps({'packages': [{'name': 'mathlib', 'rev': self.pin, 'inputRev': self.pin}]}))
        with self.assertRaisesRegex(ValueError, 'mathlib release version'):
            cache.publication_mathlib(self.root)

        _, targets = cache.checkout_targets(self.root)
        criteria = cache.shard_row(self.pin, self.version, targets, 'D5', list(targets))
        self.assertEqual(criteria['asset'], 'v4.35.0-D5.tar.gz')
        self.assertEqual(criteria['key'], criteria['asset'])
        historical = dict(criteria, key=self.pin + '-D5', asset=self.pin + '-D5.tar.gz',
                          version='lean-modules-' + self.source, bytes=123, sha256='c'*64)
        base = {'source': self.source, 'version': historical['version'], 'targets': targets,
                'shards': {'D5': historical}}
        plan = cache.make_plan('b'*40, self.pin, targets, base, self.remote, cache.DEFAULT_CAPACITY, 2,
                               mathlib_version=self.version)
        self.assertEqual(plan['lanes'], [])
        self.assertEqual(cache.finish(plan, self.remote)['shards']['D5'], historical)
        self.assertEqual(self.remote.saves, [])

    def test_first_plan_uses_verified_compressed_sizes_for_layout(self):
        _, targets = cache.checkout_targets(self.root)
        legacy = {'targets': {n: {'bytes': 12} for n in targets}}
        guessed = self.plan(capacity=40)
        self.assertEqual(len(guessed['shards']), 3)
        measured = cache.make_plan(self.source, self.pin, targets, None, self.remote, 40, 2, mathlib_version=self.version, legacy=legacy)
        self.assertEqual(measured['shards'].keys(), {'D5'})
        self.assertEqual(measured['legacy'], legacy)

    def test_actual_oversize_split_survives_interruption_and_upgrade(self):
        # Start with a cold underestimate so real compressed bytes trigger the split in the worker.
        with patch.object(cache, 'DEFAULT_MODULE_BYTES', 1):
            initial = self.plan(capacity=200)
            self.assertEqual(list(initial['shards']), ['D5'])
            self.remote.fail = 'D5.B'
            with self.assertRaises(InterruptedError):
                self.workers(initial)
            self.assertEqual(self.remote.saves, ['D5.A'])
            self.assertEqual(self.remote.indexes, {})
            self.remote.fail = None
            shutil.rmtree(self.root / PROJECT_BUILD)
            retry = self.plan(capacity=200)
            self.assertEqual(set(retry['inherited']), {'D5.A'})
            self.workers(retry)
            first = cache.finish(retry, self.remote)
            self.assertCountEqual(self.remote.saves, ['D5.A', 'D5.B', 'D5.C'])
            self.assertEqual(self.plan(first, capacity=200)['lanes'], [])
            self.pin = '2' * 40
            self.source = 'c' * 40
            upgraded = self.plan(first, capacity=200)
            self.assertFalse(upgraded['inherited'])
            self.assertTrue(all(row['key'].startswith(self.version + '-') for row in upgraded['shards'].values()))


if __name__ == '__main__':
    unittest.main()
