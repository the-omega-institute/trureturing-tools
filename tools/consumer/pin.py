"""Pin a consumer to a published trureturing-tools release."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


REPOSITORY = "the-omega-institute/trureturing-tools"
ASSET = "trureturing-tools.tar.gz"


def release_pin(version):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError(f"invalid release tag: {version}")
    release = json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{REPOSITORY}/releases/tags/{version}"]
    ))
    if release["draft"] or release["tag_name"] != version:
        raise ValueError(f"expected a published release at {version}")
    assets = [a for a in release["assets"] if a["name"] == ASSET and a["state"] == "uploaded"]
    if len(assets) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", assets[0]["digest"] or ""):
        raise ValueError(f"expected one uploaded {ASSET} with a SHA-256 digest")
    return {"version": version, "sha256": assets[0]["digest"][7:]}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pin_release(root, version):
    pin = release_pin(version)
    lockfile = root / "Meta/tools-release.json"
    mode = lockfile.stat().st_mode & 0o777
    cache = root / "build/tools-releases"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (pin["sha256"] + ".tar.gz")
    with (cache / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.is_file() or digest(target) != pin["sha256"]:
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                subprocess.run(["gh", "release", "download", version, "--repo", REPOSITORY,
                                "--pattern", ASSET, "--dir", temporary], check=True)
                download = Path(temporary) / ASSET
                if digest(download) != pin["sha256"]:
                    raise ValueError("downloaded tools archive differs from its published SHA-256")
                download.replace(target)
        with tempfile.TemporaryDirectory(dir=lockfile.parent) as temporary:
            replacement = Path(temporary) / lockfile.name
            replacement.write_text(json.dumps(pin, indent=2) + "\n")
            os.chmod(replacement, mode)
            replacement.replace(lockfile)
    print(f"Pinned {version} (sha256 {pin['sha256']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
        pin_release(root, args.version)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"tools pin: {exc}\n")
