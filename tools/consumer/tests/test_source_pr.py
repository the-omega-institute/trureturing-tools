"""Pin resolution, source promotion and Git comparison contracts."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_pr


class SourcePrTests(unittest.TestCase):
    source = "a" * 40
    adoption = "b" * 40
    base = "c" * 40
    tip = "e" * 40
    pin = {"version": "v1.0.0-feature.1.gaaaaaaaaaaaa", "sha256": "f" * 64}

    def setUp(self):
        self.refs, self.pulls, self.open = [], [], []
        self.reachable = set()
        self.branch_exists = None
        self.pr_race = False
        self.create_sha = self.source
        self.tag_sha = self.source
        self.remote_pin = self.pin
        self.published_pin = self.enterContext(patch.object(source_pr, "release_pin", return_value=self.pin))
        self.read = self.enterContext(patch.object(source_pr, "gh_json", side_effect=self.consumer))
        self.enterContext(patch.object(source_pr, "tools_api", side_effect=self.api))
        self.enterContext(patch.object(source_pr, "open_pulls", side_effect=lambda: self.open))

    def consumer(self, endpoint, *args):
        if endpoint == f"repos/{source_pr.CONSUMER}/git/ref/heads/dev":
            return {"object": {"sha": self.adoption}}
        self.assertEqual(endpoint, f"repos/{source_pr.CONSUMER}/contents/Meta/tools-release.json?ref={self.adoption}")
        return self.remote_pin

    def api(self, endpoint, *args):
        if endpoint == "git/ref/tags/" + self.pin["version"]:
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        if endpoint == "git/ref/heads/dev":
            return {"object": {"sha": self.base}}
        if endpoint.startswith("compare/"):
            return {"status": "ahead" if endpoint.split("...")[1].split("?")[0] in self.reachable else "diverged"}
        if endpoint == f"git/ref/heads/adopted-tools/{self.source}":
            return {"object": {"sha": self.branch_exists}}
        if endpoint == "git/refs":
            self.refs.append(args)
            if self.branch_exists:
                raise subprocess.CalledProcessError(1, "gh")
            return {}
        self.assertEqual(endpoint, "pulls")
        self.pulls.append(args)
        if self.pr_race:
            self.open.append(self.open_pr(self.source))
            raise subprocess.CalledProcessError(1, "gh")
        return {"html_url": "created", "head": {"sha": self.create_sha}}

    def open_pr(self, tip, repo=source_pr.REPOSITORY, base="dev", draft=False):
        return {"url": "existing", "headRefOid": tip, "baseRefName": base, "isDraft": draft,
                "headRepository": {"nameWithOwner": repo}}

    def test_mismatched_asset_stops_before_writes(self):
        self.remote_pin = dict(self.pin, sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "differs"):
            source_pr.source_pr(None)
        self.assertEqual([], self.refs)

    def test_workflow_provenance_is_independent_of_release_digest(self):
        self.reachable.add(self.base)
        self.remote_pin = dict(self.pin, workflow_sha="9" * 40)
        self.assertIn("already in", source_pr.source_pr(None))
        self.remote_pin["workflow_sha"] = "malformed"
        with self.assertRaisesRegex(ValueError, "workflow_sha"):
            source_pr.source_pr(None)

    def test_automatic_preview_checks_full_commit_even_with_matching_prefix(self):
        self.pin = dict(self.pin, version="v1.0.0-dev.g" + self.source)
        self.published_pin.return_value = self.remote_pin = self.pin
        self.reachable.add(self.base)
        self.assertIn("already in", source_pr.source_pr(None))
        self.tag_sha = self.source[:12] + "f" * 28
        with self.assertRaisesRegex(ValueError, "different source"):
            source_pr.source_pr(None)

    def test_moved_preview_tag_stops_before_writes(self):
        self.tag_sha = self.tip
        with self.assertRaisesRegex(ValueError, "different source"):
            source_pr.source_pr(None)
        self.assertEqual([], self.refs)

    def test_landed_source_needs_no_pr(self):
        self.reachable.add(self.base)
        self.assertIn("already in", source_pr.source_pr(None))
        self.assertEqual([], self.refs)

    def test_proposed_lock_opens_exact_pr_even_when_fork_or_different_base_matches(self):
        self.open = [self.open_pr(self.source, repo="fork/trureturing-tools"), self.open_pr(self.source, base="stack")]
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "tools-release.json"
            lock.write_text(json.dumps(self.pin))
            self.assertIn("created", source_pr.source_pr(lock))
        self.read.assert_not_called()
        self.assertIn(f"sha={self.source}", self.refs[0])
        self.assertIn(f"head=adopted-tools/{self.source}", self.pulls[0])
        self.assertIn("base=dev", self.pulls[0])
        self.assertTrue(any("proposed consumer lock" in field for field in self.pulls[0]))

    def test_reports_stack_and_prefers_exact_head(self):
        self.reachable.add(self.tip)
        self.open = [self.open_pr(self.tip, draft=True)]
        self.assertIn("draft", source_pr.source_pr(None))
        self.assertIn("additional commits", source_pr.source_pr(None))
        self.open.append(self.open_pr(self.source))
        self.assertIn("exact source", source_pr.source_pr(None))
        self.assertEqual([], self.refs)

    def test_concurrent_or_interrupted_publication(self):
        self.branch_exists = self.source
        self.pr_race = True
        self.assertIn("existing", source_pr.source_pr(None))
        self.branch_exists = self.tip
        self.open = []
        with self.assertRaisesRegex(ValueError, "differs"):
            source_pr.source_pr(None)

    def test_head_moved_while_opening(self):
        self.create_sha = self.tip
        with self.assertRaisesRegex(ValueError, "head changed"):
            source_pr.source_pr(None)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "-b", "dev")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("one", "base")
        self.git("switch", "-qc", "topic")
        self.head = self.commit("two", "change")
        self.git("switch", "-q", "dev")

    def git(self, *args):
        return source_pr.git(self.root, *args)

    def commit(self, value, message):
        (self.root / "a").write_text(value + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def state(self):
        return source_pr.relationship(self.root, self.git("rev-parse", "dev"), self.base, self.head)

    def test_unique_same_tree_patch_already_present_and_reverted(self):
        self.assertIn("distinct", self.state())
        self.commit("two", "equivalent commit")
        self.assertIn("same tree", self.state())
        (self.root / "b").write_text("independent\n")
        self.git("add", "b")
        self.git("commit", "-qm", "another file")
        self.assertIn("patch present in current dev", self.state())
        self.commit("one", "withdraw equivalent patch")
        self.assertIn("patches in dev history", self.state())

    def test_stacked_pr_compares_only_its_own_net_delta(self):
        self.git("switch", "-q", "topic")
        (self.root / "b").write_text("stacked\n")
        self.git("add", "b")
        self.git("commit", "-qm", "stacked addition")
        self.base, self.head = self.head, self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "dev")
        (self.root / "b").write_text("stacked\n")
        self.git("add", "b")
        self.git("commit", "-qm", "take only stacked addition")
        self.assertIn("patch present in current dev", self.state())

    def test_conflicting_delta_requires_review(self):
        self.commit("three", "conflicting edit")
        self.assertIn("conflict", self.state())

    def test_historical_equivalent_with_current_conflict(self):
        self.commit("two", "equivalent change")
        self.commit("three", "later replacement")
        state = self.state()
        self.assertIn("patches in dev history", state)
        self.assertIn("conflict", state)

    def test_ancestry_survives_revert(self):
        self.git("merge", "--ff-only", self.head)
        self.commit("one", "revert")
        self.assertIn("in dev history", self.state())
