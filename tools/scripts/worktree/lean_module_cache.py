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
import shutil
import tomllib

import lean_artifact_inventory as inventory
import lean_compile_layers as compiler
from lean_cache import publication_mathlib, seed_partitions
from lean_generation_plan import digest, file_digest, module_config, write
from lean_module_shards import DEFAULT_CAPACITY, NAME, lookup_shard, pack_shard, plan_shards, restore_shard


SCHEMA = 'lean-module-shards-v1'
VERSION_PREFIX = 'lean-modules-'
DEFAULT_MODULE_BYTES = 712650
MAX_COLD_REPORTS_PER_SHARD = 250


OMEGA_BASIS = {'OmegaInit', 'OmegaMathlib'}
OMEGA_LIBRARIES = OMEGA_BASIS | {'OmegaMath'}
OMEGA_ROOTS = OMEGA_LIBRARIES | {'OmegaChecks'}
OMEGA_OVERLAYS = ('omega-strict', 'omega-common')
OMEGA_CHECKS = Path('src/OmegaChecks')
OMEGA_CERTIFICATE = 'OmegaChecks.Interface.Equations'


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
    # The command is a Lean elaborator. Its import makes selection possible;
    # only the official compiled metadata can say that it was actually invoked.
    command = 'OmegaBuild.Commands'
    may_select = {command}
    for name in TopologicalSorter(graph).static_order():
        if graph[name] & may_select:
            may_select.add(name)
    touched = {}
    for name in TopologicalSorter(strict_graph).static_order():
        touched[name] = namespace(name) in OMEGA_LIBRARIES or name == command or any(touched[dep] for dep in strict_graph[name])
    # The strict check builds all Omega library and check modules. The dual
    # compilation also builds every downstream consumer and its local imports.
    consumers = {n for n in strict_graph if touched[n] and namespace(n) not in {'OmegaChecks', 'OmegaBuild'}}
    strict = inventory.import_closure(strict_graph, consumers | omega_roots(root, modules))
    clients = {n for n in strict_graph if n.startswith('OmegaChecks.Portable.') and touched[n]}
    common_roots = basis | clients | ({OMEGA_CERTIFICATE} & graph.keys())
    common = inventory.import_closure(strict_graph, common_roots) | inventory.import_closure(graph, common_roots)
    strict.update(common)

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
        reader = compiler.scoped_imports(root, modules, {'OmegaBuild.Plan'})
        selection_inputs = digest([
            file_digest(root / OMEGA_CHECKS / 'portable.py'),
            [(name, file_digest(root / modules[name].source)) for name in sorted(reader)],
        ])
    augmented = dict(modules)
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


def compile_omega_outputs(root, modules, names, common_prepared):
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
            skipped = portable.backend_plan(search_path, batch)['mathlibOnly']
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


def checkout_targets(root, *, requested=None, restore_only=False, scope_roots=None):
    modules = inventory.workspace_modules(root)
    roots = inventory.release_roots(modules) | omega_roots(root, modules)
    graph = compiler.scoped_imports(root, modules, roots)
    modules, graph, omega_inputs = omega_build_plan(root, modules, graph)
    scope = set(graph)
    reported = {}
    selected = {}
    utilities = {}
    if (root / 'lean-report-inputs.json').is_file():
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
            if 'LeanInformationAudit.TemplateEnrollment' in modules:
                report_roots.add('LeanInformationAudit.TemplateEnrollment')
            # Report-only compiler prerequisites do not introduce report-to-report edges.
            graph = {**compiler.scoped_imports(root, modules, report_roots - scope), **graph}
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
    if any(os.environ.get(name) for name in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'LEAN_OPTS')):
        raise ValueError('compiler paths or options overridden outside the Lake project')
    environment = [(root / 'lean-toolchain').read_text().strip(), sorted(seed_partitions(root)),
                   os.environ.get('ELAN_TOOLCHAIN', '')]

    targets = {}
    for name in TopologicalSorter(graph).static_order():
        module = modules[name]
        dependencies = sorted(graph[name])
        source = file_digest(root / module.source)
        inputs = [environment, module.package, pins[module.package], str(module.source), str(module.build),
                  module_config(configs[module.package], name), source, [(d, targets[d]['key']) for d in dependencies]]
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
        # Older readers validate every member of a tar against a compiler-only allowlist.
        # A compiler-only sibling must not pick a mixed namespace's archive at the old identity.
        namespaces = {namespace(n) for n in reported}
        for name in scope - reported.keys():
            if namespace(name) in namespaces:
                row = targets[name]
                targets[name] = dict(row, key=digest(['module-report-shard', row['key']]), compiler_input=row['key'])
    return modules, {name: targets[name] for name in sorted(scope)}


