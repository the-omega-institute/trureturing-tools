"""Checked publication behavior for ordinary full reports."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch
import zipfile

import test_reuse
import build_inputs
import materials
import publication
import reuse


class EntryRegressionTests(unittest.TestCase):
    def entry(self, *, cached=False, legacy_steps=None, recovery=None, failure=0,
              missing_member=False, archive=False):
        fixture = test_reuse.ReuseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        fixture.policy.pop('dependency_sources')
        fixture.write_policy()

        def write(name, text):
            fixture.write(name, text)
            path = root / name
            path.chmod(0o755)
            return path

        for name in ('inspect.sh', 'native.py', 'reuse.py', 'materials.py',
                     'publication.py', 'build_work.py', 'build_inputs.py'):
            relative = 'tools/lean-inspector/' + name
            write(relative, (test_reuse.ROOT / relative).read_text())

        if archive:
            fixture.report = root / '.lake/build/stratalint/previous-report.json'
            fixture.report.parent.mkdir(parents=True)
        report = fixture.report
        fixture.bundle()
        reuse._seal(root, report, reuse.capture(root), publication.bundle_identity(report), checked=True)
        if legacy_steps is not None:
            receipt = reuse.read_receipt(report)
            receipt['completed'] = legacy_steps
            publication.member(report, reuse.SUFFIX).write_text(json.dumps(receipt))
        before = {suffix: publication.member(report, suffix).read_bytes()
                  for suffix in (*publication.SUFFIXES, reuse.SUFFIX)}
        if not cached:
            fixture.write('D5/A.lean', 'def a := 2\n')
            fixture.report = root / 'produced' / publication.RAW
            fixture.report.parent.mkdir()
            fixture.bundle()
            with zipfile.ZipFile(root / 'produced.zip', 'w') as bundle:
                for suffix in publication.SUFFIXES:
                    if missing_member and suffix == '.materials.zip':
                        continue
                    member = publication.member(fixture.report, suffix)
                    bundle.write(member, member.name)
        write('tools/scripts/lib/resource-observation-lib.sh', 'resource_observe() { :; }\n')
        write('bin/dotnet', '#!/bin/sh\n[ "$2" = lean-utility-input ] || exit 74\nprintf "[]\\n"\n')
        write('tools/scripts/worktree/lean-cache-run.sh', """#!/bin/sh
case "$1" in
  --build-report-programs)
    [ "$FULL_BUILD" = 1 ] && [ "$#" = 3 ] && [ "$2" = leanInspector/inputDiscovery ] &&
        [ "$3" = leanInspector/reportInspector ] || exit 83
    echo compile >> "$CALLS" ;;
  --build-report-modules) [ "$#" = 2 ] && [ -f "$2" ] ;;
  --prebuilt) shift; exec "$@" ;;
  *) exit 84 ;;
esac
""")
        write('bin/lake', """#!/bin/sh
if [ "${1:-}" = --version ]; then printf 'fixture lake 1\n'; exit 0; fi
[ "$#" = 5 ] && [ "$1" = --log-level=error ] && [ "$2" = -d ] &&
    [ "$4" = build ] && [ "$5" = :report ] || exit 85
echo report >> "$CALLS"
if [ -n "${REPORT_ARCHIVE:-}" ]; then tar -cf "$REPORT_ARCHIVE" -C .lake build || exit 86; fi
[ "$REPORT_EXIT" = 0 ] || exit "$REPORT_EXIT"
mkdir -p .lake/build/lean-inspector
cp produced.zip .lake/build/lean-inspector/report.zip
""")
        write('bin/make', """#!/bin/bash
[[ "$#" == 3 && "$1" == lean-report && "$2" == LEAN_REPORT_CACHE_MISS_POLICY=reuse-or-build &&
    "$3" == LEAN_REPORT=* ]] || exit 97
