"""Exercise delivery from actual consumer pin transitions."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("adoption", Path(__file__).with_name("adopt-tools.py"))
adoption = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adoption)
import source_pr

OLD, A, B, A_MERGE, B_MERGE, C_MERGE, ORIGINAL, TOOLS_A, TOOLS_B = (letter * 40 for letter in "012345678")
BASE, PIN_A, PIN_B = (letter * 40 for letter in "abc")


def pull(repo, source, merge=None, base="dev"):
    return {"base": {"repo": {"full_name": repo}, "ref": base}, "merged": merge is not None,
            "merge_commit_sha": merge, "head": {"sha": source, "repo": {"full_name": repo}},
            "draft": False, "state": "closed" if merge else "open",
            "merged_at": "2026-01-02T10:00:00Z" if merge else None}


class Delivery(unittest.TestCase):
    def setUp(self):
        self.old = {"version": f"v0.1.1-dev.g{OLD}", "sha256": "d" * 64}
        self.a = {"version": f"v0.1.1-pr.10.13.g{A[:12]}", "sha256": "e" * 64, "source_sha": A}
        self.b = {"version": f"v0.1.1-pr.11.14.g{B[:12]}", "sha256": "f" * 64, "source_sha": B}
        self.consumer = {20: pull(adoption.CONSUMER, A, A_MERGE),
                         21: pull(adoption.CONSUMER, B, B_MERGE),
                         22: pull(adoption.CONSUMER, B, C_MERGE)}
        self.tools = {10: pull(adoption.TOOLS, A), 11: pull(adoption.TOOLS, B)}
        self.parents = {A_MERGE: [ORIGINAL], B_MERGE: [C_MERGE, B], C_MERGE: [A_MERGE, C_MERGE]}
        self.pins = {ORIGINAL: (BASE, self.old), A_MERGE: (PIN_A, self.a),
                     C_MERGE: (PIN_A, self.a), B_MERGE: (PIN_B, self.b)}
        self.trailers = {A: [BASE], B: [PIN_A]}
        self.sources = {item["version"]: sha for item, sha in ((self.old, OLD), (self.a, A), (self.b, B))}
        self.releases = {item["version"]: item.copy() for item in (self.a, self.b)}
        self.dev = OLD
        self.writes = []
        self.fail_response = False
        self.ancestry = {(OLD, A), (A, B), (OLD, TOOLS_A), (A, TOOLS_A), (TOOLS_A, TOOLS_B), (A, TOOLS_B)}
        for name, function in (("api", self.api), ("read_pin", self.pins.__getitem__),
                               ("tag_source", self.sources.__getitem__), ("base_trailers", self.trailers.__getitem__),
                               ("release_pin", self.release_pin), ("branch_tip", self.branch_tip),
                               ("contains", self.contains)):
            mock = patch.object(adoption, name, side_effect=function)
            mock.start()
            self.addCleanup(mock.stop)

    def release_pin(self, version, *, published_by):
        self.assertIsNotNone(published_by)
        return self.releases[version]

    def branch_tip(self, repo):
        return self.dev if repo == adoption.TOOLS else B_MERGE

    def contains(self, repo, source, tip):
        if repo == adoption.CONSUMER:
            return source in {A_MERGE, B_MERGE, C_MERGE}
        return source == tip or (source, tip) in self.ancestry

    def api(self, repo, path, *args):
        if path.startswith("git/commits/"):
            return {"parents": [{"sha": sha} for sha in self.parents[path.split("/")[-1]]]}
        if path.startswith("pulls/"):
            _, number, *action = path.split("/")
            number = int(number)
            if not action:
                return (self.consumer if repo == adoption.CONSUMER else self.tools)[number]
            self.assertEqual(repo, adoption.TOOLS)
            self.assertEqual(action, ["merge"])
            self.assertEqual(args[:6], ("-X", "PUT", "-f", f"sha={self.tools[number]['head']['sha']}",
                                       "-f", "merge_method=merge"))
            self.writes.append(number)
            merge = TOOLS_A if number == 10 else TOOLS_B
            source = self.tools[number]["head"]["sha"]
            self.parents[merge] = [self.dev, source]
            self.tools[number] = pull(adoption.TOOLS, source, merge)
            self.dev = merge
            if self.fail_response:
                raise subprocess.CalledProcessError(1, "gh api")
            return {"merged": True, "sha": merge}
        self.fail(f"unexpected API request {repo}/{path}")

    def test_a_first_b_after_republication_and_duplicate_delivery(self):
        self.trailers[B] = [BASE]
        with self.assertRaisesRegex(ValueError, "pin conflict"):
            adoption.adopt(21)
        self.assertTrue(adoption.adopt(20))
        self.assertFalse(adoption.adopt(22))
        self.trailers[B] = [PIN_A]
        self.fail_response = True
        self.assertTrue(adoption.adopt(21))
        self.assertFalse(adoption.adopt(20))
        self.assertFalse(adoption.adopt(21))
        self.assertEqual(self.writes, [10, 11])

    def test_b_waits_until_a_source_reaches_tools_dev(self):
        with self.assertRaisesRegex(ValueError, "previous consumer pin source"):
            adoption.adopt(21)
        self.assertEqual(self.writes, [])

    def test_unmerged_wrong_target_moved_head_and_digest(self):
        self.consumer[20]["merged"] = False
        with self.assertRaisesRegex(ValueError, "must have merged"):
            adoption.adopt(20)
        self.consumer[20]["merged"] = True
        self.consumer[20]["base"]["ref"] = "main"
        with self.assertRaisesRegex(ValueError, "must have merged"):
            adoption.adopt(20)
        self.consumer[20]["base"]["ref"] = "dev"
        self.tools[10]["base"]["ref"] = "main"
        with self.assertRaisesRegex(ValueError, "moved from"):
            adoption.adopt(20)
        self.tools[10]["base"]["ref"] = "dev"
        self.tools[10]["head"]["sha"] = B
        with self.assertRaisesRegex(ValueError, "moved from"):
            adoption.adopt(20)
        self.tools[10]["head"]["sha"] = A
        self.releases[self.a["version"]]["sha256"] = self.b["sha256"]
        with self.assertRaisesRegex(ValueError, "differs"):
            adoption.adopt(20)
        self.assertEqual(self.writes, [])

    def test_legacy_transition_never_authorizes_a_tools_merge(self):
        self.pins[A_MERGE] = PIN_A, {"version": "v0.2.0", "sha256": "d" * 64}
        self.assertFalse(adoption.adopt(20))
        self.assertEqual(self.writes, [])

    def test_main_only_stable_pin_is_preserved_by_the_new_tools_head(self):
        stable_source = "9" * 40
        stable = {"version": "v0.2.0", "sha256": "d" * 64}
        self.pins[ORIGINAL] = BASE, stable
        self.sources[stable["version"]] = stable_source
        self.ancestry.add((stable_source, A))
        self.assertTrue(adoption.adopt(20))
        self.assertEqual(self.writes, [10])


class History(unittest.TestCase):
    def test_first_parent_lists_only_actual_consumer_lock_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git = lambda *args: adoption.git(repo, *args)
            git("init", "-q", "-b", "dev")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            lock = repo / adoption.PIN
            lock.parent.mkdir()
            lock.write_text(json.dumps({"version": "v0.1.0"}))
            git("add", adoption.PIN)
            git("commit", "-qm", "Old release")
            root = git("rev-parse", "HEAD")
            git("switch", "-qc", "change")
            lock.write_text(json.dumps({"version": "v0.2.0"}))
            git("commit", "-qam", "New release")
            git("switch", "-q", "dev")
            git("merge", "--no-ff", "-qm", "Merge consumer PR", "change")
            merge = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-qm", "Unrelated change")
            git("fetch", "-q", ".", "refs/heads/dev")
            self.assertEqual(adoption.pin_transitions(repo), [root, merge])

    def test_catch_up_finds_only_merged_transitions_for_open_heads(self):
        paired = {"version": f"v0.1.1-pr.10.13.g{A[:12]}", "sha256": "e" * 64, "source_sha": A}
        open_pull = pull(adoption.TOOLS, A) | {"number": 10, "created_at": "2026-01-02T01:02:03Z"}
        merged_pull = {"number": 20, "base": {"ref": "dev"}, "merge_commit_sha": A_MERGE,
                       "merged_at": "2026-01-02T10:00:00Z"}
        lookup = {
            (adoption.TOOLS, "pulls?state=open&base=dev&per_page=100"): [[open_pull]],
            (adoption.CONSUMER, f"git/commits/{C_MERGE}"): {"committer": {"date": "2026-01-02T14:00:00Z"}},
            (adoption.CONSUMER, f"commits/{A_MERGE}/pulls?per_page=100"): [
                merged_pull | {"number": 999, "merge_commit_sha": B_MERGE}, merged_pull,
            ],
        }

        def api(repo, path, *args):
            return lookup[repo, path]

        def git(repo, *args):
            if args[0] == "show":
                return json.dumps(paired if args[1].startswith(A_MERGE) else paired | {"source_sha": B})
            return ""

        with (patch.object(adoption, "api", side_effect=api),
              patch.object(adoption, "branch_tip", return_value=C_MERGE),
              patch.object(adoption, "base_trailers", return_value=[BASE]),
              patch.object(adoption, "pin_transitions", return_value=[A_MERGE, B_MERGE]),
              patch.object(adoption, "git", side_effect=git),
              patch.object(adoption, "adopt") as adopt):
            self.assertEqual(list(adoption.catch_up()), [True])
        adopt.assert_called_once_with(20, A_MERGE)


class Preparation(unittest.TestCase):
    def test_prepare_records_the_pin_on_the_head_without_consuming_staged_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                return adoption.git(repo, *args)

            git("init", "-q", "-b", "feature")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            package = repo / "package_release.py"
            package.write_text("original\n")
            git("add", package.name)
            git("commit", "-qm", "Tools")
            original = git("rev-parse", "HEAD")
            package.write_text("staged change\n")
            git("add", package.name)

            def source_git(directory, *args):
                return git(*args)

            with (patch.object(source_pr, "git", side_effect=source_git),
                  patch.object(source_pr, "branch_tip", return_value=C_MERGE),
                  patch.object(source_pr, "read_pin", side_effect=[
                      (BASE, {"version": "v0.1.0"}), (BASE, {"version": "v0.1.0"}),
                      (PIN_A, {"version": "v0.1.0"}),
                  ]),
                  patch.object(source_pr, "tag_source", return_value=original)):
                source_pr.prepare()
                old_preview = git("rev-parse", "HEAD")
                self.assertIn(BASE, git("log", "-1", "--format=%B"))
                self.assertEqual(git("show", f"HEAD:{package.name}"), "original")
                self.assertIn(package.name, git("diff", "--cached", "--name-only"))
                source_pr.prepare()
                self.assertEqual(git("rev-parse", "HEAD"), old_preview)
                source_pr.prepare()
                self.assertNotEqual(git("rev-parse", "HEAD"), old_preview)
                self.assertIn(PIN_A, git("log", "-1", "--format=%B"))


class Recovery(unittest.TestCase):
    def test_ci_dispatch_is_recovered_once_for_the_current_dev_head(self):
        state = {"workflow_runs": []}
        with (patch.object(adoption, "branch_tip", return_value=TOOLS_A),
              patch.object(adoption, "api", side_effect=[
                  {"message": f"Merge\n\nPaired consumer PR: {adoption.CONSUMER}#20 at {A_MERGE}"},
                  state, None,
                  {"message": f"Merge\n\nPaired consumer PR: {adoption.CONSUMER}#20 at {A_MERGE}"},
                  {"workflow_runs": [{"event": "workflow_dispatch", "status": "queued", "conclusion": None}]},
              ]) as api):
            adoption.recover_dev_ci()
            adoption.recover_dev_ci()
        dispatches = [call for call in api.call_args_list if call.args[1].endswith("/dispatches")]
        self.assertEqual(len(dispatches), 1)


if __name__ == "__main__":
    unittest.main()
