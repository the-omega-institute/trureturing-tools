"""Build the tools release asset from a Git revision."""

import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parent
ASSET = "trureturing-tools.tar.gz"
CONSUMER_WORKFLOWS = (
    "ci-current.yml",
    "ci-publication-verify.yml",
    "ci-unit.yml",
    "lean-analysis-fixtures.yml",
    "lean-cache-publish.yml",
    "scribe-release-publish.yml",
)


def git(*args, stdin=None):
    return subprocess.check_output(["git", *args], cwd=ROOT, input=stdin)


def write_archive(revision, output):
    tree = git("rev-parse", "--verify", "--end-of-options", revision + "^{tree}").strip()
    destinations = {".github/workflows/" + name: "tools/ci/workflows/" + name
                    for name in CONSUMER_WORKFLOWS}
    destinations["LICENSE"] = "tools/LICENSE"
    listing = git("ls-tree", "-rz", tree, "--", "tools", *destinations)
    members = {}
    for record in listing.rstrip(b"\0").split(b"\0"):
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = raw_path.decode("utf-8")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError(f"release input must be a regular file: {path}")
        if path in destinations.values():
            raise ValueError(f"{path} is reserved for a packaged repository file")
        name = destinations.get(path, path)
        members[name] = (mode, oid)
    if "tools/LICENSE" not in members or len(members) == 1:
        raise ValueError("release requires LICENSE and a nonempty tools tree")

    ordered = sorted(members.items())
    objects = b"".join(oid + b"\n" for _, (_, oid) in ordered)
    blobs = io.BytesIO(git("cat-file", "--batch", stdin=objects))
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as gz:
        with tarfile.open(mode="w|", fileobj=gz, format=tarfile.PAX_FORMAT) as archive:
            for path, (mode, expected_oid) in ordered:
                oid, kind, size = blobs.readline().split()
                if oid != expected_oid or kind != b"blob":
                    raise ValueError(f"Git returned the wrong object for {path}")
                entry = tarfile.TarInfo(path)
                entry.size = int(size)
                entry.mode = 0o755 if mode == b"100755" else 0o644
                archive.addfile(entry, blobs)
                if blobs.read(1) != b"\n":
                    raise ValueError(f"incomplete Git blob for {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / ASSET
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staging:
        temp_path = Path(staging.name)
        try:
            write_archive(args.revision, staging)
            staging.flush()
            staging.seek(0)
            digest = hashlib.file_digest(staging, "sha256").hexdigest()
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
    print(f"{digest}  {ASSET}")


if __name__ == "__main__":
    main()
