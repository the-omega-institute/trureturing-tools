"""Invocation-local work facts for native Lean cache preparation."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

REPO = Path(__file__).resolve().parents[4]


sys.path.insert(0, str(REPO / 'tools/lean-inspector/tests'))


class BuildWorkContracts(unittest.TestCase):
    def recorder_inputs(self, output="Build completed successfully (1 jobs).\n", activity=()):
        temp = tempfile.TemporaryDirectory(prefix="build-work-contract-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        script = root / "build_work.py"
        shutil.copyfile(REPO / "tools/lean-inspector/build_work.py", script)
        logs = root / "logs"
        logs.mkdir()
        for name, text in (("report.exit.log", "0"), ("report.stdout.log", output),
                           ("report.stderr.log", ""),
                           ("native-work.jsonl", "".join(json.dumps(row) + "\n" for row in activity))):
            (logs / name).write_text(text)
        project = root / ".lake/build/lib/lean/Probe.olean"
        project.parent.mkdir(parents=True)
        project.write_bytes(b"project artifact")
        fact = root / "work/build-work.json"
        fact.parent.mkdir()
        fact.write_text('{"report":999,"programs":999}')
        return root, logs, fact

    def run_recorder(self, root, logs, fact):
        return subprocess.run([sys.executable, "-B", str(root / "build_work.py"),
                               str(root), str(logs), str(fact), "report"],
                              capture_output=True, text=True)

    def test_aggregate_native_activity_is_added_to_report_work(self):
        root, logs, fact = self.recorder_inputs(
            output="✔ [1/1] Built Probe\nBuild completed successfully (1 jobs).\n",
            activity=({"kind": "aggregate", "count": 2}, {"kind": "extract", "count": 3},
                      {"kind": "aggregate", "count": 0}))
        result = self.run_recorder(root, logs, fact)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual((6, 0), (json.loads(fact.read_text())["report"],
                                json.loads(fact.read_text())["programs"]))

    def test_malformed_native_activity_reports_unknown_and_exits_two(self):
        for row in ({"kind": "other", "count": 1}, {"count": 1},
                    {"kind": "extract", "count": True}, {"kind": "aggregate", "count": -1},
                    {"kind": "aggregate", "count": "1"}, {"kind": "extract", "count": 1.5},
                    {"kind": "extract", "count": None}, {"kind": "extract"}, [], None, "activity"):
            with self.subTest(row=row):
                root, logs, fact = self.recorder_inputs(activity=(row,))
                previous = fact.read_bytes()
                result = self.run_recorder(root, logs, fact)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("LEAN_BUILD_WORK_UNKNOWN invalid native build activity", result.stderr)
                self.assertNotIn("LEAN_BUILD_WORK ", result.stdout)
                self.assertEqual(previous, fact.read_bytes())

    def test_direct_phase_durations_include_batch_and_unknown_boundaries(self):
        root, logs, fact = self.recorder_inputs()
        rows = [dict(phase='native-inspect', boundary='start', monotonic_ms=100),
                dict(phase='native-inspect', boundary='finish', monotonic_ms=140),
                dict(phase='native-materials', boundary='duration', elapsed_ns=23),
                dict(phase='native-constant-check', boundary='start', monotonic_ms=150)]
        (logs / 'native-phases.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows))
        result = self.run_recorder(root, logs, fact)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(next(x.split(' ', 1)[1] for x in result.stdout.splitlines()
                              if x.startswith('LEAN_INSPECTOR_TIMING ')))
        self.assertEqual(row['native-inspect']['elapsed_ns'], 40_000_000)
        self.assertEqual(row['native-materials']['elapsed_ns'], 23)
        self.assertIsNone(row['native-constant-check']['elapsed_ns'])

    def test_extensionless_executable_captions_resolve_project_and_package_outputs(self):
        for caption, expected in (("projectProbe", 1), ("fixture/projectProbe:exe", 1),
                                  ("packageProbe", 0), ("dependency/packageProbe:exe", 0)):
            with self.subTest(caption=caption):
                root, logs, fact = self.recorder_inputs(
                    output="✔ [1/1] Built " + caption + "\nBuild completed successfully (1 jobs).\n")
                for name in (".lake/build/bin/projectProbe",
                             ".lake/packages/dependency/.lake/build/bin/packageProbe"):
                    path = root / name
                    path.parent.mkdir(parents=True)
                    path.write_text("#!/bin/sh\nexit 0\n")
                    path.chmod(0o755)
                result = self.run_recorder(root, logs, fact)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, json.loads(fact.read_text())["report"])

    def test_known_project_work_remains_a_lower_bound_with_unresolved_captions(self):
        root, logs, fact = self.recorder_inputs(
            output="✔ [1/2] Built Probe\n✔ [2/2] Built UnknownTarget\n"
                   "Build completed successfully (2 jobs).\n")
        result = self.run_recorder(root, logs, fact)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(fact.read_text())["report"])

    def test_recorder_atomically_replaces_the_work_fact(self):
        root, logs, fact = self.recorder_inputs(activity=({"kind": "aggregate", "count": 2},))
        previous = fact.read_bytes()
        retained = fact.with_name("previous-work.json")
        os.link(fact, retained)
        result = self.run_recorder(root, logs, fact)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, json.loads(fact.read_text())["report"])
        self.assertEqual(previous, retained.read_bytes())
        self.assertEqual([], list(fact.parent.glob(".build-work-*")))

    def test_recorder_canonicalizes_the_repository_address(self):
        root, logs, fact = self.recorder_inputs()
        alias = root / "repository-link"
        alias.symlink_to(root, target_is_directory=True)
        result = self.run_recorder(alias, logs, fact)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(root.resolve()), json.loads(fact.read_text())["repository"])

    def test_recorder_keeps_the_destination_present_until_replace(self):
        root, logs, fact = self.recorder_inputs()
        previous = fact.read_bytes()
        replace = Path.replace

        def checked_replace(source, destination):
            self.assertTrue(destination.is_file(), "work fact disappeared before replacement")
            self.assertEqual(previous, destination.read_bytes())
            return replace(source, destination)

        record = runpy.run_path(str(root / "build_work.py"))["record"]
        with mock.patch.object(Path, "replace", autospec=True, side_effect=checked_replace) as replacing:
            record(root, logs, fact, "report")
        replacing.assert_called_once()
        self.assertEqual(0, json.loads(fact.read_text())["report"])

    def test_recorder_io_failure_reports_unknown_and_exits_two(self):
        for defect in ("parent-file", "destination-directory", "missing-log"):
            with self.subTest(defect=defect):
                root, logs, fact = self.recorder_inputs()
                previous = fact.read_bytes()
                if defect == "parent-file":
                    blocked = root / "blocked"
                    blocked.write_text("not a directory")
                    destination = blocked / "build-work.json"
                elif defect == "destination-directory":
                    destination = fact.parent / "blocked"
                    destination.mkdir()
                    (destination / "keep").write_bytes(previous)
                else:
                    destination = fact
                    (logs / "report.stdout.log").unlink()
                result = self.run_recorder(root, logs, destination)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("LEAN_BUILD_WORK_UNKNOWN ", result.stderr)
                self.assertNotIn("LEAN_BUILD_WORK ", result.stdout)
                self.assertEqual(previous, fact.read_bytes())
                self.assertEqual([], list(fact.parent.glob(".build-work-*")))
                if defect == "destination-directory":
                    self.assertEqual(previous, (destination / "keep").read_bytes())

if __name__ == "__main__":
    unittest.main()
