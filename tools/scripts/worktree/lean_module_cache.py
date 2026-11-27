"""Publish and restore independently versioned, named Lean compiler shards."""

import argparse
from collections import defaultdict
from dataclasses import replace
from graphlib import TopologicalSorter
import json
import importlib
import datetime
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import shutil
import tomllib

import lean_artifact_inventory as inventory
import lean_compile_layers as compiler
from lean_cache import publication_mathlib, seed_partitions
from lean_generation_plan import digest, file_digest, module_config, write
from lean_module_shards import DEFAULT_CAPACITY, NAME, pack_shard, plan_shards, restore_shard


SCHEMA = 'lean-module-shards-v1'
VERSION_PREFIX = 'lean-modules-'
DEFAULT_MODULE_BYTES = 712650
MAX_COLD_REPORTS_PER_SHARD = 250
REPORT_READ_ERRORS = (OSError, EOFError, KeyError, TypeError, ValueError, ImportError, subprocess.SubprocessError, tarfile.TarError)


OMEGA_BASIS = {'OmegaInit', 'OmegaMathlib'}
OMEGA_LIBRARIES = OMEGA_BASIS | {'OmegaMath'}
OMEGA_ROOTS = OMEGA_LIBRARIES | {'OmegaChecks'}
OMEGA_OVERLAYS = ('omega-strict', 'omega-common')
OMEGA_CHECKS = Path('src/OmegaChecks')
OMEGA_CERTIFICATE = 'OmegaChecks.Interface.Equations'
BACKEND_READER = 'OmegaBuild.Plan'


def namespace(name):
    return name.partition('.')[0]


def omega_enabled(root):
    return (root / OMEGA_CHECKS / 'strict_lake.py').is_file()


def omega_roots(root, modules):
    return {n for n in modules if namespace(n) in OMEGA_ROOTS} if omega_enabled(root) else set()


def omega_build_dir(module, backend):
    subdirectory = {'trureturing': '.', 'reg': 'reg', 'leanInspectorInterface': 'leanInspectorInterface'}[module.package]
    return Path(backend) / subdirectory


def omega_build_plan(root, modules, graph):
    """Select strict and common module closures and bind their effective inputs."""
    if not omega_roots(root, modules):
        return modules, graph, {}
    basis = {name for name in graph if namespace(name) in OMEGA_BASIS}
    strict_modules = dict(modules)
    for name in basis:
        source = Path('strict') / modules[name].source.relative_to('src')
        if not (root / source).is_file():
            raise ValueError(f'no strict source for {name}')
        strict_modules[name] = replace(modules[name], source=source)
    strict_graph = dict(graph)
    strict_graph.update(inventory.project_imports(root, strict_modules, sources=sorted(basis)))
    plugin = inventory.INIT_PLUGIN
    plugin_inputs = inventory.import_closure(graph, {plugin}) if plugin in graph else set()
    if plugin_inputs:
        for name in basis:
            if namespace(name) == 'OmegaInit':
                strict_graph[name] = strict_graph[name] | {plugin}
    # The command is a Lean elaborator. Its import makes selection possible;
    # only the official compiled metadata can say that it was actually invoked.
    command = 'OmegaBuild.Commands'
    may_select = {command}
    for name in TopologicalSorter(graph).static_order():
        if graph[name] & may_select:
            may_select.add(name)
    strict = set()
    for name in TopologicalSorter(strict_graph).static_order():
        if namespace(name) == 'OmegaInit' or strict_graph[name] & strict:
            strict.add(name)
    clients = {n for n in strict if n.startswith('OmegaChecks.Portable.')}
    common_roots = (basis | clients | {OMEGA_CERTIFICATE}) & strict
    common_scope = inventory.import_closure(strict_graph, common_roots) | inventory.import_closure(graph, common_roots)
    common = strict & common_scope
    strict_scope = inventory.import_closure(strict_graph, strict)
    augmented = dict(modules)
    for name in plugin_inputs:
        augmented[name] = replace(modules[name], plugin_input=True)
    for name in (strict_scope | common_scope) - strict:
        module = modules[name]
        if module.package in ('trureturing', 'reg', 'leanInspectorInterface'):
            shared = tuple(omega_build_dir(module, backend) for backend, scope in
                           zip(OMEGA_OVERLAYS, (strict_scope, common_scope), strict=True) if name in scope)
            augmented[name] = replace(augmented[name], shared_builds=shared)

    # The overlay owns the entire root configuration and, for a Reg request,
    # the actual Reg and interface package configurations. Bind their settings
    # and pins without recording the generated files' checkout-specific paths.
    config_paths = [OMEGA_CHECKS / 'strict_lake.py']
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
                             (OMEGA_CHECKS / 'portable.py', OMEGA_CHECKS / 'Portable/Exports.lean')]])
    # The cache records the layouts chosen by this checkout's compiled planner.
    # Follow its own imports even when none of the released modules imports it.
    conditional_modules = (strict & may_select) - {command}
    if conditional_modules:
        reader = inventory.import_closure(graph, {BACKEND_READER})
        selection_inputs = digest([
            file_digest(root / OMEGA_CHECKS / 'portable.py'),
            [(name, file_digest(root / modules[name].source)) for name in sorted(reader)],
        ])
    inputs = {}
    for name in strict:
        module = modules[name]
        builds = [omega_build_dir(module, OMEGA_OVERLAYS[0])]
        values = [str(builds[0]), configuration, str(strict_modules[name].source),
                  file_digest(root / strict_modules[name].source)]
        if name in common:
            builds.append(omega_build_dir(module, OMEGA_OVERLAYS[1]))
            values.extend([str(builds[1]), common_inputs])
        conditional = name in conditional_modules
        if conditional:
            values.append(selection_inputs)
        augmented[name] = replace(module, extra_builds=tuple(builds), conditional_backends=conditional)
        inputs[name] = values
    return augmented, {n: graph[n] | (strict_graph[n] if n in strict else set()) for n in graph}, inputs


