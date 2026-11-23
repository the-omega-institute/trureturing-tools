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
SHARDS = ["fixture-a", "fixture-b", "fixture-c"]
CONTRACT = {"verify": {}, "contract": {"name": "Consumer fixture ${{ matrix.shard }}",
                                      "strategy": {"matrix": {"shard": SHARDS}}}, "package": {}}
JOBS = ["verify", "package", *(f"Consumer fixture {shard}" for shard in SHARDS)]


class DevPublication(unittest.TestCase):
    def setUp(self):
        self.run = dict(id=30, workflow_id=12, status="completed", conclusion="success", event="push",
                        head_branch="dev", head_sha="a" * 40, repository={"id": 2}, head_repository={"id": 2},
                        run_attempt=2, html_url="https://example.invalid/run/30")
        self.enterContext(patch.object(preview, "ci_contract", return_value=CONTRACT))
        self.event = {"action": "completed", "workflow_run": self.run}

    def metadata(self, *, jobs=JOBS, artifact=True):
        run, attempt, sha = self.run["id"], self.run["run_attempt"], self.run["head_sha"]
        name = f"tools-release-{attempt}"
        return {
            "actions/workflows/ci.yml": {"id": 12},
            f"actions/workflows/ci.yml/runs?head_sha={sha}&status=success&per_page=100": {"workflow_runs": [self.run]},
            f"actions/runs/{run}/attempts/{attempt}/jobs?per_page=100": {"jobs": [
                {"name": job, "head_sha": sha, "conclusion": "success"} for job in jobs]},
            f"actions/runs/{run}/artifacts?name={name}": {"artifacts": [
                {"id": 99, "name": name, "expired": False}] if artifact else []},
        }

    def test_publish_exact_attempt_with_repeatable_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event"
            output = Path(temp) / "output"
            env = dict(GITHUB_EVENT_NAME="workflow_run", GITHUB_EVENT_PATH=str(event),
                       GITHUB_REPOSITORY_ID="2", GITHUB_OUTPUT=str(output), SERIES="0.1.1")
            for number, attempt, trigger in [(30, 2, "push"), (51, 1, "workflow_dispatch")]:
                self.run.update(id=number, run_attempt=attempt, event=trigger)
                event.write_text(json.dumps(self.event))
                output.write_text("")
                with patch.dict(os.environ, env), patch.object(preview, "api", side_effect=self.metadata().__getitem__):
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
                    preview.completed_dev_ci(event, 2)

    def test_absent_expired_duplicate_and_old_attempt_artifacts(self):
        live = {"id": 99, "name": "tools-release-2", "expired": False}
        for assets in [[], [live | {"expired": True}], [live, live], [live | {"name": "tools-release-1"}]]:
            entries = self.metadata()
            entries["actions/runs/30/artifacts?name=tools-release-2"] = {"artifacts": assets}
            with self.subTest(assets=assets), patch.object(preview, "api", side_effect=entries.__getitem__):
                with self.assertRaises(SystemExit):
                    preview.completed_dev_ci(self.event, 2)

    def test_every_expected_fixture_and_package_job_must_pass_at_this_sha(self):
        wrong_head = self.metadata()
        wrong_head["actions/runs/30/attempts/2/jobs?per_page=100"]["jobs"][0]["head_sha"] = "b" * 40
        for entries in (self.metadata(jobs=JOBS[:-1]), self.metadata(jobs=JOBS[2:]), wrong_head):
            with patch.object(preview, "api", side_effect=entries.__getitem__) as api:
                with self.assertRaises(SystemExit):
                    preview.completed_dev_ci(self.event, 2)
                self.assertFalse(any("/artifacts" in call.args[0] for call in api.call_args_list))

    def test_fork_head_and_canonical_pr_target(self):
        sha = self.run["head_sha"]
        pr = {"head": {"sha": sha, "ref": "fork-branch", "repo": {"id": 5}},
              "base": {"ref": "dev", "repo": {"id": 2}}}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY_ID": "2"}), patch.object(preview, "api", return_value=pr):
            self.assertEqual(preview.source_head("pr/45"), (sha, "pr.45", "fork-branch", 5))
            pr["base"]["repo"]["id"] = 7
            with self.assertRaisesRegex(SystemExit, "canonical tools repository"):
                preview.source_head("pr/45")

        self.run.update(event="pull_request", head_branch="fork-branch", head_repository={"id": 5}, pull_requests=[])
        entries = self.metadata()
        with patch.object(preview, "api", side_effect=entries.__getitem__):
            self.assertEqual(preview.tested_archive(sha, "fork-branch", 5, 45), 99)
            with self.assertRaisesRegex(SystemExit, "No successful"):
                preview.tested_archive(sha, "fork-branch", 2, 45)
            self.run["pull_requests"] = [{"number": 46, "base": {"ref": "dev"}}]
            with self.assertRaisesRegex(SystemExit, "No successful"):
                preview.tested_archive(sha, "fork-branch", 5, 45)
            self.run["pull_requests"][0]["number"] = 45
            entries["actions/runs/30/attempts/2/jobs?per_page=100"]["jobs"][-1]["head_sha"] = "b" * 40
            with self.assertRaisesRegex(SystemExit, "No successful"):
                preview.tested_archive(sha, "fork-branch", 5, 45)


if __name__ == "__main__":
    unittest.main()
