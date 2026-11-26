import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("ci_compute.py")
spec = importlib.util.spec_from_file_location("ci_compute", SCRIPT)
compute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compute)


class ComputeBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "compute"
        compute.initialize(self.path, 120)
        environment = mock.patch.dict(os.environ, {compute.BUDGET: str(self.path), compute.ACTIVE: "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_shared_allowance_excludes_gaps_and_preserves_command_failure(self):
        with mock.patch.object(compute.time, "monotonic", side_effect=[0, 1100, 9000, 9200]), \
                mock.patch.object(compute.subprocess, "call", side_effect=[0, 23]) as execute:
            self.assertEqual(0, compute.run(["compile"]))
            self.assertEqual(23, compute.run(["audit"]))
        self.assertEqual(5900, float(self.path.read_text()))
        self.assertEqual("7200.0s", execute.call_args_list[0].args[0][2])
        self.assertEqual("6100.0s", execute.call_args_list[1].args[0][2])

    def test_exhaustion_refuses_later_phases_and_cannot_be_reset(self):
        with mock.patch.object(compute.time, "monotonic", side_effect=[0, 7200]), \
                mock.patch.object(compute.subprocess, "call", return_value=124) as execute:
            self.assertEqual(124, compute.run(["compile"]))
            self.assertEqual(124, compute.run(["audit"]))
            execute.assert_called_once()
        with self.assertRaises(FileExistsError):
            compute.initialize(self.path, 120)

    def test_split_current_transfers_only_the_measured_remainder(self):
        with mock.patch.object(compute.time, "monotonic", side_effect=[0, 1100]), \
                mock.patch.object(compute.subprocess, "call", return_value=0):
            self.assertEqual(0, compute.run(["judge-and-source"]))
        transferred = compute.export_current(self.path)
        resumed = self.path.with_name("continuation")
        compute.resume_current(resumed, transferred)
        self.assertEqual(6100, float(resumed.read_text()))
        with mock.patch.dict(os.environ, {compute.BUDGET: str(resumed)}), \
                mock.patch.object(compute.time, "monotonic", side_effect=[9000, 9200]), \
                mock.patch.object(compute.subprocess, "call", return_value=23) as execute:
            self.assertEqual(23, compute.run(["validator"]))
        self.assertEqual("6100.0s", execute.call_args.args[0][2])
        self.assertEqual(5900, float(resumed.read_text()))
        with self.assertRaises(FileExistsError):
            compute.resume_current(resumed, "7200")
        for invalid in ("", "nan", "inf", "-1", "7200.001", "21600"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compute.resume_current(self.path.with_name("invalid"), invalid)
        self.path.write_text("7201")
        with self.assertRaises(ValueError):
            compute.export_current(self.path)

    def test_zero_transferred_remainder_starts_no_new_compute(self):
        resumed = self.path.with_name("exhausted")
        compute.resume_current(resumed, "0")
        with mock.patch.dict(os.environ, {compute.BUDGET: str(resumed)}), \
                mock.patch.object(compute.subprocess, "call") as execute:
            self.assertEqual(124, compute.run(["validator"]))
        execute.assert_not_called()

    def test_interrupted_owner_leaves_no_reusable_allowance(self):
        with mock.patch.object(compute.subprocess, "call", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                compute.run(["compile"])
        self.assertEqual(0, float(self.path.read_text()))

    def test_overlapping_owner_is_refused_without_starting_a_command(self):
        with self.path.open("r+") as owner:
            compute.fcntl.flock(owner, compute.fcntl.LOCK_EX | compute.fcntl.LOCK_NB)
            with mock.patch.object(compute.subprocess, "call") as execute:
                with self.assertRaises(BlockingIOError):
                    compute.run(["compile"])
                execute.assert_not_called()

    def test_missing_or_corrupt_state_never_starts_computation(self):
        with mock.patch.dict(os.environ, {compute.BUDGET: ""}):
            with self.assertRaises(ValueError):
                compute.run(["compile"])
        for value in ("nan", "inf", "-1", "invalid", "21601"):
            self.path.write_text(value)
            with mock.patch.object(compute.subprocess, "call") as execute:
                with self.assertRaises(ValueError):
                    compute.run(["compile"])
                execute.assert_not_called()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            compute.run(["compile"])

    def test_nested_test_commands_use_the_outer_timer(self):
        with mock.patch.dict(os.environ, {compute.ACTIVE: "1"}), \
                mock.patch.object(compute.subprocess, "call", return_value=19) as execute:
            self.assertEqual(19, compute.run(["test-program", "argument"]))
            execute.assert_called_once_with(["test-program", "argument"])
        self.assertEqual(7200, float(self.path.read_text()))

    @unittest.skipUnless(shutil.which("timeout"), "GNU timeout is required")
    def test_real_timeout_stops_a_hung_command(self):
        self.path.write_text("0.1")
        result = subprocess.run([sys.executable, str(SCRIPT), "run", "--", sys.executable,
                                 "-c", "import signal; signal.pause()"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(124, result.returncode, result.stderr)
        self.assertEqual(0, float(self.path.read_text()))

    @unittest.skipUnless(shutil.which("timeout"), "GNU timeout is required")
    def test_real_child_output_and_nonzero_exit_survive(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "run", "--", sys.executable,
                                 "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(23)"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(23, result.returncode)
        self.assertEqual("out\n", result.stdout)
        self.assertIn("err\n", result.stderr)

    def test_prepared_dotnet_entry_does_not_restore_or_change_test_environment(self):
        directory = self.path.parent
        dotnet = directory / "dotnet"
        log = directory / "calls"
        dotnet.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                          "with open(os.environ['CALLS'], 'a') as stream:\n"
                          "    stream.write(json.dumps([sys.argv[1:], os.environ.get('CI_DOTNET_PREPARED')]) + '\\n')\n")
        dotnet.chmod(0o755)
        root = SCRIPT.parents[3]
        result = subprocess.run(["bash", str(root / "tools/scripts/dotnet-test.sh"), "fixture.csproj"],
                                env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                                         CI_DOTNET_PREPARED="1", CALLS=str(log),
                                         TEST_RESULTS_DIRECTORY=str(directory / "results")),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(["test", "build", "run", "run"], [args[0] for args, _ in calls])
        for args, inherited in calls:
            self.assertIsNone(inherited)
            if args[0] in ("test", "build"):
                self.assertIn("--no-restore", args)


class ParallelComputeTests(unittest.TestCase):
    @staticmethod
    def job(batch, judge, check, active=False):
        from datetime import datetime, timezone

        def point(second):
            return datetime.fromtimestamp(second, timezone.utc).isoformat() if second is not None else None

        def step(name, bounds):
            if not bounds:
                return dict(name=name, status="completed", conclusion="skipped", started_at=None, completed_at=None)
            start, end = bounds
            return dict(name=name, status="completed" if end is not None else "in_progress",
                        conclusion="success" if end is not None else None,
                        started_at=point(start), completed_at=point(end))

        return dict(name=f"current / Lean report batch {batch}", started_at=point(10),
                    conclusion=None if active else "success",
                    steps=[step("Build the batch judge", judge), step("Check the Lean batch", check)])

    def test_shared_parallel_clock_bills_compute_union_and_excludes_preparation(self):
        import ci_parallel_compute as parallel
        jobs = [self.job(0, (20, 23), (50, 59)), self.job(1, None, (55, 80))]
        self.assertEqual(65, parallel.allowance(jobs, 2, 100, 10, 110, final=True))
        self.assertEqual(64, parallel.allowance([jobs[0], self.job(1, None, (70, 91))],
                                                2, 100, 10, 110, final=True))

    def test_active_workers_subtract_previous_batches_and_are_bounded_together(self):
        import ci_parallel_compute as parallel
        active = [self.job(0, None, (50, 75)), self.job(1, None, (70, None), active=True)]
        self.assertEqual(65, parallel.allowance(active, 2, 100, 10, 85))
        self.assertEqual(74, parallel.allowance(active[:1], 2, 100, 10, 85))
        self.assertEqual(0, parallel.allowance(active[:1], 2, 20, 10, 85))
        queued = dict(name="current / Lean report batch 2", status="queued", started_at=None)
        self.assertEqual(65, parallel.allowance([*active, queued], 3, 100, 10, 85))

    def test_join_rejects_missing_duplicate_failed_and_incomplete_batches(self):
        import ci_parallel_compute as parallel
        job = self.job(0, None, (30, 45))
        incomplete = dict(job, steps=job["steps"][:1])
        for jobs in ([], [job, job], [dict(job, conclusion="failure")], [incomplete]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                parallel.allowance(jobs, 1, 100, 5, 90, final=True)

    def test_prior_completed_attempt_is_charged_without_inter_attempt_idle_time(self):
        import ci_parallel_compute as parallel
        older = self.job(0, None, (20, 39))
        later = self.job(1, None, (86000, 86009))
        self.assertEqual(70, parallel.allowance([later, older], 2, 100, 85990, 86100, final=True))


if __name__ == "__main__":
    unittest.main()
