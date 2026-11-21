"""Run the consuming checkout's Makefile and bootstrap against a local release."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


CONSUMER = Path(os.environ.get("CONSUMER_ROOT", ".")).resolve()
PROGRAM = Path(__file__).resolve().parents[1]
BOOTSTRAP = ".github/scripts/install-tools.py"


class ReleaseBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "consumer"
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / "Meta").mkdir()
        for name in ("Makefile", BOOTSTRAP):
            shutil.copy(CONSUMER / name, self.root / name)
        self.command("git", "init", "-q")
        self.archive = Path(self.temp.name) / "tools.tar.gz"
        entries = {
            "tools/Makefile": b"filemap-conform:\n\t@echo checked\n",
            "tools/consumer/Makefile": (PROGRAM / "Makefile").read_bytes(),
            "tools/consumer/install.py": (PROGRAM / "install.py").read_bytes(),
        }
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.pin = self.root / "Meta/tools-release.json"
        self.pin.write_text(json.dumps(dict(version="v1.0.0-ci.1", sha256=self.sha, workflow_sha="1" * 40)))
        self.cache = self.root / f"build/tools-releases/{self.sha}.tar.gz"

    def command(self, *args, check=True, **options):
        result = subprocess.run(args, cwd=self.root, text=True, capture_output=True, **options)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def install(self, *args, **options):
        return self.command(sys.executable, "-I", "-B", BOOTSTRAP, *args, **options)

    def test_cold_and_warm_make_with_offline_cache(self):
        dry = self.command("make", "-n", "filemap-conform")
        self.assertIn("filemap-conform", dry.stdout)
        self.assertFalse((self.root / "build").exists())
        blocked = self.command("make", "lean-cache-ensure", check=False, env={**os.environ, "FULL_BUILD": "1"})
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("FULL_BUILD=1", blocked.stderr)
        self.assertFalse((self.root / "build").exists())
        self.cache.parent.mkdir(parents=True)
        shutil.copy(self.archive, self.cache)
        self.assertIn("make lean-report-prepare", self.command("make", "-j2", "help", "filemap-conform").stdout)
        self.assertEqual((self.root / "tools/.release-sha256").read_text().strip(), self.sha)
        self.assertIn("echo checked", self.command("make", "-n", "filemap-conform").stdout)

    def test_concurrent_install_and_preserved_edits(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.install("--archive", self.archive), range(2)))
        program = self.root / "tools/consumer/install.py"
        program.write_text('raise RuntimeError("locally edited")\n')
        self.install()
        self.assertEqual(program.read_bytes(), (PROGRAM / "install.py").read_bytes())
        backups = list(self.cache.parent.glob("previous-*/tools/consumer/install.py"))
        self.assertEqual(len(backups), 1)
        self.assertIn("locally edited", backups[0].read_text())

    def test_digest_mismatch_and_old_revision(self):
        bad = Path(self.temp.name) / "bad.tar.gz"
        bad.write_bytes(b"invalid")
        result = self.install("--archive", bad, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertFalse((self.root / "tools").exists())
        self.pin.write_text(json.dumps({"version": "v1.0.0", "sha256": self.sha}))
        self.command("git", "add", "Meta/tools-release.json")
        self.command("git", "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "old pin")
        donor = Path(self.temp.name) / "donor"
        donor_cache = donor / "build/tools-releases" / self.cache.name
        donor_cache.parent.mkdir(parents=True)
        self.archive.rename(donor_cache)
        self.install("--cache-revision", "HEAD", "--cache-from", donor)
        self.assertEqual(hashlib.sha256(self.cache.read_bytes()).hexdigest(), self.sha)
        self.assertFalse((self.root / "tools").exists())
