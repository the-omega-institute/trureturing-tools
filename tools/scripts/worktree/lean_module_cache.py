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
from lean_module_shards import DEFAULT_CAPACITY, lookup_shard, pack_shard, plan_shards, restore_shard


SCHEMA = 'lean-module-shards-v1'
VERSION_PREFIX = 'lean-modules-'
DEFAULT_MODULE_BYTES = 712650


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
    touched = {}
    for name in TopologicalSorter(strict_graph).static_order():
        touched[name] = namespace(name) in OMEGA_LIBRARIES or any(touched[dep] for dep in strict_graph[name])
    # The strict check builds all Omega library and check modules. The dual
    # compilation also builds every downstream consumer and its local imports.
    consumers = {n for n in strict_graph if touched[n] and namespace(n) != 'OmegaChecks'}
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
        augmented[name] = replace(module, extra_builds=tuple(builds))
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
    common = {n for n in strict if len(modules[n].extra_builds) == 2}
    # The overlay configuration is owned by the consumer checkout. Use the
    # same implementation that the Omega audit uses for the real Lake builds.
    sys.path.insert(0, str(root / OMEGA_CHECKS))
    try:
        strict_lake = importlib.import_module('strict_lake')
        portable = importlib.import_module('portable')
        if strict_lake.ROOT != root.resolve() or portable.ROOT != root.resolve():
            raise ValueError('Omega overlay loaded from another checkout')
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


def checkout_targets(root):
    modules = inventory.workspace_modules(root)
    roots = inventory.release_roots(modules) | omega_roots(root, modules)
    graph = compiler.scoped_imports(root, modules, roots)
    modules, graph, omega_inputs = omega_build_plan(root, modules, graph)
    scope = set(graph)
    configs = {}
    pins = {}
    directories = {'trureturing': Path('.'), 'reg': Path('Reg'), 'leanInspectorInterface': inventory.INTERFACE,
                   'leanInspector': inventory.INSPECTOR, 'regInspector': inventory.REG_INSPECTOR}
    for package in {modules[n].package for n in scope}:
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
    return modules, targets


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


def make_plan(source, mathlib, targets, base, remote, capacity, workers, *, mathlib_version, legacy=None):
    if workers < 1 or workers > 256:
        raise ValueError('worker count must be within the Actions matrix limit')
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
    if base and base['source'] == source and targets.keys() == base['targets'].keys():
        layout = boundaries
    else:
        layout = plan_shards(sizes, capacity, previous=boundaries)
    planned = {}
    targets = {n: dict(row) for n, row in targets.items()}
    for name, members in layout.items():
        planned[name] = shard_row(mathlib, mathlib_version, targets, name, members)
        for n in members:
            targets[n]['shard'] = name
    plan = {'schema': SCHEMA, 'source': source, 'mathlib': mathlib, 'targets': targets,
            'shards': planned, 'mathlib_version': mathlib_version, 'version': VERSION_PREFIX + source, 'capacity': capacity}
    plan['base_version'] = base['version'] if base else None
    plan = expanded(plan, remote.splits(plan)) if planned.keys() - matching(plan, previous).keys() else plan
    planned = plan['shards']
    existing = remote.confirmed(matching(plan, previous))
    recover_partials(plan, existing, remote)
    plan['inherited'] = existing
    missing = sorted(planned.keys() - existing.keys(), key=lambda s: (-sum(sizes[n] for n in planned[s]['members']), s))
    lanes = [[] for _ in range(min(len(missing), workers))]
    loads = [0] * len(lanes)
    for name in missing:
        lane = loads.index(min(loads))
        lanes[lane].append(name)
        loads[lane] += sum(sizes[n] for n in planned[name]['members'])
    plan['lanes'] = lanes
    if legacy and lanes:
        plan['legacy'] = legacy
    return plan


def restore_matching(root, modules, targets, index, names, remote, destination):
    """Read one authenticated asset per selected shard, including old-version references."""
    if not index or not names:
        return set()
    if index.get('schema') != SCHEMA:
        raise ValueError('unsupported module cache index')
    owners = {n: t['shard'] for n, t in index['targets'].items()}
    groups = {s: set(row['members']) for s, row in index['shards'].items()}
    selected = defaultdict(list)
    for name in sorted(names & targets.keys() & index['targets'].keys()):
        if targets[name]['key'] != index['targets'][name]['key']:
            continue
        shard = lookup_shard(name, owners, groups)
        selected[shard].append(name)
    restored = set()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / 'shard.tar.gz'
        for shard, members in selected.items():
            row = index['shards'][shard]
            remote.download(row, archive)
            restored.update(restore_shard(archive, row, members, modules, destination, root=root))
            archive.unlink()
    return restored


def partial_index(plan, rows):
    return {'schema': SCHEMA, 'targets': {n: plan['targets'][n] for row in rows.values() for n in row['members']},
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
    if current != {name: {k: row[k] for k in ('key', 'deps')} for name, row in targets.items()}:
        raise ValueError('worker compiler inputs differ from its plan')
    graph = {n: set(row['deps']) for n, row in targets.items()}
    base = remote.latest(plan['base_version']) if plan['base_version'] else None
    complete = recover_partials(plan, matching(plan, plan['inherited']), remote)
    installed = set()
    compiled = uploaded = 0
    common_prepared = False
    legacy = plan.get('legacy')
    if legacy:
        from lean_generation_checkpoints import remote_for
        old_remote = remote_for(root)
    assigned = [child for name in original['lanes'][lane] for child in splits.get(name, {name: None})]
    for name in assigned:
        if name in complete:
            continue
        shard = plan['shards'][name]
        members = set(shard['members'])
        scope = inventory.import_closure(graph, members)
        destination = root / inventory.PROJECT_BUILD
        for index in (partial_index(plan, complete), base):
            installed.update(restore_matching(root, modules, targets, index, scope - installed, remote, destination))
        if legacy:
            try:
                installed.update(restore_legacy(old_remote, legacy, {n for n in scope - installed if not modules[n].extra_builds}, destination))
            except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
                print(f'Compiler seed unavailable; continuing with Lake: {error}', file=sys.stderr)
                legacy = None
        missing = scope - installed
        if missing:
            # The official and strict import graphs can have different edges.
            # Explicitly name the missing modules for each active backend.
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
            installed.clear()
        write(output / 'worker.json', {'compiled_modules': compiled, 'saved_shards': uploaded})
    return {'compiled_modules': compiled, 'saved_shards': uploaded}


def finish(plan, remote):
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
    modules, targets = checkout_targets(root)
    graph = {n: row['deps'] for n, row in targets.items()}
    wanted = inventory.import_closure(graph, set(requested) & targets.keys()) if requested is not None else set(targets)
    restored = restore_matching(root, modules, targets, index, wanted, remote, destination)
    return {'requested': len(wanted), 'restored': len(restored), 'source': index['source']}


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
        if not stats['restored']:
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
        modules, targets = checkout_targets(root)
        base = json.loads(args.base.read_bytes()) if args.base else remote.latest()
        source = compiler.checkout(root)
        legacy = None
        if args.legacy_checkpoints and not base:
            try:
                legacy = legacy_compiler_inputs(root, modules, targets, source)
            except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
                print(f'Compiler checkpoints unavailable; continuing with Lake: {error}', file=sys.stderr)
        mathlib, mathlib_version = publication_mathlib(root)
        result = make_plan(source, mathlib, targets, base, remote, args.capacity, args.workers,
                           mathlib_version=mathlib_version, legacy=legacy)
        if args.prepare_release and result['lanes']:
            publisher()
            remote.catalog(result['version'], refresh=True, create=True)
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
