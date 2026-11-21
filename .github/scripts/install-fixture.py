"""Install the candidate under test in the pinned consumer fixture."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

candidate = Path(__file__).resolve().parents[2]
fixture = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(candidate / "tools/consumer"))
from pin import update_callers

subprocess.run([sys.executable, str(candidate / "package_release.py")], check=True)
source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=candidate, text=True).strip()
archive = candidate / "build/trureturing-tools.tar.gz"
with archive.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
changes = update_callers(fixture, source)
pin = {"version": f"v0.0.0-ci.g{source}", "sha256": digest, "workflow_sha": source}
changes[fixture / "Meta/tools-release.json"] = json.dumps(pin, indent=2) + "\n"
for path, contents in changes.items():
    path.write_text(contents)
subprocess.run(["git", "add", "--", *changes], cwd=fixture, check=True)
subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                "commit", "--allow-empty", "-qm", "Test the candidate release"], cwd=fixture, check=True)
subprocess.run([sys.executable, "-I", "-B", ".github/scripts/install-tools.py", "--archive",
                str(archive)], cwd=fixture, check=True)
