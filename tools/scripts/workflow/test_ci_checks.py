"""Check execution, prerequisites, exit interpretation and complementary subsets."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import ci_checks as checks
import ci_contract as contract

ROOT = Path(__file__).resolve().parents[3]


class CheckTests(unittest.TestCase):
    def test_missing_executable_is_observable_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            code = checks.with_budget(Path(directory) / "budget", lambda: checks.execute(
                "restore", [str(Path(directory) / "missing")], compute=False))
        record = json.loads(output.getvalue().split("CI_CHECK ")[-1])
        self.assertEqual(code, 2)
        self.assertEqual(record["raw_exit"], 2)

    def test_pr_and_push_use_the_same_scoped_command_with_ambient_full_build(self):
        for purpose in ("pull-request", "push"):
            with self.subTest(purpose=purpose):
                result, calls, plan = self.local_spies(purpose=purpose)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(["pr-lean", "D5/changed.lean"], calls)
                if plan["hits"].get("omega"):
                    self.assertIn(["omega", "D5/changed.lean"], calls)
                self.assertFalse(any(call[:2] == ["make", "lean-report"] for call in calls))

    def test_hosted_setup_uses_actual_targets_and_programs(self):
        data, _ = contract.load(ROOT)
        plan = contract.selection(data, ["README.md"], "push")
        for targets, programs, needs in (([], [], False), (["D5.A"], [], True), ([], ["Audit"], True)):
            with self.subTest(targets=targets, programs=programs), tempfile.TemporaryDirectory() as temp:
                scope = Path(temp) / "plan"
                output = Path(temp) / "output"
                scope.write_text(json.dumps({"targets": targets, "programs": programs}))
                with patch.object(checks, "execute", return_value=0), patch.dict(os.environ, GITHUB_OUTPUT=str(output)):
                    self.assertEqual(checks.current_phase("lean-plan", data, plan, None, Path("changed"), scope), 0)
                self.assertEqual(output.read_text().splitlines(), [
                    "builds_lean=" + str(bool(targets or programs)).lower(), "needs_lean=" + str(needs).lower()])

    def test_failed_plan_blocks_report_and_empty_scope_skips_the_toolchain(self):
        data, _ = contract.load(ROOT)
        plan = contract.selection(data, ["Meta/domains.yaml"], "push")
        for planning in (0, 2):
            with self.subTest(planning=planning), patch.object(checks, "prepare_lean_scope", return_value=(planning, False)), \
                    patch.object(checks, "prepare_local_lean") as setup, patch.object(checks, "current_phase", return_value=0) as phase:
                self.assertEqual(checks.current_run(data, plan, "a" * 40, Path("changed"),
                    checks.Prerequisite(0, "accepted")), planning)
                setup.assert_not_called()
                self.assertEqual([call.args[0] for call in phase.call_args_list], ["pr-lean"] if planning == 0 else [])

    def test_hosted_scope_rejects_missing_manifest_before_launch(self):
        data, revision = contract.load(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "changed"
            command = ["ci_checks.py", "current", "pr-lean", "--purpose", "push",
                       "--changed", str(changed)]
            with patch.object(checks, "load", return_value=(data, revision)), \
                    patch.object(checks, "execute", return_value=0) as execute, \
                    patch.dict(os.environ, CI_BASE=""), patch.object(sys, "argv", command), \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(checks.main(), 2)
                self.assertIn("missing changed-path manifest", error.getvalue())
                execute.assert_not_called()
                contract.write_paths(changed, ["D5/Changed.lean"])
                self.assertEqual(checks.main(), 2)
                command.extend(["--base", "b" * 40])
                self.assertEqual(checks.main(), 0)
                self.assertEqual(execute.call_args.args[1][-3:], [str(changed), "--protected-base", "b" * 40])

    def test_source_delta_uses_changed_paths_without_a_report(self):
        data, _ = contract.load(ROOT)
        plan = contract.selection(data, ["D5/Changed.lean"], "pull-request")
        base = "a" * 40
        with patch.object(checks, "execute", return_value=0) as execute:
            self.assertEqual(checks.current_phase("check-delta", data, dict(plan, report=False), base, Path("changed")), 0)
            self.assertEqual(execute.call_args.args[1], ["dotnet", checks.CLI, "check-delta", "--protected-base", base,
                                                      "--changed-paths-from", "changed"])
            self.assertTrue(execute.call_args.kwargs["delta"])

    def test_report_workers_publish_only_after_scribe_and_finalize_in_plan_order(self):
        import ci_pr_lean as lean
        from types import SimpleNamespace
        from contextlib import ExitStack

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            manifest = root / "changed"
            contract.write_paths(manifest, ["some-file"])
            plan = root / "plan"
            scope = {"targets": ["D5.A", "D5.B"], "paths": ["A.scribe.cs"],
                     "definitions": {"A.scribe.cs": ["D5.A"]}, "programs": ["Reg.Program"]}
            plan.write_text(json.dumps(scope))
            args = SimpleNamespace(batch_matrix=False, manifest=manifest, read_plan=plan, artifacts=root / "artifacts",
                                   batch=None, base="b" * 40, protected_base="b" * 40)
            stack.enter_context(patch.object(lean, "report_batches", return_value=[
                (["D5.A"], ["A.scribe.cs"]), (["D5.B"], [])]))
            stack.enter_context(patch.object(lean, "build_environment", return_value={}))
            programs = stack.enter_context(patch.object(lean, "check_programs"))
            history = []
            current_exit, reject_scribe = 0, True

            def execute(command, **options):
                if command[:2] == ["make", "lean-report-scoped"]:
                    output = Path(options["env"]["LEAN_REPORT"])
                    (output.parent / (output.name + ".logs")).mkdir()
                    for suffix in ("", ".sha256", ".materials.zip", ".input.attestation", ".provenance.json"):
                        (output.parent / (output.name + suffix)).write_text(command[2])
                if len(command) > 2 and command[2] == "content-check":
                    paths = lambda flag: Path(command[command.index(flag) + 1]).read_bytes()
                    history.append((paths("--targets-from"), paths("--paths-from")))
                    if reject_scribe:
                        raise subprocess.CalledProcessError(7, command)
                if len(command) > 2 and command[2] in ("check-current", "check-delta"):
                    listing = Path(command[command.index("--scoped-reports-from") + 1]).read_bytes()
                    history.append((command[2], [Path(os.fsdecode(p)).parent.name for p in listing.split(b"\0") if p]))
                    return subprocess.CompletedProcess(command, current_exit if command[2] == "check-current" else 3)
                return subprocess.CompletedProcess(command, 0)
            stack.enter_context(patch.object(lean.subprocess, "check_output", return_value="c" * 40))
            stack.enter_context(patch.object(lean.subprocess, "run", side_effect=execute))

            args.batch = 1
            with self.assertRaises(subprocess.CalledProcessError):
                lean.distributed(args, scope)
            self.assertEqual(list(args.artifacts.iterdir()), [])
            reject_scribe = False
            lean.distributed(args, scope)
            args.batch = None
            with self.assertRaisesRegex(ValueError, "incomplete"):
                lean.distributed(args, scope)
            args.batch = 0
            lean.distributed(args, scope)
            args.batch = None
            self.assertEqual(lean.distributed(args, scope), 0)
            self.assertEqual([call.args[0] for call in programs.call_args_list], [[], [], ["Reg.Program"]])
            self.assertEqual(history[:3], [(b"D5/B.lean\0", b""), (b"D5/B.lean\0", b""),
                                          (b"D5/A.lean\0", b"A.scribe.cs\0")])
            self.assertEqual(history[3:], [(name, ["batch-0", "batch-1"]) for name in ("check-current", "check-delta")])
            current_exit = 1
            self.assertEqual(lean.distributed(args, scope), 1)

            batch = args.artifacts / "batch-1"
            extra = batch / "extra"
            extra.touch()
            with self.assertRaisesRegex(ValueError, "unexpected files"):
                lean.distributed(args, scope)
            extra.unlink()
            extra = args.artifacts / "batch-9"
            extra.mkdir()
            with self.assertRaisesRegex(ValueError, "unexpected report batch"):
                lean.distributed(args, scope)
            extra.rmdir()
            with patch.dict(os.environ, GITHUB_RUN_ID="another-run"), self.assertRaisesRegex(ValueError, "does not match"):
                lean.distributed(args, scope)
            material = batch / "scoped-lean-report.json.materials.zip"
            material.write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "does not match"):
                lean.distributed(args, scope)
            material.unlink()
            with self.assertRaises(FileNotFoundError):
                lean.distributed(args, scope)

    def test_admission_reuses_all_batches_and_runs_without_reports_for_source_changes(self):
        import ci_pr_lean
        cases = [([], [], "push", "", "", 0, 3, 0),
                 ([], ["LeanInformationAudit.Program"], "pull_request", "", "/judge/producer.dll", 0, 0, 0),
                 (["D5.A", "D5.B"], ["LeanInformationAudit.Program"], "push", "", "/judge/producer.dll", 1, 0, 1),
                 (["D5.A"], [], "pull_request", "", "", 0, 2, 2),
                 (["D5.A"], [], "pull_request", "/caller/producer.dll", "", 0, 0, 0),
                 (["D5.A"], [], "", "", "", 0, 0, 0)]
        for targets, programs, event, supplied, detected, current_exit, delta_exit, expected in cases:
            with self.subTest(targets=targets, programs=programs, event=event, supplied=supplied), tempfile.TemporaryDirectory() as temp:
                changed = Path(temp) / "changed"
                contract.write_paths(changed, ["some-file"])
                built, consumers, commands = [], [], []
                def run(command, **options):
                    commands.append(command)
                    if command[1] == "tools/scripts/workflow/judge-lean-producer.sh":
                        self.assertEqual(command, ["bash", "tools/scripts/workflow/judge-lean-producer.sh",
                                                   "tools/StrataLint.Cli/bin/Release/net10.0"])
                        return subprocess.CompletedProcess(command, 0, detected + "\n")
                    if command[0] == "make" or command[1] == "tools/scripts/worktree/lean-cache-run.sh":
                        self.assertTrue(options["check"])
                        self.assertEqual(options["env"]["STRATALINT_LEAN_PRODUCER_DLL"], supplied or detected)
                    if command[:2] == ["make", "lean-report-scoped"]:
                        self.assertEqual(commands[-2], ["make", "lean-report-prepare", command[2]])
                        report = Path(options["env"]["LEAN_REPORT"])
                        self.assertNotIn(report, built)
                        report.touch()
                        built.append(report)
                    if command[2] in ("check-current", "check-delta"):
                        consumers.append(command[2])
                        manifest = Path(command[command.index("--changed-paths-from") + 1])
                        self.assertEqual(contract.read_changed(manifest), ["some-file"])
                        reports = Path(command[command.index("--scoped-reports-from") + 1])
                        self.assertEqual(reports.read_bytes(), b"".join(os.fsencode(p) + b"\0" for p in built))
                        self.assertTrue(all(path.is_file() for path in built))
                        return subprocess.CompletedProcess(command, current_exit if command[2] == "check-current" else delta_exit)
                    return subprocess.CompletedProcess(command, 0)
                scope_file = Path(temp) / "scope"
                scope_file.write_text(json.dumps({"programs": programs, "targets": targets, "paths": [],
                    "definitions": {}, "cache_suites": ["source-evidence", "compiled-audit"]}))
                with patch.object(ci_pr_lean, "plan_scope", side_effect=AssertionError("scope planned twice")), \
                        patch.dict(os.environ, GITHUB_EVENT_NAME=event, STRATALINT_LEAN_PRODUCER_DLL=supplied), \
                        patch.object(ci_pr_lean.subprocess, "run", side_effect=run), \
                        patch.object(ci_pr_lean, "report_batches", return_value=[([target], []) for target in targets]):
                    self.assertEqual(ci_pr_lean.check(changed, "a" * 40, scope_file), expected)
                self.assertEqual(consumers, ["check-current", "check-delta"])
                self.assertEqual(len(built), len(targets))
                probes = [cmd for cmd in commands if cmd[1] == "tools/scripts/workflow/judge-lean-producer.sh"]
                self.assertEqual(len(probes), int(bool((targets or programs) and event and not supplied)))
                self.assertEqual([cmd[6:] for cmd in commands if cmd[1] == "tools/scripts/worktree/lean-cache-run.sh"],
                                 [["+" + program for program in programs]] if programs else [])
                self.assertEqual([cmd[2] for cmd in commands if cmd[:2] == ["make", "lean-report-prepare"]],
                                 ["LEAN_TARGETS=" + target for target in targets])

    def test_failed_report_preparation_stops_analysis_and_admission(self):
        import ci_pr_lean
        with tempfile.TemporaryDirectory() as temp:
            changed, scope_file = Path(temp) / "changed", Path(temp) / "scope"
            contract.write_paths(changed, ["D5/A.lean"])
            scope_file.write_text(json.dumps({"programs": [], "targets": ["D5.A"], "paths": [], "definitions": {}}))
            command = ["make", "lean-report-prepare", "LEAN_TARGETS=D5.A"]
            with patch.dict(os.environ, STRATALINT_LEAN_PRODUCER_DLL="/caller/producer.dll"), \
                    patch.object(ci_pr_lean.subprocess, "run", side_effect=subprocess.CalledProcessError(7, command)) as run:
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    ci_pr_lean.check(changed, "a" * 40, scope_file)
                self.assertEqual(error.exception.returncode, 7)
                self.assertEqual([call.args[0] for call in run.call_args_list], [command])

    def test_real_process_exits_are_preserved(self):
        for raw in (0, 1, 2, 3, 124, 143):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = checks.with_budget(Path(directory) / "budget", lambda: checks.execute(
                        "unit", ["bash", "-c", "exit " + str(raw)]))
                record = json.loads(output.getvalue().split("CI_CHECK ")[-1])
                self.assertEqual(record["raw_exit"], raw)
                self.assertEqual(code, raw)

    def test_budget_is_not_reset_by_nested_shared_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget"
            with patch.dict(os.environ, {checks.ci_compute.BUDGET: str(path), checks.ci_compute.ACTIVE: "0"}):
                checks.ci_compute.initialize(path, 120)
                with self.assertRaises(FileExistsError):
                    checks.with_budget(path, lambda: 0)
                path.write_text("0")
                self.assertEqual(checks.execute("unit", ["bash", "-c", "exit 0"]), 124)

    @patch.object(checks, "prepare_lean_scope", return_value=(0, True))
    def test_failed_judge_does_not_claim_dependent_checks_executed(self, _):
        data, _ = contract.load(ROOT)
        plan = contract.selection(data, ["tools/StrataLint.Lean/Changed.cs"], "push")
        called = []
        def phase(name, *_):
            called.append(name)
            return 2 if name == "build-judge" else 0
        with patch.object(checks, "execute", return_value=0), patch.object(checks, "prepare_unit", return_value=0), \
                patch.object(checks, "current_phase", side_effect=phase), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(checks.current_run(data, plan, "a" * 40, Path("changed")), 2)
        self.assertEqual(called, ["build-judge", "pr-lean"])
        self.assertIn('"status":"blocked"', output.getvalue())

    @patch.object(checks, "prepare_lean_scope", return_value=(0, True))
    def test_failed_dependency_preparation_blocks_actual_execution(self, _):
        data, _ = contract.load(ROOT)
        plan = contract.selection(data, ["D5/changed.lean"], "push")
        with patch.object(checks, "prepare_judge", return_value=2), \
                patch.object(checks, "prepare_local_lean", return_value=2), \
                patch.object(checks, "current_phase") as phase, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(checks.current_run(data, plan, "a" * 40, Path("changed")), 2)
        phase.assert_not_called()
        self.assertIn('"check":"pr-lean","status":"blocked"', output.getvalue())

    def test_source_command_uses_original_current_delta_for_mixed_changes(self):
        data, _ = contract.load(ROOT)
        for paths, purpose, verb, delta in (
                (["D5/changed.lean"], "pull-request", "check-source", True),
                (["D5/changed.lean"], "push", "check-source", True),
                (["D5/changed.lean", "README.md"], "pull-request", "check-source", True)):
            plan = contract.selection(data, paths, purpose)
            with self.subTest(paths=paths, purpose=purpose), patch.object(checks, "execute", return_value=0) as execute:
                checks.current_phase("source", data, plan, "a" * 40, Path("changed"))
                command = execute.call_args.args[1]
                self.assertIn(verb, command)
                self.assertEqual(delta, "--protected-base" in command)
                self.assertNotIn("--candidate-lean-report", command)

    def test_source_only_changes_follow_the_consumer_report_filter(self):
        path = "docs/fixture/input.json"
        report_paths = "*\n!docs/fixture/*.json\n"
        for purpose in ("pull-request", "push"):
            with self.subTest(purpose=purpose):
                result, calls, plan = self.local_spies(paths=(path,), purpose=purpose, report_paths=report_paths)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(plan["hits"]["current"])
                self.assertTrue(plan["source-delta"])
                self.assertFalse(plan["report"])
                self.assertFalse(plan["delta"])
                source = next(call for call in calls if "check-source" in call)
                self.assertIn("--protected-base", source)
                self.assertFalse(any(call[0] in ("lean-plan", "pr-lean", "elan") for call in calls))
                self.assertFalse(any("check-current" in call for call in calls))

        result, _, _ = self.local_spies(paths=(path,), source=1, report_paths=report_paths)
        self.assertEqual(result.returncode, 1)
        data, _ = contract.load(ROOT)
        data["current"]["report_paths"] = report_paths
        for included in ("D5/Changed.lean", "docs/fixture/input.sh"):
            with self.subTest(included=included):
                plan = contract.selection(data, [path, included], "pull-request")
                self.assertTrue(plan["report"])
                self.assertTrue(plan["delta"])
                self.assertTrue(plan["hits"]["filemap"])

    def test_source_rejection_blocks_every_guarded_launch_in_executable_local_adapter(self):
        result, calls, plan = self.local_spies(source=1)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertTrue(plan["hits"]["current"])
        self.assertTrue(any("check-source" in call for call in calls))
        self.assertFalse(any(call[0] in ("elan", "make") for call in calls), calls)
        for unit, hit in plan["hits"].items():
            if hit and unit != "current":
                self.assertIn(f'"check":"{unit}","status":"blocked"', result.stdout)

    def test_readme_skips_current_on_push_and_pull_request(self):
        for purpose in ("pull-request", "push"):
            with self.subTest(purpose=purpose):
                result, calls, plan = self.local_spies(paths=("README.md",), purpose=purpose)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertFalse(plan["hits"]["current"])
                self.assertFalse(any("check-source" in call for call in calls))

    def test_scoped_failure_is_red_and_blocks_report_consumers_only(self):
        for scoped in (0, 1):
            with self.subTest(scoped=scoped):
                result, calls, plan = self.local_spies(scoped=scoped, purpose="push")
                self.assertEqual(scoped, result.returncode, result.stdout + result.stderr)
                records = {record["check"]: record["status"] for record in (
                    json.loads(line.removeprefix("CI_CHECK ")) for line in result.stdout.splitlines()
                    if line.startswith("CI_CHECK "))}
                self.assertTrue(all(records[name] == "success" for name, hit in plan["hits"].items()
                                    if hit and name != "current"))
                self.assertEqual(records["pr-lean"], "failure" if scoped else "success")
                self.assertNotIn("compiled-audit", records)
                self.assertNotIn("source-evidence", records)
                self.assertIn(["pr-lean", "D5/changed.lean"], calls)

    def test_bootstrap_and_source_input_failures_preserve_independent_fallback(self):
        for source, judge in ((2, 0), (0, 1)):
            with self.subTest(source=source, judge=judge):
                result, calls, _ = self.local_spies(source=source, judge=judge, purpose="push")
                self.assertNotEqual(0, result.returncode)
                self.assertIn(["pr-lean", "D5/changed.lean"], calls)
                if judge:
                    self.assertFalse(any("check-source" in call for call in calls))
                self.assertIn(["make", "filemap-conform"], calls)

    def local_spies(self, source=0, scoped=0, judge=0, paths=("D5/changed.lean",), purpose="pull-request",
                    report_paths=None):
        with tempfile.TemporaryDirectory(prefix="ci source with spaces ") as directory:
            root = Path(directory)
            scripts = root / "tools/scripts/workflow"
            scripts.mkdir(parents=True)
            for path in Path(__file__).parent.glob("*.py"):
                if not path.name.startswith("test_"):
                    shutil.copy2(path, scripts / path.name)
            (scripts / "ci_pr_lean.py").write_text(
                "import json, os, sys\nfrom pathlib import Path\nfrom ci_contract import read_changed\n"
                "paths = read_changed(Path(sys.argv[1]))\n"
                "planning = '--write-plan' in sys.argv\n"
                "with open(os.environ['CALLS'], 'a') as output:\n"
                "    output.write(json.dumps(['lean-plan' if planning else 'pr-lean', *paths]) + '\\n')\n"
                "if planning:\n"
                "    Path(sys.argv[-1]).write_text(json.dumps({'programs': [], 'targets': [p for p in paths if p.endswith('.lean')]}))\n"
                "    sys.exit(0)\n"
                "sys.exit(int(os.environ['SCOPED']))\n")
            (root / "Meta").mkdir()
            data, _ = contract.load(ROOT)
            if report_paths is not None:
                data["current"]["report_paths"] = report_paths
            (root / contract.CONTRACT).parent.mkdir(exist_ok=True)
            (root / contract.CONTRACT).write_text(json.dumps(data))
            for unit in data["units"].values():
                for lock in unit["cache-dependency-path"].splitlines():
                    if not lock or "*" in lock:
                        continue
                    path = root / lock
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}")
                    (path.parent / (path.parent.name + ".csproj")).write_text("<Project />")
            if "omega" in data["units"]:
                omega = root / "src/OmegaChecks/check.py"
                omega.parent.mkdir(parents=True)
                omega.write_text(
                    "import json, os\nfrom pathlib import Path\n"
                    "paths = Path(os.environ['CI_CHANGED_PATHS_FILE']).read_text().split(chr(0))[:-1]\n"
                    "with open(os.environ['CALLS'], 'a') as output:\n"
                    "    output.write(json.dumps(['omega', *paths]) + '\\n')\n")
            (root / "lean-toolchain").write_text("fixture-pin\n")
            bindir = root / "bin"
            bindir.mkdir()
            code = (f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                    "name=Path(sys.argv[0]).name; args=sys.argv[1:]\n"
                    "with open(os.environ['CALLS'],'a') as stream: stream.write(json.dumps([name,*args])+'\\n')\n"
                    "exitcode=0\n"
                    "if name=='dotnet':\n"
                    "    if 'check-source' in args: exitcode=int(os.environ['SOURCE'])\n"
                    "    elif args and args[0]=='build': exitcode=int(os.environ['JUDGE'])\n"
                    "sys.exit(exitcode)\n")
            for name in ("dotnet", "make", "elan"):
                path = bindir / name
                path.write_text(code)
                path.chmod(0o755)
            # Isolate the persistent host's tool location without modifying its
            # real home, installed toolchain or the production preparation owner.
            tool = root / "host/.elan/bin/elan"
            tool.parent.mkdir(parents=True)
            tool.write_text(code)
            tool.chmod(0o755)
            runner = root / "invoke.py"
            runner.write_text("import sys\nfrom pathlib import Path\n"
                              "sys.path.insert(0, str(Path(__file__).parent / 'tools/scripts/workflow'))\n"
                              "import ci_checks\n"
                              "ci_checks.Path.home = classmethod(lambda cls: Path(__file__).parent / 'host')\n"
                              "raise SystemExit(ci_checks.main())\n")
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")
            for changed in paths:
                path = root / changed
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("candidate\n")
            env = dict(os.environ, FULL_BUILD="1", PATH=str(bindir) + os.pathsep + os.environ["PATH"], CALLS=str(root / "calls"),
                       SOURCE=str(source), SCOPED=str(scoped), JUDGE=str(judge), JUDGE_HIT="false",
                       STRATALINT_CI_COMPUTE_ACTIVE="0", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, str(runner), "local", "--base", "HEAD", "--purpose", purpose],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()] if (root / "calls").exists() else []
            plan = json.loads(next(line.removeprefix("CI_LOCAL ") for line in result.stdout.splitlines() if line.startswith("CI_LOCAL ")))
            return result, calls, plan

    def test_consumer_unit_executes_its_registered_filemap_check(self):
        data, _ = contract.load(ROOT)
        with patch.object(checks, "execute", return_value=2) as execute:
            self.assertEqual(checks.unit_run("filemap", data["units"]["filemap"]), 2)
            self.assertEqual(execute.call_args.args[1], ["bash", "-e", "-o", "pipefail", "-c", "make filemap-conform"])


class ScopedReportTests(unittest.TestCase):
    def test_new_mathematical_modules_compile_serially_before_the_batch(self):
        import ci_pr_lean as lean

        selection = {"new_d5_modules": ["D5.Left", "D5.Right"],
                     "programs": ["Reg.Spec"], "targets": ["Reg.Spec"], "paths": []}
        environment = {"LEAN_NUM_THREADS": "2"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(lean, "build_environment", return_value=environment), \
                patch.object(lean, "build_modules") as build, \
                patch.object(lean, "check_batch", side_effect=RuntimeError("report failed")):
            with self.assertRaisesRegex(RuntimeError, "report failed"):
                lean.write_batch(Path(directory), 0, selection, {})
            self.assertFalse((Path(directory) / "batch-0").exists())
        self.assertEqual([entry.args for entry in build.call_args_list], [
            (["D5.Left", "D5.Right"], {"LEAN_NUM_THREADS": "1"}),
            (["Reg.Spec"], {"LEAN_NUM_THREADS": "2"}),
        ])

    def test_cached_scope_uses_the_planned_report_and_program_roots(self):
        import ci_pr_lean as lean

        base = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changed, plan = root / "paths", root / "plan.json"
            contract.write_paths(changed, ["Meta/tools-release.json"])
            selected = {"programs": ["Inspector.Feature"], "targets": ["D5.Owner"]}
            with patch.object(lean.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(selected))) as scribe:
                plan.write_text(json.dumps(lean.plan_scope(changed, base)))
                self.assertEqual(scribe.call_args.args[0],
                    [*lean.SCRIBE, "lean-inputs", "--affected", "--paths-from", str(changed), "--base", base])
            command = [sys.executable, lean.__file__, str(changed), "--read-plan", str(plan), "--cache-targets"]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.split(), ["D5.Owner", "Inspector.Feature"])
            plan.write_text(json.dumps({"programs": [], "targets": []}))
            self.assertEqual(subprocess.check_output(command), b"")
            plan.write_text(json.dumps({"programs": [], "targets": ["not a module"]}))
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_saved_cache_plan_expands_selected_libraries_and_resolves_programs(self):
        import ci_pr_lean as lean

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            def write(path, contents=""):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(contents)

            write("lakefile.toml", 'name = "project"\n')
            write("Reg/lakefile.toml", 'name = "reg"\nbuildDir = "../.lake/build/reg"\n')
            write("tools/lean-inspector-interface/lakefile.toml",
                'name = "interface"\nbuildDir = "../../.lake/build/interface"\n')
            write("tools/lean-inspector-reg/lakefile.toml", """