def matching(plan, rows):
    return {name: row for name, row in rows.items() if name in plan['shards']
            and row['fingerprint'] == plan['shards'][name]['fingerprint']
            and row['members'] == plan['shards'][name]['members']}


def recover_partials(plan, rows, remote):
    missing = {name: shard for name, shard in plan['shards'].items() if name not in rows}
    if missing:
        rows.update(matching(plan, remote.partials(dict(plan, shards=missing))))
    return rows


def shard_fingerprint(mathlib, targets, members):
    return digest([SCHEMA, mathlib, [(n, targets[n]['key']) for n in sorted(members)]])


def shard_row(mathlib, mathlib_version, targets, name, members):
    members = sorted(members)
    key = mathlib_version + '-' + name + '.tar.gz'
    if len(key) > 512:
        raise ValueError('Actions key exceeds 512 characters: ' + key)
    return {'members': members, 'key': key, 'asset': key,
            'fingerprint': shard_fingerprint(mathlib, targets, members)}


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
            groups[child] = shard_row(plan['mathlib'], plan.get('mathlib_version', plan['mathlib']), targets, child, members)
            for n in members:
                targets[n] = dict(targets[n], shard=child)
    return dict(plan, targets=targets, shards=groups)


def split_cold_reports(plan, existing, base, remote):
    """Checkpoint bounded groups of reports that workers must produce before uploading a shard."""
    previous = base['targets'] if base else {}
    targets = plan['targets']
    splits = {}
    for name, shard in plan['shards'].items():
        members = shard['members']
        if name in existing or len(members) <= MAX_COLD_REPORTS_PER_SHARD:
            continue
        cold = {n for n in members if 'report_inputs' in targets[n]
                and targets[n]['key'] != previous.get(n, {}).get('key')}
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


