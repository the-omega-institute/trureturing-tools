"""Checked publication behavior for ordinary full reports."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch
import zipfile

import test_reuse
import build_inputs
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


if __name__ == '__main__':
    unittest.main()