exec bash "$PWD/tools/lean-inspector/inspect.sh" --repository "$PWD" \\
  --output "${3#LEAN_REPORT=}" --log-dir "$STRATALINT_LEAN_REPORT_LOG_DIR" \\
  --regenerate-report-artifacts "$REGENERATE_REPORT_ARTIFACTS"
""")
        producer = write('producer.dll', 'fixture producer')
        calls = root / 'calls'
        environment = dict(os.environ, FULL_BUILD='1',
            PATH=os.pathsep.join((str(root / 'bin'), str(Path(sys.executable).resolve().parent), os.environ['PATH'])),
            CALLS=str(calls), LAKE_BIN=str(root / 'bin/lake'), REPORT_EXIT=str(failure),
            STRATALINT_INSPECTOR_SUPERVISED='1', STRATALINT_LEAN_BUILD_TARGETS='[]',
            STRATALINT_LEAN_PRODUCER_DLL=str(producer), STRATALINT_LEAN_REPORT_REUSE=str(report),
            STRATALINT_LEAN_REPORT_LOG_DIR=str(root / 'logs'),
            REBUILD_REPORT_CACHE='0', REGENERATE_REPORT_ARTIFACTS='0')
        if archive:
            environment['REPORT_ARCHIVE'] = str(root / 'during-check.tar')
        if recovery:
            for suffix in before:
                publication.member(report, suffix).unlink()
            environment.update({recovery: '1', 'LEAN_REPORT_CACHE_MISS_POLICY': 'fetch-or-fail'})
        result = subprocess.run([sys.executable, '-B', str(root / 'tools/lean-inspector/build_inputs.py'),
                                 'check', '--output', str(report)], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        actual_calls = calls.read_text().splitlines() if calls.exists() else []
        return result, actual_calls, report, before

    def assert_checked_v3(self, report):
        self.assertEqual(json.loads(report.read_bytes())['schema'], 'stratalint-raw-lean-report-v3')
        self.assertEqual(reuse.read_receipt(report)['completed'], [*reuse.COMPLETED, 'full-report-checked'])

    def test_release_archive_during_check_contains_only_published_reports(self):
        result, calls, report, _ = self.entry(archive=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ['compile', 'report'])
        with tarfile.open(report.parents[3] / 'during-check.tar') as archive:
            members = archive.getmembers()
        self.assertTrue(any(member.name.endswith('/previous-report.json') for member in members))
        self.assertFalse(any('.lean-report-build.' in member.name or member.islnk() for member in members))

    def test_checked_reuse_preserves_actual_or_unknown_seed_commit(self):
        for known in (True, False):
            with self.subTest(known=known):
                fixture = test_reuse.ReuseTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.policy.pop('dependency_sources')
                fixture.write_policy()
                fixture.dev_repository()
                root, report = fixture.root, fixture.report
                reuse._seal(root, report, reuse.capture(root), publication.bundle_identity(report), checked=True)
                source = reuse.read_seed_base(root)
                self.assertIsNotNone(source)
                if not known:
                    reuse.invalidate_seed_base(root)
                subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
                                'commit', '--allow-empty', '-qm', 'reuse at another commit'], check=True)
                staging = report.parent / 'check'
                staging.mkdir()
                with patch.object(build_inputs, 'ROOT', root):
                    build_inputs.checked_report(staging, report, report, staging, staging / 'work.json',
                                                try_seed=True)
                self.assertEqual(reuse.read_seed_base(root), source if known else None)

    def test_optional_dependency_disappearance_cannot_enable_receipt_shortcut(self):
        fixture = test_reuse.ReuseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.policy['dependency_sources']['include'][0]['optional'] = True
        fixture.write_policy()
        fixture.receipt()
        (fixture.root / 'Audit.lean').unlink()
        self.assertFalse(reuse.probe(fixture.root, fixture.report)['needs_lake'])
        self.assertTrue(reuse.probe(fixture.root, fixture.report, require_covered_dependencies=True)['needs_lake'])
        # Removing the registration itself cannot promote an older incomplete receipt.
        fixture.policy.pop('dependency_sources')
        fixture.write_policy()
        self.assertTrue(reuse.probe(fixture.root, fixture.report, require_covered_dependencies=True)['needs_lake'])
        fixture.receipt()
        self.assertFalse(reuse.probe(fixture.root, fixture.report, require_covered_dependencies=True)['needs_lake'])

    def test_explicit_recovery_without_seed_through_checked_entry(self):
        for option in ('REBUILD_REPORT_CACHE', 'REGENERATE_REPORT_ARTIFACTS'):
            with self.subTest(option=option):
                result, calls, report, _ = self.entry(recovery=option)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, ['compile', 'report'])
                self.assert_checked_v3(report)

    def test_fresh_report_replaces_ordinary_v3_cache(self):
        result, calls, report, before = self.entry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ['compile', 'report'])
        self.assertNotEqual(report.read_bytes(), before[''])
        self.assert_checked_v3(report)

    def test_ordinary_v3_reuse_accepts_current_and_historical_receipts(self):
        for steps in (None, reuse.COMPLETED, reuse.LEGACY_CHECKED):
            with self.subTest(steps=steps):
                result, calls, report, before = self.entry(cached=True, legacy_steps=steps)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [])
                for suffix in ('', '.materials.zip'):
                    self.assertEqual(publication.member(report, suffix).read_bytes(), before[suffix])
                self.assert_checked_v3(report)

    def test_report_failure_preserves_every_published_member_and_receipt(self):
        for options, status in ((dict(failure=41), 41), (dict(missing_member=True), 1)):
            with self.subTest(options=options):
                result, calls, report, before = self.entry(**options)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(calls, ['compile', 'report'])
                self.assertEqual(before, {suffix: publication.member(report, suffix).read_bytes()
                                          for suffix in before})



class SparseReportEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_reuse.ReuseTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        for name in ('Keep', 'Successor', 'ClaimConsumer', 'Deleted'):
            self.fixture.write('D5/' + name + '.lean', 'def x := 1\n')
        self.fixture.dev_repository()
        self.seed = self.fixture.report
        rows = list(publication.report_rows(self.seed, materials.REPORT_SCHEMA))
        origins = publication.read_json(publication.member(self.seed, '.provenance.json').read_bytes())['module_origins']
        with zipfile.ZipFile(publication.member(self.seed, '.materials.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
            for row in rows:
                if row['module'] in ('D5.Keep', 'D5.Deleted'):
                    material = row['module'] + ' type'
                    key = 'x'
                    address = materials.statement_address(material.encode())
                    row['declarations'] = [dict(axioms=[], include_in_statement=True, kind='theorem', name='x',
                        name_key=key, type_sha256=address, statement_id=materials.declaration_statement_id(
                            row['source_path'], 'theorem', key, material))]
                    archive.writestr('sha256/' + address[7:], material)
                    origins[row['module']]['report_sha256'] = hashlib.sha256(materials.canonical_json(
                        dict(modules=[row], schema=materials.REPORT_SCHEMA))).hexdigest()
        self.seed.write_bytes(materials.canonical_json(dict(modules=rows, schema=materials.REPORT_SCHEMA)))
        publication.write_sidecars(self.seed, publication.coordinates(self.root), origins)
        reuse.write_receipt(self.seed, reuse.capture(self.root), checked=True)
        self.base = reuse._git(self.root, 'rev-parse', 'HEAD')
        self.plan = dict(schema='lean-release-sparse-plan-v1', base_commit=self.base, target_commit=self.base,
                         base_tag='previous', base_manifest_sha256='a' * 64,
                         base_report_sha256=reuse.seed_identity(self.seed),
                         rebuild_modules=[], removed_modules=[], restore_modules=[])
        root = patch.object(build_inputs, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.wd = self.root / '.lake/work'
        self.wd.mkdir()
        self.modules = publication.selection.Selection(self.root).modules()
        self.graph = {name: dict(module=name, source_path=path, imports=[]) for name, path in self.modules.items()}

    def commit(self):
        command = ['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                   '-c', 'commit.gpgsign=false']
        subprocess.run([*command, 'add', '-A'], check=True)
        subprocess.run([*command, 'commit', '-qm', 'target'], check=True)
        self.plan['target_commit'] = reuse._git(self.root, 'rev-parse', 'HEAD')

    def test_private_fallback_marker_is_bound_and_reserved_for_compatibility(self):
        plan_file = self.wd / 'plan.json'
        plan_file.write_text(json.dumps(self.plan))
        marker = self.wd / 'fallback.json'
        environment = dict(LEAN_CACHE_SPARSE_PLAN=str(plan_file), LEAN_CACHE_SPARSE_FALLBACK_FILE=str(marker),
                           REGENERATE_REPORT_ARTIFACTS='1', FULL_BUILD='1')
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', ['build_inputs.py', 'check']):
            with self.assertRaises(build_inputs.SparseFallback):
                build_inputs.main()
            value = publication.read_json(marker.read_bytes())
            self.assertEqual(value['target_commit'], self.plan['target_commit'])
            self.assertEqual(value['base_manifest_sha256'], self.plan['base_manifest_sha256'])
            self.assertIn('full regeneration', value['reason'])
            marker.unlink()
            plan_file.write_text(json.dumps(dict(self.plan, target_commit='0' * 40)))
            with self.assertRaisesRegex(ValueError, 'HEAD'):
                build_inputs.main()
            self.assertFalse(marker.exists())

    def test_sparse_target_ancestry_digest_and_completion_are_separate(self):
        file = self.wd / 'plan.json'
        file.write_text(json.dumps(self.plan))
        self.assertEqual(build_inputs.sparse_plan(file), self.plan)
        file.write_text(json.dumps(dict(self.plan, target_commit='0' * 40)))
        with self.assertRaisesRegex(ValueError, 'HEAD'):
            build_inputs.sparse_plan(file)
        with self.assertRaisesRegex(ValueError, 'authenticated predecessor'):
            build_inputs.sparse_candidate(self.wd, self.seed, self.wd, reuse.capture(self.root),
                                          dict(self.plan, base_report_sha256='0' * 64))
        reuse.write_receipt(self.seed, reuse.capture(self.root))
        with self.assertRaisesRegex(build_inputs.SparseFallback, 'checked full report'):
            build_inputs.sparse_rows(self.plan, self.seed, reuse.capture(self.root), self.modules, self.graph, [])

    def test_global_inputs_request_cold_without_changing_published_receipt(self):
        self.fixture.write('Audit.lean', 'def audit := 2\n')
        self.commit()
        with self.assertRaisesRegex(build_inputs.SparseFallback, 'global'):
            build_inputs.sparse_rows(self.plan, self.seed, reuse.capture(self.root), self.modules, self.graph, [])
        self.assertEqual(reuse.seed_identity(self.seed), self.plan['base_report_sha256'])

    def test_full_sparse_candidate_replaces_rename_and_preserves_materials_and_checks_report(self):
        import native
        import scoped

        self.fixture.write('D5/A.lean', 'def a := 2\n')
        (self.root / 'D5/Deleted.lean').rename(self.root / 'D5/Renamed.lean')
        self.commit()
        self.plan.update(removed_modules=['D5.Deleted'], rebuild_modules=['D5.A', 'D5.Renamed', 'D5.Successor'])
        modules = publication.selection.Selection(self.root).modules()
        self.graph = {name: dict(module=name, source_path=path, imports=[]) for name, path in modules.items()}
        self.graph['Bridge'] = dict(module='Bridge', source_path='Audit.lean', imports=['D5.A'])
        self.graph['D5.Successor']['imports'] = ['Bridge']
        utilities = [dict(modulePath=modules['D5.ClaimConsumer'], claimGid='claim', claimModule='D5.Successor',
            claimSelector='claim', claimSourcePath=modules['D5.Successor'],
            claimSourceSha256='sha256:' + publication.digest(self.root / modules['D5.Successor']),
            resultGid='result', resultModule='D5.ClaimConsumer', resultSelector='result')]
        expected = sorted(set(modules) - {'D5.Keep'})

        def phase(entry, kind, command):
            if kind == 'utility':
                path = self.wd / 'utility.json'
                path.write_text(json.dumps(utilities))
                return path
            self.assertEqual(kind, 'report')
            self.assertEqual(command[-len(expected):], ['+' + name + ':report' for name in expected])
            for name in expected:
                path = modules[name]
                row = dict(module=name, source_path=path, source_sha256='sha256:' + publication.digest(self.root / path),
                           imports=self.graph[name]['imports'], declarations=[])
                if name == 'D5.ClaimConsumer':
                    row['utility_refutation'] = {field: utilities[0][key] for key, field in native.UTILITY_BINDINGS}
                    row['utility_refutation']['is_closed_negation'] = True
                raw = materials.canonical_json(dict(modules=[row], schema=materials.REPORT_SCHEMA))
                origin = dict(module=name, report_sha256=hashlib.sha256(raw).hexdigest(),
                    input_projection=dict(schema='stratalint-judge-input-projection-v1', module=name, inputs=[]),
                    producer_sources_sha256='c' * 64, inspector_executable_sha256='d' * 64)
                artifact = native.state(self.root) / 'modules' / (name + '.zip')
                artifact.parent.mkdir(parents=True, exist_ok=True)
                material = self.wd / 'empty.zip'
                with zipfile.ZipFile(material, 'w'):
                    pass
                with zipfile.ZipFile(artifact, 'w') as archive:
                    archive.writestr(publication.RAW, raw)
                    archive.writestr(publication.RAW + '.provenance.json', materials.canonical_json(origin))
                    archive.write(material, publication.RAW + '.materials.zip')

        for name in ('materials.py', 'publication.py'):
            target = self.root / 'tools/lean-inspector' / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(test_reuse.HERE / name, target)
        self.commit()
        logs = self.wd / 'logs'
        program_logs = self.wd / 'programs'
        program_logs.mkdir()
        before = reuse.read_receipt(self.seed)
        with patch.object(scoped.Entry, 'provision'), patch.object(scoped.Entry, 'phase', phase), \
                patch.object(build_inputs, 'sparse_graph', return_value=self.graph), \
                patch.object(build_inputs, 'sparse_programs', return_value={}), \
                patch.object(build_inputs, 'prepare') as prepare, \
                patch.dict(os.environ, STRATALINT_LEAN_REPORT_LOG_DIR=str(logs)):
            build_inputs.checked_report(self.wd, self.seed, self.seed, program_logs, self.wd / 'work.json',
                                        try_seed=False, plan=self.plan)
        self.assertEqual(prepare.call_args.args[0], ' '.join(expected))
        receipt = reuse.read_receipt(self.seed, reuse.capture(self.root))
        self.assertEqual(receipt['completed'], reuse.CHECKED)
        self.assertNotEqual(receipt['bundle'], before['bundle'])
        publication.validate_bundle(self.seed, publication.coordinates(self.root), self.root)
        provenance = publication.read_json(publication.member(self.seed, '.provenance.json').read_bytes())
        with zipfile.ZipFile(publication.member(self.seed, '.materials.zip')) as archive:
            self.assertEqual(archive.namelist(), ['sha256/' + materials.statement_address(b'D5.Keep type')[7:]])
        self.assertEqual(set(provenance['module_origins']), set(modules))
        self.assertEqual(provenance['module_origins']['D5.Keep']['producer_sources_sha256'], 'a' * 64)
        self.assertEqual(provenance['module_origins']['D5.Renamed']['producer_sources_sha256'], 'c' * 64)


if __name__ == '__main__':
    unittest.main()