def materialize_shared(build, modules, names):
    """Give each overlay the verified ordinary outputs of its unchanged prerequisites."""
    from lean_cache_release import has_unsafe_parent
    for name in names:
        module = modules[name]
        if not module.shared_builds:
            continue
        for artifact in inventory.lean_artifact_paths(name, module):
            source = build / artifact
            if not source.is_file():
                continue
            if source.is_symlink() or has_unsafe_parent(build, artifact):
                raise ValueError(f'unsafe ordinary compiler output: {artifact}')
            for overlay in module.shared_builds:
                relative = overlay / artifact.relative_to(module.build)
                target = build / relative
                if target.is_symlink() or has_unsafe_parent(build, relative):
                    raise ValueError(f'unsafe shared compiler output: {relative}')
                target.parent.mkdir(parents=True, exist_ok=True)
                # Replace atomically. An overlay may be left over from an older checkout.
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
                    path = Path(staged.name)
                try:
                    shutil.copy2(source, path)
                    os.replace(path, target)
                finally:
                    path.unlink(missing_ok=True)


def build_omega_targets(root, lake, modules, names):
    targets = [f'@{modules[n].package}/+{n}:olean' for n in sorted(names)]
    for i in range(0, len(targets), 256):
        subprocess.run([*lake, 'build', *targets[i:i+256]], cwd=root, check=True)


def prepare_common_facades(root, modules, strict_lake, portable):
    basis = sorted(n for n in modules if namespace(n) in OMEGA_BASIS)
    strict_lake_command, libraries = strict_lake.lake_layout(True, False)
    build_omega_targets(root, strict_lake_command, modules, basis)
    build_omega_targets(root, ['lake'], modules, basis)
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


def compile_omega_outputs(root, modules, names, common_prepared, *, prepared_reader=False):
    native = sorted(n for n in names if modules[n].plugin_input)
    if native:
        facets = [f'+{n}:c.o.export' for n in native]
        if inventory.INIT_PLUGIN in native:
            facets.append(inventory.INIT_PLUGIN + ':shared')
        subprocess.run(['lake', 'build', *facets], cwd=root, check=True)
    materialize_shared(root / inventory.PROJECT_BUILD, modules, names)
    strict = {n for n in names if modules[n].extra_builds}
    if not strict:
        return common_prepared
    # The overlay configuration is owned by the consumer checkout. Use the
    # same implementation that the Omega audit uses for the real Lake builds.
    sys.path.insert(0, str(root / OMEGA_CHECKS))
    try:
        strict_lake = importlib.import_module('strict_lake')
        portable = importlib.import_module('portable')
        if strict_lake.ROOT != root.resolve() or portable.ROOT != root.resolve():
            raise ValueError('Omega overlay loaded from another checkout')
        candidates = sorted(n for n in strict if modules[n].conditional_backends)
        for offset in range(0, len(candidates), 128):
            batch = candidates[offset:offset + 128]
            lake, _ = strict_lake.lake_layout(False, any(modules[n].package != 'trureturing' for n in batch))
            search_path = portable.output(*lake, 'env', 'printenv', 'LEAN_PATH').strip()
            selection = (portable.backend_plan(search_path, batch, prepared=True) if prepared_reader
                         else portable.backend_plan(search_path, batch))
            skipped = selection['mathlibOnly']
            for name in skipped:
                modules[name] = replace(modules[name], extra_builds=())
            strict.difference_update(skipped)
        if not strict:
            return common_prepared
        common = {n for n in strict if len(modules[n].extra_builds) == 2}
        lake, _ = strict_lake.lake_layout(True, any(modules[n].package != 'trureturing' for n in strict))
        build_omega_targets(root, lake, modules, strict)
        if common:
            if not common_prepared:
                prepare_common_facades(root, modules, strict_lake, portable)
                common_prepared = True
            lake, _ = strict_lake.lake_layout(False, any(modules[n].package != 'trureturing' for n in common),
                                              strict_lake.COMMON_SRC)
            build_omega_targets(root, lake, modules, common)
    finally:
        sys.path.pop(0)
    return common_prepared


def compiler_environment(root):
    if any(os.environ.get(name) for name in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'LEAN_OPTS')):
        raise ValueError('compiler paths or options overridden outside the Lake project')
    return [(root / 'lean-toolchain').read_text().strip(), sorted(seed_partitions(root)),
                   os.environ.get('ELAN_TOOLCHAIN', '')]


