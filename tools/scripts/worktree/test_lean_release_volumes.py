"""Cross-Release payload reuse and clean Lake consumers on a three-module fixture."""

import json
import os
import random
from pathlib import Path
from contextlib import ExitStack, redirect_stdout
import io
import hashlib
import sys
import subprocess
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import lean_release_volumes as volumes


def run(root, *args):
    result = subprocess.run(args, cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(f"{args} exited {result.returncode}: {result.stdout}{result.stderr}")
    return result.stdout


class VolumeDownloads(unittest.TestCase):
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

    def github_transport(self, stack, chunk_bytes):
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
        stack.enter_context(patch.object(release.report_reuse, "capture", return_value={}))
        stack.enter_context(patch.object(release.report_reuse, "read_receipt",
                                        return_value={"completed": release.report_reuse.CHECKED}))
        stack.enter_context(patch.object(release.report_reuse.publication, "validate_bundle"))
        stack.enter_context(patch.object(release.report_reuse, "record_seed_base", return_value=True))
        stack.enter_context(patch.object(release, "partition_path", return_value=partition))
        stack.enter_context(patch.object(release, "release_key", return_value=key))
        identity = stack.enter_context(patch.object(release, "publication_identity"))
        return release, key, partition, identity

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
                    result = release.publish(self.project, partition, build_report=False, indexed=True)
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
                    code = release.publish(self.project, partition, build_report=False, indexed=True)
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
                    code = release.publish(self.project, partition, verification=verified, build_report=False, indexed=True)
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
