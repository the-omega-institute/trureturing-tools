"""Cross-Release payload reuse and clean Lake consumers on a three-module fixture."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lean_release_volumes as volumes


def run(root, *args):
    result = subprocess.run(args, cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(f"{args} exited {result.returncode}: {result.stdout}{result.stderr}")
    return result.stdout


class VolumeReleases(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        self.project = self.area / "source"
        self.project.mkdir()
        self.pool = self.area / "store"
        run(self.project, "git", "init", "-q")
        self.write("lean-toolchain", (Path(__file__).resolve().parents[3] / "lean-toolchain").read_text())
        self.write(".gitignore", ".lake/\n")
        self.write("lakefile.toml", 'name="fixture"\n[[lean_lib]]\nname="D5"\nglobs=["D5.+"]\n')
        for name in ("Reg", "tools/lean-inspector-reg", "tools/lean-inspector-interface"):
            self.write(name + "/lakefile.toml", f'name="{Path(name).name}"\nbuildDir="{os.path.relpath(".lake/build/" + Path(name).name, name)}"\n')
        for name in "ABC":
            self.write(f"D5/{name}.lean", f"def val{name} : Nat := 1\n")
        self.build()
        self.commit("A B C")

    def write(self, path, data):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def build(self):
        run(self.project, "lake", "build", *[f"@fixture/+D5.{name}:olean" for name in "ABC"])

    def commit(self, message):
        run(self.project, "git", "add", ".")
        run(self.project, "git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.org",
            "commit", "-qm", message)

    def change_b(self):
        self.write("D5/B.lean", "def valB : Nat := 12345\n")
        self.build()
        self.commit("B prime")
        for path in (self.project / ".lake/build").rglob("*"):
            if path.is_file():
                os.utime(path, (123456789, 123456789))

    def consumer(self, name):
        target = self.area / name
        run(self.area, "git", "clone", "-q", "--local", str(self.project), str(target))
        return target

    def test_changed_member_only_and_full_and_scoped_after_retirement(self):
        first = volumes.publish(self.pool, "first", self.project, chunk_bytes=5000)
        self.change_b()
        second = volumes.publish(self.pool, "second", self.project, "first", chunk_bytes=5000)
        old = volumes.read_snapshot(self.pool, "first")[1]
        current = volumes.read_snapshot(self.pool, "second")[1]
        def frames(tree):
            return {r["module"]: (r, loc) for r, loc in zip(tree["members"], tree["locations"], strict=True)}
        orig, new = frames(old), frames(current)
        self.assertEqual(set(new), {"D5.A", "D5.B", "D5.C"})
        for name in ("D5.A", "D5.C"):
            self.assertEqual(orig[name][0]["sha256"], new[name][0]["sha256"])
            self.assertEqual(orig[name][1], new[name][1])
        self.assertNotEqual(orig["D5.B"][0]["sha256"], new["D5.B"][0]["sha256"])
        self.assertEqual(second["payload_bytes"], new["D5.B"][0]["bytes"])
        self.assertEqual(second["reused_bytes"], sum(new[name][0]["bytes"] for name in ("D5.A", "D5.C")))
        self.assertFalse(second["compacted"])
        removed = volumes.retire(self.pool, ["first"])
        self.assertEqual(set(removed), old["volumes"].keys() - current["volumes"].keys())
        self.assertFalse((self.pool / "snapshots/first").exists())
        full = self.consumer("full")
        scoped = self.consumer("scoped")
        full_receipt = volumes.restore(self.pool, "second", full, full / ".lake/build")
        scoped_receipt = volumes.restore(self.pool, "second", scoped, scoped / ".lake/build", ["D5.B"])
        self.assertEqual(scoped_receipt["payload_bytes"], new["D5.B"][0]["bytes"])
        self.assertEqual(full_receipt["payload_bytes"], sum(r["bytes"] for r in current["members"]))
        self.assertFalse((scoped / ".lake/build/lib/lean/D5/A.olean").exists())
        for project, names in ((scoped, "B"), (full, "ABC")):
            run(project, "lake", "--no-build", "build", *[f"@fixture/+D5.{name}:olean" for name in names])
        if os.getenv("LEAN_VOLUME_MEASURE"):
            print(json.dumps({"first": first, "second": second, "full": full_receipt, "scoped": scoped_receipt,
                              "modules": {name: row["bytes"] for name, (row, _) in new.items()}}, sort_keys=True))

    def test_idempotence_compaction_and_failed_gc(self):
        volumes.publish(self.pool, "first", self.project)
        retry = volumes.publish(self.pool, "first", self.project)
        self.assertEqual(retry["payload_bytes"], 0)
        repeat = volumes.publish(self.pool, "repeat", self.project, "first")
        self.assertEqual(repeat["new_volumes"], [])
        self.change_b()
        with self.assertRaisesRegex(ValueError, "already bound"):
            volumes.publish(self.pool, "first", self.project)
        with patch.object(volumes, "MAX_VOLUMES", 1):
            repacked = volumes.publish(self.pool, "second", self.project, "first")
            self.assertTrue(repacked["compacted"])
        old_tag = self.pool / "snapshots/first"
        current = self.pool / "snapshots/second" / volumes.INDEX
        current.write_bytes(current.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "checksum"):
            volumes.retire(self.pool, ["first", "repeat"])
        self.assertTrue(old_tag.exists())

    def test_damaged_physical_member_leaves_no_partial_install(self):
        volumes.publish(self.pool, "one", self.project)
        _, tree = volumes.read_snapshot(self.pool, "one")
        extent = tree["locations"][0][0]
        path = self.pool / "volumes" / extent[0]
        with path.open("r+b") as out:
            out.seek(extent[1] + extent[2] // 2)
            value = out.read(1)[0]
            out.seek(-1, 1)
            out.write(bytes([value ^ 17]))
        dest = self.area / "corrupt"
        with self.assertRaises((OSError, ValueError, EOFError)):
            volumes.restore(self.pool, "one", self.project, dest)
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
