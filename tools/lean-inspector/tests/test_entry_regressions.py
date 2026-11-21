"""Production regression decisions survive phase capture without extra FIB work."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

import test_reuse
import build_inputs
import fib_analysis
import materials
import publication
import reuse


class EntryRegressionTests(unittest.TestCase):
    def entry(self, *, cached=False, changed=False, selected=False, failure=0,
              unavailable_clock=False, missing_inputs=False, regenerate=False, obsolete=False, invalid_fresh=False,
              force_without_seed=None, follow_with_unchecked_report=False, legacy=False, archive=False):
        fixture = test_reuse.ReuseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        fixture.policy.pop('dependency_sources')
        fixture.write_policy()

        def write(name, text):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o755)
            return path

        for name in ('inspect.sh', 'native.py', 'reuse.py', 'materials.py',
                     'publication.py', 'fib_analysis.py', 'build_work.py', 'build_inputs.py'):
            relative = 'tools/lean-inspector/' + name
            write(relative, (test_reuse.ROOT / relative).read_text())

        def bundle(identity, missing=False):
            fixture.bundle()
            value = json.loads(fixture.report.read_text())
            row = value['modules'][0]
            leaf = dict(application='D5.A.application', owner='D5.A', target='D5.A.a',
                target_module='D5.A', target_type_identity='c' * 64, input_identity=identity,
                request=dict(kind='unavailable', parameter=None), unavailable_reason='fixture',
                reading=fib_analysis.unavailable('fixture'))
            if not missing:
                row['fib_analysis'] = dict(applications=[leaf])
            fixture.report.write_bytes(materials.canonical_json(value))
            origins = json.loads(publication.member(fixture.report, '.provenance.json').read_text())['module_origins']
            origins['D5.A']['input_projection']['inputs'] = [dict(owner='D5.A',
                name=leaf['application'], type='LeanInformationAudit.AuricFib.Contract.Application')]
            origins['D5.A']['report_sha256'] = hashlib.sha256(materials.canonical_json(
                dict(schema=materials.REPORT_SCHEMA, modules=[row]))).hexdigest()
            publication.write_sidecars(fixture.report, publication.coordinates(root), origins)

        if archive:
            fixture.report = root / '.lake/build/stratalint/previous-report.json'
            fixture.report.parent.mkdir(parents=True, exist_ok=True)
        bundle('a' * 64)
        report = fixture.report
        reuse._seal(root, report, reuse.capture(root), publication.bundle_identity(report), checked=True)
        if legacy:
            reuse.write_receipt(report, reuse.capture(root))
        if obsolete:
            value = json.loads(report.read_text())
            value['modules'][0]['fib_analysis']['schema_version'] = 1
            report.write_bytes(materials.canonical_json(value))
            origins = json.loads(publication.member(report, '.provenance.json').read_text())['module_origins']
            origins['D5.A']['report_sha256'] = hashlib.sha256(materials.canonical_json(value)).hexdigest()
            publication.write_sidecars(report, publication.coordinates(root), origins)
        before = {suffix: publication.member(report, suffix).read_bytes() for suffix in publication.SUFFIXES}
        if not cached:
            fixture.write('D5/A.lean', 'def a := 2\n')
            fixture.report = root / 'produced' / publication.RAW
            fixture.report.parent.mkdir()
            bundle(('b' if changed else 'a') * 64, missing_inputs)
            if invalid_fresh:
                value = json.loads(fixture.report.read_text())
                value['modules'][0]['fib_analysis']['schema_version'] = 1
                fixture.report.write_bytes(materials.canonical_json(value))
                origins = json.loads(publication.member(fixture.report, '.provenance.json').read_text())['module_origins']
                origins['D5.A']['report_sha256'] = hashlib.sha256(materials.canonical_json(value)).hexdigest()
                publication.write_sidecars(fixture.report, publication.coordinates(root), origins)
            with zipfile.ZipFile(root / 'produced.zip', 'w') as archive:
                for suffix in publication.SUFFIXES:
                    member = publication.member(fixture.report, suffix)
                    archive.write(member, member.name)
        elif not selected and not legacy:
            # Accepted reuse must not decode the complete report for telemetry,
            # invoke the regression consumer, or launch an analyzer.
            with (root / 'tools/lean-inspector/publication.py').open('a') as script:
                script.write('\ndef report_rows(*args, **kwargs):\n'
                             '    raise AssertionError("unexpected complete row reread")\n')
            path = root / 'tools/lean-inspector/fib_analysis.py'
            path.write_text('if __name__ == "__main__":\n    raise SystemExit(96)\n' + path.read_text())

        write('tools/scripts/lib/resource-observation-lib.sh', 'resource_observe() { :; }\n')
        write('tools/scripts/worktree/lean-cache-ensure.sh',
              '#!/bin/sh\nprintf "ensure\\n" >> "$CALLS"\n')
        write('bin/dotnet', '#!/bin/sh\n[ "$2" = lean-utility-input ] || exit 74\nprintf "[]\\n"\n')
        write('tools/scripts/worktree/lean-cache-run.sh', '#!/bin/sh\n[ "$1" = --build-report-programs ] && { [ "$FULL_BUILD" = 1 ] || exit 83; echo compile >> "$CALLS"; exit 0; }\n[ "$1" = --build-report-modules ] && exit 0\n[ "$1" = --prebuilt ] && shift\nexec "$@"\n')
        write('bin/lake', '#!/bin/sh\n'
              'if [ "${1:-}" = --version ]; then printf "fixture lake 1\\n"; exit 0; fi\n'
              'printf "report\\n" >> "$CALLS"\nmkdir -p .lake/build/lean-inspector\n'
              'cp produced.zip .lake/build/lean-inspector/report.zip\n')
        write('bin/make', """#!/bin/bash
