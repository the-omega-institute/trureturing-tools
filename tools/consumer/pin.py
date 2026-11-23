"""Pin a consumer to a published trureturing-tools release."""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from paired import ACTIONS_BOT, ASSET, PR_PREVIEW, SHA, TOOLS as REPOSITORY, VERSION, api, base_trailers, tag_source


def release_pin(version, *, published_by=None):
    if not re.fullmatch(VERSION, version):
        raise ValueError(f"invalid release tag: {version}")
    release = api(REPOSITORY, f"releases/tags/{version}")
    if release["draft"] or release["tag_name"] != version:
        raise ValueError(f"expected a published release at {version}")
    assets = [a for a in release["assets"] if a["name"] == ASSET and a["state"] == "uploaded"]
    if len(assets) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", assets[0]["digest"] or ""):
        raise ValueError(f"expected one uploaded {ASSET} with a SHA-256 digest")
    pin = {"version": version, "sha256": assets[0]["digest"][7:]}
    preview = PR_PREVIEW.fullmatch(version)
    if preview:
        source = tag_source(version, lightweight=True)
        trailers = base_trailers(source)
        if trailers:
            if (len(trailers) != 1 or not re.fullmatch(SHA, trailers[0]) or not source.startswith(preview[2])
                    or not release["prerelease"] or release["author"]["id"] != ACTIONS_BOT
                    or assets[0]["uploader"]["id"] != ACTIONS_BOT):
                raise ValueError(f"invalid paired tools preview {version}")
            if published_by and datetime.fromisoformat(release["published_at"]) > datetime.fromisoformat(published_by):
                raise ValueError(f"{version} was published after the consumer PR merged")
            pin["source_sha"] = source
    return pin


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
    if "source_sha" in pin:
        print(f"Submit the pin to consumer dev first. After it merges, tools PR #{PR_PREVIEW.fullmatch(version)[1]} is eligible.")
    else:
        print("To submit this source for tools/dev review, run python3 tools/consumer/source_pr.py --lock Meta/tools-release.json.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--consumer", type=Path, help="pin this consumer checkout instead of the current checkout")
    args = parser.parse_args()
    try:
        root = args.consumer or Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
        pin_release(root, args.version)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"tools pin: {exc}\n")