name = "regInspector"
buildDir = "../../.lake/build/inspection"
[[lean_lib]]
name = "LeanInformationAuditRegTests"
roots = ["Audit"]
globs = ["Audit.*", "AuditOnly"]
[[lean_exe]]
name = "compiledJudgeTests"
root = "Audit.Main"
""")
            write("tools/lean-inspector/lakefile.lean", """
lean_exe reportInspector where
  root := `Producer
  supportInterpreter := true

lean_exe inputDiscovery where
  root := `Producer.Discover
""")
            for source in ("Audit", "Audit/Main", "Audit/Deep/Case", "AuditOnly", "AuditOther",
                           "Producer", "Producer/Discover", "Program", "reportInspector"):
                write(f"tools/lean-inspector/{source}.lean")
            for source in ("D5/S3/Fourier/IntegerCharacterCoercivity",
                           "D5/S3/TotalVariation/PrimitiveBridgeCancellation"):
                write(source + ".lean")
                write("Reg/" + source + ".lean")
            write("tools/lean-inspector-interface/LeanInformationAuditInterface/Contract/Registration.lean")
            write("tools/lean-inspector/LeanInformationAudit/TemplateEnrollment.lean")
            write("D5/WithRuntime.lean", "import LeanInformationAuditInterface.Contract.Registration\n")
            write("bin/lean", """#!/usr/bin/env python3
