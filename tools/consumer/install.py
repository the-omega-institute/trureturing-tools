"""Install a verified tools archive under the invoking checkout.

The consumer bootstrap holds the checkout's installation lock and runs this
archive member after verifying the SHA-256 of the entire archive.
"""

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path.cwd()
CACHE = ROOT / "build/tools-releases"
STAMP = ".release-sha256"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def unpack(archive, stage):
    with tarfile.open(archive, "r:gz") as package:
        seen = set()
        for entry in package:
            parts = entry.name.split("/")
            if (parts[0] != "tools" or any(p in {"", ".", "..", ".git", STAMP} for p in parts)
                    or entry.name in seen or not (entry.isdir() or entry.isfile())):
                raise ValueError(f"unexpected tools archive entry {entry.name!r}")
            seen.add(entry.name)
            package.extract(entry, stage, filter="data")
        for name in ("tools/Makefile", "tools/consumer/Makefile"):
            if not (stage / name).is_file():
                raise ValueError(f"tools release is missing {name}")


def preserve_extra_files(entries):
    files = {Path(entry.name) for entry in entries if entry.isfile()}
    directories = {parent for path in files for parent in path.parents}
    project_dirs = {path.parent for path in files if path.suffix == ".csproj"}
    build_dirs = {parent / name for parent in project_dirs for name in ("bin", "obj")}
    build_dirs.update(path.parent / ".lake" for path in files
                      if path.name in {"lakefile.lean", "lakefile.toml"})
    python_caches = {path.parent / "__pycache__" for path in files if path.suffix == ".py"}
    generated = Path("tools/Generated/scribe-emissions.v1.json")
    directories.update(python_caches | {generated.parent})
    files.update({Path("tools") / STAMP, generated})

    backup = None
    pending = [Path("tools")]
    while pending:
        parent = pending.pop()
        for item in (ROOT / parent).iterdir():
            relative = parent / item.name
            try:
                mode = item.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                if relative in directories:
                    pending.append(relative)
                    continue
                if relative in build_dirs:
                    continue
            if stat.S_ISREG(mode):
                bytecode = parent in python_caches and re.fullmatch(r".+\.pyc(?:\.[0-9]+)?", item.name)
                if relative in files or bytecode:
                    continue
            if backup is None:
                backup = Path(tempfile.mkdtemp(prefix="previous-", dir=CACHE))
                print(f"Preserving extra tools files at {backup}", file=sys.stderr)
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                item.rename(destination)
            except FileNotFoundError:
                if os.path.lexists(item):
                    raise


def installation_matches(archive, *, preserve_extras=True):
    with tarfile.open(archive, "r:gz") as package:
        if preserve_extras:
            preserve_extra_files(package.getmembers())
        for entry in package:
            if entry.isdir():
                continue
            path = ROOT / entry.name
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            if (not stat.S_ISREG(info.st_mode) or info.st_size != entry.size
                    or (info.st_mode & 0o111) != (entry.mode & 0o111)):
                return False
            with package.extractfile(entry) as expected, path.open("rb") as installed:
                if hashlib.file_digest(installed, "sha256").digest() != hashlib.file_digest(expected, "sha256").digest():
                    return False
    return True


def install(archive, pin):
    target = ROOT / "tools"
    stamp = target / STAMP
    if target.is_symlink():
        raise ValueError(f"unexpected symbolic link at {target}")
    installed = stamp.read_text().strip() if stamp.is_file() else None
    if subprocess.check_output(["git", "ls-files", "--", "tools"], cwd=ROOT):
        raise ValueError("the checkout still tracks files under tools/")
    clean = False
    if installed and re.fullmatch(r"[0-9a-f]{64}", installed):
        previous_archive = CACHE / (installed + ".tar.gz")
        if installed == pin["sha256"]:
            clean = installation_matches(archive)
            if clean:
                return
        elif previous_archive.is_file() and digest(previous_archive) == installed:
            clean = installation_matches(previous_archive)
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        stage = Path(tmp)
        unpack(archive, stage)
        (stage / "tools" / STAMP).write_text(pin["sha256"] + "\n")
        previous = None
        if target.exists():
            # Retain local edits and installations with an unavailable manifest.
            previous = stage / "previous"
            if not clean:
                previous = Path(tempfile.mkdtemp(prefix="previous-", dir=CACHE)) / "tools"
            target.rename(previous)
        try:
            (stage / "tools").rename(target)
        except BaseException:
            if previous is not None:
                previous.rename(target)
            raise
        if previous is not None and not clean:
            print(f"Preserved the previous tools directory at {previous}", file=sys.stderr)
    print(f"Installed tools {pin['version']}")


if __name__ == "__main__":
    try:
        command, path, version, sha256 = sys.argv[1:]
        archive = Path(path)
        if command == "read-makefile":
            if not installation_matches(archive, preserve_extras=False):
                raise ValueError("installed tools differ from the locked release. Run make install-tools.")
            sys.stdout.buffer.write((ROOT / "tools/consumer/Makefile").read_bytes())
        elif command == "install":
            install(archive, {"version": version, "sha256": sha256})
        else:
            raise ValueError(f"unexpected installer command {command}")
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        sys.exit(f"tools release: {exc}")
