"""Schedule cold compiler modules separately from published namespace shards."""

import base64
from collections import defaultdict
from dataclasses import replace
from graphlib import TopologicalSorter
import math
import os
import json
from pathlib import Path
import sys
import tempfile
import zlib

import lean_compile_layers as compiler
from lean_artifact_inventory import import_closure
from lean_generation_plan import digest
from lean_release_index import safe_path, valid_digest

UNIT_LIMIT = 32
BASIS = {'OmegaInit', 'OmegaMathlib'}
QUEUE_HANDOFF_COST = 5


def unit_key(mathlib, inputs):
    return digest(['lean-compiler-unit-v2', mathlib, sorted(inputs.items())])


def unit_row(key, members):
    asset = 'compiler-unit-' + key + '.tar.gz'
    return dict(key=asset, asset=asset, fingerprint=key, members=sorted(members), artifact_kind='compiler')


def assign_queues(units, count):
    """List-schedule in module units, charging a transfer between distinct queues."""
    available = [0] * count
    finish = {}
    siblings = defaultdict(set)
    for key in sorted(units, key=lambda k: (-units[k]['rank'], k)):
        unit = units[key]
        def start(queue):
            ready = available[queue]
            for dep in unit['deps']:
                handoff = QUEUE_HANDOFF_COST if units[dep]['queue'] != queue else 0
                ready = max(ready, finish[dep] + handoff)
            return ready
        # Independent children with the same prerequisites can build simultaneously.
        used = siblings[tuple(unit['deps'])]
        if len(used) == count:
            used.clear()
        begins, _, queue = min((start(q), available[q], q) for q in range(count) if q not in used)
        used.add(queue)
        unit['queue'] = queue
        finish[key] = available[queue] = begins + len(unit['members'])


def group_units(graph, cold, basis, workers, cold_edges):
    """Fold the common facade generator's simultaneous basis build into one vertex."""
    foundation = import_closure(graph, basis) & cold
    anchor = min(foundation) if foundation else None

    def owner(n):
        return anchor if n in foundation else n

    edges = defaultdict(set)
    for n in cold:
        parent = owner(n)
        edges[parent].update(owner(d) for d in cold_edges[n] if owner(d) != parent)
    sorter = TopologicalSorter(edges)
    sorter.prepare()
    groups = []
    narrow = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        sorter.done(*ready)
        if anchor in ready:
            if narrow:
                groups.append(narrow)
                narrow = []
            groups.append(sorted(foundation))
            ready.remove(anchor)
        if len(ready) == 1:
            narrow.extend(ready)
            if len(narrow) == UNIT_LIMIT:
                groups.append(narrow)
                narrow = []
            continue
        if narrow:
            groups.append(narrow)
            narrow = []
        width = min(UNIT_LIMIT, max(1, math.ceil(len(ready) / workers)))
        groups.extend(ready[i:i + width] for i in range(0, len(ready), width))
    if narrow:
        groups.append(narrow)
    return groups


def matching_seeds(targets, indexes):
    """Retain a single confirmed archive per target, including mixed historical indexes."""
    rows, owners = {}, {}
    for index in indexes:
        if not index:
            continue
        for n, t in index['targets'].items():
            if n in owners or n not in targets or t.get('compiler_input', t['key']) != targets[n]['key']:
                continue
            row = index['shards'].get(t['shard'])
            if not row:
                continue
            key = row['version'] + '/' + row['asset']
            rows[key] = row
            owners[n] = key
    return {'rows': rows, 'owners': owners}


def recovery_index(plan, catalogs, assets):
    """Recover individual matching members even if a later unit has different membership."""
    targets, shards = {}, {}
    for catalog in catalogs:
        if catalog['mathlib'] != plan['mathlib']:
            continue
        for key, inputs in catalog['units'].items():
            if unit_key(catalog['mathlib'], inputs) != key:
                # Earlier unit archives did not verify each ordinary module against its Lake trace.
                previous = digest(['lean-compiler-unit-v1', catalog['mathlib'], sorted(inputs.items())])
                if key == previous:
                    continue
                raise ValueError('compiler unit inputs differ from catalog')
            row = unit_row(key, inputs)
            address = assets.get(row['asset'])
            if address is None:
                continue
            version, asset = address
            sha = (asset.get('digest') or '').removeprefix('sha256:')
            if asset['state'] != 'uploaded' or not valid_digest(sha) or asset['size'] <= 0:
                continue
            for n, compiler_key in inputs.items():
                if n not in targets and plan['targets'].get(n, {}).get('key') == compiler_key:
                    shards[key] = dict(row, version=version, sha256=sha, bytes=asset['size'])
                    targets[n] = dict(key=compiler_key, shard=key)
    return dict(targets=targets, shards=shards)