case "$1" in
  lean-report)
    exec bash "$PWD/tools/lean-inspector/inspect.sh" --repository "$PWD" \
      --output "${3#LEAN_REPORT=}" --log-dir "$STRATALINT_LEAN_REPORT_LOG_DIR" \
      --regenerate-report-artifacts "$REGENERATE_REPORT_ARTIFACTS" ;;
esac
[[ "$#" == 3 && "$1" == -C && "$2" == "$PWD" && "$3" == compiled-fib-test ]] || exit 97
echo regressions >> "$CALLS"
if [[ -n "$FIB_TEST_ARCHIVE" ]]; then tar -cf "$FIB_TEST_ARCHIVE" -C .lake build || exit 98; fi
exit """ + str(failure) + '\n')
        write('tools/scripts/auric-fib-analysis.sh', '#!/bin/sh\n'
              'printf "unexpected analyzer\\n" >> "$CALLS"\nexit 98\n')
        producer = write('producer.dll', 'fixture producer')
        shell_environment = write('shell-env', 'unset SECONDS\n' if unavailable_clock else '')
        calls = root / 'calls'
        logs = root / 'logs'
        environment = dict(os.environ, FULL_BUILD="1",
            PATH=os.pathsep.join((str(root / 'bin'), str(Path(sys.executable).resolve().parent), os.environ['PATH'])),
            CALLS=str(calls), LAKE_BIN=str(root / 'bin/lake'),
            STRATALINT_INSPECTOR_SUPERVISED='1', STRATALINT_LEAN_BUILD_TARGETS='[]',
            STRATALINT_LEAN_PRODUCER_DLL=str(producer),
            STRATALINT_FIB_REGRESSIONS_SELECTED=str(selected).lower(),
            STRATALINT_LEAN_REPORT_REUSE=str(report), BASH_ENV=str(shell_environment))
        if archive:
            environment['FIB_TEST_ARCHIVE'] = str(root / 'during-check.tar')
        environment.update(REGENERATE_REPORT_ARTIFACTS=str(int(regenerate)), STRATALINT_LEAN_REPORT_LOG_DIR=str(logs))
        if force_without_seed:
            write('.github/scripts/install-tools.py', '# The fixture supplies its own tools.\n')
            for suffix in (*publication.SUFFIXES, reuse.SUFFIX):
                publication.member(report, suffix).unlink()
            command = [shutil.which('make'), '-f', str(test_reuse.ROOT / 'Makefile'), 'check-lean-report',
                       'FULL_BUILD=1', 'LEAN_REPORT_CACHE_MISS_POLICY=fetch-or-fail',
                       force_without_seed + '=1', 'LEAN_REPORT=' + str(report)]
        else:
            command = [sys.executable, '-B', str(root / 'tools/lean-inspector/build_inputs.py'),
                       'check', '--output', str(report)]
        result = subprocess.run(command, cwd=root, env=environment,
            capture_output=True, text=True, timeout=30)
        if follow_with_unchecked_report:
            self.assertEqual(result.returncode, failure, result.stdout + result.stderr)
            analyzed = subprocess.run(['make', 'lean-report', 'LEAN_REPORT_CACHE_MISS_POLICY=reuse-or-build',
                                       'LEAN_REPORT=' + str(report)], cwd=root, env=environment,
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)
            self.assertEqual(reuse.read_receipt(report)['completed'], reuse.COMPLETED)
            result = subprocess.run(command, cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
        actual_calls = calls.read_text().splitlines() if calls.exists() else []
        return result, actual_calls, report, logs, before

    def test_release_archive_during_check_contains_only_published_reports(self):
        import tarfile
        result, calls, report, _, _ = self.entry(cached=True, selected=True, archive=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ['regressions'])
        with tarfile.open(report.parents[3] / 'during-check.tar') as archive:
            members = archive.getmembers()
        self.assertTrue(any(member.name.endswith('/previous-report.json') for member in members))
        self.assertFalse(any('.lean-report-build.' in member.name or member.islnk() for member in members))

    def test_analysis_publication_cannot_clear_failed_regressions(self):
        result, calls, report, _, _ = self.entry(changed=True, failure=41, follow_with_unchecked_report=True)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual(calls, ['compile', 'report', 'regressions', 'report', 'regressions'])
        self.assertEqual(reuse.read_receipt(report)['completed'], reuse.COMPLETED)

    def test_old_reuse_receipt_is_checked_before_becoming_a_regression_baseline(self):
        result, calls, report, _, _ = self.entry(cached=True, legacy=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ['regressions'])
        self.assertEqual(reuse.read_receipt(report)['completed'], reuse.CHECKED)

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
                with patch.object(build_inputs, 'ROOT', root), patch.dict(os.environ, {
                        'STRATALINT_FIB_REGRESSIONS_SELECTED': 'false'}):
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

    def test_explicit_recovery_without_seed_through_checked_make_entry(self):
        for option in ('REBUILD_REPORT_CACHE', 'REGENERATE_REPORT_ARTIFACTS'):
            with self.subTest(option=option):
                result, calls, report, _, _ = self.entry(force_without_seed=option)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, ['compile', 'report', 'regressions'])
                self.assertTrue(publication.member(report, reuse.SUFFIX).is_file())

    def test_fresh_or_regenerated_inputs_select_the_suite_once(self):
        for regenerate, obsolete, changed, selected in (
                (False, False, False, False), (False, False, True, True),
                (True, False, True, False), (True, False, False, True),
                (True, True, False, False)):
            with self.subTest(regenerate=regenerate, changed=changed, selected=selected, obsolete=obsolete):
                result, calls, report, _, _ = self.entry(regenerate=regenerate, obsolete=obsolete,
                    changed=changed, selected=selected)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, ['compile', 'report']
                                 + (['regressions'] if changed or selected or obsolete else []))
                self.assertTrue(publication.member(report, '.reuse.json').is_file())

    def test_accepted_reuse_skips_analysis_and_unselected_regressions(self):
        for selected in (False, True):
            with self.subTest(selected=selected):
                result, calls, report, _, before = self.entry(cached=True, selected=selected)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, ['regressions'] if selected else [])
                self.assertEqual(report.read_bytes(), before[''])
                self.assertTrue(publication.member(report, '.reuse.json').is_file())

    def test_rejected_comparison_or_regressions_preserve_all_published_members(self):
        for options, failure in (
                (dict(changed=True, failure=41), 41),
                (dict(cached=True, selected=True, failure=41), 41),
                (dict(changed=True, missing_inputs=True), 1),
                (dict(obsolete=True), 1),
                (dict(regenerate=True, obsolete=True, invalid_fresh=True), 1)):
            with self.subTest(options=options):
                result, calls, report, _, before = self.entry(**options)
                self.assertEqual(result.returncode, failure, result.stdout + result.stderr)
                self.assertEqual(calls.count('regressions'), int(failure == 41))
                self.assertEqual(before, {suffix: publication.member(report, suffix).read_bytes()
                                          for suffix in publication.SUFFIXES})



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
        with self.assertRaisesRegex(build_inputs.SparseFallback, 'checked FIB'):
            build_inputs.sparse_rows(self.plan, self.seed, reuse.capture(self.root), self.modules, self.graph, [])

    def test_global_inputs_request_cold_without_changing_published_receipt(self):
        self.fixture.write('Audit.lean', 'def audit := 2\n')
        self.commit()
        with self.assertRaisesRegex(build_inputs.SparseFallback, 'global'):
            build_inputs.sparse_rows(self.plan, self.seed, reuse.capture(self.root), self.modules, self.graph, [])
        self.assertEqual(reuse.seed_identity(self.seed), self.plan['base_report_sha256'])

    def test_full_sparse_candidate_replaces_rename_and_preserves_materials_and_fib_check(self):
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

        for name in ('fib_analysis.py', 'materials.py', 'publication.py'):
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
