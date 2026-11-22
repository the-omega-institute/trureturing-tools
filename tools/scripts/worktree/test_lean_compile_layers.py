"""Regression tests for transport of compiler outputs between independent runners."""

from io import BytesIO
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import lean_compile_layers as layers
from lean_artifact_inventory import INSPECTOR, Module, lake_configuration, matches_lake_configuration, required_compiler_paths


class CompilePartitions(unittest.TestCase):
    def test_cache_partition_accepts_only_the_compatible_inspector_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = INSPECTOR / "lakefile.lean"
            installed = root / config_path
            installed.parent.mkdir(parents=True)
            installed.write_bytes((Path(__file__).resolve().parents[3] / config_path).read_bytes())
            original = {config_path.as_posix(): "8e400dfa19ad583ef7f4790b92fa7bb36db40e31f7cdc38a6b8a756ede48343e"}
            old_key = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(old_key, lake_configuration(root, cache_key=True))
            self.assertNotEqual(old_key, lake_configuration(root))
            self.assertTrue(matches_lake_configuration(root, old_key))
            (root / "lean-toolchain").write_text("changed compiler")
            self.assertFalse(matches_lake_configuration(root, old_key))
            (root / "lean-toolchain").unlink()
            installed.write_text("a different Lake package")
            self.assertFalse(matches_lake_configuration(root, old_key))
            self.assertEqual(lake_configuration(root), lake_configuration(root, cache_key=True))

    def test_terminal_closures_cover_scoped_diamond_once(self):
        graph = {"Base": set(), "Shared": {"Base"}, "Unused": set(),
                 **{name: {"Shared"} for name in ("A", "B", "C", "D")}}
        targets = ["A", "B", "C", "D", "Shared"]
        plan = layers.partition(graph, targets)
        self.assertEqual(plan, layers.partition(dict(reversed(graph.items())), reversed(targets)))
        self.assertEqual(plan["common"], layers.Layer(("Shared",), frozenset(("Base", "Shared"))))
        self.assertEqual([part.owned for stage, part in plan.items() if stage != "common"],
                         [{name} for name in "ABCD"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            layers.partition(graph, ["Absent"])

    def test_header_batches_follow_dependencies_outside_the_first_batch(self):
        graph = {f"A{i}": {"Shared"} for i in range(1025)}
        graph.update({"Shared": {"Base"}, "Base": set(), "Unused": set()})
        observed = []
        def read(root, modules, *, sources):
            observed.extend(sources)
            self.assertLessEqual(len(sources), 512)
            return {name: graph[name] for name in sources}
        targets = [name for name in graph if name.startswith("A")]
        with patch.object(layers, "project_imports", side_effect=read):
            result = layers.scoped_imports(Path("."), graph, targets)
        self.assertEqual(result, {name: deps for name, deps in graph.items() if name != "Unused"})
        self.assertEqual(len(observed), len(set(observed)))

    def test_single_target_and_package_batched_verification(self):
        plan = layers.partition({"Reg.A": {"D5.A"}, "D5.A": set()}, ["Reg.A"])
        self.assertEqual(plan["common"].roots, ())
        self.assertEqual(plan["0"].owned, {"Reg.A", "D5.A"})
        names = ["Inspector", "DependencyReadoutMain", "InformationSourceFixture"]
        names.extend("Long." + "A" * 210 + str(i) for i in range(1100))
        modules = {name: Module("leanInspector", Path(name), Path(".")) for name in names}
        with patch.object(subprocess, "run") as run:
            layers.compile_roots(Path("."), modules, names, no_build=True)
        output = []
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--no-build", command)
            self.assertIn("tools/lean-inspector-reg", command)
            portion = command[command.index("build") + 1:]
            self.assertLessEqual(sum(len(t.encode()) + 1 for t in portion), 48 * 1024)
            output.extend(portion)
        self.assertEqual(output, [f"@leanInspector/+{name}:olean" for name in names])


class CompileTransport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.producer = self.root / "producer"
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        self.name = "Reg.Example"
        self.module = Module("reg", Path("Reg/Example.lean"), Path("reg"))
        self.modules = {self.name: self.module}
        self.archive = self.root / "artifacts/0.tar.gz"
        self.identity = {"source": "a" * 40, "partition": "sample", "stage": "0"}
        self.build = self.producer / layers.PROJECT_BUILD
        outputs = {"o": ["hash.olean"], "i": "hash.ilean", "c": "hash.c"}
        for path in required_compiler_paths(self.name, self.module):
            dest = self.build / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".trace":
                dest.write_text(json.dumps({"outputs": outputs}))
            else:
                dest.write_bytes(b"compiler output")
                dest.with_name(dest.name + ".hash").write_bytes(b"hash")

    def pack(self):
        layers.pack(self.producer, self.modules, self.archive, self.identity)

    def restore(self, identity=None):
        layers.recover(self.consumer, self.modules, self.archive, identity or self.identity)

    def test_verified_roundtrip_conflict_and_empty_layer(self):
        self.pack()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.restore(dict(self.identity, source="2" * 40))
        self.assertFalse((self.consumer / layers.PROJECT_BUILD).exists())
        self.restore()
        for path in self.build.rglob("*"):
            if path.is_file():
                replica = self.consumer / layers.PROJECT_BUILD / path.relative_to(self.build)
                self.assertEqual(replica.read_bytes(), path.read_bytes())
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.restore()
        layers.pack(self.root / "empty", {}, self.archive, {"stage": "common"})
        layers.recover(self.consumer, {}, self.archive, {"stage": "common"})

    def test_missing_outputs_and_trace_declared_outputs(self):
        trace = next(self.build.rglob("*.trace"))
        data = json.loads(trace.read_text())
        data["outputs"]["r"] = "hash.ir"
        trace.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "omits Lake compiler outputs"):
            self.pack()
        next(self.build.rglob("*.ilean")).unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.pack()
        self.assertFalse(self.archive.exists())

    def test_corruption_unsafe_members_duplicates_and_parent_symlinks(self):
        self.pack()
        with tarfile.open(self.archive) as archive:
            members = [(m, archive.extractfile(m).read()) for m in archive]
        path, data = members[0]
        for extra in ("../escape", "/tmp/escape", "reg/unowned.olean", path.name):
            with self.subTest(extra=extra):
                with tarfile.open(self.archive, "w:gz") as archive:
                    for meta, content in members[:-1]:
                        archive.addfile(meta, BytesIO(content))
                    archive.addfile(tarfile.TarInfo(extra))
                    archive.addfile(members[-1][0], BytesIO(members[-1][1]))
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertFalse((self.consumer / layers.PROJECT_BUILD).exists())
        with tarfile.open(self.archive, "w:gz") as archive:
            for meta, content in members:
                archive.addfile(meta, BytesIO(b"x" * len(data) if meta == path else content))
        with self.assertRaisesRegex(ValueError, "hashes"):
            self.restore()
        with tarfile.open(self.archive, "w:gz") as archive:
            link = tarfile.TarInfo(path.name)
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            archive.addfile(link)
        with self.assertRaises(ValueError):
            self.restore()
        self.pack()
        build = self.consumer / layers.PROJECT_BUILD
        build.mkdir()
        (build / "reg").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.restore()

    def test_checkout_detects_modified_or_added_sources(self):
        git = lambda *cmd: subprocess.run(["git", *cmd], cwd=self.consumer, check=True,
                                          stdout=subprocess.DEVNULL)
        git("init", "-q")
        source = self.consumer / "Main.lean"
        source.write_text("import Lean\n")
        git("add", "Main.lean")
        git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "source")
        self.assertEqual(len(layers.checkout(self.consumer)), 40)
        source.write_text("import Lake\n")
        with self.assertRaisesRegex(ValueError, "compile inputs differ"):
            layers.checkout(self.consumer)
        git("restore", "Main.lean")
        (self.consumer / "Extra.lean").touch()
        with self.assertRaisesRegex(ValueError, "compile inputs differ"):
            layers.checkout(self.consumer)


if __name__ == "__main__":
    unittest.main()