def encode_scope(mask):
    data = mask.to_bytes((mask.bit_length() + 7) // 8, 'little')
    return base64.b64encode(zlib.compress(data, level=1)).decode('ascii')


def decode_scope(encoded, names):
    data = zlib.decompress(base64.b64decode(encoded, validate=True))
    return {names[8 * i + bit] for i, byte in enumerate(data) if byte
            for bit in range(8) if byte & (1 << bit)}


def module_snapshot(root, modules, targets):
    from lean_module_cache import compiler_environment, file_digest
    from lean_artifact_inventory import INSPECTOR, INTERFACE, REG_INSPECTOR
    metadata = {}
    for n in targets:
        module = modules[n]
        metadata[n] = [module.package, str(module.source), str(module.build),
                       list(map(str, module.extra_builds)), module.conditional_backends, list(map(str, module.shared_builds)), module.plugin_input]
    pin = Path('Meta/tools-release.json')
    files = set()
    if (root / pin).is_file():
        files.add(pin)
        release_digest = json.loads((root / pin).read_bytes())['sha256']
        if not valid_digest(release_digest):
            raise ValueError('invalid tools release pin')
        files.add(Path('build/tools-releases') / (release_digest + '.tar.gz'))
        files.update(m.source for n, m in modules.items() if n in targets
                     and (m.source.is_relative_to(INSPECTOR) or m.source.is_relative_to(INTERFACE)))
        for package in (INSPECTOR, INTERFACE, REG_INSPECTOR):
            files.update(package / name for name in ('lakefile.toml', 'lakefile.lean', 'lake-manifest.json')
                         if (root / package / name).is_file())
    return dict(modules=metadata, files={str(p): file_digest(root / p) for p in sorted(files)},
                environment=compiler_environment(root))


def worker_modules(root, dag):
    from lean_artifact_inventory import Module
    from lean_module_cache import compiler_environment, file_digest
    if compiler_environment(root) != dag['environment']:
        raise ValueError('compiler environment differs from plan')
    for name, expected in dag['files'].items():
        path = root / name
        if path.is_symlink() or file_digest(path) != expected:
            raise ValueError('compiler input differs from plan: ' + name)
    return {n: Module(package, Path(source), Path(build), tuple(map(Path, extra)), conditional_backends=conditional,
                      shared_builds=tuple(map(Path, shared)), plugin_input=plugin)
            for n, (package, source, build, extra, conditional, shared, plugin) in dag['modules'].items()}


def prepare(plan, base, remote, workers, prepare_release, modules, root):
    from lean_module_cache import BACKEND_READER, partial_index
    targets = plan['targets']
    checked_base = dict(base, shards=remote.confirmed(base['shards'])) if base else None
    recovered = remote.compiler_seeds(plan)
    seeds = matching_seeds(targets, [partial_index(plan, plan['inherited']), checked_base, recovered])
    cold = targets.keys() - seeds['owners'].keys()
    graph = {n: set(t['deps']) for n, t in targets.items()}
    for n in targets:
        if modules[n].conditional_backends:
            graph[n].add(BACKEND_READER)
    facade_consumers = {n for n in targets if len(modules[n].extra_builds) == 2}
    basis = {n for n in targets if n.partition('.')[0] in BASIS} if facade_consumers else set()
    # Common facade generation reads all official and strict basis interfaces.
    foundation = import_closure(graph, basis)
    for n in facade_consumers - foundation:
        graph[n].update(basis)
    names = sorted(targets)
    ordinal = {n: i for i, n in enumerate(names)}
    closure = {}
    cold_edges = {}
    # Bypass warm vertices for scheduling, while keeping their files in the restore scope.
    for n in TopologicalSorter(graph).static_order():
        parents = set()
        mask = 1 << ordinal[n]
        for dep in graph[n]:
            mask |= closure[dep]
            if dep in cold:
                parents.add(dep)
            else:
                parents.update(cold_edges[dep])
        cold_edges[n] = parents
        closure[n] = mask
    groups = group_units(graph, cold, basis, workers, cold_edges)
    units = {}
    owner = {}
    catalog = {'mathlib': plan['mathlib'], 'units': {}}
    for members in groups:
        inputs = {n: targets[n]['key'] for n in members}
        key = unit_key(plan['mathlib'], inputs)
        catalog['units'][key] = inputs
        owner.update(dict.fromkeys(members, key))
        units[key] = unit_row(key, members)
    foundation_scope = sum(1 << ordinal[n] for n in foundation)
    # Every cold dependency below a warm intermediary must still be installed
    # before Lake can consume the intermediary's restored artifact.
    ancestors = {}
    successors = defaultdict(list)
    # Groups were emitted in topological order. Compress transitive edges while
    # preserving cold dependencies reached through any number of warm modules.
    for members in groups:
        key = owner[members[0]]
        unit = units[key]
        scope = foundation_scope if basis.intersection(members) else 0
        for n in members:
            scope |= closure[n]
        scope &= ~sum(1 << ordinal[n] for n in members)
        unit['restore'] = encode_scope(scope)
        deps = {owner[d] for n in members for d in cold_edges[n]} - {key}
        indirect = set()
        for d in deps:
            indirect.update(ancestors[d])
        unit['deps'] = sorted(deps - indirect)
        ancestors[key] = deps | indirect
        for d in unit['deps']:
            successors[d].append(key)
        roots = members if any(modules[n].extra_builds for n in members) else compiler.terminal(set(members), graph)
        unit['roots'] = sorted(roots)
    for key in reversed([owner[m[0]] for m in groups]):
        units[key]['next'] = sorted(successors[key])
        units[key]['rank'] = 1 + max((units[k]['rank'] for k in successors[key]), default=0)
    assign_queues(units, workers)
    plan['compiler_dag'] = dict(seeds=seeds, units=units, names=names,
                                **module_snapshot(root, modules, targets))
    if catalog['units'] and prepare_release:
        remote.save_compiler_catalog(plan['version'], catalog)
    # Even one physical shard may have enough independent modules for many runners.
    jobs = min(workers, max(len(units), len(plan['lanes'])))
    plan['lanes'].extend([] for _ in range(jobs - len(plan['lanes'])))


def compile_units(root, plan, lane, remote, modules, output):
    import lean_module_cache as cache
    from lean_cache_release import has_unsafe_parent
    from lean_module_remote import CompilerCacheMiss
    from lean_module_shards import checked_shard_files, pack_shard, restore_shard
    dag = plan['compiler_dag']
    units = dag['units']
    run = os.environ.get('GITHUB_RUN_ID')
    retry = int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')) > 1 or (run and run != plan.get('planner_run', run))
    owners = dict(dag['seeds']['owners'])
    rows = dict(dag['seeds']['rows'])
    installed = set()
    destination = root / cache.inventory.PROJECT_BUILD
    common_prepared = False
    seed_members = defaultdict(set)
    for n, address in owners.items():
        seed_members[address].add(n)

    def install(names):
        nonlocal common_prepared
        rebuild = set()
        needed = defaultdict(set)
        for n in names - installed:
            needed[owners[n]].add(n)
        with tempfile.TemporaryDirectory(dir=output) as stage:
            path = Path(stage) / 'compiler.tar.gz'
            for key, wanted in needed.items():
                row = rows[key]
                if 'public_digest' in row:
                    row = rows[key] = remote.resolve_checkpoint(row)
                # A warm physical shard may serve many small DAG units. Install its
                # other matching members during this download while space permits.
                if cache.shutil.disk_usage(root).free > 6 * 1024**3:
                    wanted.update(seed_members[key] - installed)
                try:
                    remote.download(row, path)
                except CompilerCacheMiss:
                    rebuild.update(wanted)
                    continue
                if row['asset'].startswith('compiler-unit-'):
                    with path.open('rb') as stream:
                        header = stream.read(75)
                    if header[:4] != b'\x1f\x8b\x08\x08' or header[10:] != row['fingerprint'].encode() + b'\0':
                        raise ValueError('compiler checkpoint differs from requested inputs')
                restored = restore_shard(path, row, wanted, modules, destination, root=root, kind='compiler')
                # A previous Lake job may have built a dependency in this checkout.
                # Its trace and sidecars alone cannot establish that the live bytes are intact.
                for name in wanted - restored:
                    for relative in cache.inventory.lean_artifact_paths(name, modules[name]):
                        live = destination / safe_path(relative.as_posix())
                        if (has_unsafe_parent(destination, relative) or live.is_symlink()
                                or (live.exists() and not live.is_file())):
                            raise ValueError('unsafe local compiler artifact: ' + str(relative))
                        live.unlink(missing_ok=True)
                    rebuild.add(name)
                cache.materialize_shared(destination, modules, restored)
                installed.update(restored)
        if rebuild:
            print(f'Rebuilding {len(rebuild)} unavailable compiler checkpoint members from source', file=sys.stderr)
            cache.compiler.compile_roots(root, modules, sorted(rebuild))
            common_prepared = cache.compile_omega_outputs(root, modules, rebuild, common_prepared)
            selected = {n: replace(modules[n], report=False) for n in rebuild}
            checked_shard_files(root, selected, 'compiler', verify_compilers=True)
            installed.update(rebuild)
        return len(rebuild)

    pending = set(units)
    compiled = 0
    remaining = {key: len(row['deps']) for key, row in units.items()}
    ready = {key for key, count in remaining.items() if not count}

    persisted = set()

    def observed(key, row):
        rows[key] = row
        owners.update(dict.fromkeys(units[key]['members'], key))
        seed_members[key].update(units[key]['members'])
        persisted.add(key)
        ready.discard(key)
        # A cached child may come from a different partition; settle the actual plan's parents first.
        settled = [] if remaining[key] else [key]
        while settled:
            finished = settled.pop()
            pending.remove(finished)
            for child in units[finished]['next']:
                remaining[child] -= 1
                if not remaining[child]:
                    if child in persisted:
                        settled.append(child)
                    else:
                        ready.add(child)

    task_plan = dict(plan, shards=units)
    output.mkdir(parents=True, exist_ok=True)
    # Within each rank and preferred queue, start different lanes at different
    # points on the sorted fingerprint ring to spread simultaneous claims.
    pivot = f'{(lane << 256) // len(plan["lanes"]):064x}'
    while pending:
        for key, row in remote.compiler_progress(task_plan, pending - persisted).items():
            observed(key, row)
        if not pending:
            break
        ordered = sorted(ready, key=lambda k: (-units[k]['rank'], units[k]['queue'] != lane, k < pivot, k))
        key = remote.claim_compiler_unit(plan, lane, ordered)
        if key is None:
            remote.wait_compiler_progress(plan)
            continue
        if retry or 'part' not in units[key]:
            saved = remote.retry_checkpoint(task_plan, key)
            if saved:
                remote.complete_compiler_unit(plan, lane, key, saved)
                observed(key, saved)
                continue
        members = set(units[key]['members'])
        compiled += install(decode_scope(units[key]['restore'], dag['names']))
        cache.compiler.compile_roots(root, modules, units[key]['roots'])
        common_prepared = cache.compile_omega_outputs(root, modules, members, common_prepared, prepared_reader=True)
        with tempfile.TemporaryDirectory(dir=output) as stage:
            archive = Path(stage) / 'unit.tar.gz'
            data = pack_shard(root, modules, members, archive, key, kind='compiler', verify_compilers=True)
            row = dict(units[key], bytes=data['bytes'], sha256=data['sha256'], version=plan['version'])
            rows[key] = remote.save(task_plan, key, archive, row)
        remote.complete_compiler_unit(plan, lane, key, rows[key])
        installed.update(members)
        observed(key, rows[key])
        compiled += len(members)
        if cache.shutil.disk_usage(root).free < 4 * 1024**3:
            for n in installed:
                for path in cache.inventory.lean_artifact_paths(n, modules[n]):
                    (destination / path).unlink(missing_ok=True)
            installed.clear()
        cache.write(output / 'worker.json', {'compiled_modules': compiled})
    return install, installed, compiled