def checkout_targets(root, *, kind='compiler', requested=None, restore_only=False, scope_roots=None):
    modules = inventory.workspace_modules(root)
    roots = inventory.release_roots(modules) | omega_roots(root, modules)
    if omega_enabled(root) and BACKEND_READER in modules:
        roots.add(BACKEND_READER)
    registry = 'LeanInformationAudit.TemplateEnrollment'
    if registry in modules:
        roots.add(registry)
    graph = compiler.scoped_imports(root, modules, roots)
    modules, graph, omega_inputs = omega_build_plan(root, modules, graph)
    scope = set(graph)
    reported = {}
    selected = {}
    utilities = {}
    if kind == 'report' and (root / 'lean-report-inputs.json').is_file():
        from lean_cache_release import report_selection
        from lean_generation_facets import module_report_targets, utility_inputs
        reported = report_selection.Selection(root).modules()
        if not reported.keys() <= scope:
            raise ValueError('reported module is outside the compiler release')
        wanted = scope if requested is None else inventory.import_closure(graph, set(requested) & scope)
        selected = {n: reported[n] for n in reported.keys() & wanted}
        if selected:
            # The CLI accepts an explicit scope. Large selections use one snapshot parse.
            roots = set(requested or ()) & selected.keys()
            scope_inputs = {} if roots and scope_roots is not None else None
            subset = sorted(selected) if requested is not None and sum(len(n) + 1 for n in selected) < 64000 else None
            try:
                utilities = utility_inputs(root, subset, scope_inputs=scope_inputs)
                if scope_inputs is not None:
                    # Match the scoped inspector's utility references on explicitly requested roots.
                    for name in roots:
                        scope_roots.update(scope_inputs.get(reported[name], ()))
                    wanted |= inventory.import_closure(graph, scope_roots & scope)
                    selected = {n: reported[n] for n in reported.keys() & wanted}
            except (OSError, subprocess.CalledProcessError) as error:
                if not restore_only:
                    raise
                print(f'Report inputs unavailable; restoring matching compiler artifacts: {error}', file=sys.stderr)
                selected = {}
        if selected:
            paths = set(selected.values())
            report_roots = {row['claimModule'] for path, row in utilities.items() if path in paths}
            if missing := report_roots - scope:
                if not restore_only:
                    raise ValueError('report dependency is outside the compiler publication: ' + min(missing))
                selected = {n: path for n, path in selected.items()
                            if utilities.get(path, {}).get('claimModule') not in missing}
    configs = {}
    pins = {}
    directories = {'trureturing': Path('.'), 'reg': Path('Reg'), 'leanInspectorInterface': inventory.INTERFACE,
                   'leanInspector': inventory.INSPECTOR, 'regInspector': inventory.REG_INSPECTOR}
    for package in {modules[n].package for n in graph}:
        directory = directories[package]
        path = root / directory
        dsl = path / 'lakefile.lean'
        if dsl.is_file():
            config = {'lakefile.lean': file_digest(dsl)}
        else:
            config = tomllib.loads((path / 'lakefile.toml').read_text())
        configs[package] = config
        manifest = path / 'lake-manifest.json'
        pins[package] = file_digest(manifest) if manifest.is_file() else None
    environment = compiler_environment(root)

    targets = {}
    for name in TopologicalSorter(graph).static_order():
        module = modules[name]
        dependencies = sorted(graph[name])
        source = file_digest(root / module.source)
        inputs = [environment, module.package, pins[module.package], str(module.source), str(module.build),
                  module_config(configs[module.package], name), source, [(d, targets[d]['key']) for d in dependencies]]
        if module.plugin_input:
            inputs.append(['Lake plugin native outputs', sorted(str(p) for p in inventory.plugin_outputs(name, module))])
        if name in omega_inputs:
            inputs.extend(omega_inputs[name])
        key = digest(inputs)
        targets[name] = {'key': key, 'deps': dependencies}
    if reported:
        modules = dict(modules)
        reports = module_report_targets(targets, selected, utilities) if selected else {}
        for name, path in reported.items():
            row = targets[name]
            report = reports.get(name)
            targets[name] = dict(row, key=report['key'] if report else None, compiler_input=row['key'],
                                 report_inputs=report['deps'] if report else [])
            modules[name] = replace(modules[name], report=True, report_utility=utilities.get(path))
    return modules, {name: targets[name] for name in sorted(scope)}


def matching(plan, rows):
    return {name: row for name, row in rows.items() if name in plan['shards']
            and row['fingerprint'] == plan['shards'][name]['fingerprint']
            and row['members'] == plan['shards'][name]['members']
            and (plan.get('kind') != 'report' or row['key'] == plan['shards'][name]['key'])}


def recover_partials(plan, rows, remote):
    missing = {name: shard for name, shard in plan['shards'].items() if name not in rows}
    if missing:
        rows.update(matching(plan, remote.partials(dict(plan, shards=missing))))
    return rows


def shard_fingerprint(mathlib, targets, members, kind=None):
    domain = SCHEMA if kind is None else [SCHEMA, kind]
    return digest([domain, mathlib, [(n, targets[n]['key']) for n in sorted(members)]])


def shard_row(mathlib, mathlib_version, targets, name, members, *, kind=None, compiler_key=None):
    members = sorted(members)
    key = compiler_key + '-report' if kind == 'report' else mathlib_version + '-' + name + '.tar.gz'
    if len(key) > 512:
        raise ValueError('Actions key exceeds 512 characters: ' + key)
    return {'members': members, 'key': key, 'asset': key,
            'fingerprint': shard_fingerprint(mathlib, targets, members, kind),
            **({'artifact_kind': kind} if kind is not None else {})}


def expanded(plan, splits):
    if not splits:
        return plan
    groups = dict(plan['shards'])
    targets = dict(plan['targets'])
    for name, layout in splits.items():
        expected = groups.pop(name)['members']
        if sorted(n for members in layout.values() for n in members) != expected or groups.keys() & layout.keys():
            raise ValueError('split layout differs from group membership: ' + name)
        for child, members in layout.items():
            groups[child] = shard_row(plan['mathlib'], plan.get('mathlib_version', plan['mathlib']), targets, child, members,
                                      kind=plan.get('kind'))
            for n in members:
                targets[n] = dict(targets[n], shard=child)
    return dict(plan, targets=targets, shards=groups)


def split_cold_reports(plan, existing, base, remote):
    """Keep each new compiler owner small enough to publish its paired report in one upload."""
    previous = base['targets'] if base else {}
    targets = plan['targets']
    splits = {}
    for name, shard in plan['shards'].items():
        members = shard['members']
        if name in existing or len(members) <= MAX_COLD_REPORTS_PER_SHARD:
            continue
        cold = set(members) if plan.get('kind') == 'compiler' else {
            n for n in members if 'report_inputs' in targets[n] and targets[n]['key'] != previous.get(n, {}).get('key')}
        if len(cold) > MAX_COLD_REPORTS_PER_SHARD:
            weights = {n: int(n in cold) for n in members}
            splits[name] = plan_shards(weights, MAX_COLD_REPORTS_PER_SHARD, previous={name: members})
    if not splits:
        return plan

    plan = expanded(plan, splits)
    # A child may itself have a measured byte split from an interrupted attempt.
    children = {child: plan['shards'][child] for layout in splits.values() for child in layout}
    plan = expanded(plan, remote.splits(dict(plan, shards=children)))
    if base:
        candidates = {n: r for n, r in matching(plan, base['shards']).items() if n not in existing}
        existing.update(remote.confirmed(candidates))
    recover_partials(plan, existing, remote)
    return plan


