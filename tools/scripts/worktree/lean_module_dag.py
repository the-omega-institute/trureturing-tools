"""Schedule cold compiler modules separately from published namespace shards."""

from collections import defaultdict
from graphlib import TopologicalSorter
import math
from pathlib import Path
import tempfile

from lean_artifact_inventory import import_closure
from lean_generation_plan import digest
from lean_release_index import valid_digest

UNIT_LIMIT = 32
BASIS = {'OmegaInit', 'OmegaMathlib'}


def unit_key(mathlib, inputs):
    return digest(['lean-compiler-unit-v1', mathlib, sorted(inputs.items())])


def unit_row(key, members):
    asset = 'compiler-unit-' + key + '.tar.gz'
    return dict(key=asset, asset=asset, fingerprint=key, members=sorted(members), artifact_kind='compiler')


def group_units(graph, cold, basis, workers):
    """Fold the common facade generator's simultaneous basis build into one vertex."""
    foundation = import_closure(graph, basis) & cold
    anchor = min(foundation) if foundation else None

    def owner(n):
        return anchor if n in foundation else n

    edges = defaultdict(set)
    for n in cold:
        parent = owner(n)
        edges[parent].update(owner(d) for d in graph[n] if d in cold and owner(d) != parent)
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


def prepare(plan, base, remote, workers, prepare_release, facade_consumers):
    from lean_module_cache import partial_index
    from lean_module_remote import UNIT_PART_START
    targets = plan['targets']
    checked_base = dict(base, shards=remote.confirmed(base['shards'])) if base else None
    recovered = remote.compiler_seeds(plan)
    seeds = matching_seeds(targets, [partial_index(plan, plan['inherited']), checked_base, recovered])
    cold = targets.keys() - seeds['owners'].keys()
    graph = {n: set(t['deps']) for n, t in targets.items()}
    basis = {n for n in targets if n.partition('.')[0] in BASIS} if facade_consumers else set()
    # Common facade generation reads all official and strict basis interfaces.
    foundation = import_closure(graph, basis)
    for n in facade_consumers - foundation:
        graph[n].update(basis)
    groups = group_units(graph, cold, basis, workers)
    units = {}
    owner = {}
    catalog = {'mathlib': plan['mathlib'], 'units': {}}
    for members in groups:
        inputs = {n: targets[n]['key'] for n in members}
        key = unit_key(plan['mathlib'], inputs)
        catalog['units'][key] = inputs
        owner.update(dict.fromkeys(members, key))
        units[key] = unit_row(key, members)
    # Separate immutable execution packs from the ordinary physical release parts.
    first_part = UNIT_PART_START + int(digest(sorted(units))[:9], 16) * 100000
    for ordinal, key in enumerate(sorted(units)):
        unit = units[key]
        unit['part'] = first_part + ordinal // 600 * 8
        unit['queue'] = ordinal % (4 * workers)
        deps = {owner[d] for n in unit['members'] for d in graph[n] if d in owner} - {key}
        unit['deps'] = sorted(deps)
    tuple(TopologicalSorter({key: row['deps'] for key, row in units.items()}).static_order())
    plan['compiler_dag'] = dict(seeds=seeds, units=units, basis=sorted(basis),
                                facade_consumers=sorted(facade_consumers - foundation))
    if catalog['units'] and prepare_release:
        remote.save_compiler_catalog(plan['version'], catalog)
    # Even one physical shard may have enough independent modules for many runners.
    jobs = min(workers, max(len(units), len(plan['lanes'])))
    plan['lanes'].extend([] for _ in range(jobs - len(plan['lanes'])))


