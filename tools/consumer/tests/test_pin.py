"""Check the version lock against the published asset."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("pin", Path(__file__).resolve().parents[1] / "pin.py")
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)


class PinTests(unittest.TestCase):
    def test_release_accepts_published_preview_and_stable_tags(self):
        for tag in ("v0.2.0", "v0.2.0-feature.12.gabc", "v0.2.0-pr.18.14.gabc"):
            digest = "a" * 64
            asset = {"name": pin.ASSET, "state": "uploaded", "digest": "sha256:" + digest}
            metadata = {"tag_name": tag, "draft": False, "assets": [asset]}
            with patch.object(pin.subprocess, "check_output", return_value=json.dumps(metadata)):
                self.assertEqual(pin.release_pin(tag), {"version": tag, "sha256": digest})
            metadata["draft"] = True
            with patch.object(pin.subprocess, "check_output", return_value=json.dumps(metadata)):
                with self.assertRaisesRegex(ValueError, "published"):
                    pin.release_pin(tag)

    def test_pin_changes_only_after_verifying_the_download(self):
        tag = "v0.2.0"
        archive = b"published archive"
        lock = {"version": tag, "sha256": hashlib.sha256(archive).hexdigest()}
        baseline = "b" * 40
        original = '{"version":"v0.1.0","sha256":"' + "c" * 64 + '"}\n'
        with (tempfile.TemporaryDirectory() as tmp, patch.object(pin, "release_pin", return_value=lock),
              patch.object(pin, "branch_tip", return_value="a" * 40),
              patch.object(pin, "read_pin", return_value=(baseline, json.loads(original)))):
            root = Path(tmp)
            path = root / "Meta/tools-release.json"
            path.parent.mkdir()
            path.write_text(original)

            def download(content):
                def run(args, **kwargs):
                    (Path(args[-1]) / pin.ASSET).write_bytes(content)
                return run

            with patch.object(pin.subprocess, "run", side_effect=download(b"wrong")):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    pin.pin_release(root, tag)
            self.assertEqual(path.read_text(), original)
            with patch.object(pin.subprocess, "run", side_effect=download(archive)):
                pin.pin_release(root, tag)
            saved = json.loads(path.read_text()) | {"workflow_sha": "1" * 40}
            path.write_text(json.dumps(saved))
            with patch.object(pin.subprocess, "run") as repeated:
                pin.pin_release(root, tag)
                repeated.assert_not_called()
            self.assertEqual(path.read_text(), json.dumps(saved))
            self.assertEqual(saved, lock | {"previous_pin": baseline, "workflow_sha": "1" * 40})

    def test_rollback_is_a_new_generation_and_invalidates_the_stacked_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / pin.PIN
            path.parent.mkdir()
            archive = root / "build/tools-releases"
            archive.mkdir(parents=True)
            old = {"version": "v0.1.0", "sha256": hashlib.sha256(b"old").hexdigest()}
            (archive / (old["sha256"] + ".tar.gz")).write_bytes(b"old")
            path.write_text(json.dumps(old) + "\n")
            original = path.read_bytes()
            old_blob = hashlib.sha1(f"blob {len(original)}\0".encode() + original).hexdigest()
            new_blob = "c" * 40
            with (patch.object(pin, "branch_tip", return_value="a" * 40),
                  patch.object(pin, "read_pin", return_value=(new_blob, {})) as read,
                  patch.object(pin, "release_pin", return_value=old) as release):
                pin.pin_release(root, old["version"])
                self.assertNotEqual(path.read_bytes(), original)
                self.assertEqual(json.loads(path.read_text()), old | {"previous_pin": new_blob})
                rollback = path.read_bytes()
                read.return_value = hashlib.sha1(f"blob {len(rollback)}\0".encode() + rollback).hexdigest(), {}
                release.return_value = old | {"source_sha": "d" * 40}
                with (patch.object(pin, "base_trailers", return_value=[old_blob]),
                      self.assertRaisesRegex(ValueError, "pin conflict")):
                    pin.pin_release(root, old["version"])
                self.assertEqual(path.read_bytes(), rollback)

    def test_paired_tag_pins_its_full_source_independently_of_the_digest(self):
        tag, sha, digest = "v0.2.0-pr.18.14.g" + "a" * 12, "a" * 40, "d" * 64
        release = {"tag_name": tag, "draft": False, "prerelease": True, "author": {"id": pin.ACTIONS_BOT},
                   "published_at": "2026-01-02T01:00:00Z",
                   "assets": [{"name": pin.ASSET, "state": "uploaded", "digest": f"sha256:{digest}",
                               "uploader": {"id": pin.ACTIONS_BOT}}]}
        with (patch.object(pin, "api", return_value=release),
              patch.object(pin, "tag_source", return_value=sha),
              patch.object(pin, "base_trailers", return_value=["b" * 40])):
            self.assertEqual(pin.release_pin(tag), {"version": tag, "sha256": digest, "source_sha": sha})
            with self.assertRaisesRegex(ValueError, "published after"):
                pin.release_pin(tag, published_by="2026-01-01T01:00:00Z")
            release["author"]["id"] = 123
            with self.assertRaisesRegex(ValueError, "invalid paired"):
                pin.release_pin(tag)
