"""Execute native Lake facets in private pinned-toolchain fixture packages."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
import zlib

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
ROOT = Path(os.environ.get('STRATALINT_NATIVE_SOURCE_ROOT', ROOT)).resolve()
sys.path.insert(0, str(HERE))
import publication
import materials
import native



from test_native_support import *

class NativePublicationConsumerTests:
    def test_traced_artifacts_are_reused_without_validation(self):
        # Like a restored olean, a traced artifact is reused as is: only
        # production validates, and Lake's trace alone decides reuse.
        self.build()
        artifact = native.state(self.root) / 'modules/D5.Alone.zip'
        with zipfile.ZipFile(artifact) as archive:
            entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        key = publication.RAW + '.provenance.json'
        origin = json.loads(entries[key])
        origin['report_sha256'] = '0' * 64
        entries[key] = materials.canonical_json(origin)
        artifact.unlink()  # Never write through a Lake cache hard link.
        with zipfile.ZipFile(artifact, 'w') as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        tampered = artifact.read_bytes()
        self.write('D5/B.lean', (self.root / 'D5/B.lean').read_text() + '-- aggregate again\n')
        self.build()
        records = [json.loads(line) for line in (self.root / 'activity.jsonl').read_text().splitlines()]
        self.assertEqual(records, [dict(kind='extract', count=1)],
                         '[FAIL] only_changed_module_reanalyzed')
        self.assertEqual(artifact.read_bytes(), tampered, '[FAIL] traced_artifact_reused_as_is')
        self.assertEqual(self.origins()['D5.Alone']['report_sha256'], '0' * 64,
                         '[FAIL] traced_artifact_not_revalidated')


    def test_aggregation_concatenates_without_revalidation(self):
        self.build()
        state = native.state(self.root)
        config = publication.read_json((state / 'module-inputs.json').read_bytes())
        artifacts = [str(state / 'modules' / (name + '.zip')) for name in config['modules']]
        output = self.root / 'aggregate.zip'
        with patch.object(publication, 'validate_rows', wraps=publication.validate_rows) as validations, \
                patch.object(zlib, 'compressobj', wraps=zlib.compressobj) as compressors:
            self.collection(output, artifacts)
            self.assertEqual(validations.call_count, 0, '[FAIL] aggregate_rows_not_revalidated')
            self.assertEqual(compressors.call_count, 0, '[FAIL] aggregate_materials_not_recompressed')
        self.assertTrue(output.is_file())

    def test_aggregation_rejects_a_malformed_module_artifact(self):
        self.build()
        state = native.state(self.root)
        config = publication.read_json((state / 'module-inputs.json').read_bytes())
        artifacts = [state / 'modules' / (name + '.zip') for name in config['modules']]
        def entries(artifact):
            with zipfile.ZipFile(artifact) as archive:
                return {entry.filename: archive.read(entry) for entry in archive.infolist()}
        original, provenance = entries(artifacts[0]), publication.RAW + '.provenance.json'
        origin = json.loads(original[provenance])
        cases = {
            'extra_member': (dict(original, extra=b''), 'invalid native artifact members'),
            'missing_materials': ({key: value for key, value in original.items()
                                   if not key.endswith('.materials.zip')}, 'invalid native artifact members'),
            'other_module': (entries(artifacts[1]), 'membership mismatch'),
            'input_projection': (dict(original, **{provenance: materials.canonical_json(
                dict(origin, input_projection={}))}), 'unexpected fields')}
        for damage, (members, message) in cases.items():
            with self.subTest(damage=damage):
                candidate = self.root / (damage + '.zip')
                with zipfile.ZipFile(candidate, 'w') as archive:
                    for name, data in members.items():
                        archive.writestr(name, data)
                output = self.root / 'rejected' / damage / 'report.zip'
                with self.assertRaisesRegex(ValueError, message, msg='[FAIL] aggregate_rejects_' + damage):
                    self.collection(output, [candidate, *artifacts[1:]])
                self.assertFalse(output.exists())


    def test_publication_snapshot_integrity_and_replace_failure(self):
        self.build()
        self.publish()
        destination = self.root / 'public.json'
        before = {suffix: publication.member(destination, suffix).read_bytes() for suffix in publication.SUFFIXES}
        self.write('D5/Alone.lean', (self.root / 'D5/Alone.lean').read_text() + '-- changed publication input\n')
        self.build()
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            collection = Path(directory) / 'collection.zip'
            self.collection(collection)
            report = publication.unpack(collection, directory)
            validate = self.validate_collection
            def corrupt_validated_snapshot(*args, **kwargs):
                result = validate(*args, **kwargs)
                publication.member(args[0], '.materials.zip').write_bytes(b'changed after validation')
                return result
            with self.assertRaisesRegex(ValueError, 'snapshot changed'):
                publication.publish(report, destination, validator=corrupt_validated_snapshot)
            self.assertEqual(before, {suffix: publication.member(destination, suffix).read_bytes()
                                     for suffix in publication.SUFFIXES})

            write_bytes = Path.write_bytes
            def damage_publication_update(path, data):
                if path.name.endswith('.sha256') and path.parent.name == 'bundle':
                    data += b'damaged after normalization'
                return write_bytes(path, data)
            with patch.object(Path, 'write_bytes', damage_publication_update):
                with self.assertRaisesRegex(ValueError, 'snapshot changed after validation'):
                    publication.publish(report, destination, validator=validate)
            self.assertEqual(before, {suffix: publication.member(destination, suffix).read_bytes()
                                     for suffix in publication.SUFFIXES})

            for failed_suffix in (*publication.SUFFIXES[1:], ''):
                with self.subTest(failed_suffix=failed_suffix):
                    replace = os.replace
                    failed = False
                    def fail_one_replace(source, target):
                        nonlocal failed
                        if Path(target) == publication.member(destination, failed_suffix) and not failed:
                            failed = True
                            raise OSError('injected publication replacement failure')
                        return replace(source, target)
                    with patch.object(publication.os, 'replace', side_effect=fail_one_replace):
                        with self.assertRaisesRegex(OSError, 'replacement failure'):
                            publication.publish(report, destination, validator=validate)
                    self.assertTrue(failed)
                    self.assertEqual(before, {suffix: publication.member(destination, suffix).read_bytes()
                                             for suffix in publication.SUFFIXES})

    def test_native_config(self):
        self.build()
        self.publish()
        self.write('activity.jsonl', '')
        self.run_lake('--no-build', 'build', ':moduleReports')
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        before = self.stamps()
        original = self.report()[1:]
        origins = self.origins()
        oleans = {str(path.relative_to(self.root)): (path.stat().st_mtime_ns, publication.digest(path))
                  for path in (self.root / '.lake/build/lib/lean').rglob('*.olean*')}
        self.write('lakefile.toml', (self.root / 'lakefile.toml').read_text() + '\n# config bytes\n')
        self.run_lake('--no-build', 'build', ':moduleReports')
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        self.assertEqual(before, self.stamps())
        built = self.build()
        records = [json.loads(line) for line in (self.root / 'activity.jsonl').read_text().splitlines()]
        result = dict(extracted=sum(r['count'] for r in records if r['kind'] == 'extract'),
            aggregated=sum(r['count'] for r in records if r['kind'] == 'aggregate'),
            compiled_modules=[name for name in [*before, 'Audit', 'External', 'ClaimSupport', 'Cache']
                              if f'Built {name} (' in built.stdout + built.stderr],
            unchanged_rows=before == self.stamps(), unchanged_origins=origins == self.origins(),
            unchanged_oleans=oleans == {str(path.relative_to(self.root)):
                (path.stat().st_mtime_ns, publication.digest(path))
                for path in (self.root / '.lake/build/lib/lean').rglob('*.olean*')})
        self.record_result('config-metadata', result)
        self.assertEqual(result['extracted'], 0)
        self.assertEqual(result['aggregated'], 0)
        self.assertEqual(result['compiled_modules'], [])
        self.assertTrue(result['unchanged_rows'])
        self.assertTrue(result['unchanged_origins'])
        self.assertTrue(result['unchanged_oleans'])
        self.assertEqual(original, self.report()[1:])
        self.build()
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        self.assertEqual(before, self.stamps())


    def test_compiler_stage_covers_native_readers_without_warming_projects(self):
        for package in (self.root, self.root / 'Reg'):
            self.assertFalse((package / '.lake/build').exists())
        self.assertFalse((self.root / 'public.json').exists())
        built = self.build()
        output = built.stdout + built.stderr
        self.assertNotIn('Built LeanInformationAudit.Contract.InputDiscovery (', output,
                         '[FAIL] compiler_stage_must_restore_input_discovery_module')
        self.assertNotIn('Built inputDiscovery:exe (', output,
                         '[FAIL] compiler_stage_must_restore_input_discovery_executable')
        for program in ('reportInspector', 'inputDiscovery'):
            self.assertTrue((self.root / '.lake/build/lean-inspector/producer/bin' / program).is_file())


class NativeArtifactConsumerTests:
    """Consumers of a valid report; subsequent mutations stay private."""

    def test_warm_native_artifacts_do_not_require_production_source_files(self):
        self.build()
        before, expected = self.stamps(), self.report()
        inputs = native.state(self.root) / 'inputs'
        for path in inputs.glob('*.sources.json'):
            path.unlink()
        self.build()
        self.assertEqual(list(inputs.glob('*.sources.json')), [])
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        self.assertEqual(self.stamps(), before)
        self.assertEqual(self.report(), expected)

    def test_native_miss_prepares_only_its_production_sources(self):
        self.build()
        before = self.stamps()
        inputs = native.state(self.root) / 'inputs'
        for path in inputs.glob('*.sources.json'):
            path.unlink()
        self.write('D5/Extra.lean', 'import D5.A\ndef extra : Nat := value\n')
        self.build()
        self.assertEqual([path.name for path in inputs.glob('*.sources.json')],
                         ['D5.Extra.json.sources.json'])
        self.assertEqual({name: stamp for name, stamp in self.stamps().items() if name in before}, before)
        self.assertEqual([json.loads(line) for line in (self.root / 'activity.jsonl').read_text().splitlines()],
                         [dict(kind='extract', count=1)])
        self.assertIn('D5.Extra', {row['module'] for row in self.report()[0]})


    def test_native_module_integrity_rejections(self):
        self.build()
        state = native.state(self.root)
        artifact = state / 'modules/D5.Alone.zip'
        utility = state / 'inputs/D5.Alone.json'
        with zipfile.ZipFile(artifact) as archive:
            original = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        candidate = self.root / 'corrupt-module.zip'
        for damage in ['report_sha256', 'missing_member', 'noncanonical_json']:
            with self.subTest(damage=damage):
                entries = dict(original)
                if damage == 'report_sha256':
                    key = publication.RAW + '.provenance.json'
                    origin = json.loads(entries[key])
                    origin['report_sha256'] = '0' * 64
                    entries[key] = materials.canonical_json(origin)
                elif damage == 'missing_member':
                    del entries[publication.RAW + '.materials.zip']
                else:
                    entries[publication.RAW] = json.dumps(json.loads(entries[publication.RAW]), indent=2).encode()
                with zipfile.ZipFile(candidate, 'w') as archive:
                    for name, data in entries.items():
                        archive.writestr(name, data)
                # Production validates every artifact before writing it.
                with self.assertRaises(ValueError, msg='[FAIL] native_module_integrity_' + damage), \
                        tempfile.TemporaryDirectory(dir=self.root) as directory:
                    report = publication.unpack(candidate, directory, native.ROW_SUFFIXES)
                    native.validate_module(report, self.root, 'D5.Alone', utility)

    @contextmanager
    def damaged_production(self):
        """Real production of D5.Alone whose written origin misstates its report digest."""
        self.build()
        environment = dict(self.env)
        for line in self.run_lake('env').stdout.splitlines():
            name, _, value = line.partition('=')
            if value: environment[name] = value
            else: environment.pop(name, None)
        state = native.state(self.root)
        write_origin = publication.write_origin
        def damaged(report, name, origin, projection):
            write_origin(report, name, origin, projection)
            provenance = publication.member(report, '.provenance.json')
            provenance.write_bytes(materials.canonical_json(
                dict(json.loads(provenance.read_bytes()), report_sha256='0' * 64)))
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(publication, 'write_origin', side_effect=damaged):
            yield [str(self.root), 'D5.Alone', str(self.root / 'D5/Alone.lean'),
                   str(state / 'inputs/D5.Alone.json'), str(state / 'producer/bin/reportInspector')]

    def test_production_rejects_an_invalid_module_before_writing_it(self):
        output = self.root / 'produced/D5.Alone.zip'
        # The subtest keeps the artifact pin observable when rejection is missing.
        with self.damaged_production() as arguments, self.subTest(pin='rejection'), self.assertRaisesRegex(
                ValueError, 'module production origin', msg='[FAIL] production_rejects_invalid_module'):
            native.module(*arguments, str(output))
        self.assertFalse(output.exists(), '[FAIL] production_writes_no_invalid_artifact')

    def test_batch_production_rejects_an_invalid_module_before_writing_it(self):
        output, request = self.root / 'produced/D5.Alone.zip', self.root / 'batch-request.json'
        with self.damaged_production() as arguments, self.subTest(pin='rejection'), self.assertRaisesRegex(
                ValueError, 'module production origin', msg='[FAIL] batch_production_rejects_invalid_module'):
            request.write_text(json.dumps([['produce', [*arguments, str(output)]]]))
            native.batch(request)
        self.assertFalse(output.exists(), '[FAIL] batch_production_writes_no_invalid_artifact')

    def test_native_producer_inputs(self):
        self.build()
        before = self.stamps()
        origins = self.origins()
        executable = self.root / '.lake/build/lean-inspector/producer/bin/reportInspector'
        executable_before = publication.digest(executable)
        inspector = self.root / 'tools/lean-inspector/Inspector.lean'
        # A real implementation edit changes executable bytes while preserving
        # valid report semantics. Compilation must still succeed.
        inspector.write_text(inspector.read_text().replace('at least one module is required', 'a report module is required'))
        self.build()
        self.assertNotEqual(executable_before, publication.digest(executable))
        self.assertEqual(before, self.stamps())
        self.assertEqual(origins, self.origins())
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        self.publish()
        published = publication.read_json(publication.member(self.root / 'public.json', '.provenance.json').read_bytes())
        self.assertEqual(published['module_origins'], origins)
        for producer, comment in [('tools/lean-inspector/materials.py', '#'),
                                  ('tools/lean-inspector/native.py', '#'),
                                  ('tools/lean-inspector/lakefile.lean', '--'),
                                  ('tools/scripts/report/lean-report-input.sh', '#'),
                                  ('tools/StrataLint.Lean/Lean/LeanUtilityInputCommand.cs', '//')]:
            with self.subTest(producer=producer):
                calls = (self.root / 'utility-calls').read_text().splitlines()
                implementation = (self.root / producer).read_text()
                if producer.endswith('materials.py'):
                    implementation = implementation.replace('BUFFER_BYTES = 64 * 1024', 'BUFFER_BYTES = 32 * 1024')
                self.write(producer, implementation + '\n' + comment + ' compatible producer bytes\n')
                self.build()
                self.assertEqual(before, self.stamps())
                self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
                self.assertEqual(len((self.root / 'utility-calls').read_text().splitlines()), len(calls) + 1)
                self.assertEqual(origins, self.origins())
        self.write('lean-report-inputs.json', (self.root / 'lean-report-inputs.json').read_text() + '\n')
        self.build()
        self.assertEqual(before, self.stamps())
        self.write('tools/lean-inspector/CompatibleHelper.lean', 'def compatibleHelper : Nat := 1\n')
        policy = json.loads((self.root / 'lean-report-inputs.json').read_text())
        for scope in ['inspector_sources', 'dependency_sources']:
            policy[scope]['include'].append(dict(pattern='tools/lean-inspector/CompatibleHelper.lean', optional=False))
        self.write('lean-report-inputs.json', json.dumps(policy))
        self.build()
        self.assertEqual(before, self.stamps())
        self.assertEqual(origins, self.origins())
        # Subsequent content changes assemble mixed actual production origins.
        self.write('D5/Alone.lean', (self.root / 'D5/Alone.lean').read_text() + '-- content input\n')
        self.build()
        self.assertEqual({n for n, value in self.stamps().items() if value != before[n]}, {'D5.Alone'})
        mixed = self.origins()
        for name in origins:
            if name == 'D5.Alone':
                self.assertNotEqual(origins[name]['producer_sources_sha256'], mixed[name]['producer_sources_sha256'])
                self.assertNotEqual(origins[name]['inspector_executable_sha256'], mixed[name]['inspector_executable_sha256'])
            else:
                self.assertEqual(origins[name], mixed[name])
        self.publish()
        self.build()
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')
        self.assertEqual(mixed, self.origins())
        self.publish()
        before = self.stamps()
        self.write('D5/B.lean', (self.root / 'D5/B.lean').read_text() + '-- content after producer changes\n')
        self.write('activity.jsonl', '')
        self.prepare_report_modules('D5.B')
        self.run_lake('build', 'D5.B:report', ':moduleReports')
        records = [json.loads(line) for line in (self.root / 'activity.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row['count'] for row in records if row['kind'] == 'extract'), 1)
        self.assertEqual(sum(row['count'] for row in records if row['kind'] == 'aggregate'), 0)
        self.assertEqual({name for name, stamp in self.stamps().items() if stamp != before[name]}, {'D5.B'})
        recovered = self.origins()
        self.assertNotEqual(mixed['D5.B']['producer_sources_sha256'], recovered['D5.B']['producer_sources_sha256'])
        for name in mixed:
            if name != 'D5.B': self.assertEqual(mixed[name], recovered[name])
        self.publish()


    def test_native_no_build_rejects_a_miss_without_production(self):
        self.build()
        self.write('activity.jsonl', '')
        self.write('D5/Alone.lean', (self.root / 'D5/Alone.lean').read_text() + '-- native miss\n')
        rejected = self.run_lake('--no-build', 'build', ':moduleReports', success=False)
        self.assertIn('needs to be rebuilt', rejected.stdout + rejected.stderr)
        self.assertEqual((self.root / 'activity.jsonl').read_text(), '')


class NativeColdPublicationTests:
    def test_generation_report_from_published_native_artifacts(self):
        sys.path.insert(0, str(HERE.parent / 'scripts/worktree'))
        import lean_generation_facets as generation

        workspace = 'tools/lean-inspector-reg'
        self.prepare_report_modules('D5.A', workspace=workspace)
        build = self.root / '.lake/build'
        native_root = build / 'lean-inspector/producer'
        with tempfile.TemporaryDirectory() as directory:
            donor = Path(directory) / 'producer'
            native_root.rename(donor)
            for program in ('inputDiscovery', 'reportInspector'):
                for path in generation.facet_paths('native/' + program):
                    destination = build / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(donor / destination.relative_to(native_root), destination)
            self.assertFalse((native_root / 'ir').exists())
            self.env.update(LAKE_ARTIFACT_CACHE='false', LAKE_RESTORE_ARTIFACTS='false')
            # Prepare just the compiler exports as on a restored generation.
            self.run_lake('-d', workspace, 'build', '+D5.A:olean')
            probe = self.run_lake('-d', workspace, 'build', '+D5.A:report', success=False)
            self.assertIn('out-of-date', probe.stdout + probe.stderr)
            with patch.dict(os.environ, self.env, clear=True):
                generation.build_facets(self.root, ['report/D5.A'], {'report/D5.A': {'deps': ['D5.A']}})
            bundle = build / generation.facet_paths('report/D5.A')[0]
            with zipfile.ZipFile(bundle) as archive:
                row = json.loads(archive.read('raw-lean-report.json'))['modules'][0]
                self.assertEqual(row['module'], 'D5.A')
            self.run_lake('-d', workspace, '--no-build', 'build', '+D5.A:report')

    def test_native_compiler_seed_is_private(self):
        self.assertFalse((self.root / '.lake').exists())
        if self.compiler_seed is None:
            stage = self.root / 'compiler-stage'
            stage_compiler(stage)
            self.compiler_seed = str(stage)
        stage = Path(self.compiler_seed)
        before = {path.name: (publication.digest(path), path.stat().st_mode)
                  for path in stage.iterdir()}
        self.run_lake('env', 'true')
        # Unstage admits only compiler artifacts; each fixture still creates
        # its own producer build, reports, utility inputs, and ensure stamp.
        self.assertFalse((self.root / '.lake/build/lean-inspector').exists())
        # Both compiler tools must restore through Lake without compilation.
        # Restoring compiler outputs must leave all project/report work cold.
        self.run_lake('--no-build', '-d', 'tools/lean-inspector', 'build',
                      'reportInspector', 'inputDiscovery')
        # The compiler library shares the root Lean directory. Its exact
        # inventory must be restored, with no project-owned modules mixed in.
        library = self.root / '.lake/build/lib/lean'
        self.assertEqual({str(path.relative_to(library)) for path in library.rglob('*.olean')}, {
            'Inspector.olean', 'LeanInformationAudit/RawArtifacts.olean',
            'LeanInformationAudit/CompiledMetadata.olean', 'LeanInformationAudit/CompiledAxioms.olean',
            'LeanInformationAudit/Contract/SourceAudit.olean', 'LeanInformationAudit/Contract/InputDiscovery.olean'})
        self.assertEqual({path.name for path in library.parent.iterdir()}, {'lean'})
        self.assertFalse((self.root / '.lake/build/ir').exists())
        self.assertEqual({path.name for path in (self.root / '.lake/build/lean-inspector').iterdir()}, {'producer'})
        self.assertFalse((self.root / '.lake/build/reg').exists())
        self.assertFalse((self.root / 'Reg/.lake/build').exists())
        self.assertFalse((self.root / '.lake/build/lean-inspector/modules').exists())
        self.assertFalse((self.root / '.lake/build/lean-inspector/report.zip').exists())
        for name in before:
            if name == 'outputs.jsonl':
                continue
            donor = stage / name
            private = self.root / '.lake/artifact-cache/artifacts' / name
            self.assertEqual(publication.digest(private), before[name][0])
            self.assertFalse(os.path.samestat(donor.stat(), private.stat()))
            self.assertEqual(donor.stat().st_mode & 0o222, 0)
            private.unlink()
            private.write_bytes(b'fixture-private damage')
        self.assertEqual(before, {path.name: (publication.digest(path), path.stat().st_mode)
                                  for path in stage.iterdir()})