def compile_units(root, plan, lane, remote, modules, graph, output):
    import lean_module_cache as cache
    from lean_module_shards import pack_shard, restore_shard
    dag = plan['compiler_dag']
    units = dag['units']
    owners = dict(dag['seeds']['owners'])
    rows = dict(dag['seeds']['rows'])
    installed = set()
    destination = root / cache.inventory.PROJECT_BUILD
    common_prepared = False
    seed_members = defaultdict(set)
    for n, address in owners.items():
        seed_members[address].add(n)

    def install(names):
        needed = defaultdict(set)
        for n in names - installed:
            needed[owners[n]].add(n)
        with tempfile.TemporaryDirectory(dir=output) as stage:
            path = Path(stage) / 'compiler.tar.gz'
            for key, wanted in needed.items():
                row = rows[key]
                # A warm physical shard may serve many small DAG units. Install its
                # other matching members during this download while space permits.
                if cache.shutil.disk_usage(root).free > 6 * 1024**3:
                    wanted.update(seed_members[key] - installed)
                remote.download(row, path)
                if row['asset'].startswith('compiler-unit-'):
                    with path.open('rb') as stream:
                        header = stream.read(75)
                    if header[:4] != b'\x1f\x8b\x08\x08' or header[10:] != row['fingerprint'].encode() + b'\0':
                        raise ValueError('compiler checkpoint differs from requested inputs')
                restored = restore_shard(path, row, wanted, modules, destination, root=root, kind='compiler')
                if wanted != restored:
                    raise ValueError('compiler dependency could not be installed: ' + min(wanted - restored))
                installed.update(restored)

    for n in dag['facade_consumers']:
        graph[n].update(dag['basis'])
    pending = set(units)
    compiled = 0
    complete = {}
    # Prefer units that unlock the longest remaining dependency path.
    ranks = dict.fromkeys(units, 1)
    order = TopologicalSorter({k: r['deps'] for k, r in units.items()}).static_order()
    for k in reversed(list(order)):
        for d in units[k]['deps']:
            ranks[d] = max(ranks[d], 1 + ranks[k])
    held = set()
    task_plan = dict(plan, shards=units)
    output.mkdir(parents=True, exist_ok=True)
    refresh = True
    while pending:
        discovered = remote.compiler_progress(task_plan, pending) if refresh else {}
        refresh = False
        for key, row in discovered.items():
            rows[key] = row
            owners.update(dict.fromkeys(row['members'], key))
            seed_members[key].update(row['members'])
            complete[key] = row
        pending.difference_update(complete)
        if not pending:
            break
        ready = sorted((k for k in pending if complete.keys() >= set(units[k]['deps'])),
                       key=lambda k: (-ranks[k], k))
        # Spread simultaneous claim attempts across equally urgent work.
        if ready:
            top = [k for k in ready if ranks[k] == ranks[ready[0]]]
            offset = lane % len(top)
            ready[:len(top)] = top[offset:] + top[:offset]
        key = next((k for k in ready if units[k]['queue'] in held), None)
        if key is None:
            candidates = {}
            for candidate in ready:
                candidates.setdefault(units[candidate]['queue'], candidate)
            for queue, candidate in candidates.items():
                if remote.claim_compiler_queue(plan, queue, lane):
                    held.add(queue)
                    key = candidate
                    break
        if key is None:
            remote.wait_compiler_progress(plan)
            refresh = True
            continue
        members = set(units[key]['members'])
        scope = cache.inventory.import_closure(graph, members)
        if members.intersection(dag['basis']):
            scope.update(cache.inventory.import_closure(graph, dag['basis']))
        install(scope - members)
        roots = members if any(modules[n].extra_builds for n in members) else cache.compiler.terminal(members, graph)
        cache.compiler.compile_roots(root, modules, sorted(roots))
        common_prepared = cache.compile_omega_outputs(root, modules, members, common_prepared)
        with tempfile.TemporaryDirectory(dir=output) as stage:
            archive = Path(stage) / 'unit.tar.gz'
            data = pack_shard(root, modules, members, archive, key, kind='compiler')
            row = dict(units[key], bytes=data['bytes'], sha256=data['sha256'], version=plan['version'])
            rows[key] = remote.save(task_plan, key, archive, row)
        installed.update(members)
        owners.update(dict.fromkeys(members, key))
        complete[key] = rows[key]
        pending.remove(key)
        compiled += len(members)
        if cache.shutil.disk_usage(root).free < 4 * 1024**3:
            for n in installed:
                for path in cache.inventory.lean_artifact_paths(n, modules[n]):
                    (destination / path).unlink(missing_ok=True)
            installed.clear()
        cache.write(output / 'worker.json', {'compiled_modules': compiled})
    return install, installed, compiled