def make_plan(source, mathlib, targets, base, remote, capacity, workers, *, mathlib_version, legacy=None,
              prepare_release=False, kind=None, compiler_index=None, compiler_dag=False, modules=None, root=None):
    if compiler_dag and kind != 'compiler':
        raise ValueError('dependency scheduling requires a compiler publication')
    if workers < 1 or workers > 256:
        raise ValueError('worker count must be within the Actions matrix limit')
    version = VERSION_PREFIX + source + ('-report' if kind == 'report' else '')
    if base is not None and (kind == 'report' and base.get('kind') != 'report'
                             or kind == 'compiler' and base.get('kind') == 'report'):
        raise ValueError('publication base belongs to another artifact kind')
    if kind == 'report' and (not compiler_index or compiler_index['source'] != source
                             or compiler_index['mathlib'] != mathlib or compiler_index.get('kind') == 'report'
                             or compiler_index['version'] != VERSION_PREFIX + source):
        raise ValueError('report publication requires the published compiler index for this checkout')
    if kind == 'report':
        for name, row in targets.items():
            published = compiler_index['targets'].get(name, {})
            if published.get('compiler_input', published.get('key')) != row['compiler_input']:
                raise ValueError('published compiler input differs from report target: ' + name)
    if base is None or base['version'] != version:
        # A finalizer can stop after uploading index.json and before advancing the published ref.
        base = remote.staged_index(version) or base
    finished = base is not None and base['source'] == source and targets.keys() == base['targets'].keys()
    if kind == 'compiler' and base and base['source'] == source and not base.get('kind'):
        # A mixed index already owns this immutable tag. It supplies compiler artifacts through the old manifest.
        if any(n not in targets or row.get('compiler_input', row['key']) != targets[n]['key']
               for n, row in base['targets'].items()):
            raise ValueError('historical module publication differs from current compiler inputs')
        return {**base, 'kind': kind, 'historical_index': True, 'lanes': [], 'inherited': base['shards']}
    checkpoint = None if finished else remote.publication_layout(version)
    if checkpoint and checkpoint.get('kind') != kind:
        # A pre-split unfinished source owns these immutable shard addresses.
        raise ValueError('source already has a different module publication layout')
    if checkpoint is not None:
        original_base = checkpoint['base_version']
        if original_base != (base['version'] if base else None):
            base = remote.latest(original_base) if original_base else None
            if original_base and (not base or base['version'] != original_base):
                raise ValueError('missing original module publication base')
        capacity = checkpoint['capacity']
        legacy = checkpoint.get('legacy')
    previous = base['shards'] if base else {}
    sizes = {n: DEFAULT_MODULE_BYTES for n in targets}
    if legacy:
        sizes.update({n: row['bytes'] for n, row in legacy['targets'].items() if n in targets})
    for row in previous.values():
        average = max(1, row['bytes'] // len(row['members']))
        for name in row['members']:
            if name in sizes:
                sizes[name] = row.get('module_sizes', {}).get(name, average)
    boundaries = {n: r['members'] for n, r in previous.items()}
    if kind == 'report':
        layout = defaultdict(list)
        for name in sorted(targets):
            owner = compiler_index['targets'][name]['shard']
            layout[owner].append(name)
        if any(not set(members) <= set(compiler_index['shards'][name]['members']) for name, members in layout.items()):
            raise ValueError('published compiler shard disagrees with its target index')
        if checkpoint is not None and (checkpoint['groups'] != layout
                or checkpoint['fingerprint'] != shard_fingerprint(mathlib, targets, targets, kind)):
            raise ValueError('report publication layout differs from compiler groups or report inputs')
    elif finished:
        layout = boundaries
    elif checkpoint is not None:
        layout = checkpoint['groups']
        if (checkpoint['version'] != version or checkpoint['mathlib_version'] != mathlib_version
                or checkpoint['fingerprint'] != shard_fingerprint(mathlib, targets, targets, kind)
                or any(not NAME.fullmatch(name) or not isinstance(members, list) or not members
                       for name, members in layout.items())
                or sorted(n for members in layout.values() for n in members) != sorted(targets)):
            raise ValueError('module publication layout differs from checkout')
    else:
        layout = plan_shards(sizes, capacity, previous=boundaries)
    planned = {}
    targets = {n: dict(row) for n, row in targets.items()}
    for name, members in layout.items():
        compiler_key = compiler_index['shards'][name]['key'] if kind == 'report' else None
        planned[name] = shard_row(mathlib, mathlib_version, targets, name, members, kind=kind, compiler_key=compiler_key)
        for n in members:
            targets[n]['shard'] = name
    plan = {'schema': SCHEMA, 'source': source, 'mathlib': mathlib, 'targets': targets,
            'shards': planned, 'mathlib_version': mathlib_version, 'version': version, 'capacity': capacity}
    if kind:
        plan['kind'] = kind
    if kind == 'report':
        plan['compiler_version'] = compiler_index['version']
    if kind == 'compiler' and base:
        # Carry the last combined release until report publication has had a chance to migrate its ZIPs.
        combined = base['version'] if base.get('kind') is None else base.get('combined_report_version')
        if combined:
            plan['combined_report_version'] = combined
    plan['base_version'] = base['version'] if base else None
    plan['source_only'] = checkpoint is not None or finished
    if kind != 'report' and (checkpoint is not None or planned.keys() - matching(plan, previous).keys()):
        plan = expanded(plan, remote.splits(plan))
    inherited = {**previous, **checkpoint['partials']} if checkpoint is not None else previous
    existing = remote.confirmed(matching(plan, inherited))
    recover_partials(plan, existing, remote)
    if checkpoint is None and not finished and kind != 'report':
        plan = split_cold_reports(plan, existing, base, remote)
    planned = plan['shards']
    plan['inherited'] = existing
    missing = sorted(planned.keys() - existing.keys(), key=lambda s: (-sum(sizes[n] for n in planned[s]['members']), s))
    if prepare_release and checkpoint is None and not finished:
        # Workers address immutable assets by group name. Keep the first coordinates across base advances.
        remote.save_publication_layout(version, {
            'version': version, 'mathlib_version': mathlib_version, 'base_version': plan['base_version'],
            **({'kind': kind} if kind else {}),
            'capacity': capacity, 'partials': {n: r for n, r in existing.items() if previous.get(n) != r},
            **({'legacy': legacy} if legacy and missing else {}),
            'fingerprint': shard_fingerprint(mathlib, targets, targets, kind),
            'groups': {name: row['members'] for name, row in planned.items()},
        })
    # The planner already adopted all usable historical uploads. Workers now follow this source's writes.
    plan['source_only'] = True
    lanes = [[] for _ in range(min(len(missing), workers))]
    loads = [0] * len(lanes)
    for name in missing:
        lane = loads.index(min(loads))
        lanes[lane].append(name)
        loads[lane] += sum(sizes[n] for n in planned[name]['members'])
    plan['lanes'] = lanes
    if compiler_dag and missing:
        from lean_module_dag import prepare
        prepare(plan, base, remote, workers, prepare_release, modules, root)
    if legacy and lanes:
        plan['legacy'] = legacy
    return plan


def partial_index(plan, rows):
    return {'schema': SCHEMA, **({'kind': plan['kind']} if plan.get('kind') else {}), 'targets': {n: plan['targets'][n] for row in rows.values() for n in row['members']},
            'shards': rows}


def legacy_compiler_inputs(root, modules, targets, source):
    import lean_generation_plan as legacy
    graph = {n: set(r['deps']) for n, r in targets.items()}
    criteria = legacy.module_targets(root, modules, graph)
    domain, _ = legacy.package_inputs(root)
    remote = legacy.checkpoints.remote_for(root)
    empty = {'source': source, 'targets': {}, 'volumes': {}}
    rows, sizes = legacy.verified_inputs(remote, domain, criteria, empty)
    rows = {name: row for name, row in rows.items() if not modules[name].extra_builds}
    return dict(empty, targets=rows, volumes={row['volume']: sizes[row['volume']] for row in rows.values()})


def restore_legacy(remote, index, names, destination):
    from lean_cache_generation import restore as restore_frames
    rows = {n: index['targets'][n] for n in names & index['targets'].keys()}
    if not rows:
        return set()
    subset = {'source': index['source'], 'targets': rows,
              'volumes': {r['volume']: index['volumes'][r['volume']] for r in rows.values()}}
    report = restore_frames(remote, subset, subset, rows, destination)
    return set(rows) if report['restored'] == len(rows) else set()


def restore_artifacts(root, modules, targets, index, names, remote, destination, kind, *, restored=None):
    if restored is None:
        restored = set()
    if not index:
        return restored
    if index['schema'] != SCHEMA or index.get('kind') not in (None, kind):
        raise ValueError('unexpected ' + kind + ' publication schema')
    selected = defaultdict(list)
    for name in sorted(names & targets.keys() & index['targets'].keys()):
        row = index['targets'][name]
        expected = targets[name]
        if kind == 'compiler':
            previous_key, current_key = row.get('compiler_input', row['key']), expected.get('compiler_input', expected['key'])
        else:
            if not expected.get('key') or (index.get('kind') is None and 'report_inputs' not in row):
                continue
            previous_key, current_key = row['key'], expected['key']
        if previous_key == current_key:
            shard = row['shard']
            selected[shard].append(name)
    if selected:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            archive = Path(temporary) / 'shard.tar.gz'
            for name, members in selected.items():
                row = index['shards'][name]
                if not set(members) <= set(row['members']) or row.get('artifact_kind') != index.get('kind'):
                    raise ValueError('shard content differs from publication index')
                remote.download(row, archive)
                added = restore_shard(archive, row, members, modules, destination, root=root, kind=kind)
                if kind == 'compiler':
                    materialize_shared(destination, modules, added)
                restored.update(added)
    return restored


def measured_archives(root, modules, name, members, directory, plan):
    capacity = plan['capacity']
    path = directory / (name + '.tar.gz')
    kind = plan.get('kind')
    fingerprint = shard_fingerprint(plan['mathlib'], plan['targets'], members, kind)
    row = pack_shard(root, modules, members, path, fingerprint, kind=kind or 'combined')
    if kind == 'report':
        return {name: row}
    if row['bytes'] <= capacity or len(members) == 1:
        return {name: row}
    path.unlink()
    # Each step must reduce the member count even if the gzip header accounts for the excess.
    sizes = {n: max(1, size) for n, size in row['module_sizes'].items()}
    effective = min(capacity, sum(sizes.values()) - 1)
    layout = plan_shards(sizes, effective, previous={name: members})
    archives = {}
    for child, selected in layout.items():
        archives.update(measured_archives(root, modules, child, selected, directory, plan))
    return archives


def work(root, plan, lane, remote, output, *, compiler_remote=None):
    if not 0 <= lane < len(plan['lanes']) or compiler.checkout(root) != plan['source']:
        raise ValueError('worker checkout or lane differs from its plan')
    original = plan
    kind = plan.get('kind', 'compiler')
    splits = {} if kind == 'report' or 'compiler_dag' in original else remote.splits(original)
    plan = expanded(original, splits)
    targets = plan['targets']
    if 'compiler_dag' in plan:
        from lean_module_dag import worker_modules
        modules = worker_modules(root, plan['compiler_dag'])
    else:
        modules, checkout = checkout_targets(root, kind=kind)
        current = {n: r for n, r in checkout.items() if 'report_inputs' in r} if kind == 'report' else checkout
        if current != {name: {k: v for k, v in row.items() if k != 'shard'} for name, row in targets.items()}:
            raise ValueError('worker compiler inputs differ from its plan')
        graph = {n: set(row['deps']) for n, row in checkout.items()}
    compiler_index = None
    if kind == 'report':
        compiler_index = compiler_remote.published(plan['compiler_version'])
        if not compiler_index or compiler_index['source'] != plan['source']:
            raise ValueError('published compiler prerequisite unavailable')
    base = remote.latest(plan['base_version']) if plan['base_version'] and 'compiler_dag' not in plan else None
    combined = None
    if compiler_index and not base:
        if compiler_index.get('kind') is None:
            combined = compiler_index
        elif previous := compiler_index.get('combined_report_version'):
            try:
                combined = compiler_remote.published(previous)
                if combined and (combined.get('kind') is not None or combined['version'] != previous
                                 or combined['mathlib'] != plan['mathlib']):
                    raise ValueError('ineligible historical combined module publication')
            except REPORT_READ_ERRORS as error:
                print(f'Historical reports unavailable: {error}', file=sys.stderr)
                combined = None
    if 'compiler_dag' in original:
        assigned_rows = {n: plan['shards'][n] for n in original['lanes'][lane]}
        assigned_plan = dict(plan, shards=assigned_rows)
        splits = remote.splits(assigned_plan) if assigned_rows else {}
        plan = expanded(plan, splits)
        assigned_plan = expanded(assigned_plan, splits)
        complete = recover_partials(assigned_plan, matching(assigned_plan, plan['inherited']), remote)
    else:
        complete = recover_partials(plan, matching(plan, plan['inherited']), remote)
    installed = set()
    restored_reports = set()
    produced_reports = 0
    compiled = uploaded = 0
    common_prepared = False
    legacy = plan.get('legacy')
    if legacy and 'compiler_dag' not in plan:
        from lean_generation_checkpoints import remote_for
        old_remote = remote_for(root)
    dag_install = None
    if 'compiler_dag' in plan:
        from lean_module_dag import compile_units
        dag_install, installed, compiled = compile_units(root, plan, lane, remote, modules, output)
    assigned = [child for name in original['lanes'][lane] for child in splits.get(name, {name: None})]
    for name in assigned:
        if name in complete:
            continue
        shard = plan['shards'][name]
        members = set(shard['members'])
        destination = root / inventory.PROJECT_BUILD
        if kind == 'report':
            reports = restore_artifacts(root, modules, targets, base, members, remote, destination, 'report')
            if combined:
                try:
                    restore_artifacts(root, modules, targets, combined, members - reports, compiler_remote,
                                      destination, 'report', restored=reports)
                except REPORT_READ_ERRORS as error:
                    print(f'Historical report shard unavailable: {error}', file=sys.stderr)
            pending = members - reports
            if pending:
                inputs = {d for n in pending for d in targets[n]['report_inputs']}
                scope = inventory.import_closure(graph, inputs)
                installed.update(restore_artifacts(root, modules, checkout, compiler_index, scope - installed,
                                                  compiler_remote, destination, 'compiler'))
                if missing := scope - installed:
                    raise ValueError('published compiler prerequisite unavailable: ' + min(missing))
                from lean_generation_facets import materialize
                materialize(root, sorted(pending), inputs, prebuilt=True)
                produced_reports += len(pending)
            restored_reports.update(members)
        elif dag_install:
            dag_install(members)
        else:
            scope = inventory.import_closure(graph, members)
            for index in (partial_index(plan, complete), base):
                installed.update(restore_artifacts(root, modules, targets, index, scope - installed,
                                                   remote, destination, 'compiler'))
            if legacy:
                try:
                    candidates = {n for n in scope - installed if not modules[n].extra_builds}
                    recovered = restore_legacy(old_remote, legacy, candidates, destination)
                    materialize_shared(destination, modules, recovered)
                    installed.update(recovered)
                except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
                    print(f'Compiler seed unavailable; continuing with Lake: {error}', file=sys.stderr)
                    legacy = None
            if missing := scope - installed:
                roots = missing if any(modules[n].extra_builds for n in missing) else compiler.terminal(missing, graph)
                compiler.compile_roots(root, modules, sorted(roots))
                common_prepared = compile_omega_outputs(root, modules, missing, common_prepared)
                installed.update(missing)
                compiled += len(missing)
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            directory = Path(temporary)
            archives = measured_archives(root, modules, name, sorted(members), directory, plan)
            if len(archives) > 1:
                layout = {child: archive['members'] for child, archive in archives.items()}
                remote.save_split(original, name, layout)
                plan = expanded(plan, {name: layout})
            for child, manifest in archives.items():
                row = dict(plan['shards'][child], sha256=manifest['sha256'], bytes=manifest['bytes'],
                           version=plan['version'], module_sizes=manifest['module_sizes'])
                complete[child] = remote.save(plan, child, directory / (child + '.tar.gz'), row)
                uploaded += 1
        if shutil.disk_usage(root).free < 4 * 1024**3:
            for n in installed:
                for path in inventory.lean_artifact_paths(n, modules[n]):
                    (destination / path).unlink(missing_ok=True)
            if restored_reports:
                from lean_module_shards import report_paths
                for n in restored_reports:
                    for path in report_paths(n):
                        (destination / path).unlink(missing_ok=True)
                restored_reports.clear()
            installed.clear()
        write(output / 'worker.json', {'compiled_modules': compiled, 'saved_shards': uploaded})
    result = {'compiled_modules': compiled, 'saved_shards': uploaded}
    if kind == 'report':
        result['prepared_reports'] = produced_reports
    return result


def finish(plan, remote, *, stage_only=False):
    publish = remote.stage_index if stage_only else remote.publish
    staged = remote.staged_index(plan['version'])
    if plan.get('historical_index') and not staged:
        raise ValueError('historical compiler index unavailable')
    if staged is not None:
        # An earlier plan may have picked different physical shards for the same inputs.
        if staged.get('kind') != (None if plan.get('historical_index') else plan.get('kind')):
            raise ValueError('staged module publication kind differs from plan')
        if any(staged.get(field) != plan.get(field) for field in ('compiler_version', 'combined_report_version')):
            raise ValueError('staged publication input version differs from plan')
        targets = staged['targets']
        if (any(staged[k] != plan[k] for k in ('schema', 'source', 'version', 'mathlib'))
                or targets.keys() != plan['targets'].keys()):
            raise ValueError('staged module index differs from publication plan')
        members = defaultdict(list)
        for name, row in targets.items():
            planned = plan['targets'][name]
            if dict(row, shard=planned['shard']) != planned:
                raise ValueError('staged module inputs differ from publication plan: ' + name)
            members[row['shard']].append(name)
        if (members.keys() != staged['shards'].keys()
                or any(row['members'] != sorted(members[group])
                       or row['fingerprint'] != shard_fingerprint(plan['mathlib'], targets, members[group], staged.get('kind'))
                       for group, row in staged['shards'].items())):
            raise ValueError('staged module shards differ from indexed targets')
        publish(staged)
        return staged
    if plan.get('kind') != 'report' and plan['shards'].keys() - plan['inherited'].keys():
        plan = expanded(plan, remote.splits(plan))
    rows = recover_partials(plan, matching(plan, plan['inherited']), remote)
    missing = plan['shards'].keys() - rows.keys()
    if missing:
        raise ValueError(f'{len(missing)} planned shards unavailable, including {min(missing)}')
    index = {k: plan[k] for k in ('schema', 'source', 'version', 'mathlib', 'targets')}
    index.update((k, plan[k]) for k in ('kind', 'compiler_version', 'combined_report_version') if k in plan)
    index['shards'] = rows
    publish(index)
    return index


def restore(root, index, requested, remote, destination, *, kind=None, report_index=None, report_remote=None,
            load_reports=None):
    roots = set(requested or ())
    report_error = None
    try:
        modules, targets = checkout_targets(root, kind='compiler' if kind == 'compiler' else 'report',
                                            requested=requested, restore_only=True, scope_roots=roots)
    except REPORT_READ_ERRORS as error:
        if kind is not None:
            raise
        roots = set(requested or ())
        modules, targets = checkout_targets(root, kind='compiler', requested=requested)
        report_error = str(error)
    graph = {n: row['deps'] for n, row in targets.items()}
    wanted = inventory.import_closure(graph, roots & targets.keys()) if requested is not None else set(targets)
    reports = {n for n in wanted if modules[n].report}
    prerequisites = {d for n in reports for d in targets[n]['report_inputs'] if d in graph}
    supporting = inventory.import_closure(graph, prerequisites) - wanted
    compiled = set()
    if kind != 'report':
        compiled = restore_artifacts(root, modules, targets, index, wanted | supporting, remote, destination, 'compiler')
    restored = set()
    if kind != 'compiler' and reports:
        try:
            if load_reports is not None:
                report_index, report_remote = load_reports()
            restore_artifacts(root, modules, targets, report_index, reports, report_remote, destination, 'report',
                              restored=restored)
            if index.get('kind') is None:
                restore_artifacts(root, modules, targets, index, reports - restored, remote, destination, 'report',
                                  restored=restored)
        except REPORT_READ_ERRORS as error:
            if kind == 'report':
                raise
            report_error = str(error)
    if kind == 'report':
        return {'requested': len(reports), 'restored': len(restored), 'reports_requested': len(reports),
                'reports_restored': len(restored), 'source': index['source']}
    exact = wanted & compiled
    if kind is None:
        exact -= reports - restored
    result = {'requested': len(wanted), 'restored': len(exact), 'source': index['source']}
    if supporting:
        result.update(supporting_compilers_requested=len(supporting), supporting_compilers_restored=len(supporting & compiled))
    if reports or kind == 'compiler':
        result.update(compiler_restored=len(wanted & compiled))
    if reports:
        result.update(reports_requested=len(reports), reports_restored=len(restored))
    if report_error is not None:
        result.update(compiler_restored=len(wanted & compiled), report_error=report_error)
        print('Module report restore failed: ' + report_error, file=sys.stderr)
    return result


def validate_report_pair(index, compiler_index):
    if (index.get('kind') != 'report' or index.get('compiler_version') != compiler_index['version']
            or index['source'] != compiler_index['source'] or index['mathlib'] != compiler_index['mathlib']):
        raise ValueError('report publication differs from compiler release')
    owners = defaultdict(list)
    for name, row in index['targets'].items():
        compiler_row = compiler_index['targets'].get(name, {})
        if (compiler_row.get('compiler_input', compiler_row.get('key')) != row['compiler_input']
                or row['shard'] != compiler_row.get('shard')):
            raise ValueError('report publication differs from compiler target: ' + name)
        owners[row['shard']].append(name)
    if owners.keys() != index['shards'].keys():
        raise ValueError('report publication shard owners differ from targets')
    for name, members in owners.items():
        shard = index['shards'][name]
        compiler_shard = compiler_index['shards'][name]
        key = compiler_shard['key'] + '-report'
        if (shard['key'] != key or shard['asset'] != key or shard.get('artifact_kind') != 'report'
                or sorted(members) != shard['members'] or not set(members) <= set(compiler_shard['members'])
                or shard['fingerprint'] != shard_fingerprint(index['mathlib'], index['targets'], members, 'report')):
            raise ValueError('report shard differs from paired compiler shard: ' + name)


def restore_published(root, index, names, remote, destination, kind=None, *, exact=False,
                      report_remote=None, report_index=None):
    from lean_module_remote import Remote
    def load_reports():
        reader, reports = report_remote, report_index
        if reader is None:
            reader = Remote(root, remote.deadline, kind='report')
            reports = reader.published(index['version'] + '-report') if exact else reader.latest()
            if reports is None and not exact and index.get('combined_report_version'):
                mixed = remote.published(index['combined_report_version'])
                if mixed is not None:
                    if mixed.get('kind') is not None or mixed['mathlib'] != index['mathlib']:
                        raise ValueError('ineligible historical combined report publication')
                    return mixed, remote
        if reports is not None:
            version = reports['compiler_version']
            paired = index if version == index['version'] else remote.published(version)
            if paired is None:
                raise ValueError('published compiler prerequisite for the selected report index is unavailable')
            validate_report_pair(reports, paired)
        return reports, reader

    return restore(root, index, names, remote, destination, kind=kind, load_reports=load_reports)


def fetch_published(root, names, deadline, *, refresh_stale, version=None):
    from lean_cache_release import receipt
    from lean_module_remote import Remote
    remote = Remote(root, deadline)
    require_report = bool(version and version.endswith('-report'))
    reports = report_remote = None
    if require_report:
        report_remote = Remote(root, deadline, kind='report')
        reports = report_remote.published(version)
        if reports is None:
            return 1
        index = remote.published(reports['compiler_version'])
    else:
        index = remote.published(version) if version else remote.latest()
    if index is None:
        return None
    destination = root / inventory.PROJECT_BUILD
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('unsafe local compiler build directory')
    if not names and destination.is_dir() and any(destination.iterdir()) and not refresh_stale and not require_report:
        receipt('fetch', 'skipped', reason='local build already exists')
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary)
        output = destination if names else stage / 'build'
        if require_report:
            stats = restore_published(root, index, names or None, remote, destination, 'report', exact=True,
                                      report_remote=report_remote, report_index=reports)
            complete = stats['restored'] == stats['requested']
            receipt('fetch', 'scoped' if complete else 'miss', mode='report', producer_commit_sha=index['source'], **stats)
            return 0 if complete else 1
        stats = restore_published(root, index, names or None, remote, output, exact=version is not None)
        if not stats.get('compiler_restored', stats['restored']):
            receipt('fetch', 'miss', reason='no matching published compiler targets', **stats)
            return 1
        if not names:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            (output / '.release-refreshed-at').write_text(stamp + '\n')
            backup = stage / 'previous'
            try:
                if destination.exists():
                    destination.rename(backup)
                output.rename(destination)
            except BaseException:
                if backup.exists() and not destination.exists():
                    backup.rename(destination)
                raise
        receipt('fetch', 'scoped' if names else 'unpacked', mode='partition', producer_commit_sha=index['source'], **stats)
        return 0


