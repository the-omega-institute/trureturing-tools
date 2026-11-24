"""Check per-module keys, recovery boundaries and execution of a shared compiler DAG."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import Mock, patch

import lean_cache_generation as generation
import lean_generation_plan as planner
from lean_artifact_inventory import Module
from lean_generation_index import read
from test_lean_cache_generation import Store, read_from


class GenerationPlanner(unittest.TestCase):
    def test_target_config_ignores_unrelated_libraries_binds_compiler_options(self):
        config = {"name": "pkg", "lean_lib": [{"name": "Math"}, {"name": "Test"}]}
        original = planner.module_config(config, "Math.A")
        config["lean_lib"][0]["roots"] = ["Math", "Other"]
        config["lean_lib"][0]["globs"] = ["Math.+", "Other.+"]
        config["lean_lib"][1]["leanOptions"] = {"trace.compiler": True}
        config["lean_exe"] = [{"name": "audit", "root": "Test.Main"}]
        self.assertEqual(original, planner.module_config(config, "Math.A"))
        config["lean_lib"][0]["leanOptions"] = {"autoImplicit": False}
        self.assertNotEqual(original, planner.module_config(config, "Math.A"))

    def test_conflicting_checkpoint_and_missing_predecessor(self):
        targets = {name: {"key": planner.digest(name), "deps": deps} for name, deps in
                   [("A", []), ("B", ["A"])]}
        def snapshot(names):
            return {"targets": {n: dict(targets[n], sha256=n, volume=n, paths=[n]) for n in names},
                    "volumes": {n: 1 for n in names}}
        self.assertEqual(planner.checked_hits(targets, [snapshot(["B"])])[0], {})
        cp = snapshot(["A", "B"])
        other = deepcopy(cp)
        other["targets"]["A"]["sha256"] = "conflict"
        with self.assertRaisesRegex(ValueError, "conflicting payloads"):
            planner.checked_hits(targets, [cp, other])
        plan, _ = planner.plan_layers("s", targets, [cp])
        self.assertEqual(plan["stages"], {})

    def test_discovery_fetches_only_intersecting_partial_index_and_binds_its_source(self):
        key = "b" * 64
        row = {"key": key, "deps": [], "paths": ["lib/A.olean"], "sha256": "c" * 64,
               "volume": "d" * 64, "offset": 0, "bytes": 3}
        source = "a" * 40
        receipt = {"source": source, "targets": {"A": row}, "volumes": {row["volume"]: 3},
                   "producer": {"run": "12", "attempt": "1"}}
        remote = Mock()
        remote.get_index.return_value = receipt
        remote.api.return_value = {"repository": {"full_name": planner.checkpoints.host.REPO}, "head_branch": "dev",
                                   "path": ".github/workflows/cache-release-publish.yml", "event": "schedule"}
        catalog = [("1" * 64, source, ["other"]), ("2" * 64, source, [key])]
        with patch.object(planner.checkpoints, "descriptors", return_value=catalog):
            self.assertEqual(list(planner.checkpoints.discover(remote, "0" * 64, {key})), [receipt])
        self.assertEqual(remote.get_index.call_count, 1)
        self.assertEqual(remote.get_index.call_args.args[-1], "checkpoint")
        candidates = [("4" * 64, source, [key]), ("5" * 64, source, [key])]
        for error in (planner.checkpoints.MissingCheckpoint("expired"), planner.checkpoints.InvalidIndexBody("digest")):
            remote.get_index.side_effect = [error, receipt]
            with self.subTest(error=error), patch.object(planner.checkpoints, "descriptors", return_value=candidates):
                self.assertEqual(list(planner.checkpoints.discover(remote, "0" * 64, {key})), [receipt])
        invalid = deepcopy(receipt)
        invalid["targets"]["A"]["bytes"] = -1
        remote.get_index.side_effect = [invalid, receipt]
        with patch.object(planner.checkpoints, "descriptors", return_value=candidates):
            self.assertEqual(list(planner.checkpoints.discover(remote, "0" * 64, {key})), [receipt])
        conflict = deepcopy(receipt)
        conflict["targets"]["B"] = {**row, "paths": ["lib/B.olean"], "sha256": "f"*64}
        remote.get_index.side_effect = [conflict, receipt]
        with patch.object(planner.checkpoints, "descriptors", return_value=candidates), self.assertRaisesRegex(
                ValueError, "conflicting artifacts for input key"):
            list(planner.checkpoints.discover(remote, "0" * 64, {key}))
        remote.get_index.side_effect = None
        remote.api.return_value["event"] = "push"
        with patch.object(planner.checkpoints, "descriptors", return_value=catalog):
            with self.assertRaisesRegex(ValueError, "protected dev publisher"):
                list(planner.checkpoints.discover(remote, "0" * 64, {key}))

    @unittest.skipUnless(shutil.which("lake"), "pinned Lake required for compiler fixture")
    def test_parallel_diamond_and_interrupted_worker(self):
        temp = tempfile.TemporaryDirectory(prefix="generation worker ")
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        pin = (Path(__file__).resolve().parents[3] / "lean-toolchain").read_text()
        names = ["Fixture.Base", "Fixture.Left", "Fixture.Right", "Fixture.Tail"]
        graph = {names[0]: [], names[1]: names[:1], names[2]: names[:1], names[3]: [names[2]]}
        modules = {n: Module("fixture", Path(n.replace(".", "/") + ".lean"), Path(".")) for n in graph}
        remote = Store()
        start = threading.Barrier(2)
        windows = {}
        saved = []
        compile_roots = planner.compiler.compile_roots

        def checkout(path, mathlib_revision="2" * 40):
            src = path / "tools/lean-inspector-reg"
            src.mkdir(parents=True)
            (src / "lakefile.toml").write_text('name = "fixture"\nsrcDir = "../.."\nbuildDir = "../../.lake/build"\n'
                                             '[[lean_lib]]\nname = "Fixture"\nglobs = ["Fixture.+"]\n')
            (path / "lean-toolchain").write_text(pin)
            (path / "Fixture").mkdir()
            for n, deps in graph.items():
                file = path / modules[n].source
                file.write_text("".join("import " + d + "\n" for d in deps) + "def " + n.split(".")[-1] + "Value := 5\n")
            wrapper = path / "tools/scripts/worktree/lean-cache-run.sh"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('exec "$@"\n')
            # Lake creates its manifest once, ahead of each independent timed compile.
            subprocess.run(["lake", "-d", str(src), "update"], cwd=path, check=True, capture_output=True)

            # A fixed manifest can model an upstream pin change without fetching mathlib.
            planner.write(path / "lake-manifest.json",
                          {"version": "1.3.0", "packages": [{"name": "mathlib", "type": "git",
                                                             "rev": mathlib_revision}]})
            for relative, package in [(".", "main"), ("Reg", "reg"), ("tools/lean-inspector", "producer"),
                                      ("tools/lean-inspector-interface", "interface")]:
                folder = path / relative
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "lakefile.toml").write_text(f'name = "{package}"\n')

        def keys(path):
            selection = generation.host.report_selection
            def paths(*names):
                return {"include": [{"pattern": n, "optional": False} for n in names], "exclude": []}
            loader = path / selection.LOADER
            loader.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(selection.__file__, loader)
            planner.write(path / selection.MANIFEST, {
                "schema_version": 1, "report_execution": {"toolchain": "lean-toolchain", **selection.REPORT_EXECUTION},
                "report_modules": paths("Fixture/Base.lean"), "inspector_sources": paths(),
                "config_inputs": paths("lean-toolchain"),
                "producer_scopes": {"lean-report": paths(selection.MANIFEST, selection.LOADER), "scribe-content": paths()}})
            return planner.package_inputs(path)[0], planner.module_targets(
                path, modules, {name: set(deps) for name, deps in graph.items()})

        old_root = directory / "old revision"
        checkout(old_root, "1" * 40)
        old_domain, old_targets = keys(old_root)
        old_plan = {"source": "a" * 40, "domain": old_domain, "targets": old_targets}
        compile_roots(old_root, modules, names)
        with patch.object(planner.checkpoints, "save"):
            old_receipt, _ = planner.batch_frames(old_root, old_plan, modules, names, old_root / "receipt.json",
                                                  old_domain, remote)
        generation.publish(remote, old_plan, None, [old_receipt], "")
        old_index = read_from(remote, directory, "ci")
        new_root = directory / "upgraded revision"
        checkout(new_root)
        domain, targets = keys(new_root)
        self.assertNotEqual(domain, old_domain)
        self.assertTrue(all(old_targets[n]["key"] != targets[n]["key"] for n in names))
        plan, hits = planner.plan_layers("b" * 40, targets, [old_index])
        plan["domain"] = domain
        self.assertFalse(hits["targets"])

        def compile(root, modules, roots, *, no_build=False):
            if no_build:
                return compile_roots(root, modules, roots, no_build=True)
            name, = roots
            if name == names[3]:
                raise InterruptedError("fixture stopped after the preceding durable batch")
            if name in names[1:3]:
                start.wait(timeout=45)
            began = time.monotonic()
            compile_roots(root, modules, roots)
            if name in windows:
                raise AssertionError("duplicate compilation of " + name)
            windows[name] = (began, time.monotonic())

        def record(remote, domain, receipt, path):
            from lean_cache_generation import stage_indexes
            stage_indexes(remote, receipt, path)
            saved.append(deepcopy(receipt))

        def worker(stage, seed):
            root = directory / stage
            checkout(root)
            output = root / "result.json"
            planner.work_batches(root, modules, remote, plan, deepcopy(seed), plan["stages"][stage], output,
                                 batch_size=1)
            return read(output)

        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"}), \
                patch.object(planner.compiler, "compile_roots", side_effect=compile), \
                patch.object(planner.checkpoints, "save", side_effect=record):
            common = worker("common", hits)
            with ThreadPoolExecutor(2) as pool:
                jobs = [pool.submit(worker, s, common) for s in plan["stages"] if s != "common"]
                outcomes = []
                for job in jobs:
                    try:
                        job.result()
                        outcomes.append("built")
                    except InterruptedError:
                        outcomes.append("interrupted")
        self.assertCountEqual(outcomes, ["built", "interrupted"])
        lo = max(windows[n][0] for n in names[1:3])
        hi = min(windows[n][1] for n in names[1:3])
        self.assertGreater(hi - lo, 0)
        self.assertEqual(set(windows), set(names[:3]))
        for transport in ("ci", "release"):
            visible = read_from(remote, directory, transport)
            self.assertEqual(visible, old_index)
            restored = generation.restore(remote, visible, old_plan, [names[0]], directory / ("old-" + transport))
            self.assertEqual(restored["restored"], 1)
        next_plan, recovered = planner.plan_layers("c" * 40, targets, [old_index, *saved])
        next_plan["domain"] = domain
        self.assertEqual(set(recovered["targets"]), set(names[:3]))
        self.assertEqual({tuple(layer["owned"]) for layer in next_plan["stages"].values()}, {(names[3],)})
        failed_stage = next(s for s, layer in plan["stages"].items() if names[3] in layer["owned"])
        original_layer = plan["stages"][failed_stage]
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), \
                patch.object(planner.checkpoints, "discover", return_value=list(saved)), \
                patch.object(planner.checkpoints, "save", side_effect=record):
            rerun = directory / "failed jobs only"
            checkout(rerun)
            output = rerun / "result.json"
            planner.work_batches(rerun, modules, remote, plan, deepcopy(common), original_layer, output, batch_size=1)
            finished = read(output)
        self.assertEqual(finished["metrics"]["compiled_targets"], 1)
        self.assertEqual(finished["metrics"]["checkpoint_targets"], 1)
        self.assertEqual(set(finished["targets"]), {names[2], names[3]})
        self.assertEqual(finished["targets"][names[2]], recovered["targets"][names[2]])
        independent = directory / "new publication run"
        checkout(independent)
        self.assertEqual(keys(independent), (domain, targets))
        only_layer, = next_plan["stages"].values()
        resumed = independent / "receipt.json"
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"}), patch.object(planner.checkpoints, "save"):
            planner.work_batches(independent, modules, remote, next_plan, deepcopy(recovered), only_layer, resumed)
        missing = read(resumed)
        self.assertEqual(missing["metrics"]["compiled_targets"], 1)
        self.assertEqual(set(missing["targets"]), {names[3]})
        generation.publish(remote, next_plan, old_index, [recovered, missing], remote.revision)
        current = read_from(remote, directory, "release")
        self.assertEqual(current, read_from(remote, directory, "ci"))
        body_bytes, physical = remote.block_bytes, len(remote.volumes)
        warm, warm_hits = planner.plan_layers(next_plan["source"], targets, [current])
        self.assertFalse(warm["stages"])
        self.assertEqual(set(warm_hits["targets"]), set(names))
        generation.publish(remote, warm, current, [], remote.revision)
        self.assertEqual((remote.block_bytes, len(remote.volumes)), (body_bytes, physical))
        print(f"LEAN_MATHLIB_UPGRADE checkpoint_reuse={len(recovered['targets'])} "
              f"resumed_compiles={missing['metrics']['compiled_targets']} "
              f"uploaded_bytes={missing['metrics']['uploaded_bytes']} warm_compiles=0 warm_old_bytes=0 warm_volumes=0")
        print(f"LEAN_GENERATION_FIXTURE exclusive_compile_overlap_seconds={hi-lo:.3f} "
              f"shared_compiles=1 recovered_after_interruption={len(recovered['targets'])}")

    @unittest.skipUnless(shutil.which("lake"), "pinned Lake required for compiler fixture")
    def test_lake_rebuilds_only_the_library_with_changed_compiler_settings(self):
        with tempfile.TemporaryDirectory(prefix="generation Lake config ") as directory:
            root = Path(directory)
            pkg = root / "tools/lean-inspector-reg"
            pkg.mkdir(parents=True)
            cfg = pkg / "lakefile.toml"
            fixture_config = ('name = "fixture"\nsrcDir = "../.."\nbuildDir = "../../.lake/build"\n'
                              '[[lean_lib]]\nname = "Unchanged"\n[[lean_lib]]\nname = "Changed"\n')
            cfg.write_text(fixture_config)
            (root / "lean-toolchain").write_bytes((Path(__file__).resolve().parents[3] / "lean-toolchain").read_bytes())
            (root / "Unchanged.lean").write_text("def original := 1\n")
            (root / "Changed.lean").write_text(
                'import Lean\nopen Lean Elab Term\nelab "settingValue" : term => do\n'
                '  return mkNatLit (if (← getOptions).getBool `autoImplicit true then 1 else 2)\n'
                'def configured : Nat := settingValue\n')
            modules = {name: Module("fixture", Path(name + ".lean"), Path(".")) for name in ("Changed", "Unchanged")}
            wrapper = root / "tools/scripts/worktree/lean-cache-run.sh"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('exec "$@"\n')
            def inputs(_):
                return "fixture compiler", {"fixture": tomllib.loads(cfg.read_text())}
            def keys():
                return planner.module_targets(root, modules, {name: set() for name in modules})
            def values():
                (pkg / "Probe.lean").write_text("import Changed\n#eval configured\n")
                return subprocess.check_output(["lake", "env", "lean", "Probe.lean"], cwd=pkg, text=True).strip()
            with patch.object(planner, "package_inputs", side_effect=inputs):
                old = keys()
                planner.compiler.compile_roots(root, modules, sorted(modules))
                self.assertEqual(values(), "1")
                cfg.write_text(fixture_config + "leanOptions = { autoImplicit = false }\n")
                new = keys()
                self.assertEqual(old["Unchanged"], new["Unchanged"])
                self.assertNotEqual(old["Changed"], new["Changed"])
                planner.compiler.compile_roots(root, modules, ["Unchanged"], no_build=True)
                planner.compiler.compile_roots(root, modules, ["Changed"])
                self.assertEqual(values(), "2")


if __name__ == "__main__":
    unittest.main()
