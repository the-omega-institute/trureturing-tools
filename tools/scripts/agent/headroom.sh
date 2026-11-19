#!/bin/bash
# SL-003 余量普查。读取指定 Git 修订的工具源码及其中的容量阈值。
# 用法: headroom.sh [REV] [MARGIN]，缺省 REV=origin/dev，MARGIN=50。
exec python3 - "$@" <<'PY'
from collections import Counter
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile


SOURCE = "tools/StrataLint.Engine/Rules/RepositoryRules.Structure.cs"
PIN = "Meta/tools-release.json"
ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())


def git(*args):
    return subprocess.check_output(["git", "-C", ROOT, *args], stderr=subprocess.PIPE)


@contextmanager
def tools_archive(revision):
    tree = git("rev-parse", "--verify", "--end-of-options", revision + "^{tree}").decode().strip()
    if git("ls-tree", tree, "--", PIN):
        digest = json.loads(git("show", f"{tree}:{PIN}"))["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid tools digest in {PIN}")
        archive = ROOT / "build/tools-releases" / (digest + ".tar.gz")
        if not archive.exists():
            subprocess.run([sys.executable, ROOT / ".github/scripts/install-tools.py",
                            "--cache-revision", tree], check=True)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"tools release digest mismatch: {archive}")
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r|gz") as package:
                yield package
    else:
        payload = git("archive", "--format=tar", tree, "--", "tools")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r|") as package:
            yield package


def survey(revision):
    lines = {}
    directories = Counter()
    limits = {}
    with tools_archive(revision) as package:
        for entry in package:
            if not entry.isfile() or not entry.name.startswith("tools/") or not entry.name.endswith((".cs", ".sh")):
                continue
            data = package.extractfile(entry).read()
            path = entry.name.removeprefix("tools/")
            lines[path] = data.count(b"\n")
            if path.endswith(".cs"):
                directory, _, _ = path.rpartition("/")
                directories[directory] += 1
            if entry.name == SOURCE:
                for name in ("ArtifactHardLineLimit", "DirectoryFileLimit"):
                    match = re.search(rf"\b{name}\s*=\s*([0-9]+)", data.decode())
                    if match:
                        limits[name] = int(match[1])
    return limits["ArtifactHardLineLimit"], limits["DirectoryFileLimit"], lines, directories


def print_rows(counts, limit, margin, unit, width):
    for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        left = limit - count
        if left < margin:
            print(f"  余量{left:4d}  {count:{width}d} {unit}  {path}")


selftest = sys.argv[1:2] == ["--selftest"]
revision = "origin/dev" if selftest or len(sys.argv) == 1 else sys.argv[1]
try:
    line_limit, directory_limit, lines, directories = survey(revision)
    if selftest:
        if min(line_limit, directory_limit) <= 0:
            sys.exit("  FAIL 阈值非正整数")
        print(f"  ok   解析阈值 行={line_limit} 目录={directory_limit}")
        try:
            survey("refs/heads/__no_such_ref__")
        except subprocess.CalledProcessError:
            print("  ok   不存在的 rev 已 fail-closed")
        else:
            sys.exit("  FAIL 不存在的 rev 未 fail-closed")
        print("SELFTEST_PASS")
    else:
        margin = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        print(f"rev={revision}  行数硬线={line_limit}  目录上限={directory_limit}  报告阈值=余量<{margin}")
        print("--- 文件(按余量升序) ---")
        print_rows(lines, line_limit, margin, "行", 4)
        print("--- 目录(按余量升序) ---")
        print_rows(directories, directory_limit, margin, "个", 3)
except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as error:
    print(f"HEADROOM_LIMITS_UNREADABLE rev={revision} src={SOURCE}: {error}", file=sys.stderr)
    sys.exit(1 if selftest else 2)
PY
