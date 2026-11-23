"""Package a truth release and publish it under its content address."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


PUBLICATION = "truth-release-publication.v1.json"
REPO = "the-omega-institute/trureturing"
REGISTRY = f"ghcr.io/{REPO}/truth-release"
ARTIFACT_TYPE = "application/vnd.theomega.truth-release.bundle.v1"


def release_files(directory):
    record = json.loads((directory / PUBLICATION).read_bytes())
    digest = record["release_digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid truth release digest")
    return digest, record["source_commit"], f"truth-release-{digest[7:]}.tar.gz"


def prepare(bundle, assets):
    _, _, archive = release_files(bundle)
    # Stable tar headers allow the same release to resume an interrupted upload.
    with (assets / archive).open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
    ) as compressed, tarfile.open(mode="w|", fileobj=compressed) as contents:
        for path in sorted(bundle.iterdir()):
            info = tarfile.TarInfo("./" + path.name)
            info.size = path.stat().st_size
            with path.open("rb") as source:
                contents.addfile(info, source)
    shutil.copyfile(bundle / PUBLICATION, assets / PUBLICATION)


def lookup(args, missing):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode and re.search(missing, result.stderr, re.I):
        return None
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def oci(assets):
    digest, commit, archive = release_files(assets)
    tag = f"{REGISTRY}:release-{digest[7:]}"
    describe = ["oras", "manifest", "fetch", "--descriptor", tag]
    descriptor = lookup(describe, r"MANIFEST_UNKNOWN|manifest unknown|: not found\s*$")
    if descriptor is None:
        subprocess.run(["oras", "push", tag, "--artifact-type", ARTIFACT_TYPE,
                        "--annotation", f"org.opencontainers.image.source=https://github.com/{REPO}/commit/{commit}",
                        "--annotation", f"org.opencontainers.image.revision={commit}",
                        "--annotation", f"org.theomega.truth-release.release-digest={digest}",
                        f"{archive}:{ARTIFACT_TYPE}+tar+gzip"], cwd=assets, check=True)
        descriptor = json.loads(subprocess.check_output(describe))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print("digest=" + descriptor["digest"], file=output)
        print(f"reference={REGISTRY}@" + descriptor["digest"], file=output)


def mirror(assets, reference):
    digest, commit, archive = release_files(assets)
    tag = "truth-release-" + digest[7:]
    release = lookup(["gh", "release", "view", tag, "--repo", REPO,
                      "--json", "isDraft,targetCommitish,assets,url"], r"^release not found\s*$")
    if release is None:
        subprocess.run(["gh", "release", "create", tag, "--repo", REPO, "--draft",
                        "--target", commit, "--title", f"Truth release {digest[7:]}",
                        "--notes", f"DAG and truth bundle for {commit}. OCI: {reference}."], check=True)
        existing = {}
    elif not release["isDraft"]:
        print(release["url"])
        return
    else:
        if release["targetCommitish"] != commit:
            raise ValueError(f"Draft {tag} targets another commit")
        existing = {asset["name"]: asset for asset in release["assets"]}

    pending = []
    for name in ("DAG.md", archive, PUBLICATION):
        path = assets / name
        asset = existing.get(name)
        if asset is None:
            pending.append(path)
        elif asset["state"] == "starter":
            # GitHub leaves a starter asset behind when an upload is interrupted.
            subprocess.run(["gh", "release", "delete-asset", tag, name, "--repo", REPO, "--yes"], check=True)
            pending.append(path)
        else:
            with path.open("rb") as source:
                digest = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
            if asset["state"] != "uploaded" or asset["digest"] != digest:
                raise ValueError(f"Conflicting asset on draft {tag}: {name}")
    if pending:
        subprocess.run(["gh", "release", "upload", tag, *pending, "--repo", REPO], check=True)
    subprocess.run(["gh", "release", "edit", tag, "--repo", REPO, "--draft=false", "--latest=false"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("prepare")
    pack.add_argument("bundle", type=Path)
    for command in (pack, commands.add_parser("oci"), commands.add_parser("mirror")):
        command.add_argument("assets", type=Path)
    commands.choices["mirror"].add_argument("reference")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.bundle, args.assets)
    elif args.command == "oci":
        oci(args.assets)
    else:
        mirror(args.assets, args.reference)


if __name__ == "__main__":
    main()
