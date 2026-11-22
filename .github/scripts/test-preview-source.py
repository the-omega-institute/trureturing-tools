"""Exercise source selection for automatic dev publication."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("preview_source", Path(__file__).with_name("preview-source.py"))
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class DevPublication(unittest.TestCase):
    def setUp(self):
        self.run = dict(id=30, workflow_id=12, status="completed", conclusion="success", event="push",
                        head_branch="dev", head_sha="a" * 40, repository={"id": 2}, head_repository={"id": 2},
                        run_attempt=2, html_url="https://example.invalid/run/30")
        self.event = {"action": "completed", "workflow_run": self.run}

    def test_publish_exact_attempt_with_repeatable_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event"
            output = Path(temp) / "output"
            env = dict(GITHUB_EVENT_NAME="workflow_run", GITHUB_EVENT_PATH=str(event),
                       GITHUB_REPOSITORY_ID="2", GITHUB_OUTPUT=str(output), SERIES="0.1.1")
            for number, attempt in [(30, 2), (51, 1)]:
                self.run.update(id=number, run_attempt=attempt)
                event.write_text(json.dumps(self.event))
                output.write_text("")
                name = f"tools-release-{attempt}"
                entries = {f"actions/runs/{number}/artifacts?name={name}": {"artifacts": [
                    {"id": 99, "name": name, "expired": False}]}, "actions/workflows/ci.yml": {"id": 12}}
                with patch.dict(os.environ, env), patch.object(preview, "api", side_effect=entries.__getitem__):
                    preview.main()
                self.assertEqual(output.read_text().splitlines(), ["sha=" + "a" * 40,
                                 "tag=v0.1.1-dev.g" + "a" * 40, "artifact=99"])

    def test_reject_foreign_incomplete_failed_and_other_ci(self):
        for changes in [dict(event="pull_request"), dict(head_branch="feature"), dict(head_repository={"id": 7}),
                        dict(repository={"id": 7}), dict(status="in_progress"), dict(conclusion="failure"),
                        dict(workflow_id=99)]:
            with self.subTest(changes=changes), patch.object(preview, "api", return_value={"id": 12}):
                event = dict(self.event, workflow_run=self.run | changes)
                with self.assertRaises(SystemExit):
                    preview.completed_dev_push(event, 2)

    def test_absent_expired_duplicate_and_old_attempt_artifacts(self):
        live = {"id": 99, "name": "tools-release-2", "expired": False}
        for assets in [[], [live | {"expired": True}], [live, live], [live | {"name": "tools-release-1"}]]:
            with self.subTest(assets=assets), patch.object(preview, "api", side_effect=[{"id": 12}, {"artifacts": assets}]):
                with self.assertRaises(SystemExit):
                    preview.completed_dev_push(self.event, 2)


if __name__ == "__main__":
    unittest.main()
