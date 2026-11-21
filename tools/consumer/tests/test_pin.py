"""Check pinning against the caller files and the workflows in the installed release."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import hashlib


spec = importlib.util.spec_from_file_location("pin", Path(__file__).resolve().parents[1] / "pin.py")
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)


class PinTests(unittest.TestCase):
    def test_all_callers_keep_their_contents_except_for_the_revision(self):
        source = "f" * 40
        target = "e" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            for name in pin.WORKFLOWS:
                (workflows / name).write_text(f"  run:\n    uses: {pin.REPOSITORY}/.github/workflows/{name}@{source}\n\n")
            updates = pin.update_callers(root, target)
            self.assertEqual(len(updates), len(pin.WORKFLOWS))
            for path, contents in updates.items():
                self.assertEqual(contents, path.read_text().replace(source, target))
            (workflows / pin.WORKFLOWS[-1]).write_text("jobs: {}\n")
            with self.assertRaises(ValueError):
                pin.update_callers(root, target)

    def test_workflow_bytes_are_bound_to_both_the_asset_and_the_git_object(self):
        data = b"on: workflow_call\n"
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / pin.ASSET
            tree = []
            with tarfile.open(archive, "w:gz") as tar:
                for name in (*pin.WORKFLOWS, "ci-unit.yml"):
                    member = tarfile.TarInfo("tools/ci/workflows/" + name)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
                    tree.append({"type": "blob", "sha": blob, "path": ".github/workflows/" + name})
            identity = {"workflow_sha": "a" * 40, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
            with patch.object(pin, "api", return_value={"truncated": False, "tree": tree}):
                pin.verify_workflows(archive, identity)
                tree[-1]["sha"] = "0" * 40
                with self.assertRaisesRegex(ValueError, "differs from the release source"):
                    pin.verify_workflows(archive, identity)
            identity["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "digest"):
                pin.verify_workflows(archive, identity)
