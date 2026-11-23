"""Exercise the installed candidate on compiled mixed proofs and frozen state."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import tomllib

FIXTURES = Path(__file__).resolve().parent
TARGET = "D5.FrozenDependencyFixture.Mixed"
MODULE = Path(TARGET.replace(".", "/") + ".lean")
STATE = Path("Golden/Frozen/state")
RECORD = STATE / (str(MODULE) + ".json")
INSPECTOR = Path("tools/lean-inspector/build_inputs.py")
CLI = Path("tools/StrataLint.Cli/bin/Release/net10.0/StrataLint.dll")
REFRESH = Path("tools/scripts/agent/frozen-refresh.sh")
MEASURE = Path("tools/scripts/agent/frozen-dependencies.sh")


def run(*args, succeed=True):
    args = [str(arg) for arg in args]
    print("+", *args, flush=True)
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    if succeed:
        result.check_returncode()
    else:
        assert result.returncode != 0
    return result.stdout


def check_independent_reader(directory, states, count):
    checkout = directory / "reader"
    checkout.mkdir()
    git = ["git", "-C", checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid"]
    run(*git, "init", "--quiet")
    run(*git, "commit", "--quiet", "--allow-empty", "-m", "base")
    run(*git, "update-ref", "refs/remotes/origin/dev", "HEAD")
    source = checkout / MODULE
    source.parent.mkdir(parents=True)
    shutil.copyfile(MODULE, source)
    state = checkout / RECORD
    state.parent.mkdir(parents=True)
    reader = "tools/scripts/agent/deposit-evidence/facts.py"
    for record in states:
        state.write_bytes(record)
        run(*git, "add", ".")
        run(*git, "commit", "--quiet", "-m", "frozen state")
        output = run("python3", "-B", reader, checkout)
        assert f"Included declarations: {count}" in output


def write_record(record):
    RECORD.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")


def check_duplicate_theorems(directory):
    direct, classical = "DuplicateDirect", "DuplicateClassical"
    omega_init = "OmegaInit.FrozenDuplicateOwner"
    omega_math = "OmegaMath.FrozenDuplicateOwner"
    proposition = "∀ (p : Prop), p → p"

    def target(name):
        return ".".join(MODULE.with_name(name).with_suffix("").parts)

    def write_theorem(source, theorem, proof):
        source.write_text(f"module\n\npublic theorem {theorem} : {proposition} := {proof}\n",
                          encoding="utf-8")

    write_theorem(MODULE.with_name(direct + ".lean"), "frozenDuplicate", "fun _ h => h")
    write_theorem(MODULE.with_name(classical + ".lean"), "frozenDuplicate",
                  "Classical.choice ⟨fun _ h => h⟩")
    write_theorem(Path("src/OmegaInit/FrozenDuplicateOwner.lean"), "frozenDuplicateOwner", "fun _ h => h")
    write_theorem(Path("src/OmegaMath/FrozenDuplicateOwner.lean"), "frozenDuplicateOwner",
                  "fun p h => (fun (_ : Prop) => h) p")

    for name, first, last in (("MergedForward", direct, classical), ("MergedReverse", classical, direct)):
        owners = (omega_init, omega_math) if first == direct else (omega_math, omega_init)
        source = MODULE.with_name(name + ".lean")
        # Import the compiled theorems directly. Their exported axiom forms
        # have a variable-headed proposition and fail Lean's cheap Prop check.
        source.write_text(
            f"module\nimport all {target(first)}\nimport all {target(last)}\n"
            + "".join(f"import all {owner}\n" for owner in owners)
            + f"\npublic opaque frozenAxioms : {proposition} := frozenDuplicate\n"
            + f"public theorem frozenOwner : {proposition} := frozenDuplicateOwner\n", encoding="utf-8")
    targets = (direct, classical, "MergedForward", "MergedReverse")
    run("python3", "-B", INSPECTOR, "prepare", "--targets", " ".join(map(target, targets)))

    request = directory / "request.json"
    response = directory / "response.json"

    def measure(*names):
        modules = [target(name) for name in names]
        request.write_text(json.dumps(modules), encoding="utf-8")
        run(MEASURE, request, response)
        rows = json.loads(response.read_bytes())
        assert [row["module"] for row in rows] == modules
        return rows

    direct_row, = measure(direct)
    classical_row, = measure(classical)
    a, = direct_row["declarations"]
    b, = classical_row["declarations"]
    assert a["nameKey"] == b["nameKey"] and a["statementTypeAddress"] == b["statementTypeAddress"]
    assert a["libraries"] == a["axioms"] == [], a
    assert b["libraries"] == ["Init"] and b["axioms"] == ["Classical.choice"], b
    assert measure(direct, classical) == [direct_row, classical_row]
    assert measure(classical, direct) == [classical_row, direct_row]

    merged = measure("MergedForward", "MergedReverse")
    expected = ((b, "OmegaMath", []), (a, "OmegaInit", ["Classical.choice"]))
    for row, (chosen, owner, report_axioms) in zip(merged, expected, strict=True):
        declarations = {d["nameKey"]: d for d in row["declarations"]}
        axioms = declarations["ns(n0,12:frozenAxioms)"]
        provenance = declarations["ns(n0,11:frozenOwner)"]
        assert axioms["kind"] == "opaque" and axioms["includeInStatement"], axioms
        assert axioms["axioms"] == chosen["axioms"] and axioms["libraries"] == chosen["libraries"], axioms
        assert provenance["libraries"] == [owner] and provenance["axioms"] == [], provenance

        module = row["module"]
        source = Path(module.replace(".", "/") + ".lean")
        report = directory / (source.stem + ".json")
        run("bash", "tools/scripts/report/lean-report-scoped.sh", "--targets", module, "--output", report)
        compiled = json.loads(report.read_bytes())
        report_module, = [m for m in compiled["modules"] if m["module"] == module]
        report_row, = [d for d in report_module["declarations"] if d["name_key"] == axioms["nameKey"]]
        assert report_row["axioms"] == report_axioms, report_row
        output = run("dotnet", CLI, "ledger-align", "--add", source,
                     "--candidate-lean-report", report, succeed=False)
        assert f"measured declaration does not match current report: {module}:{axioms['nameKey']}" in output
        assert not (STATE / (str(source) + ".json")).exists()
        assert list(STATE.rglob("*.json")) == [RECORD]


def check_stale_hidden_axioms(report, deposited):
    source = MODULE.read_text(encoding="utf-8")
    proof = "private theorem _closedWitness : True := "
    assert proof + "True.intro" in source
    MODULE.write_text(source.replace(proof + "True.intro", proof + "by sorry"), encoding="utf-8")
    run("python3", "-B", INSPECTOR, "prepare", "--targets", TARGET)
    run("bash", "tools/scripts/report/lean-report-scoped.sh", "--targets", TARGET, "--output", report)
    compiled = json.loads(report.read_bytes())
    module, = [module for module in compiled["modules"] if module["module"] == TARGET]
    hidden, = [row for row in module["declarations"] if row["name"].endswith("._closedWitness")]
    assert not hidden["include_in_statement"] and hidden["axioms"] == ["sorryAx"], hidden
    assert all("sorryAx" not in row["axioms"] for row in module["declarations"] if row is not hidden)

    # Keep current source hashes and materials so only the live axiom check can catch this.
    hidden["axioms"] = []
    canonical_json = runpy.run_path("tools/lean-inspector/materials.py")["canonical_json"]
    report.write_bytes(canonical_json(compiled))
    RECORD.unlink()
    for options in (("--add",), ("--refresh-annotations", "--selector")):
        before = RECORD.read_bytes() if RECORD.exists() else None
        output = run("dotnet", CLI, "ledger-align", *options, MODULE,
                     "--candidate-lean-report", report, succeed=False)
        assert f"measured declaration does not match current report: {TARGET}:{hidden['name_key']}" in output
        assert (RECORD.read_bytes() if RECORD.exists() else None) == before
        if before is None:
            RECORD.write_bytes(deposited)


def exercise(directory):
    lakefile = Path("lakefile.toml")
    libraries = {lib["name"] for lib in tomllib.loads(lakefile.read_text())["lean_lib"]}
    with lakefile.open("a", encoding="utf-8") as config:
        for name in ("OmegaInit", "OmegaMath", "OmegaChecks"):
            if name not in libraries:
                config.write(f'\n[[lean_lib]]\nname = "{name}"\nsrcDir = "src"\n')
    inputs = Path("lean-report-inputs.json")
    selection = json.loads(inputs.read_bytes())
    sources = selection["dependency_sources"]["include"]
    if not any(rule["pattern"] == "src/**/*.lean" for rule in sources):
        sources.append({"pattern": "src/**/*.lean", "optional": True})
        inputs.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    files = {
        "Prelude.lean": Path("src/OmegaInit/FrozenDependencyFixture.lean"),
        "Bridge.lean": Path("src/OmegaMath/FrozenDependencyFixture.lean"),
        "OmegaConsumer.lean": Path("src/OmegaChecks/FrozenDependencyFixture.lean"),
        "Helper.lean": MODULE.with_name("Helper.lean"),
        "Mixed.lean": MODULE,
    }
    for source, destination in files.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / source, destination)

    report = directory / "report.json"
    run("dotnet", "build", "tools/StrataLint.Cli/StrataLint.Cli.csproj", "-c", "Release", "--nologo")
    run("python3", "-B", INSPECTOR, "prepare", "--targets", TARGET)
    run("bash", "tools/scripts/report/lean-report-scoped.sh", "--targets", TARGET, "--output", report)
    output = run("dotnet", CLI, "ledger-align", "--add", MODULE, "--candidate-lean-report", report)
    assert "added=1" in output
    deposited = RECORD.read_bytes()
    record = json.loads(deposited)
    rows = {item["declaration_name_key"].split(",")[-1].removesuffix(")").split(":", 1)[-1]: item
            for item in record["declarations"]}
    assert rows["omegaProof"]["libraries"] == ["OmegaInit", "OmegaMath"], rows
    assert rows["omegaProof"]["axioms"] == [], rows
    assert rows["typeOnly"]["libraries"] == ["OmegaInit", "OmegaMath"], rows
    assert rows["axiomFree"]["libraries"] == rows["axiomFree"]["axioms"] == [], rows
    assert "Mathlib" in rows["mathlibProof"]["libraries"], rows
    assert "Init" in rows["mathlibProof"]["libraries"], rows
    assert "D5" not in rows["mathlibProof"]["libraries"], rows
    assert rows["oneAxiom"]["axioms"] == ["propext"], rows
    assert "axioms" not in rows["fullAxioms"], rows
    run("dotnet", CLI, "ledger-frozen", "--target", MODULE)
    assert list(STATE.rglob("*.json")) == [RECORD]

    for index, row in enumerate(record["declarations"]):
        if index % 2 == 0 and row != rows["fullAxioms"]:
            row.pop("libraries")
            row.pop("axioms", None)
        else:
            row["libraries"] = ["Init"]
            row["axioms"] = ["propext"]
    write_record(record)
    run("dotnet", CLI, "ledger-frozen", "--target", MODULE)
    with tempfile.TemporaryDirectory(prefix="frozen-reader-") as reader:
        check_independent_reader(Path(reader), (deposited, RECORD.read_bytes()), len(rows))
    run(REFRESH)
    assert RECORD.read_bytes() == deposited
    timestamp = RECORD.stat().st_mtime_ns
    output = run(REFRESH)
    assert "changed=0" in output and "selectors=1" in output
    assert RECORD.read_bytes() == deposited and RECORD.stat().st_mtime_ns == timestamp
    assert list(STATE.rglob("*.json")) == [RECORD]

    record["statement_id"] = "sha256:" + "0" * 64
    write_record(record)
    mismatched = RECORD.read_bytes()
    output = run("dotnet", CLI, "ledger-align", "--refresh-annotations", "--selector", MODULE,
                 "--candidate-lean-report", report, succeed=False)
    assert "pin or declaration inventory does not match" in output
    assert RECORD.read_bytes() == mismatched

    check_stale_hidden_axioms(report, deposited)
    check_duplicate_theorems(directory)


if __name__ == "__main__":
    # The CI shard's checkout is disposable. Isolate its registrations so the
    # full-repository door sees just the fixture's existing frozen membership.
    os.environ.setdefault("MATHLIB_NO_CACHE_ON_UPDATE", "1")
    os.environ.setdefault("STRATALINT_ACCEPT_COLD_BUILD", "1")
    with tempfile.TemporaryDirectory(prefix="frozen-dependencies-", dir=Path.cwd()) as temporary:
        directory = Path(temporary)
        backup = directory / "original-state"
        STATE.rename(backup)
        try:
            exercise(directory)
        finally:
            shutil.rmtree(STATE, ignore_errors=True)
            backup.rename(STATE)
