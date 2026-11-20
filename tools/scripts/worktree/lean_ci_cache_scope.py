"""Resolve hosted CI's selected Lean targets against the shard module inventory."""

import json
from pathlib import Path
import re
import subprocess
import tomllib

from lean_artifact_inventory import (
    INSPECTOR, REG_INSPECTOR, library_globs, matches_module_glob, workspace_modules,
)

TOOLS = Path(__file__).resolve().parents[2]
COMPILED_SUITES = {
    "compiled-audit": "compiled-judge-test.sh",
    "fib-regressions": "compiled-fib-test.sh",
}
SOURCE_INPUTS = TOOLS / "tests/StrataLint.DeclaredTemplate.Tests/FourierBridgeSourceInputs.json"
REPORT_PROGRAMS = {"leanInspector/inputDiscovery", "leanInspector/reportInspector"}


def inspector_program_root(config, target):
    # The producer uses a Lean lakefile for its custom facets. Read its literal
    # executable declaration without loading Lake or resolving dependencies.
    declaration = re.search(
        rf"^lean_exe[ \t]+{re.escape(target)}(?:[ \t]+where)?[ \t]*\n((?:[ \t]+[^\n]*\n)*)",
        config,
        re.MULTILINE,
    )
    if declaration is None:
        raise ValueError(f"unknown inspector executable: {target}")
    body = declaration[1]
    root = re.search(r"^[ \t]+root[ \t]*:=[ \t]*([^\n]+)$", body, re.MULTILINE)
    if root is None:
        return target
    quoted = root[1].partition(" --")[0].strip()
    if not quoted.startswith("`"):
        raise ValueError(f"expected a literal Lean root for inspector executable {target}")
    return quoted[1:]


def cache_modules(root, scope, scoped_modules):
    suites = set(scope["cache_suites"])
    unknown = suites - COMPILED_SUITES.keys() - {"source-evidence"}
    if unknown:
        raise ValueError("unknown Lean cache suites: " + ", ".join(sorted(unknown)))

    targets = set()
    for suite in sorted(suites & COMPILED_SUITES.keys()):
        route = TOOLS / "scripts" / COMPILED_SUITES[suite]
        targets.update(subprocess.check_output(
            ["bash", str(route), "--print-lean-targets"], text=True).split())

    modules = workspace_modules(root)
    selected = set(scoped_modules)
    source_evidence = "source-evidence" in suites
    if source_evidence:
        for source in json.loads(SOURCE_INPUTS.read_text()).values():
            name = Path(source).with_suffix("").as_posix().replace("/", ".")
            selected.update((name, "Reg." + name))

    # The report facet schedules these programs for normal scoped reports,
    # the source/mirror evidence, and the FIB suite's native report tests.
    if scope["targets"] or source_evidence or "fib-regressions" in suites:
        targets.update(REPORT_PROGRAMS)

    inspector = (root / INSPECTOR / "lakefile.lean").read_text() if targets else ""
    reg = tomllib.loads((root / REG_INSPECTOR / "lakefile.toml").read_text())
    reg_modules = {name for name, module in modules.items() if module.package == reg["name"]}
    reg_libraries = {library["name"]: library for library in reg.get("lean_lib", [])}
    reg_executables = {exe["name"]: exe for exe in reg.get("lean_exe", [])}
    for target in sorted(targets):
        package, _, name = target.partition("/")
        if package == reg["name"] and name in reg_libraries:
            globs = library_globs(reg_libraries[name])
            selected.update(module for module in reg_modules
                            if any(matches_module_glob(module, glob) for glob in globs))
        elif package == reg["name"] and name in reg_executables:
            selected.add(reg_executables[name].get("root", name))
        elif package == "leanInspector":
            selected.add(inspector_program_root(inspector, name))
        else:
            raise ValueError(f"unknown CI Lean build target: {target}")

    missing = selected - modules.keys()
    if missing:
        raise ValueError("Lean cache roots missing from the workspace: " + ", ".join(sorted(missing)))
    return sorted(selected)
