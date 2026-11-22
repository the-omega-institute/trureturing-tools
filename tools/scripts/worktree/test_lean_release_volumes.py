"""Cross-Release payload reuse and clean Lake consumers on a three-module fixture."""

import json
import os
import random
import resource
import signal
from pathlib import Path
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import hashlib
import sys
import subprocess
import tempfile
import tarfile
import time
import zipfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import lean_release_volumes as volumes


def run(root, *args):
    result = subprocess.run(args, cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(f"{args} exited {result.returncode}: {result.stdout}{result.stderr}")
    return result.stdout


@contextmanager
def restore_file_limit(size):
    # Each installed file fits. A staged compressed group or large frame exceeds the limit.
    previous = resource.getrlimit(resource.RLIMIT_FSIZE)
    handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, previous[1]))
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, previous)
        signal.signal(signal.SIGXFSZ, handler)


class VolumeDownloads(unittest.TestCase):
    def test_sparse_cache_make_failure_requires_a_bound_compatibility_signal(self):
        from contextlib import nullcontext
        import lean_release_sparse as sparse

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            run(root, "git", "init", "-q")
            (root / ".gitignore").write_text(".lake/\n")
            # Make maps both command statuses to 2, so the outer publisher must bind the reason.
            (root / "Makefile").write_text(
                "check:\n\t@if test -n \"$$LEAN_CACHE_SPARSE_PLAN\"; then "
                "test -z \"$$SIGNAL\" || echo \"$$SIGNAL\" > \"$$LEAN_CACHE_SPARSE_FALLBACK_FILE\"; "
                "exit 75; fi\n")
            run(root, "git", "add", ".")
            run(root, "git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "base")
            head = run(root, "git", "rev-parse", "HEAD").strip()
            plan = dict(schema=sparse.SCHEMA, target_commit=head, base_commit=head, base_tag="previous",
                        base_manifest_sha256="a"*64, rebuild_modules=[])
            marker = sparse.plan_path(root)
            marker.parent.mkdir()
            marker.write_text(json.dumps(plan))
            build = root / ".lake/build"
            build.mkdir()
            host = Mock()
            host.cache_guard.side_effect = lambda unused: nullcontext()
            with patch.object(sparse, "workspace_modules", return_value={}), \
                 patch.object(sparse.compiler, "compile_roots"), \
                 patch.object(sparse, "bound_snapshot", return_value=("remote", "manifest", [])), \
                 patch.dict(os.environ, GITHUB_ACTIONS="true", GITHUB_WORKSPACE=str(root), GITHUB_ENV="",
                            LEAN_CACHE_SPARSE_PLAN=str(marker), SIGNAL=""):
                def check(current):
                    return sparse.prepare_cache(host, root, ["make", "check"], "fixture", True, current).returncode
                self.assertEqual(check(plan), 2)
                self.assertTrue(build.exists())
                reason = dict(target_commit="0"*40, base_manifest_sha256=plan["base_manifest_sha256"], reason="regenerate")
                os.environ["SIGNAL"] = json.dumps(reason)
                with self.assertRaisesRegex(ValueError, "unbound"):
                    check(plan)
                self.assertTrue(build.exists())
                os.environ["SIGNAL"] = json.dumps(dict(reason, target_commit=head))
                host.restore_snapshot.side_effect = [OSError("interrupted full restore"), True]
                with self.assertRaisesRegex(OSError, "interrupted"):
                    check(plan)
                pending = sparse.read_plan(root)
                self.assertTrue(pending["restoring_full"])
                self.assertEqual(check(pending), 0)
                self.assertFalse(marker.exists())
                self.assertEqual(host.restore_snapshot.call_count, 2)

    def test_resume_only_the_unique_empty_starter_in_a_serialized_physical_draft(self):
        import lean_release_volume_remote as remote

        host = Mock()
        host.REPO = "fixture/private"
        body = b"complete physical volume"
        digest = hashlib.sha256(body).hexdigest()
        physical = remote.volume_prefix("isolated-scope") + digest
        starter = {"state": "starter", "id": 23, "name": digest, "size": 0, "digest": None}
        complete = {**starter, "state": "uploaded", "size": len(body), "digest": "sha256:" + digest}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "payload"
            path.write_bytes(body)
            for draft, assets, serialized, source in (
                    (False, [starter], True, path), (True, [starter], False, path),
                    (True, [starter, starter], True, path), (True, [starter], True, None),
                    (True, [dict(starter, name="wrong")], True, path),
                    (True, [dict(starter, digest="wrong")], True, path),
                    (True, [dict(starter, state="uploaded")], True, path)):
                host.existing_release.return_value = {"draft": draft, "assets": assets}
                with self.assertRaises(ValueError):
                    remote.ensure_asset(host, physical, digest, len(body), source, None, serialized=serialized)
                host.gh.assert_not_called()
            path.write_bytes(b"Z" * len(body))
            host.existing_release.return_value = {"draft": True, "assets": [starter]}
            with self.assertRaises(ValueError):
                remote.ensure_asset(host, physical, digest, len(body), path, None, serialized=True)
            host.gh.assert_not_called()
            path.write_bytes(body)
            host.existing_release.side_effect = [{"draft": True, "assets": [starter]},
                                                 {"draft": False, "assets": [complete]}]
            self.assertTrue(remote.ensure_asset(host, physical, digest, len(body), path, None, serialized=True))
            calls = [c.args for c in host.gh.call_args_list]
            self.assertEqual(calls[0], (None, "api", "--method", "DELETE", "repos/fixture/private/releases/assets/23"))
            self.assertEqual([c[2] for c in calls[1:]], ["upload", "edit"])

    def test_full_alternating_frames_use_two_unbounded_volume_transfers(self):
        import lean_cache_release as release
        import lean_release_volume_remote as remote

        frames = [i.to_bytes(4, "big") for i in range(2048)]
        payload = b"".join(frames)
        blobs = [b"".join(frames[::2]), b"".join(frames[1::2])]
        keys = [hashlib.sha256(blob).hexdigest() for blob in blobs]
        manifest = {"volume_scope": "fixture", "archive_bytes": len(payload),
                    "archive_sha256": hashlib.sha256(payload).hexdigest()}
        tree = {"volumes": {key: len(blob) for key, blob in zip(keys, blobs, strict=True)},
                "members": [{"offset": i * 4} for i in range(len(frames))],
                "locations": [[[keys[i % 2], (i // 2) * 4, 4]] for i in range(len(frames))]}
        downloads = []
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            def gh(deadline, *args):
                self.assertIsNone(deadline)
                key = args[-1]
                self.assertEqual(list(stage.glob("[0-9a-f]" * 64)), [])
                (stage / key).write_bytes(blobs[keys.index(key)])
                downloads.append(key)
            with patch.object(release, "gh", side_effect=gh), patch.object(remote, "asset_for", return_value={}), \
                    patch.object(release, "receipt") as receipt:
                remote.download_full(release, stage, manifest, tree, release.operation_deadline())
            self.assertEqual((stage / "lean-members").read_bytes(), payload)
            self.assertCountEqual(downloads, keys)
            self.assertEqual(receipt.call_args.kwargs, {"payload_requests": 2, "payload_bytes": 8192,
                "logical_bytes": 8192, "staged_payload_bound": 12288})


    def test_low_disk_reader_accepts_backwards_and_repeated_spans_and_bounds_the_spool(self):
        import lean_cache_release as release
        import lean_release_volume_remote as remote

        blobs = [bytes(range(48)), bytes(range(80, 128))]
        keys = [hashlib.sha256(blob).hexdigest() for blob in blobs]
        segments = [(0, 34, 6), (1, 8, 12), (0, 0, 10), (1, 12, 10), (0, 34, 6)]
        sizes = {key: len(blob) for key, blob in zip(keys, blobs, strict=True)}
        rows, locations, expected = [], [], b""
        for volume, start, count in segments:
            rows.append({"offset":len(expected)})
            locations.append([[keys[volume], start, count]])
            expected += blobs[volume][start:start+count]
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            requested = []
            observed = []
            def gh(deadline, *args):
                self.assertIsNone(deadline)
                self.assertEqual(list(stage.iterdir()), [])
                key = args[-1]
                (stage / key).write_bytes(blobs[keys.index(key)])
                observed.append(sum(p.stat().st_size for p in stage.iterdir()))
                requested.append(key)
            def read_range(asset, start, count, output, deadline):
                output.write(blobs[asset["id"]][start:start+count])
            with patch.object(remote, "asset_for", side_effect=lambda host, tag, key, size, deadline: {"id": keys.index(key)}), \
                    patch.object(release, "gh", side_effect=gh), patch.object(release, "download_range", side_effect=read_range), \
                    patch.object(remote.LogicalVolumeInput, "WINDOW", 8):
                remote_tree = {"members": rows, "locations": locations, "volumes": sizes}
                reader = remote.LogicalVolumeInput(release, stage, {"volume_scope":"test"}, remote_tree, release.operation_deadline())
                self.assertEqual(b"".join(iter(lambda: reader.read(5), b"")), expected)
                self.assertGreater(reader.range_requests, 0)
                self.assertLessEqual(reader.transferred, sum(sizes.values()) + len(expected))
                self.assertCountEqual(requested, keys)
                self.assertEqual(max(observed), 48)


class NativeReleaseProducts(unittest.TestCase):
    def test_cross_platform_and_mixed_seed_invalidate_only_native_products(self):
        from lean_cache_release_io import check_native_products

        elf = bytes.fromhex("7f454c46020101").ljust(18, b"\0") + (183).to_bytes(2, "little")
        macho = bytes.fromhex("cffaedfe0c000001").ljust(32, b"\0")
        for source in ("darwin-arm64", "linux-arm64"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = {"bin/tool": macho, "bin/tool.trace": b"trace", "bin/tool.rsp": b"objects",
                         "ir/D5/A.c.o.export": elf, "ir/D5/A.c.o.export.hash": b"hash",
                         "lib/libD5.a": b"!<arch>\n", "bin/portable": b"#!/bin/sh\n",
                         "ir/D5/A.c": b"int a;", "lib/lean/D5/A.olean": b"portable"}
                for name, data in files.items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                with patch("lean_cache.binary_platform", return_value=("linux", "arm64")):
                    with self.assertRaisesRegex(ValueError, "foreign native Lake product"):
                        check_native_products(root, "revision/linux-arm64")
                    self.assertTrue((root / "bin/tool").is_file())
                    self.assertEqual(check_native_products(root, "revision/" + source, staging=True), 3)
                    self.assertEqual({str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()},
                                     {"bin/portable", "ir/D5/A.c", "lib/lean/D5/A.olean"})
                    (root / "bin/tool").write_bytes(elf)
                    self.assertEqual(check_native_products(root, "revision/linux-arm64", staging=True), 0)
                    self.assertEqual((root / "bin/tool").read_bytes(), elf)


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

    def test_sparse_seed_installs_only_the_build_prerequisites_and_residuals(self):
        import lean_release_sparse as sparse
        import lean_release_volume_remote as remote

        self.write("D5/B.lean", "import D5.A\ndef valB : Nat := valA + 1\n")
        self.build()
        self.commit("record the project dependency")
        base = run(self.project, "git", "rev-parse", "HEAD").strip()
        report = self.project / ".lake/build/stratalint/raw-lean-report.json"
        report.parent.mkdir(parents=True)
        report.write_text("{}")
        for i in range(3):
            (report.parent / f"payload-{i}").write_bytes(random.Random(i).randbytes(96 * 1024))
        volumes.publish(self.pool, "base", self.project, chunk_bytes=128 * 1024)
        manifest, tree = volumes.read_snapshot(self.pool, "base")
        self.assertGreater(max(row["bytes"] for row in tree["members"] if row["module"] is None), 144 * 1024)
        host = Mock()
        host.prefix.return_value = "test-volume-"
        scope = remote.namespace("fixture", {}, False, "ci-1", host)
        manifest.update(schema=remote.SCHEMA, partition="fixture", cache_key={}, volume_scope=scope)
        self.write("D5/B.lean", "import D5.A\ndef valB : Nat := valA + 2\n")
        self.commit("change B")
        target = run(self.project, "git", "rev-parse", "HEAD").strip()
        clone = self.consumer("sparse")

        def range_blocks(digest, start, size, deadline):
            with (self.pool / "volumes" / digest).open("rb") as stream:
                stream.seek(start)
                yield stream.read(size)
        host.range_blocks.side_effect = range_blocks

        with patch.object(sparse.compiler, "production_targets", return_value={"D5.B", "D5.C"}), \
             patch.object(remote, "load_tree", return_value=tree), \
             patch.object(remote, "asset_for", side_effect=lambda host, tag, digest, size, deadline: digest), \
             patch.dict(os.environ, {"GITHUB_ENV": "", "LEAN_CACHE_VOLUME_TEST_ID": ""}):
            graph = sparse.project_imports(clone, sparse.workspace_modules(clone))
            with patch.object(sparse, "project_imports", return_value=graph), restore_file_limit(144 * 1024):
                self.assertTrue(sparse.install(host, clone, self.area, "base", manifest, "2"*64, target,
                                              "refs/heads/dev", False, None))
            plan = sparse.read_plan(clone)
            self.assertEqual(plan["base_commit"], base)
            self.assertEqual(plan["rebuild_modules"], ["D5.B"])
            self.assertEqual(plan["restore_modules"], ["D5.A"])
            self.assertEqual(plan["inherit_modules"], ["D5.A", "D5.C"])
            self.assertEqual(plan["base_report_sha256"], "1" * 64)
            restored = clone / ".lake/build"
            self.assertTrue((restored / "lib/lean/D5/A.olean").is_file())
            self.assertFalse((restored / "lib/lean/D5/B.olean").exists())
            self.assertFalse((restored / "lib/lean/D5/C.olean").exists())
            self.assertTrue((restored / "stratalint/raw-lean-report.json").is_file())
            # Rerunning the same protected install also recovers a partially rebuilt workspace.
            with patch.dict(os.environ, GITHUB_ACTIONS="true", GITHUB_WORKSPACE=str(clone)):
                leftover = restored / "interrupted"
                leftover.touch()
                with self.assertRaisesRegex(ValueError, "different publisher or snapshot"):
                    sparse.install(host, clone, self.area, "other", manifest, "2"*64, target,
                                   "refs/heads/dev", False, None)
                self.assertTrue(leftover.exists())
                def interrupted(*args):
                    yield from range_blocks(*args)
                    raise KeyboardInterrupt
                with patch.object(host, "range_blocks", interrupted), self.assertRaises(KeyboardInterrupt):
                    sparse.install(host, clone, self.area, "base", manifest, "2"*64, target,
                                   "refs/heads/dev", False, None)
                self.assertTrue(leftover.exists())
                self.assertEqual(sparse.read_plan(clone), plan)
                self.assertTrue(sparse.install(host, clone, self.area, "base", manifest, "2"*64, target,
                                               "refs/heads/dev", False, None))
                self.assertFalse(leftover.exists())
            self.write("Reg/D5/Registration.lean", "import D5.A\n")
            self.commit("add a registration in the project Reg package")
            with patch.object(sparse.compiler, "production_targets", return_value={"Reg.D5.Registration", "D5.B", "D5.C"}):
                new_plan = sparse.sparse_plan(self.project, "base", manifest, "2"*64, tree,
                                              run(self.project, "git", "rev-parse", "HEAD").strip())
                self.assertEqual(new_plan["rebuild_modules"], ["D5.B", "Reg.D5.Registration"])
            self.write("external-build-input.txt", "global input")
            self.commit("change an unevaluated build input")
            self.assertIsNone(sparse.sparse_plan(self.project, "base", manifest, "2"*64, tree,
                              run(self.project, "git", "rev-parse", "HEAD").strip()))

    def test_native_residuals_and_full_fallback_index(self):
        import lean_release_sparse as sparse
        import lean_release_volume_remote as remote

        tree = {"members": [{"module": "D5.B", "paths": ["ir/D5/B.c"]}]}
        changed = {"rebuild_modules": ["D5.B"], "removed_modules": []}
        added = dict(changed, rebuild_modules=["D5.New"])
        root = self.project / ".lake/build"
        def fallback(paths, plan=changed):
            return sparse.native_fallback(root, [Path(p) for p in paths], tree, plan)

        self.assertEqual(fallback(["ir/D5/B.c.o.export.trace"]), "ir/D5/B.c.o.export.trace")
        self.assertIsNone(fallback(["ir/D5/A.c.o.export"]))
        self.assertIsNone(fallback(["ir/D5/A.c.o.export", "bin/tool"], added))
        self.assertEqual(fallback(["lib/lean/libD5.so.1"], added), "lib/lean/libD5.so.1")
        self.assertEqual(fallback(["bin/tool"]), "bin/tool")
        self.write(".lake/build/bin/tool.rsp", '"/old/.lake/build/ir/D5/B.c.o.export"\n')
        self.assertEqual(fallback(["bin/tool"]), "bin/tool")
        self.write(".lake/build/bin/tool.rsp", '"/old/.lake/build/ir/D5/A.c.o.export"\n')
        self.assertIsNone(fallback(["bin/tool"]))
        self.assertIsNone(fallback(["lib/libD5.a"], dict(changed, rebuild_modules=[])))

        # gh refuses to overwrite a downloaded index on an ordinary full fallback.
        host = Mock()
        host.prefix.return_value = "test-volume-"
        manifest = {"partition": "fixture", "cache_key": {}, "volume_scope":
                    remote.namespace("fixture", {}, False, "ci-1", host)}
        def load_tree(host, tag, stage, manifest, deadline):
            with (stage / volumes.INDEX).open("xb") as out:
                out.write(b"index")
            return tree
        clone = self.consumer("fallback")
        stage = self.area / "downloads"
        stage.mkdir()
        with patch.object(remote, "load_tree", side_effect=load_tree), patch.object(sparse, "sparse_plan", return_value=None):
            self.assertFalse(sparse.install(host, clone, stage, "base", manifest, "a"*64, "b"*40, "", False, None))
            load_tree(host, "base", stage, manifest, None)

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
        with patch.object(volumes, "MAX_VOLUMES", 1), \
                patch.object(volumes.index, "write_frame", wraps=volumes.index.write_frame) as encode:
            repacked = volumes.publish(self.pool, "second", self.project, "first")
            self.assertTrue(repacked["compacted"])
            self.assertEqual([call.args[2] for call in encode.call_args_list], ["D5.A", "D5.B", "D5.C"] * 2)
        old_tag = self.pool / "snapshots/first"
        current = self.pool / "snapshots/second" / volumes.INDEX
        current.write_bytes(current.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "checksum"):
            volumes.retire(self.pool, ["first", "repeat"])
        self.assertTrue(old_tag.exists())

    def test_sparse_overlay_matches_complete_build_and_compacts_without_local_inherited_frames(self):
        import lean_release_sparse_pack as sparse
        import lean_release_volume_remote as remote

        stage = self.area / "sparse-stage"
        stage.mkdir()
        self.write(".lake/build/report/fixture.json", "first report")
        volumes.publish(self.pool, "base", self.project, chunk_bytes=5000)
        previous = volumes.read_snapshot(self.pool, "base")
        self.write("D5/C.lean", "def valC : Nat := 34567\n")
        self.build()
        self.commit("C prime")
        self.write(".lake/build/report/fixture.json", "new results")
        volumes.publish(self.pool, "complete", self.project, "base", chunk_bytes=6500)
        expected, complete_tree = volumes.read_snapshot(self.pool, "complete")
        plan = {"inherit_modules": ["D5.A", "D5.B"], "rebuild_modules": ["D5.C"],
                "restore_modules": ["D5.A"], "removed_modules": []}
        # B never needed restoring, and locally present A is still inherited from the predecessor.
        for path in next(r for r in previous[1]["members"] if r["module"] == "D5.B")["paths"]:
            (self.project / ".lake/build" / path).unlink()
        (self.project / ".lake/build/lib/lean/D5/A.olean").write_bytes(b"stale local file")
        reads = []
        def read_prior(rows, out):
            reads.append([row["module"] for row in rows])
            for key, start, size in remote.ranges(previous[1], rows):
                volumes.copy_range(self.pool / "volumes" / key, start, size, out)

        def save(part, path):
            self.assertEqual(list(stage.iterdir()), [path])
            self.assertLessEqual(path.stat().st_size, 6500)
            volumes.store_asset(self.pool, part, path)

        for tag, limit in (("sparse", 100), ("compact", 2)):
            reads.clear()
            with patch.object(volumes, "MAX_VOLUMES", limit):
                contents, tree, _, compact, peak = sparse.pack_overlay(
                    self.project, stage, previous, plan, 6500, save, read_prior)
                self.assertEqual(contents, {k: expected[k] for k in contents})
                self.assertEqual(compact, tag == "compact")
                self.assertLessEqual(peak, 6500)
                self.assertEqual(reads, [["D5.A", "D5.B"]] * (2 if compact else 1))
                if not compact:
                    self.assertEqual(tree, complete_tree)
                target = self.pool / "snapshots" / tag
                target.mkdir()
                volumes.write_tree(target / volumes.INDEX, tree)
                (target / volumes.MANIFEST).write_bytes(volumes.json_bytes(
                    dict(expected, tag=tag, index_sha256=volumes.archive_sha(target / volumes.INDEX, None))))
                volumes.read_snapshot(self.pool, tag)
            for mode, targets in (("full", []), ("scoped", ["D5.B"])):
                dest = self.consumer(tag + "-" + mode)
                volumes.restore(self.pool, tag, dest, dest / ".lake/build", targets)
                names = ["D5.A", "D5.B", "D5.C"] if mode == "full" else targets
                run(dest, "lake", "--no-build", "build", *[f"@fixture/+{name}:olean" for name in names])
                if mode == "full":
                    self.assertEqual((dest / ".lake/build/report/fixture.json").read_text(), "new results")

    def test_sparse_overlay_adds_and_removes_modules_in_target_order(self):
        import lean_release_sparse_pack as sparse
        import lean_release_volume_remote as remote
        volumes.publish(self.pool, "base", self.project, chunk_bytes=8192)
        previous = volumes.read_snapshot(self.pool, "base")
        old = {r["module"]: r for r in previous[1]["members"]}
        for path in old["D5.C"]["paths"]:
            (self.project / ".lake/build" / path).unlink()
        (self.project / "D5/C.lean").unlink()
        self.write("D5/New.lean", "import D5.A\ndef valNew : Nat := valA + 1\n")
        self.commit("Replace C with New")
        run(self.project, "lake", "build", "@fixture/+D5.New:olean")
        volumes.publish(self.pool, "full", self.project, "base", chunk_bytes=8192)
        expected, full_tree = volumes.read_snapshot(self.pool, "full")
        for path in old["D5.B"]["paths"]:
            (self.project / ".lake/build" / path).unlink()

        def read_prior(rows, out):
            for key, start, size in remote.ranges(previous[1], rows):
                volumes.copy_range(self.pool / "volumes" / key, start, size, out)

        plan = {"inherit_modules": ["D5.A", "D5.B"], "restore_modules": ["D5.A"],
                "rebuild_modules": ["D5.New"], "removed_modules": ["D5.C"]}
        stage = self.area / "stage"
        stage.mkdir()
        contents, tree, _, _, _ = sparse.pack_overlay(
            self.project, stage, previous, plan, 8192,
            lambda part, path: volumes.store_asset(self.pool, part, path), read_prior)
        self.assertEqual(contents["archive_sha256"], expected["archive_sha256"])
        self.assertEqual(tree, full_tree)

    def test_sparse_overlay_rejects_invalid_plan_frames_and_changed_configuration(self):
        import lean_release_sparse_pack as sparse
        import lean_release_volume_remote as remote

        volumes.publish(self.pool, "base", self.project, chunk_bytes=8192)
        previous = volumes.read_snapshot(self.pool, "base")
        stage = self.area / "stage"
        stage.mkdir()
        plan = {"inherit_modules": ["D5.A", "D5.B", "D5.C"], "rebuild_modules": [],
                "restore_modules": [], "removed_modules": []}
        def overlay(candidate, read):
            return sparse.pack_overlay(self.project, stage, previous, candidate, 8192, self.fail, read)

        for field, names in (("inherit_modules", ["D5.A", "D5.B"]), ("inherit_modules", ["D5.A"] * 2),
                             ("restore_modules", ["unknown"]), ("rebuild_modules", ["D5.A"])):
            with self.subTest(field=field, names=names), self.assertRaises(ValueError):
                overlay(dict(plan, **{field: names}), self.fail)
        duplicate = dict(previous[1], members=[dict(row, module="D5.A") for row in previous[1]["members"]])
        with self.assertRaisesRegex(ValueError, "invalid indexed module"):
            sparse.pack_overlay(self.project, stage, (previous[0], duplicate), plan, 8192, self.fail, self.fail)
        for damage in ("truncated", "corrupt", "excess"):
            def damaged(rows, output):
                raw = io.BytesIO()
                for key, start, size in remote.ranges(previous[1], rows):
                    volumes.copy_range(self.pool / "volumes" / key, start, size, raw)
                data = raw.getvalue()
                output.write({"truncated": data[:-1], "corrupt": bytes([data[0] ^ 1]) + data[1:],
                              "excess": data + b"x"}[damage])
            with self.subTest(damage=damage), self.assertRaisesRegex(ValueError, "predecessor frame"):
                overlay(plan, damaged)
        rebuild = dict(plan, inherit_modules=["D5.A", "D5.B"], rebuild_modules=["D5.C"])
        (self.project / ".lake/build/lib/lean/D5/C.olean").unlink()
        with self.assertRaisesRegex(ValueError, "compiler outputs"):
            overlay(rebuild, self.fail)
        self.write("lakefile.toml", 'name="another"\n')
        with self.assertRaisesRegex(ValueError, "configuration"):
            overlay(plan, self.fail)

    def github_transport(self, stack, chunk_bytes, *, real_report=False):
        import lean_cache_release as release

        fixture = Path(__file__).resolve().parents[2] / "tests/StrataLint.ScriptTests/Fixtures"
        sys.path.insert(0, str(fixture))
        self.addCleanup(sys.path.remove, str(fixture))
        from lean_seed_transport_fake import FAKE_GH

        binary = self.area / "bin"
        binary.mkdir()
        gh = binary / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.pool.mkdir()
        key = {"report_format": "fixture", "execution_sha256": "e" * 64}
        partition = "fixture/linux-arm64"
        remote_env = {"PATH": str(binary) + os.pathsep + os.environ["PATH"],
                      "FAKE_REMOTE": str(self.pool), "GITHUB_ACTIONS": "true",
                      "LEAN_CACHE_RELEASE_VOLUMES": "1"}
        stack.enter_context(patch.dict(os.environ, remote_env))
        stack.enter_context(patch.object(release, "REPO", "fixture/private"))
        stack.enter_context(patch.object(release, "CHUNK_BYTES", chunk_bytes))
        if not real_report:
            # These transport fixtures supply compiled bytes directly.
            self.write(".lake/build/lean-inspector/modules/D5.A.zip", "module report")
            selection = stack.enter_context(patch.object(release.publication.selection, "Selection"))
            selection.return_value.modules.return_value = {"D5.A": "D5/A.lean"}
        stack.enter_context(patch.object(release, "partition_path", return_value=partition))
        stack.enter_context(patch.object(release, "release_key", return_value=key))
        identity = stack.enter_context(patch.object(release, "publication_identity"))
        return release, key, partition, identity

    def test_scoped_coalesced_ranges_can_exceed_the_restore_file_limit(self):
        import lean_release_index as index

        graph = index.project_imports(self.project, index.workspace_modules(self.project))
        chunk = 128 * 1024
        names = ["D5.A", "D5.B", "D5.C"]
        for i, name in enumerate(names):
            path = self.project / f".lake/build/ir/D5/{name[-1]}.bc"
            path.write_bytes(random.Random(i).randbytes(96 * 1024))
        author = run(self.project, "git", "rev-parse", "HEAD").strip()
        with ExitStack() as stack:
            release, key, partition, identity = self.github_transport(stack, chunk)
            identity.return_value = dict(producer_commit_sha=author, publication_id="ci-701-1",
                                         workflow_run_id="701", workflow_run_attempt="1")
            for mode in ("0", "1"):
                with self.subTest(mode=mode), patch.dict(os.environ, LEAN_CACHE_RELEASE_VOLUMES=mode):
                    tag = release.prefix(partition, key, indexed="volumes" if mode == "1" else True) + "ci-701-1"
                    self.assertEqual(release.publish(self.project, partition, build_cache=False, indexed=True), 0)
                    target = self.consumer("scoped-" + mode)
                    log = self.area / ("requests-" + mode)
                    with tempfile.TemporaryDirectory() as stage, restore_file_limit(144 * 1024), \
                            patch.object(index, "project_imports", return_value=graph), \
                            patch.dict(os.environ, FAKE_GH_LOG=str(log)):
                        count, transferred = release.restore_scoped(target, partition, tag, Path(stage),
                            release.operation_deadline(), cache_key=key, producer=author, targets=names, verification=False)
                    self.assertEqual(count, len(names))
                    self.assertGreater(transferred, 2 * chunk)
                    requests = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(sum(call[:2] == ["api", "--include"] for call in requests), 3)
                    for name in names:
                        path = Path(f".lake/build/ir/D5/{name[-1]}.bc")
                        self.assertEqual((target / path).read_bytes(), (self.project / path).read_bytes())

            # Failure reported after the final body byte must precede installation.
            original_blocks = release.range_blocks
            for error in (ValueError("late transport failure"), KeyboardInterrupt()):
                dest = self.consumer("interrupted-" + type(error).__name__)
                previous = dest / ".lake/build/local"
                previous.parent.mkdir(parents=True)
                previous.write_bytes(b"existing")
                def interrupted(asset, offset, length, deadline):
                    yield from original_blocks(asset, offset, length, deadline)
                    if length < chunk:
                        raise error
                with tempfile.TemporaryDirectory() as stage, patch.object(release, "range_blocks", interrupted), \
                        self.assertRaises(type(error)):
                    release.restore_scoped(dest, partition, tag, Path(stage), release.operation_deadline(),
                                           cache_key=key, producer=author, targets=names, verification=False)
                self.assertEqual(list(previous.parent.iterdir()), [previous])
                self.assertEqual(previous.read_bytes(), b"existing")
                self.assertEqual(list((dest / ".lake").glob(".release-*")), [])

    def test_two_generations_with_sparse_module_reports_and_empty_consumers(self):
        import lean_release_index as index
        import lean_release_sparse as sparse
        import lean_cache_release as release
        import build_inputs
        import native
        import scoped

        with ExitStack() as stack:
            _, key, partition, identity = self.github_transport(stack, 16384, real_report=True)
            from lean_seed_support import prepare_release_report, write_release_report
            self.write("Trureturing.lean", "def rootValue : Nat := 0\n")
            prepare_release_report(self.project)
            self.commit("report selection for the fixture")
            with patch.object(sys, "path", list(sys.path)):
                write_release_report(self.project)
            base = run(self.project, "git", "rev-parse", "HEAD").strip()
            def publish(project, commit, number):
                suffix = f"ci-{number}-1"
                identity.return_value = {"producer_commit_sha": commit, "publication_id": suffix,
                    "workflow_run_id": str(number), "workflow_run_attempt": "1"}
                with redirect_stdout(io.StringIO()) as output:
                    code = release.publish(project, partition, build_cache=False, indexed=True)
                self.assertEqual(code, 0, output.getvalue())
                self.assertIn('"status":"published"', output.getvalue())
                return release.prefix(partition, key, indexed="volumes") + suffix

            first = publish(self.project, base, 801)
            self.write("D5/B.lean", "import D5.A\ndef valB : Nat := valA + 5\n")
            self.write("Library/Arithmetic/Reference.md", "A reference for B\n")
            self.write("Problems/Arithmetic.md", "A related problem\n")
            self.commit("B consumes A")
            target = run(self.project, "git", "rev-parse", "HEAD").strip()
            rolling = self.consumer("rolling-checked")
            stack.enter_context(patch.dict(os.environ, GITHUB_WORKSPACE=str(rolling),
                                    LEAN_CACHE_RELEASE_SPARSE="1", GITHUB_ENV=""))
            stack.enter_context(patch.object(release, "rolling_source", return_value=("workflow", {"push"})))
            stack.enter_context(patch.object(release, "rolling_producer", return_value=True))
            stack.enter_context(patch.object(release, "seed_partitions", return_value=[partition]))
            stack.enter_context(patch.object(sparse.compiler, "production_targets", return_value={"D5.A", "D5.B", "D5.C"}))
            args = SimpleNamespace(command="select-base", mode="production", repository=rolling, source_commit=target,
                source_ref="refs/heads/dev", previous_commit_only=True, writer_owned=False, approved_tag=None,
                approved_producer=None, local_assets=None, refresh_stale=False, no_build=False, indexed=False,
                target=[], tag="", producer="", manifest_sha256="")
            with redirect_stdout(io.StringIO()) as selected:
                self.assertEqual(release.rolling_base(args), 0)
            chosen = json.loads(selected.getvalue().split(" ", 1)[1])
            self.assertEqual((chosen["tag"], chosen["producer"]), (first, base))
            args.command, args.previous_commit_only = "install-base", False
            args.tag, args.producer, args.manifest_sha256 = first, base, chosen["manifest_sha256"]
            self.assertEqual(release.rolling_base(args), 0)
            plan = sparse.read_plan(rolling)
            self.assertEqual(plan["rebuild_modules"], ["D5.B"])
            self.assertEqual(plan["restore_modules"], ["D5.A"])
            self.assertFalse((rolling / ".lake/build/lib/lean/D5/C.olean").exists())
            run(rolling, "lake", "build", "@fixture/+D5.B:olean")

            # The predecessor retains module ZIPs even for compiler frames it omits.
            names = release.publication.selection.Selection(rolling).modules()
            state = native.state(rolling)
            encoded = {name: json.dumps({"claims": [], "utilities": [], "source_path": path})
                       for name, path in names.items()}
            (state / "module-inputs.json").write_text(json.dumps({"records": encoded}))
            (state / "inputs").mkdir(exist_ok=True)
            for name, record in encoded.items():
                (state / "inputs" / (name + ".json")).write_text(record)
            with tempfile.TemporaryDirectory(dir=rolling / ".lake") as temporary, ExitStack() as check:
                check.enter_context(patch.object(build_inputs, "ROOT", rolling))
                check.enter_context(patch.object(build_inputs, "prepare_producer"))
                check.enter_context(patch.object(build_inputs, "sparse_programs", return_value={}))
                check.enter_context(patch.object(build_inputs, "prepare"))
                check.enter_context(patch.object(scoped.Entry, "provision"))
                check.enter_context(patch.object(scoped.Entry, "phase"))
                targets = build_inputs.prepare_sparse_inputs(Path(temporary), plan, regenerate=False)
            self.assertEqual(targets, ["+D5.B:report"])
            updated = b"current module fixture"
            (state / "modules/D5.B.zip").write_bytes(updated)
            second = publish(rolling, target, 802)
            manifest = json.loads((self.pool / second / volumes.MANIFEST).read_bytes())
            previous = json.loads((self.pool / first / volumes.MANIFEST).read_bytes())
            tree = volumes.read_tree(self.pool / second / volumes.INDEX, manifest)
            old = volumes.read_tree(self.pool / first / volumes.INDEX, previous)
            def location(snapshot, name):
                return next(spans for row, spans in zip(snapshot["members"], snapshot["locations"], strict=True)
                            if row["module"] == name)
            self.assertEqual(location(tree, "D5.C"), location(old, "D5.C"))
            for mode in ("full", "scoped"):
                dest = self.consumer("checked-" + mode)
                stage = self.area / ("download-" + mode)
                stage.mkdir()
                if mode == "full":
                    self.assertTrue(release.restore_snapshot(dest, partition, second, stage, release.operation_deadline(),
                                                             cache_key=key))
                    self.assertEqual((native.state(dest) / "modules/D5.B.zip").read_bytes(), updated)
                    self.assertFalse((dest / ".lake/build/stratalint/raw-lean-report.json").exists())
                    targets = ["D5.A", "D5.B", "D5.C"]
                else:
                    count, _ = release.restore_scoped(dest, partition, second, stage, release.operation_deadline(),
                        cache_key=key, producer=target, targets=["D5.B"], verification=False)
                    self.assertEqual(count, 2)
                    self.assertFalse((dest / ".lake/build/lib/lean/D5/C.olean").exists())
                    targets = ["D5.B"]
                run(dest, "lake", "--no-build", "build", *[f"@fixture/+{n}:olean" for n in targets])

    def test_streaming_publisher_cross_volume_frame_and_retry(self):
        import lean_release_index as index
        import lean_release_volume_remote as remote

        chunk = 8192
        artifact = self.project / ".lake/build/ir/D5/B.bc"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(random.Random(1).randbytes(6 * chunk))
        reference = self.area / "reference"
        reference.mkdir()
        expected = index.pack(self.project, reference, chunk, None, stable_timestamps=True)
        modules = ["D5.A", "D5.B", "D5.C"]
        author = run(self.project, "git", "rev-parse", "HEAD").strip()
        with ExitStack() as stack:
            release, key, partition, identity = self.github_transport(stack, chunk)
            send = release.gh
            measurements = []
            transfers = []

            def observed_send(deadline, *args):
                if args[:2] == ("release", "upload"):
                    tag, path = args[2], Path(args[3])
                    if tag.startswith("lean-volume-v1-"):
                        transfers.append(tag)
                        stage = path.parent.parent
                        # The digest-named upload is a hard link to the active volume.
                        stats = [p.stat() for p in stage.rglob("*") if p.is_file()]
                        inodes = {(st.st_dev, st.st_ino) for st in stats}
                        self.assertEqual(len(inodes), 1)
                        self.assertLessEqual(stats[0].st_size, chunk)
                        measurements.append((stats[0].st_size, stats[0].st_blocks * 512))
                    else:
                        self.assertEqual({p.name for p in path.parent.rglob("*") if p.is_file()},
                                         {volumes.INDEX, volumes.MANIFEST})
                return send(deadline, *args)

            stack.enter_context(patch.object(release, "gh", side_effect=observed_send))
            encode = stack.enter_context(patch.object(index, "write_frame", wraps=index.write_frame))

            def publish(num, status="published"):
                suffix = f"ci-{num}-1"
                identity.return_value = {"producer_commit_sha": author, "publication_id": suffix,
                                         "workflow_run_id": str(num), "workflow_run_attempt": "1"}
                encode.reset_mock()
                transfers.clear()
                with redirect_stdout(io.StringIO()) as output:
                    result = release.publish(self.project, partition, build_cache=False, indexed=True)
                events = dict(line.split(" ", 1) for line in output.getvalue().splitlines())
                self.assertEqual(result, 2 if status == "failed" else 0, output.getvalue())
                self.assertEqual(json.loads(events["LEAN_CACHE_PUBLISH"])["status"], status, output.getvalue())
                return release.prefix(partition, key, indexed="volumes") + suffix

            tag = publish(701)
            self.assertEqual([call.args[2] for call in encode.call_args_list], modules)
            manifest = json.loads((self.pool / tag / volumes.MANIFEST).read_bytes())
            self.assertEqual((manifest["archive_bytes"], manifest["archive_sha256"]),
                             (expected["archive_bytes"], expected["archive_sha256"]))
            tree = volumes.read_tree(self.pool / tag / volumes.INDEX, manifest)
            row = tree["members"][1]
            self.assertGreater(len(tree["locations"][1]), 6)
            self.assertEqual(max(n for n, _ in measurements), chunk)
            if os.environ.get("LEAN_VOLUME_MEASURE"):
                print(json.dumps({"logical_bytes": manifest["archive_bytes"], "large_frame_bytes": row["bytes"],
                                  "large_frame_spans": len(tree["locations"][1]), "volume_bytes": chunk,
                                  "peak_staged_bytes": max(n for n, _ in measurements),
                                  "peak_allocated_bytes": max(n for _, n in measurements)}))

            publish(702)
            self.assertEqual([call.args[2] for call in encode.call_args_list], modules)
            self.assertEqual(transfers, [])

            artifact.write_bytes(random.Random(2).randbytes(6 * chunk))
            confirm = remote.ensure_asset
            confirmed = []

            def interrupt(*args, **kwargs):
                saved = confirm(*args, **kwargs)
                if saved:
                    confirmed.append(args[1])
                    raise OSError("interrupted after the first confirmed volume")
                return saved

            with patch.object(remote, "ensure_asset", side_effect=interrupt):
                failed_tag = publish(703, "failed")
            self.assertEqual(transfers, confirmed)
            self.assertEqual(len(confirmed), 1)
            self.assertFalse((self.pool / failed_tag).exists())
            self.assertTrue((self.pool / tag / volumes.MANIFEST).exists())

            retry = publish(703)
            self.assertEqual([call.args[2] for call in encode.call_args_list], modules + ["D5.B"])
            self.assertNotIn(confirmed[0], transfers)
            self.assertTrue(transfers)
            manifest = json.loads((self.pool / retry / volumes.MANIFEST).read_bytes())
            tree = volumes.read_tree(self.pool / retry / volumes.INDEX, manifest)
            self.assertIn(confirmed[0].rsplit("-", 1)[1], tree["volumes"])
            dest = self.consumer("cross-volume")
            stage = self.area / "scoped-transfer"
            stage.mkdir()
            release.restore_scoped(dest, partition, retry, stage, release.operation_deadline(),
                                   cache_key=key, producer=author, targets=["D5.B"], verification=False)
            self.assertEqual((dest / artifact.relative_to(self.project)).read_bytes(), artifact.read_bytes())

    def test_low_disk_full_restore_uses_one_spool_and_rolls_back_late_corruption(self):
        import lean_release_volume_remote as remote

        with ExitStack() as stack:
            release, key, partition, identity = self.github_transport(stack, 65536)
            # A residual larger than multiple volumes exercises extraction across boundaries.
            artifact = self.project / ".lake/build/generated.bin"
            artifact.write_bytes(random.Random(17).randbytes(256 * 1024))
            self.write(".lake/build/compressible.txt", "a" * (4 * 1024 * 1024))
            author = run(self.project, "git", "rev-parse", "HEAD").strip()
            identity.return_value = {"producer_commit_sha":author,"publication_id":"ci-901-1",
                                     "workflow_run_id":"901","workflow_run_attempt":"1"}
            tag = release.prefix(partition, key, indexed="volumes") + "ci-901-1"
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release.publish(self.project, partition, build_cache=False, indexed=True), 0)
            self.assertIn('"status":"published"', output.getvalue())
            manifest = json.loads((self.pool / tag / volumes.MANIFEST).read_bytes())
            tree = volumes.read_tree(self.pool / tag / volumes.INDEX, manifest)
            dest = self.consumer("bounded-full")
            stage = self.area / "bounded-transfer"
            stage.mkdir()
            separate = Mock()
            separate.stat.return_value.st_dev = stage.stat().st_dev + 1
            required = manifest["archive_bytes"] + max(tree["volumes"].values())
            for free, bounded in ((required - 1, True), (required, False)):
                with patch.object(remote.shutil, "disk_usage", return_value=SimpleNamespace(free=free)):
                    self.assertEqual(remote.single_volume_restore(stage, separate, manifest, tree), bounded)
            expanded = sum(p.stat().st_size for p in (self.project / ".lake/build").rglob("*") if p.is_file())
            budget = expanded + max(tree["volumes"].values())
            self.assertGreater(budget, 2 * manifest["archive_bytes"])
            self.assertLess(budget, expanded + manifest["archive_bytes"])
            with patch.object(remote.shutil, "disk_usage", return_value=SimpleNamespace(free=budget)):
                self.assertTrue(remote.single_volume_restore(stage, dest, manifest, tree))
            send = release.gh
            peak = []
            def observe(deadline, *args):
                result = send(deadline, *args)
                if args[:2] == ("release", "download") and args[2].startswith("lean-volume-v1-"):
                    files = [p for p in Path(args[args.index("--dir") + 1]).parent.rglob("*")
                             if p.is_file() and p.name not in (volumes.INDEX, volumes.MANIFEST)]
                    self.assertEqual(len(files), 1)
                    st = files[0].stat()
                    peak.append((st.st_size, st.st_blocks * 512))
                return result
            stack.enter_context(patch.object(release, "gh", side_effect=observe))
            start = time.monotonic()
            self.assertTrue(release.restore_snapshot(dest, partition, tag, stage, release.operation_deadline(), cache_key=key))
            duration = time.monotonic() - start
            self.assertEqual(len(peak), len(tree["volumes"]))
            self.assertEqual(max(x for x, _ in peak), 65536)
            self.assertEqual((dest / ".lake/build/generated.bin").read_bytes(), artifact.read_bytes())
            run(dest, "lake", "--no-build", "build", "@fixture/+D5.B:olean")
            if os.getenv("LEAN_VOLUME_MEASURE"):
                print(json.dumps({"archive_bytes":manifest["archive_bytes"],"volumes":len(peak),"seconds":duration,
                                  "peak_bytes":max(x for x,_ in peak),"allocated_peak":max(y for _,y in peak)}))
            saved = dest / ".lake/build/old-marker"
            saved.write_bytes(b"previous generation")
            original_tree = remote.load_tree
            for component in ("frame", "archive"):
                check_stage = self.area / ("corrupt-" + component)
                check_stage.mkdir()
                snapshot = release.snapshot_manifest(partition, tag, check_stage, release.operation_deadline(), cache_key=key)
                if component == "archive":
                    snapshot[1]["archive_sha256"] = "0" * 64
                def metadata(*args):
                    verified = original_tree(*args)
                    if component == "frame":
                        verified["members"][-1]["sha256"] = "0" * 64
                    return verified
                with self.subTest(component=component), patch.object(remote, "load_tree", side_effect=metadata), \
                        self.assertRaisesRegex(ValueError, "checksum"):
                    release.restore_snapshot(dest, partition, tag, check_stage, release.operation_deadline(),
                                             cache_key=key, refresh_stale=True, validated_snapshot=snapshot)
                self.assertEqual(saved.read_bytes(), b"previous generation")
            last = tree["locations"][-1][-1][0]
            original_asset = remote.asset_for
            def damage(host, address, digest, size, deadline):
                asset = original_asset(host, address, digest, size, deadline)
                if digest == last:
                    path = self.pool / address / digest
                    data = path.read_bytes()
                    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                return asset
            with patch.object(remote, "asset_for", side_effect=damage), self.assertRaisesRegex(ValueError,"physical volume checksum"):
                release.restore_snapshot(dest, partition, tag, stage, release.operation_deadline(), cache_key=key, refresh_stale=True)
            self.assertEqual(saved.read_bytes(), b"previous generation")
            self.assertEqual((dest / ".lake/build/generated.bin").read_bytes(), artifact.read_bytes())

    def test_github_command_transport_and_retention(self):
        with ExitStack() as stack:
            release, key, partition, identity = self.github_transport(stack, 5000)
            tags = []
            uploads = []
            for num in range(1, 7):
                if num == 2:
                    self.change_b()
                author = run(self.project, "git", "rev-parse", "HEAD").strip()
                run_id = str(100 + num)
                suffix = f"ci-{run_id}-1"
                identity.return_value = {"producer_commit_sha": author, "publication_id": suffix,
                                         "workflow_run_id": run_id, "workflow_run_attempt": "1"}
                tag = release.prefix(partition, key, indexed="volumes") + suffix
                tags.append(tag)
                with redirect_stdout(io.StringIO()) as output:
                    code = release.publish(self.project, partition, build_cache=False, indexed=True)
                self.assertEqual(code, 0, output.getvalue())
                events = output.getvalue().splitlines()
                final = json.loads(next(x for x in events if x.startswith("LEAN_CACHE_PUBLISH ")).split(" ", 1)[1])
                self.assertEqual(final["status"], "published", events)
                self.assertIsNone(final["prune_error"], events)
                self.assertEqual(set(p.name for p in (self.pool / tag).iterdir()),
                                 {"release.json", "manifest.json", "index.json.gz"})
                uploads.append(json.loads(next(x for x in events if x.startswith("LEAN_CACHE_VOLUME-UPLOAD "))
                                          .split(" ", 1)[1]))
            self.assertFalse((self.pool / tags[0]).exists())
            self.assertTrue((self.pool / tags[1]).exists())
            self.assertTrue(all(row["payload_bytes"] == 0 for row in uploads[2:]))
            tag = tags[1]
            project = self.consumer("github-full")
            stage = self.area / "fetch"
            stage.mkdir()
            self.assertTrue(release.restore_snapshot(project, partition, tag, stage, release.operation_deadline(),
                                                     cache_key=key))
            run(project, "lake", "--no-build", "build", *[f"@fixture/+D5.{n}:olean" for n in "ABC"])
            scoped = self.consumer("github-scoped")
            stage = self.area / "fetch-scoped"
            stage.mkdir()
            amount = release.restore_scoped(scoped, partition, tag, stage, release.operation_deadline(),
                cache_key=key, producer=author, targets=["D5.B"], verification=False)
            self.assertEqual(amount, (1, uploads[1]["payload_bytes"]))
            run(scoped, "lake", "--no-build", "build", "@fixture/+D5.B:olean")
            self.assertFalse((scoped / ".lake/build/lib/lean/D5/A.olean").exists())

            rolling = self.consumer("rolling")
            args = SimpleNamespace(command="select-base", mode="production", writer_owned=False,
                approved_tag=None, approved_producer=None, local_assets=None, refresh_stale=False,
                no_build=False, indexed=False, previous_commit_only=True, tag="", producer="",
                manifest_sha256="", target=[], repository=rolling, source_commit="f" * 40)
            stack.enter_context(patch.object(release, "rolling_source", return_value=("workflow", {"push"})))
            stack.enter_context(patch.object(release, "rolling_producer", return_value=True))
            stack.enter_context(patch.object(release, "seed_partitions", return_value=[partition]))
            with redirect_stdout(io.StringIO()) as selection:
                self.assertEqual(release.rolling_base(args), 0)
            chosen = json.loads(selection.getvalue().split(" ", 1)[1])
            self.assertEqual((chosen["tag"], chosen["producer"], chosen["indexed"]), (tags[-1], author, "true"))
            args.source_ref = "refs/heads/dev"
            with patch.dict(os.environ, LEAN_CACHE_RELEASE_SPARSE_ONLY="1"), redirect_stdout(io.StringIO()) as probe:
                self.assertEqual(release.rolling_base(args), 0)
            self.assertEqual(json.loads(probe.getvalue().split(" ", 1)[1]), chosen)
            args.command, args.tag, args.producer = "install-base", chosen["tag"], chosen["producer"]
            args.previous_commit_only, args.manifest_sha256, args.target = False, chosen["manifest_sha256"], ["D5.A"]
            with redirect_stdout(io.StringIO()) as installed:
                self.assertEqual(release.rolling_base(args), 0)
            self.assertIn('"status":"hit"', installed.getvalue())
            run(rolling, "lake", "--no-build", "build", "@fixture/+D5.A:olean")
            self.assertFalse((rolling / ".lake/build/lib/lean/D5/B.olean").exists())

            import lean_release_volume_remote as remote
            source_ref = "refs/heads/integration-ci-volume-tests"
            verified = {"source_ref": source_ref}
            original_scoped = release.restore_scoped
            def fixture_probe(*args, **options):
                return original_scoped(*args, **dict(options, targets=["D5.A"]))
            stack.enter_context(patch.object(release, "restore_scoped", side_effect=fixture_probe))
            responses = self.area / "run-identity.json"
            api = {}
            stack.enter_context(patch.dict(os.environ, {"FAKE_VERIFICATION_API": str(responses)}))
            vtags = {}
            prefix = release.prefix(partition, key, True, indexed="volumes")
            for num, test_id in ((501, "other-test"), (502, "pair"), (503, "pair"), (504, "pair")):
                kind = str(num)
                vtag = prefix + f"ci-{num}-1"
                vtags[num] = vtag
                if num == 503:
                    self.write("D5/B.lean", "def valB : Nat := 98765\n")
                    self.build()
                    self.commit("B double prime")
                author = run(self.project, "git", "rev-parse", "HEAD").strip()
                api[f"repos/fixture/private/actions/runs/{num}/attempts/1"] = {
                    "id": num, "run_attempt": 1, "path": ".github/workflows/ci-publication-verify.yml",
                    "repository": {"full_name": "fixture/private"}, "head_sha": author,
                    "head_branch": source_ref.removeprefix("refs/heads/")}
                responses.write_text(json.dumps(api))
                identity.return_value = {"producer_commit_sha": author, "publication_id": f"ci-{num}-1",
                                         "source_ref": source_ref, "workflow_run_id": kind, "workflow_run_attempt": "1"}
                options = {"LEAN_CACHE_VOLUME_TEST_ID": test_id}
                if num >= 503:
                    options["LEAN_CACHE_VOLUME_RETIRE_TAG"] = vtags[502]
                original_gh = release.gh
                def interrupted(deadline, *args):
                    value = original_gh(deadline, *args)
                    if num == 503 and args[:3] == ("release", "delete", vtags[502]):
                        raise OSError("interrupted after deleting predecessor")
                    return value
                with patch.dict(os.environ, options), patch.object(release, "gh", side_effect=interrupted), \
                        redirect_stdout(io.StringIO()) as output:
                    code = release.publish(self.project, partition, verification=verified, build_cache=False, indexed=True)
                if num == 503:
                    self.assertEqual(code, 1, output.getvalue())
                    self.assertIn("interrupted after deleting predecessor", output.getvalue())
                    self.assertFalse((self.pool / vtags[502]).exists())
                else:
                    self.assertEqual(code, 0, output.getvalue())
                if num == 504:
                    uploaded = json.loads(next(x for x in output.getvalue().splitlines()
                                               if x.startswith("LEAN_CACHE_VOLUME-UPLOAD ")).split(" ", 1)[1])
                    self.assertEqual(uploaded["payload_bytes"], 0)
                    self.assertGreater(uploaded["reused_bytes"], 0)
            retired = json.loads(next(x for x in output.getvalue().splitlines()
                                      if x.startswith("LEAN_CACHE_VOLUME-RETIRE ")).split(" ", 1)[1])
            self.assertEqual((retired["tag"], retired["source_ref"], retired["test_id"]),
                             (vtags[502], source_ref, "pair"))
            self.assertGreater(retired["retired_snapshot_payload_bytes"], 0)
            self.assertEqual(retired["status"], "already-retired")
            self.assertFalse((self.pool / vtags[502]).exists())
            self.assertTrue((self.pool / vtags[501]).exists())
            manifest = json.loads((self.pool / vtags[504] / release.MANIFEST).read_bytes())
            with self.assertRaisesRegex(ValueError, "no published binding"):
                remote.predecessor_binding(release, partition, key, manifest["volume_scope"], "pair", manifest,
                                           prefix + "ci-500-1", manifest, release.operation_deadline())
            with self.assertRaisesRegex(ValueError, "same verification test"):
                remote.retire_verification(release, partition, key, vtags[504], manifest, vtags[501],
                                           release.operation_deadline())
            self.assertTrue((self.pool / vtags[501]).exists())
            second = self.consumer("verification-empty")
            stage = self.area / "fetch-verification"
            stage.mkdir()
            self.assertTrue(release.restore_snapshot(second, partition, vtags[504], stage, release.operation_deadline(),
                expected={"producer_commit_sha": author}, cache_key=key))
            run(second, "lake", "--no-build", "build", *[f"@fixture/+D5.{n}:olean" for n in "ABC"])
            scoped = self.consumer("verification-scoped")
            stage = self.area / "fetch-verification-scoped"
            stage.mkdir()
            result = original_scoped(scoped, partition, vtags[504], stage, release.operation_deadline(),
                cache_key=key, producer=author, targets=["D5.B"], verification=True)
            self.assertGreater(result[1], 0)
            run(scoped, "lake", "--no-build", "build", "@fixture/+D5.B:olean")

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
        with self.assertRaises((OSError, ValueError, EOFError, tarfile.ReadError)):
            volumes.restore(self.pool, "one", self.project, dest)
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
