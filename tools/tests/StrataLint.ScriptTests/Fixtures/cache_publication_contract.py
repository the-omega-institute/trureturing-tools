"""Publication authority for sharded project artifacts and dependency snapshots."""
import os
from unittest import mock


class PublicationCases:
    def test_measured_work_for_sharded_publication(self):
        owner = self.restore_owner()
        for report, programs in ((0, 0), (2, 0), (0, 3)):
            with self.subTest(report=report, programs=programs):
                self.work(report, programs)
                with mock.patch.dict(os.environ, self.env, clear=True):
                    allowed, _, work = owner.project_publication(self.root)
                    self.assertEqual(report + programs, work)
                    self.assertEqual(work > 0, allowed)

    def test_dependency_snapshot_requires_a_successful_protected_check(self):
        self.dependency_seed()
        for event, ref, checked, allowed in (
                ("push", "refs/heads/dev", "true", True),
                ("push", "refs/heads/integration-ci", "true", True),
                ("push", "refs/heads/topic", "true", False),
                ("push", "refs/heads/dev", "false", False),
                ("pull_request", "refs/pull/12/merge", "true", False)):
            with self.subTest(event=event, ref=ref, checked=checked):
                self.env.update(GITHUB_EVENT_NAME=event, GITHUB_REF=ref, STRATALINT_CHECK_SUCCEEDED=checked)
                ready, _ = self.snapshot_result()
                self.assertEqual(str(allowed).lower(), ready["dependency_ready"])

    def test_project_unknown_work_never_claims_zero_or_uploads(self):
        owner = self.restore_owner()
        (self.root / ".lake/build").mkdir(parents=True)
        for defect in ("missing", "malformed", "stale", "negative", "bool", "unknown", "foreign-root", "duplicate-field", "archived-path", "symlink"):
            with self.subTest(defect=defect):
                path = self.work()
                if defect == "missing": path.unlink()
                elif defect == "malformed": path.write_text("[]")
                elif defect == "stale": self.work(run_attempt="1")
                elif defect == "negative": self.work(-1)
                elif defect == "bool": self.work(True)
                elif defect == "unknown": self.work(None)
                elif defect == "foreign-root": self.work(repository="/other")
                elif defect == "duplicate-field": path.write_text(path.read_text().rstrip()[:-1] + ', "report": 0}')
                elif defect == "archived-path":
                    archived = self.root / ".lake/build/work.json"
                    archived.write_bytes(path.read_bytes())
                    self.env["STRATALINT_LEAN_BUILD_WORK_FILE"] = str(archived)
                elif defect == "symlink":
                    link = path.with_suffix(".link")
                    link.symlink_to(path)
                    self.env["STRATALINT_LEAN_BUILD_WORK_FILE"] = str(link)
                with mock.patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual((False, "build-work-unknown", None), owner.project_publication(self.root))