def make_plan(source, mathlib, targets, base, remote, capacity, workers, *, mathlib_version,
              prepare_release=False):
    if workers < 1 or workers > 256:
        raise ValueError('worker count must be within the Actions matrix limit')
    version = VERSION_PREFIX + source
    if base is None or base['version'] != version:
        # A finalizer can stop after uploading index.json and before advancing the published ref.
        base = remote.staged_index(version) or base
    finished = base is not None and base['source'] == source and targets.keys() == base['targets'].keys()
    checkpoint = None if finished else remote.publication_layout(version)
    if checkpoint is not None:
        original_base = checkpoint['base_version']
        if original_base != (base['version'] if base else None):
            base = remote.latest(original_base) if original_base else None
            if original_base and (not base or base['version'] != original_base):
                raise ValueError('missing original module publication base')
        capacity = checkpoint['capacity']
    previous = base['shards'] if base else {}
    sizes = {n: DEFAULT_MODULE_BYTES for n in targets}
    for row in previous.values():
        average = max(1, row['bytes'] // len(row['members']))
        for name in row['members']:
            if name in sizes:
                sizes[name] = row.get('module_sizes', {}).get(name, average)
    boundaries = {n: r['members'] for n, r in previous.items()}
    if finished:
        layout = boundaries
    elif checkpoint is not None:
        layout = checkpoint['groups']
        if (checkpoint['version'] != version or checkpoint['mathlib_version'] != mathlib_version
                or checkpoint['fingerprint'] != shard_fingerprint(mathlib, targets, targets)
                or any(not NAME.fullmatch(name) or not isinstance(members, list) or not members
                       for name, members in layout.items())
                or sorted(n for members in layout.values() for n in members) != sorted(targets)):
            raise ValueError('module publication layout differs from checkout')
    else:
        layout = plan_shards(sizes, capacity, previous=boundaries)
    planned = {}
    targets = {n: dict(row) for n, row in targets.items()}
    for name, members in layout.items():
        planned[name] = shard_row(mathlib, mathlib_version, targets, name, members)
        for n in members:
            targets[n]['shard'] = name
    plan = {'schema': SCHEMA, 'source': source, 'mathlib': mathlib, 'targets': targets,
            'shards': planned, 'mathlib_version': mathlib_version, 'version': version, 'capacity': capacity}
    plan['base_version'] = base['version'] if base else None
    plan['source_only'] = checkpoint is not None or finished
    if checkpoint is not None or planned.keys() - matching(plan, previous).keys():
        plan = expanded(plan, remote.splits(plan))
    inherited = {**previous, **checkpoint['partials']} if checkpoint is not None else previous
    existing = remote.confirmed(matching(plan, inherited))
    recover_partials(plan, existing, remote)
    if checkpoint is None and not finished:
        plan = split_cold_reports(plan, existing, base, remote)
    planned = plan['shards']
    plan['inherited'] = existing
    missing = sorted(planned.keys() - existing.keys(), key=lambda s: (-sum(sizes[n] for n in planned[s]['members']), s))
    if prepare_release and checkpoint is None and not finished:
        # Workers address immutable assets by group name. Keep the first coordinates across base advances.
        remote.save_publication_layout(version, {
            'version': version, 'mathlib_version': mathlib_version, 'base_version': plan['base_version'],
            'capacity': capacity, 'partials': {n: r for n, r in existing.items() if previous.get(n) != r},
            'fingerprint': shard_fingerprint(mathlib, targets, targets),
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
    return plan


def restore_matching(root, modules, targets, index, names, remote, destination, *,
                     compiler_seeds=None, compiler_only=(), available_compilers=()):
    """Read one authenticated asset per selected shard, including old-version references."""
    if not index or not names:
        return set()
    if index.get('schema') != SCHEMA:
        raise ValueError('unsupported module cache index')
    owners = {n: t['shard'] for n, t in index['targets'].items()}
    groups = {s: set(row['members']) for s, row in index['shards'].items()}
    selected = defaultdict(list)
    seed_only = set()
    for name in sorted(names & targets.keys() & index['targets'].keys()):
        previous = index['targets'][name]
        if name in compiler_only or targets[name]['key'] != previous['key']:
            if name in available_compilers:
                continue
            # Report inputs may change independently of the compiler output in the same shard.
            current_compiler = targets[name].get('compiler_input', targets[name]['key'])
            if compiler_seeds is None or previous.get('compiler_input', previous['key']) != current_compiler:
                continue
            seed_only.add(name)
        shard = lookup_shard(name, owners, groups)
        selected[shard].append(name)
    restored = set()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / 'shard.tar.gz'
        for shard, members in selected.items():
            row = index['shards'][shard]
            remote.download(row, archive)
            selected_seeds = seed_only.intersection(members)
            installed = restore_shard(archive, row, members, modules, destination, root=root,
                                      compiler_only=selected_seeds, compiler_hits=compiler_seeds)
            missing_reports = {n for n in installed & selected_seeds if modules[n].report}
            restored.update(installed - missing_reports)
            if compiler_seeds is not None:
                compiler_seeds.update(missing_reports)
            archive.unlink()
    return restored


def partial_index(plan, rows):
    return {'schema': SCHEMA, 'targets': {n: plan['targets'][n] for row in rows.values() for n in row['members']},
            'shards': rows}


def measured_archives(root, modules, name, members, directory, plan):
    capacity = plan['capacity']
    path = directory / (name + '.tar.gz')
    row = pack_shard(root, modules, members, path, shard_fingerprint(plan['mathlib'], plan['targets'], members))
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


def work(root, plan, lane, remote, output):
    if not 0 <= lane < len(plan['lanes']) or compiler.checkout(root) != plan['source']:
        raise ValueError('worker checkout or lane differs from its plan')
    original = plan
    splits = remote.splits(original)
    plan = expanded(original, splits)
    modules, current = checkout_targets(root)
    targets = plan['targets']
    if current != {name: {k: v for k, v in row.items() if k != 'shard'} for name, row in targets.items()}:
        raise ValueError('worker compiler inputs differ from its plan')
    graph = {n: set(row['deps']) for n, row in targets.items()}
    base = remote.latest(plan['base_version']) if plan['base_version'] else None
    complete = recover_partials(plan, matching(plan, plan['inherited']), remote)
    installed = set()
    restored_reports = set()
    produced_reports = 0
    compiled = uploaded = 0
    common_prepared = False
    assigned = [child for name in original['lanes'][lane] for child in splits.get(name, {name: None})]
    for name in assigned:
        if name in complete:
            continue
        shard = plan['shards'][name]
        members = set(shard['members'])
        scope = inventory.import_closure(graph, members)
        destination = root / inventory.PROJECT_BUILD
        for index in (partial_index(plan, complete), base):
            seeds = set()
            missing_reports = {n for n in members - restored_reports if modules[n].report}
            exact = restore_matching(root, modules, targets, index, (scope - installed) | missing_reports,
                                     remote, destination, compiler_seeds=seeds,
                                     compiler_only=scope - members, available_compilers=installed)
            restored_reports.update(n for n in exact if modules[n].report)
            installed.update(exact | seeds)
        missing = scope - installed
        if missing:
            # The official and strict import graphs can have different edges.
            # Explicitly name the missing modules for each active backend.
            roots = missing if any(modules[n].extra_builds for n in missing) else compiler.terminal(missing, graph)
            compiler.compile_roots(root, modules, sorted(roots))
            common_prepared = compile_omega_outputs(root, modules, missing, common_prepared)
            installed.update(missing)
            compiled += len(missing)
        reports = sorted(n for n in members - restored_reports if modules[n].report)
        if reports:
            from lean_generation_facets import materialize
            inputs = {dep for n in reports for dep in targets[n]['report_inputs']}
            materialize(root, reports, inputs)
            restored_reports.update(reports)
            produced_reports += len(reports)
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
    if produced_reports:
        result['prepared_reports'] = produced_reports
    return result


def finish(plan, remote):
    staged = remote.staged_index(plan['version'])
    if staged is not None:
        # An earlier plan may have picked different physical shards for the same inputs.
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
                       or row['fingerprint'] != shard_fingerprint(plan['mathlib'], targets, members[group])
                       for group, row in staged['shards'].items())):
            raise ValueError('staged module shards differ from indexed targets')
        remote.publish(staged)
        return staged
    if plan['shards'].keys() - plan['inherited'].keys():
        plan = expanded(plan, remote.splits(plan))
    rows = recover_partials(plan, matching(plan, plan['inherited']), remote)
    missing = plan['shards'].keys() - rows.keys()
    if missing:
        raise ValueError(f'{len(missing)} planned shards unavailable, including {min(missing)}')
    index = {k: plan[k] for k in ('schema', 'source', 'version', 'mathlib', 'targets')}
    index['shards'] = rows
    remote.publish(index)
    return index


def restore(root, index, requested, remote, destination):
    roots = set(requested or ())
    modules, targets = checkout_targets(root, requested=requested, restore_only=True, scope_roots=roots)
    graph = {n: row['deps'] for n, row in targets.items()}
    wanted = inventory.import_closure(graph, roots & targets.keys()) if requested is not None else set(targets)
    prerequisites = {d for n in wanted for d in targets[n].get('report_inputs', ()) if d in graph}
    supporting = inventory.import_closure(graph, prerequisites) - wanted
    seeds = set()
    restored = restore_matching(root, modules, targets, index, wanted | supporting, remote, destination,
                                compiler_seeds=seeds, compiler_only=supporting)
    result = {'requested': len(wanted), 'restored': len(wanted & restored), 'source': index['source']}
    if supporting:
        result.update(supporting_compilers_requested=len(supporting),
                      supporting_compilers_restored=len(supporting & (restored | seeds)))
    reports = {n for n in wanted if modules[n].report}
    if reports:
        result.update(reports_requested=len(reports), reports_restored=len(reports & restored),
                      compiler_restored=len(wanted & (restored | seeds)))
    return result


def fetch_published(root, names, deadline, *, refresh_stale, version=None):
    from lean_cache_release import receipt
    from lean_module_remote import Remote
    remote = Remote(root, deadline)
    index = remote.latest(version)
    if index is None:
        return None
    destination = root / inventory.PROJECT_BUILD
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('unsafe local compiler build directory')
    if not names and destination.is_dir() and any(destination.iterdir()) and not refresh_stale:
        receipt('fetch', 'skipped', reason='local build already exists')
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary)
        output = destination if names else stage / 'build'
        stats = restore(root, index, names or None, remote, output)
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


