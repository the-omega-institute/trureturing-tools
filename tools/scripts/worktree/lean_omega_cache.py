"""Include the Omega Lake overlays in the ordinary module cache plan and build."""

from dataclasses import replace
from graphlib import TopologicalSorter
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import lean_artifact_inventory as inventory
from lean_generation_plan import digest, file_digest

BASIS = {'OmegaInit', 'OmegaMathlib'}
LIBRARIES = BASIS | {'OmegaMath'}
OMEGA_ROOTS = LIBRARIES | {'OmegaChecks'}
OVERLAYS = ('omega-strict', 'omega-common')
CHECKS = Path('src/OmegaChecks')
CERTIFICATE = 'OmegaChecks.Interface.Equations'


def namespace(name):
    return name.partition('.')[0]


def enabled(root):
    return (root / CHECKS / 'strict_lake.py').is_file()


def roots(root, modules):
    return {n for n in modules if namespace(n) in OMEGA_ROOTS} if enabled(root) else set()


def relative_build(module, backend):
    subdirectory = {'trureturing': '.', 'reg': 'reg', 'leanInspectorInterface': 'leanInspectorInterface'}[module.package]
    return Path(backend) / subdirectory


def plan(root, modules, graph):
    """Select strict and common module closures and bind their effective inputs."""
    if not roots(root, modules):
        return modules, graph, {}
    basis = {name for name in graph if namespace(name) in BASIS}
    strict_modules = dict(modules)
    for name in basis:
        source = Path('strict') / modules[name].source.relative_to('src')
        if not (root / source).is_file():
            raise ValueError(f'no strict source for {name}')
        strict_modules[name] = replace(modules[name], source=source)
    strict_graph = dict(graph)
    strict_graph.update(inventory.project_imports(root, strict_modules, sources=sorted(basis)))
    touched = {}
    for name in TopologicalSorter(strict_graph).static_order():
        touched[name] = namespace(name) in LIBRARIES or any(touched[dep] for dep in strict_graph[name])
    # The strict check builds all Omega library and check modules. The dual
    # compilation also builds every downstream consumer and its local imports.
    consumers = {n for n in strict_graph if touched[n] and namespace(n) != 'OmegaChecks'}
    strict = inventory.import_closure(strict_graph, consumers | roots(root, modules))
    clients = {n for n in strict_graph if n.startswith('OmegaChecks.Portable.') and touched[n]}
    common_roots = basis | clients | ({CERTIFICATE} & graph.keys())
    common = inventory.import_closure(strict_graph, common_roots) | inventory.import_closure(graph, common_roots)
    strict.update(common)

    # The overlay owns the entire root configuration and, for a Reg request,
    # the actual Reg and interface package configurations. Bind their settings
    # and pins without recording the generated files' checkout-specific paths.
    config_paths = [CHECKS / 'strict_lake.py']
    for directory in (Path('.'), Path('Reg'), inventory.INTERFACE):
        config_paths.extend(directory / filename for filename in ('lakefile.toml', 'lake-manifest.json'))
    configuration = [(str(p), file_digest(root / p)) for p in config_paths if (root / p).is_file()]
    # Common facades are generated together from compiled strict exports and
    # official interfaces. A change to either basis or their generator affects
    # the common overlay even when the generated source is not tracked by Git.
    common_inputs = digest([configuration,
                            [(n, file_digest(root / modules[n].source), file_digest(root / strict_modules[n].source))
                             for n in sorted(basis)],
                            [(str(p), file_digest(root / p)) for p in
                             (CHECKS / 'portable.py', CHECKS / 'Portable/Exports.lean')]])
    augmented = dict(modules)
    inputs = {}
    for name in strict:
        module = modules[name]
        builds = [relative_build(module, OVERLAYS[0])]
        values = [str(builds[0]), configuration, str(strict_modules[name].source),
                  file_digest(root / strict_modules[name].source)]
        if name in common:
            builds.append(relative_build(module, OVERLAYS[1]))
            values.extend([str(builds[1]), common_inputs])
        augmented[name] = replace(module, extra_builds=tuple(builds))
        inputs[name] = values
    return augmented, {n: graph[n] | (strict_graph[n] if n in strict else set()) for n in graph}, inputs


def build(root, lake, modules, names):
    targets = [f'@{modules[n].package}/+{n}:olean' for n in sorted(names)]
    for i in range(0, len(targets), 256):
        subprocess.run([*lake, 'build', *targets[i:i+256]], cwd=root, check=True)


def prepare_common(root, modules, strict_lake, portable):
    basis = sorted(n for n in modules if namespace(n) in BASIS)
    strict_lake_command, libraries = strict_lake.lake_layout(True, False)
    build(root, strict_lake_command, modules, basis)
    build(root, ['lake'], modules, basis)
    lean = portable.output('lake', 'env', 'printenv', 'LEAN').strip()
    official_path = portable.output('lake', 'env', 'printenv', 'LEAN_PATH').strip()
    normal, common = portable.derived_facades(lean, libraries[''], basis, official_path)
    for name in basis:
        source = modules[name].source
        if (root / source).read_bytes() != normal[name]:
            raise ValueError(f'{source} does not re-export the compiled strict interface')
        target = strict_lake.COMMON_SRC / source.relative_to('src')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != common[name]:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
                staged.write(common[name])
            os.replace(staged.name, target)


def compile_missing(root, modules, names, common_prepared):
    strict = {n for n in names if modules[n].extra_builds}
    if not strict:
        return common_prepared
    common = {n for n in strict if len(modules[n].extra_builds) == 2}
    # The overlay configuration is owned by the consumer checkout. Use the
    # same implementation that the Omega audit uses for the real Lake builds.
    sys.path.insert(0, str(root / CHECKS))
    try:
        strict_lake = importlib.import_module('strict_lake')
        portable = importlib.import_module('portable')
        if strict_lake.ROOT != root.resolve() or portable.ROOT != root.resolve():
            raise ValueError('Omega overlay loaded from another checkout')
        lake, _ = strict_lake.lake_layout(True, any(modules[n].package != 'trureturing' for n in strict))
        build(root, lake, modules, strict)
        if common:
            if not common_prepared:
                prepare_common(root, modules, strict_lake, portable)
                common_prepared = True
            lake, _ = strict_lake.lake_layout(False, any(modules[n].package != 'trureturing' for n in common),
                                              strict_lake.COMMON_SRC)
            build(root, lake, modules, common)
    finally:
        sys.path.pop(0)
    return common_prepared