import json
from pathlib import Path
import sys
assert sys.argv[1:] == ["--deps-json", "--stdin"]
headers = []
for path in sys.stdin.read().splitlines():
    imports = [dict(module=line.split()[1]) for line in Path(path).read_text().splitlines() if line.startswith("import ")]
    headers.append(dict(errors=[], result=dict(imports=imports)))
print(json.dumps(dict(imports=headers)))
""")
            (root / "bin/lean").chmod(0o755)
            write("D5/Scoped.lean")
            write("D5/NamedExample.lean")
            write("bin/dotnet", """#!/usr/bin/env python3
import json
import sys
assert sys.argv[2:5] == ["lean-utility-input", "--modules", "--targets"]
roots = sys.argv[5].split()
print(json.dumps(roots + (["D5.NamedExample"] if "D5.Scoped" in roots else [])))
""")
            (root / "bin/dotnet").chmod(0o755)
            env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])

            targets_file = root / "targets"
            command = [sys.executable, "-B", lean.__file__, "changed", "--read-plan", "plan", "--cache-targets"]

            def restored(suites, targets=(), programs=()):
                write("plan", json.dumps(dict(targets=targets, programs=programs, cache_suites=suites)))
                with targets_file.open("w") as output:
                    subprocess.run(command, cwd=root, env=env, check=True, stdout=output)
                modules = targets_file.read_text().split()
                self.assertEqual(modules, sorted(set(modules)))
                return set(modules)

            report_programs = {"Producer", "Producer.Discover"}
            audit = report_programs | {"Audit", "Audit.Main", "Audit.Deep.Case", "AuditOnly"}
            evidence = report_programs | {
                "D5.S3.Fourier.IntegerCharacterCoercivity", "Reg.D5.S3.Fourier.IntegerCharacterCoercivity",
                "D5.S3.TotalVariation.PrimitiveBridgeCancellation", "Reg.D5.S3.TotalVariation.PrimitiveBridgeCancellation",
            }
            self.assertEqual(restored(["compiled-audit"]), audit)
            self.assertEqual(restored(["source-evidence"]), evidence)
            self.assertEqual(restored(["source-evidence", "compiled-audit"],
                ["D5.Scoped"], ["Program"]), evidence | audit | {"D5.Scoped", "D5.NamedExample", "Program"})
            self.assertEqual(restored([], ["D5.Scoped"]), report_programs | {"D5.Scoped", "D5.NamedExample"})
            self.assertEqual(restored([], ["D5.WithRuntime"]), report_programs | {
                "D5.WithRuntime", "LeanInformationAudit.TemplateEnrollment"})
            self.assertEqual(restored([], programs=["D5.WithRuntime"]), {"D5.WithRuntime"})
            self.assertEqual(restored([], programs=["Program"]), {"Program"})
            self.assertEqual(restored([]), set())
            self.assertEqual(targets_file.read_bytes(), b"")

    def test_empty_scope_has_no_build_and_large_scopes_keep_scribe_inputs_together(self):
        from ci_pr_lean import report_batches
        self.assertEqual(list(report_batches({"targets": []})), [])
        scope = {"targets": ["D5.A", "D5.B", "Reg.Z"], "paths": ["Blueprint/doc.scribe.cs"],
                 "definitions": {"Blueprint/doc.scribe.cs": ["D5.A", "D5.B"]}}
        self.assertEqual(list(report_batches(scope, limit=10)),
                         [(["D5.A", "D5.B"], ["Blueprint/doc.scribe.cs"]), (["Reg.Z"], [])])


if __name__ == "__main__":
    unittest.main()
