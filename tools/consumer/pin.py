"""Pin the consumer's release archive and its reusable GitHub workflows together."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


REPOSITORY = "the-omega-institute/trureturing-tools"
ASSET = "trureturing-tools.tar.gz"
WORKFLOWS = ("ci-current.yml", "ci-publication-verify.yml", "lean-analysis-fixtures.yml",
             "lean-cache-publish.yml", "scribe-release-publish.yml")


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/{path}"]))


def release_identity(version):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError(f"invalid release tag: {version}")
    release = api(f"releases/tags/{version}")
    if release["draft"] or release["tag_name"] != version:
        raise ValueError(f"expected a published release at {version}")
    assets = [a for a in release["assets"] if a["name"] == ASSET and a["state"] == "uploaded"]
    if len(assets) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", assets[0]["digest"] or ""):
        raise ValueError(f"missing released asset or SHA-256: {ASSET}")
    source = api(f"commits/{version}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError(f"unexpected commit for {version}: {source}")
    return {"version": version, "sha256": assets[0]["digest"][7:], "workflow_sha": source}


def verify_workflows(archive, pin):
    with archive.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != pin["sha256"]:
            raise ValueError("downloaded release digest differs from GitHub asset metadata")
    tree = api(f"git/trees/{pin['workflow_sha']}?recursive=1")
    if tree["truncated"]:
        raise ValueError("incomplete GitHub workflow tree")
    blobs = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
    with tarfile.open(archive, "r:gz") as package:
        for name in (*WORKFLOWS, "ci-unit.yml"):
            member = package.getmember("tools/ci/workflows/" + name)
            if not member.isfile():
                raise ValueError(f"expected a workflow file: {name}")
            data = package.extractfile(member).read()
            oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if oid != blobs.get(".github/workflows/" + name):
                raise ValueError(f"packaged workflow differs from the release source: {name}")


def update_callers(root, source):
    updates = {}
    for name in WORKFLOWS:
        path = root / ".github/workflows" / name
        original = path.read_text()
        target = f"{REPOSITORY}/.github/workflows/{name}@"
        pattern = rf"^([ \t]*uses:[ \t]*{re.escape(target)})[a-f0-9]{{40}}[ \t]*$"
        changed, count = re.subn(pattern, lambda m: m[1] + source, original, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"expected one immutable release workflow reference in {path}")
        updates[path] = changed
    return updates


def write_file(path, content):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def pin_release(root, version):
    pin = release_identity(version)
    cache = root / "build/tools-releases"
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = cache / (pin["sha256"] + ".tar.gz")
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            download = Path(directory) / ASSET
            if target.is_file():
                with target.open("rb") as f:
                    if hashlib.file_digest(f, "sha256").hexdigest() == pin["sha256"]:
                        download = target
            if download != target:
                subprocess.run(["gh", "release", "download", version, "--repo", REPOSITORY,
                                "--pattern", ASSET, "--dir", directory], check=True)
            verify_workflows(download, pin)
            if download != target:
                download.replace(target)
        updates = update_callers(root, pin["workflow_sha"])
        updates[root / "Meta/tools-release.json"] = json.dumps(pin, indent=2) + "\n"
        before = {path: path.read_text() for path in updates}
        written = []
        try:
            for path, content in updates.items():
                if before[path] != content:
                    write_file(path, content)
                    written.append(path)
        except BaseException:
            for path in reversed(written):
                write_file(path, before[path])
            raise
    print(f"Pinned {version} at {pin['workflow_sha']} (sha256 {pin['sha256']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    try:
        pin_release(root, args.version)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"tools pin: {exc}\n")
