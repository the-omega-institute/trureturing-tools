"""Shared synthetic repository setup for Lean seed behavior contracts."""
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import sys
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[4]
INPUT = ROOT / "tools/scripts/worktree/lean-cache-input.sh"
REV = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
COMPILER_BUILD = "lean FULL_BUILD=1"
PUBLISH = ROOT / "tools/scripts/worktree/lean-cache-publish.sh"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


class PartitionFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.manifest = {"packages": [{"name": "mathlib", "rev": REV, "inputRev": "v1"}]}
        self.save_manifest()
        write(self.root / "lean-toolchain", "leanprover/lean4:v4.33.0\n")
        write(self.root / "lakefile.toml", 'name = "fixture"\n[leanOptions]\nmaxRecDepth = 1000\n')
        write(self.root / "Trureturing.lean", "import D5.A\n")
        write(self.root / "D5/A.lean", "def a := 1\n")
        write(self.root / "Meta/FILEMAP.toml", 'files = [{pattern = "Meta/ci-cache-paths.json"}]\n')
        write(self.root / "Meta/ci-cache-paths.json", (ROOT / "Meta/ci-cache-paths.json").read_text())

    def save_manifest(self):
        write(self.root / "lake-manifest.json", json.dumps(self.manifest))

    def run_input(self, command, *extra, env=None):
        return subprocess.run(["bash", str(INPUT), command, "--repository", str(self.root), *extra],
                              text=True, capture_output=True, env={**os.environ, **(env or {})})

    def partition(self):
        result = self.run_input("partition")
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()


def prepare_release_report(root):
    """Register Lean execution inputs for the Release transport fixture."""
    for directory, names in (("tools/lean-inspector", ("publication.py", "materials.py")),
                             ("tools/scripts/report", ("lean-report-selection.py", "lean-report-input.sh"))):
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(ROOT / directory / name, target / name)
    paths = lambda *names: dict(include=[dict(pattern=name, optional=False) for name in names], exclude=[])
    sources = ["Trureturing.lean", *sorted(p.relative_to(root).as_posix() for p in (root / "D5").rglob("*.lean"))]
    configs = [name for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json") if (root / name).is_file()]
    write(root / "lean-report-inputs.json", json.dumps(dict(schema_version=1,
        report_execution=dict(toolchain="lean-toolchain", tools=["lake", "lean"], platform=["system", "machine"],
            environment=["LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "ELAN_TOOLCHAIN", "LEAN_OPTS"]),
        report_modules=paths(*sources), inspector_sources=paths(), config_inputs=paths(*configs),
        producer_scopes={"lean-report": paths("lean-report-inputs.json", "tools/scripts/report/lean-report-selection.py"),
                         "scribe-content": paths()})))


def write_release_report(root):
    directory = root / ".lake/build/lean-inspector/modules"
    directory.mkdir(parents=True, exist_ok=True)
    compiled = root / ".lake/build/lib/lean"
    if not (compiled / "Trureturing.olean").exists():
        write(compiled / "Trureturing.olean", "fixture compiler artifact")
    for path in [root / "Trureturing.lean", *sorted((root / "D5").rglob("*.lean"))]:
        address = path.relative_to(root).with_suffix("")
        module = ".".join(address.parts)
        with zipfile.ZipFile(directory / (module + ".zip"), "w") as archive:
            archive.writestr("fixture.txt", module)


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[2])
    if sys.argv[1] == "prepare-release-report":
        prepare_release_report(root)
    else:
        # Concurrent publisher fixtures share the same synthetic compiler output.
        import fcntl
        with (root / ".fixture-report.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            write_release_report(root)