def plan_publication(root, args, remote, source):
    from lean_module_remote import Remote
    modules, targets = checkout_targets(root, kind=args.kind)
    base = json.loads(args.base.read_bytes()) if args.base else remote.latest()
    compiler_index = None
    if args.kind == 'report':
        compiler_index = Remote(root).published(VERSION_PREFIX + source)
        if not compiler_index:
            raise ValueError('report publication requires a published compiler cache for ' + source)
        targets = {n: r for n, r in targets.items() if 'report_inputs' in r}
    legacy = None
    if args.kind == 'compiler' and args.legacy_checkpoints and not base:
        try:
            legacy = legacy_compiler_inputs(root, modules, targets, source)
        except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
            print(f'Compiler checkpoints unavailable; continuing with Lake: {error}', file=sys.stderr)
    mathlib, mathlib_version = publication_mathlib(root)
    return make_plan(source, mathlib, targets, base, remote, args.capacity, args.workers,
                       mathlib_version=mathlib_version, legacy=legacy, prepare_release=args.prepare_release,
                       kind=args.kind, compiler_index=compiler_index, compiler_dag=args.compiler_dag,
                       modules=modules, root=root)


def run():
    from lean_module_remote import Remote, publisher
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--base', type=Path)
    plan.add_argument('--kind', choices=('compiler', 'report'), default='compiler')
    plan.add_argument('--compiler-dag', action='store_true', help='schedule cold compiler units by dependency')
    plan.add_argument('--legacy-checkpoints', action='store_true')
    plan.add_argument('--prepare-release', action='store_true', help='create the shared Release before starting its workers')
    plan.add_argument('--capacity', type=int, default=DEFAULT_CAPACITY)
    plan.add_argument('--workers', type=int, default=20)
    plan.add_argument('--output', type=Path, required=True)
    worker = commands.add_parser('worker')
    worker.add_argument('--plan', type=Path, required=True)
    worker.add_argument('--lane', type=int, required=True)
    worker.add_argument('--output', type=Path, required=True)
    final = commands.add_parser('finish')
    final.add_argument('--plan', type=Path, required=True)
    final.add_argument('--output', type=Path, required=True)
    final.add_argument('--stage-only', action='store_true')
    promote = commands.add_parser('promote')
    promote.add_argument('--kind', choices=('compiler', 'report'), required=True)
    promote.add_argument('--version', required=True)
    get = commands.add_parser('restore')
    get.add_argument('--version')
    get.add_argument('--staged', action='store_true')
    get.add_argument('--kind', choices=('compiler', 'report'))
    roots = get.add_mutually_exclusive_group()
    roots.add_argument('--targets', nargs='*')
    roots.add_argument('--targets-file', type=Path)
    get.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = args.repository.resolve()
    if args.command == 'worker':
        plan = json.loads(args.plan.read_bytes())
        kind = plan['kind']
        remote = Remote(root, lane=args.lane, lanes=len(plan['lanes']), kind=kind)
        print(json.dumps(work(root, plan, args.lane, remote, args.output,
                              compiler_remote=Remote(root) if kind == 'report' else None)))
        return 0
    if args.command == 'plan':
        from lean_module_remote import COMPILER_PLAN
        remote = Remote(root, kind=args.kind)
        source = compiler.checkout(root)
        durable = args.compiler_dag and args.prepare_release and args.kind == 'compiler'
        result = None
        if durable:
            publisher('compiler')
            remote.clear_compiler_claims()
            version = VERSION_PREFIX + source
            result = remote.publication_layout(version, COMPILER_PLAN)
            if result is not None and (result.get('kind') != 'compiler' or result.get('source') != source
                                       or result.get('version') != version or result.get('schema') != SCHEMA):
                raise ValueError('saved compiler plan differs from pinned source')
        if result is None:
            result = plan_publication(root, args, remote, source)
            if durable:
                remote.save_publication_layout(version, result, COMPILER_PLAN)
        if args.prepare_release and result['lanes']:
            publisher(args.kind)
        write(args.output, result)
        if path := os.environ.get('GITHUB_OUTPUT'):
            with open(path, 'a') as output:
                print('matrix=' + json.dumps({'lane': list(range(len(result['lanes'])))}), file=output)
                print('has_work=' + str(bool(result['lanes'])).lower(), file=output)
        print(json.dumps({'shards': len(result['shards']), 'lanes': len(result['lanes'])}))
    elif args.command == 'finish':
        plan = json.loads(args.plan.read_bytes())
        if compiler.checkout(root) != plan['source']:
            raise ValueError('publisher checkout differs from plan')
        remote = Remote(root, kind=plan['kind'])
        write(args.output, finish(plan, remote, stage_only=args.stage_only))
        if 'compiler_dag' in plan:
            remote.clear_compiler_claims(plan['source'])
    elif args.command == 'promote':
        status = Remote(root, kind=args.kind).promote(args.version)
        print(json.dumps({'version': args.version, 'status': status}))
    else:
        if args.staged and not (args.kind and args.version):
            parser.error('--staged requires --kind and --version')
        if args.kind is None and args.version and args.version.endswith('-report'):
            args.kind = 'report'
        roots = args.targets_file.read_text().split() if args.targets_file else args.targets
        if roots == []:
            print(json.dumps({'requested': 0, 'restored': 0}))
            return 0
        remote = Remote(root)
        report_remote, reports = None, None
        if args.kind == 'report':
            report_remote = Remote(root, kind='report')
            if args.staged:
                reports = report_remote.staged_index(args.version)
            else:
                reports = report_remote.published(args.version) if args.version else report_remote.latest()
            if reports is None:
                return 3
            index = remote.published(reports['compiler_version'])
        else:
            if args.staged:
                index = remote.staged_index(args.version)
            else:
                index = remote.published(args.version) if args.version else remote.latest()
        if index is None:
            return 3
        stats = restore_published(root, index, roots, remote, args.destination or root / inventory.PROJECT_BUILD,
                                  args.kind, exact=args.version is not None, report_remote=report_remote, report_index=reports)
        print(json.dumps(stats))
        if args.kind and stats['restored'] != stats['requested']:
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
