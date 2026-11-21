"""Install the candidate under test in the pinned consumer fixture."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

candidate = Path(__file__).resolve().parents[2]
fixture = Path(sys.argv[1]).resolve()

subprocess.run([sys.executable, str(candidate / "package_release.py")], check=True)
source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=candidate, text=True).strip()
archive = candidate / "build/trureturing-tools.tar.gz"
with archive.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
pin = {"version": f"v0.0.0-ci.g{source}", "sha256": digest}
lock = fixture / "Meta/tools-release.json"
lock.write_text(json.dumps(pin, indent=2) + "\n")
subprocess.run(["git", "add", "--", lock], cwd=fixture, check=True)
subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                "commit", "--allow-empty", "-qm", "Test the candidate release"], cwd=fixture, check=True)
subprocess.run([sys.executable, "-I", "-B", ".github/scripts/install-tools.py", "--archive",
                str(archive)], cwd=fixture, check=True)