def run():
    from lean_module_remote import Remote, publisher
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--base', type=Path)
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
    get = commands.add_parser('restore')
    get.add_argument('--version')
    roots = get.add_mutually_exclusive_group()
    roots.add_argument('--targets', nargs='*')
    roots.add_argument('--targets-file', type=Path)
    get.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = args.repository.resolve()
    if args.command == 'worker':
        plan = json.loads(args.plan.read_bytes())
        remote = Remote(root, lane=args.lane, lanes=len(plan['lanes']))
        print(json.dumps(work(root, plan, args.lane, remote, args.output)))
        return 0
    remote = Remote(root)
    if args.command == 'plan':
        _, targets = checkout_targets(root)
        base = json.loads(args.base.read_bytes()) if args.base else remote.latest()
        source = compiler.checkout(root)
        mathlib, mathlib_version = publication_mathlib(root)
        result = make_plan(source, mathlib, targets, base, remote, args.capacity, args.workers,
                           mathlib_version=mathlib_version, prepare_release=args.prepare_release)
        if args.prepare_release and result['lanes']:
            publisher()
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
        write(args.output, finish(plan, remote))
    else:
        roots = args.targets_file.read_text().split() if args.targets_file else args.targets
        if roots == []:
            print(json.dumps({'requested': 0, 'restored': 0}))
            return 0
        index = remote.latest(args.version)
        if index is None:
            return 3
        print(json.dumps(restore(root, index, roots, remote, args.destination or root / inventory.PROJECT_BUILD)))
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
