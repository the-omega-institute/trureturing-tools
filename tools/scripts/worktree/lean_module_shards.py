"""Plan stable namespace shards and transport their modules' compiler and report outputs."""

from collections import defaultdict
from dataclasses import replace
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from lean_artifact_inventory import Module, PROJECT_BUILD, compiler_path, compiler_variants, lean_artifact_paths, plugin_outputs, plugin_paths
from lean_cache_release import has_unsafe_parent, install_scoped, report_selection
from lean_cache_release_io import archive_sha, copy_chunks
from lean_compile_layers import check_outputs
from lean_release_index import safe_path, valid_digest

DEFAULT_CAPACITY = 256 * 1024**2
MANIFEST = 'shard-members.json'
NAME = re.compile(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*')
ARTIFACT_HASH_READER = Path(__file__).with_name('ModuleArtifactHashes.lean').resolve()


def prefixes(name):
    while name:
        yield name
        name = name.rpartition('.')[0]


def plan_shards(sizes, capacity=DEFAULT_CAPACITY, previous=None):
    """Preserve prior namespace boundaries, splitting an overflowing group by its heaviest branches."""
    if type(capacity) is not int or capacity <= 0:
        raise ValueError('invalid shard capacity')
    previous = previous or {}
    groups = defaultdict(list)
    for name, size in sizes.items():
        if not NAME.fullmatch(name) or type(size) is not int or size < 0:
            raise ValueError('invalid module name or size: ' + str(name))
        owner = next((p for p in prefixes(name) if p in previous), name.partition('.')[0])
        groups[owner].append(name)

    result = {}

    def assign(prefix, names):
        size = sum(sizes[n] for n in names)
        if len(names) == 1 or size <= capacity:
            key = names[0] if len(names) == 1 and size > capacity else prefix
            result[key] = sorted(names)
            return
        branches = defaultdict(list)
        retained = []
        for name in names:
            if name == prefix:
                retained.append(name)
            else:
                component = name.removeprefix(prefix + '.').partition('.')[0]
                child = prefix + '.' + component
                branches[child].append(name)
        ranked = sorted((-sum(sizes[n] for n in branch), child) for child, branch in branches.items())
        for negative_size, child in ranked:
            if size > capacity:
                assign(child, branches[child])
                size += negative_size
            else:
                retained.extend(branches[child])
        if retained:
            result[prefix] = sorted(retained)

    for prefix, names in groups.items():
        assign(prefix, names)
    return dict(sorted(result.items()))


def lookup_shard(name, module_to_shard, shards):
    """Resolve the most specific registered membership, or a registered cross-namespace owner."""
    owner = module_to_shard[name]
    for prefix in prefixes(name):
        if prefix in shards and name in shards[prefix]:
            if prefix != owner:
                raise ValueError('shard membership disagrees with the module index: ' + name)
            return prefix
    if name not in shards.get(owner, ()):
        raise ValueError('missing module in registered shard: ' + name)
    return owner


def report_paths(name):
    directory = Path('lean-inspector/modules')
    return tuple(directory / (name + suffix) for suffix in ('.zip', '.zip.hash', '.zip.trace'))


def read_trace(path):
    return json.loads(path.read_bytes(), object_pairs_hook=report_selection.unique_object)


def inspector_publication():
    # Compiler-only consumers can run from a reduced transport-tools installation.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lean-inspector'))
    try:
        import publication
        return publication
    finally:
        sys.path.pop(0)


def check_report_utility(root, name, row, module, public):
    utility = module.report_utility
    evidence = row.get('utility_refutation')
    if utility is None:
        if evidence is not None:
            raise ValueError('unexpected report utility evidence: ' + name)
        return
    fields = {'claimGid': 'claim_gid', 'claimSourcePath': 'claim_source_path',
              'claimSourceSha256': 'claim_source_sha256', 'resultGid': 'result_gid'}
    if (utility['resultModule'] != name or utility['modulePath'] != module.source.as_posix() or evidence is None
            or any(utility[key] != evidence[field] for key, field in fields.items())):
        raise ValueError('report utility differs from its current obligation: ' + name)
    path = safe_path(utility['claimSourcePath'])
    claim = root / path
    if (has_unsafe_parent(root, path) or claim.is_symlink() or not claim.is_file()
            or 'sha256:' + public.digest(claim) != utility['claimSourceSha256']):
        raise ValueError('stale or unsafe report utility claim source: ' + name)


def verify_report(root, build, name, module, hashes):
    archive, sidecar, trace = report_paths(name)
    public = inspector_publication()
    document = read_trace(build / trace)
    output = document.get('outputs') if isinstance(document, dict) else None
    if (not isinstance(output, str) or not re.fullmatch(r'[0-9a-f]{16}\.zip', output)
            or not isinstance(document.get('depHash'), str) or not re.fullmatch(r'[0-9a-f]{16}', document['depHash'])
            or (build / sidecar).read_bytes() != output[:16].encode('ascii')):
        raise ValueError('invalid Lake report trace or hash for ' + name)

    source = root / module.source
    if source.is_symlink() or has_unsafe_parent(root, module.source) or not source.is_file():
        raise ValueError('unsafe report source: ' + str(source))
    try:
        with tempfile.TemporaryDirectory() as directory:
            report = public.unpack(build / archive, directory, ('', '.materials.zip', '.provenance.json'))
            rows = public.validate_rows(report, public.member(report, '.materials.zip'))
            if (len(rows) != 1 or rows[0]['module'] != name or rows[0]['source_path'] != module.source.as_posix()
                    or rows[0]['source_sha256'] != 'sha256:' + public.digest(source)):
                raise ValueError('report differs from the current module or source: ' + name)
            check_report_utility(root, name, rows[0], module, public)
            public.validate_origin(report, rows)
    except zipfile.BadZipFile as error:
        raise ValueError('invalid report ZIP for ' + name) from error
    hashes[build / archive] = output[:16]
    return build / trace


def verify_backend_hashes(root, build, modules, grouped, *, destination=None, reports_only=False, all_compilers=False):
    """Check module reports and every compiler variant of paired modules against their Lake traces."""
    hashes = {}
    local_traces = []
    traces = []
    for name, module in modules.items():
        if module.report:
            if not set(report_paths(name)) <= grouped[name]:
                raise ValueError('missing module report, hash or trace: ' + name)
            traces.append(str(verify_report(root, build, name, module, hashes).absolute()))
            trace = report_paths(name)[2]
            if destination is not None and not has_unsafe_parent(destination, trace):
                live = destination / trace
                if not live.is_symlink() and live.is_file():
                    local_traces.append(live)
        if module.plugin_input and not reports_only:
            if not plugin_paths(name, module) <= grouped[name]:
                raise ValueError('missing native compiler plugin output: ' + name)
            for output in plugin_outputs(name, module):
                trace = output.with_name(output.name + '.trace')
                sidecar = output.with_name(output.name + '.hash')
                suffix = 'o' if output.name.endswith('.c.o.export') else output.suffix[1:]
                description = read_trace(build / trace)['outputs']
                if (not isinstance(description, str)
                        or not re.fullmatch(r'[0-9a-f]{16}\.' + suffix, description)
                        or (build / sidecar).read_text() != description[:16]):
                    raise ValueError('native compiler output differs from its Lake trace: ' + str(output))
                hashes[build / output] = description[:16]
                traces.append(str((build / trace).absolute()))
        if reports_only or not (all_compilers or module.extra_builds or module.conditional_backends):
            continue
        for variant in compiler_variants(module):
            trace = compiler_path(name, variant, 'lib/lean', 'trace')
            if all_compilers:
                traces.append(str((build / trace).absolute()))
            outputs = read_trace(build / trace)['outputs']
            descriptors = dict(zip(('olean', 'olean.server', 'olean.private'), outputs['o']))
            descriptors.update(ilean=outputs['i'], c=outputs['c'])
            for key, suffix in (('rs', 'ir.sig'), ('r', 'ir'), ('b', 'bc')):
                if key in outputs:
                    descriptors[suffix] = outputs[key]

            traced = {trace}
            for suffix, description in descriptors.items():
                location = 'ir' if suffix in ('c', 'bc') else 'lib/lean'
                path = compiler_path(name, variant, location, suffix)
                sidecar = path.with_name(path.name + '.hash')
                if (not isinstance(description, str)
                        or not re.fullmatch(r'[0-9a-f]{16}\.' + re.escape(suffix), description)
                        or not {path, sidecar} <= grouped[name]):
                    raise ValueError(f'invalid Lake output description for {path}')
                digest = description[:16]
                if (build / sidecar).read_bytes() != digest.encode('ascii'):
                    raise ValueError(f'artifact sidecar differs from its Lake trace: {sidecar}')
                traced.update((path, sidecar))
                hashes[build / path] = digest
            if (grouped[name] & lean_artifact_paths(name, variant)) != traced:
                raise ValueError(f'untraced compiler artifacts: {name} in {variant.build}')

    if hashes:
        request = dict(files=[str(path.absolute()) for path in hashes], traces=traces,
                       localTraces=[str(path.absolute()) for path in local_traces])
        observed = subprocess.run(['lean', '--run', str(ARTIFACT_HASH_READER)], cwd=root, text=True,
                                  input=json.dumps(request), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if observed.returncode:
            raise ValueError('Lake artifact or report trace verification failed: ' + observed.stderr + observed.stdout)
        readings = observed.stdout.splitlines()
        for (path, digest), actual in zip(hashes.items(), readings[:len(hashes)], strict=True):
            if actual != digest:
                raise ValueError(f'artifact differs from its Lake trace: {path}')
        return {path for path, status in zip(local_traces, readings[len(hashes):], strict=True) if status != 'valid'}
    return set()


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def traces_match(live, staged):
    # Lake records checkout-specific paths in the diagnostic inputs and log.
    try:
        current = read_trace(live)
    except ValueError:
        return False
    published = read_trace(staged)
    return (isinstance(current, dict) and isinstance(published.get('depHash'), str)
            and all(current.get(key) == published.get(key) for key in ('schemaVersion', 'depHash', 'outputs')))


def checked_shard_files(root, selected, kind, verify_compilers):
    build = root / PROJECT_BUILD
    if build.is_symlink() or build.parent.is_symlink():
        raise ValueError('unsafe build directory')
    grouped = {}
    verified_files = {}
    for name, module in selected.items():
        paths = set()
        candidates = set() if kind == 'report' else lean_artifact_paths(name, module)
        reports = set(report_paths(name)) if module.report else set()
        for path in candidates | reports:
            file = build / path
            if file.is_symlink() or has_unsafe_parent(build, path):
                raise ValueError('unsafe compiler artifact: ' + str(path))
            if file.is_file():
                paths.add(path)
                if verify_compilers or module.extra_builds or module.conditional_backends or module.plugin_input or path in reports:
                    verified_files[path.as_posix()] = file_identity(file.stat())
        grouped[name] = paths
    if kind != 'report':
        check_outputs(build, selected, grouped)
    verify_backend_hashes(root, build, selected, grouped, reports_only=kind == 'report',
                          all_compilers=verify_compilers)
    return grouped, verified_files


def pack_shard(root, modules, members, output, fingerprint, *, kind='combined', verify_compilers=False):
    if kind not in ('combined', 'compiler', 'report'):
        raise ValueError('unknown module shard kind: ' + str(kind))
    if not valid_digest(fingerprint):
        raise ValueError('invalid module shard fingerprint')
    build = root / PROJECT_BUILD
    selected = {name: modules[name] for name in members}
    if not selected:
        raise ValueError('empty module shard')
    if kind == 'compiler':
        selected = {name: replace(module, report=False) for name, module in selected.items()}
    if kind == 'report' and not all(module.report for module in selected.values()):
        raise ValueError('report-only archive includes a module without a report')
    grouped, verified_files = checked_shard_files(root, selected, kind, verify_compilers)
    index = {n: {'build': selected[n].build.as_posix(), 'paths': sorted(map(str, grouped[n]))}
             for n in sorted(selected)}
    for name, row in index.items():
        if kind != 'report' and selected[name].plugin_input:
            row['plugin_input'] = True
        if selected[name].report:
            row['report'] = True
        if kind != 'report' and selected[name].extra_builds:
            row['extra_builds'] = [p.as_posix() for p in selected[name].extra_builds]
    manifest = json.dumps(index, separators=(',', ':')).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        candidate = Path(directory) / 'shard.tar.gz'
        module_sizes = {}
        with candidate.open('wb') as raw, gzip.GzipFile(
            filename=fingerprint, fileobj=raw, mode='wb', compresslevel=1, mtime=0,
        ) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
            entry = tarfile.TarInfo(MANIFEST)
            entry.size = len(manifest)
            archive.addfile(entry, io.BytesIO(manifest))
            compressed.flush()
            for name, row in index.items():
                start = raw.tell()
                for path in row['paths']:
                    with (build / path).open('rb') as source:
                        # Confirm that verified content has stayed stable through hashing and packing.
                        info = os.fstat(source.fileno())
                        verified = verified_files.get(path)
                        if verified is not None and file_identity(info) != verified:
                            raise ValueError(f'compiler artifact changed during packing: {path}')
                        entry = tarfile.TarInfo(path)
                        entry.mode = info.st_mode & 0o777
                        entry.size = info.st_size
                        archive.addfile(entry, source)
                        if verified is not None and file_identity(os.fstat(source.fileno())) != verified:
                            raise ValueError(f'compiler artifact changed during packing: {path}')
                compressed.flush()
                module_sizes[name] = raw.tell() - start
        descriptor = {'sha256': archive_sha(candidate, None), 'bytes': candidate.stat().st_size,
                      'members': sorted(selected), 'module_sizes': module_sizes}
        if kind != 'combined':
            descriptor['artifact_kind'] = kind
        candidate.replace(output)
    return descriptor


def restore_shard(archive, descriptor, members, modules, destination, *, root=Path('.'), kind='combined',
                  compiler_only=(), compiler_hits=None):
    """Install the requested material from a matching shard or a historical combined shard."""
    archived_kind = descriptor.get('artifact_kind', 'combined')
    if kind not in ('combined', 'compiler', 'report') or archived_kind not in ('combined', kind):
        raise ValueError(f'cannot restore {kind} from a {archived_kind} module shard')
    wanted = set(members)
    seeds = wanted if kind == 'compiler' else set(compiler_only)
    if not seeds <= wanted:
        raise ValueError('compiler-only restore must select requested modules')
    if seeds and kind == 'report':
        raise ValueError('report-only restore cannot install compiler seeds')
    indexed = set(descriptor['members'])
    if not wanted <= indexed or not wanted <= modules.keys():
        raise ValueError('requested module is outside the indexed shard or checkout')
    if (archive.is_symlink() or archive.stat().st_size != descriptor['bytes']
            or archive_sha(archive, None) != descriptor['sha256']):
        raise ValueError('shard length or checksum mismatch')
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('unsafe restore destination')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive, 'r|gz') as bundle:
            entries = iter(bundle)
            header = next(entries, None)
            if (not header or header.name != MANIFEST or not header.isfile()
                    or not 0 < header.size <= 128 * 1024**2):
                raise ValueError('missing shard member manifest')
            manifest = json.load(bundle.extractfile(header))
            if set(manifest) != indexed:
                raise ValueError('shard manifest differs from the version index')
            owners = {}
            grouped = {}
            selected = {}
            for name, row in manifest.items():
                if not NAME.fullmatch(name):
                    raise ValueError('invalid archived module name')
                extra_builds = tuple(Path(safe_path(p)) for p in row.get('extra_builds', []))
                has_report = row.get('report', False)
                if (type(has_report) is not bool or (archived_kind == 'compiler' and has_report)
                        or (archived_kind == 'report' and not has_report)
                        or (name in wanted and name not in seeds and has_report != modules[name].report)
                        or (kind == 'report' and name in wanted and not has_report)):
                    raise ValueError('module report inventory differs from the checkout: ' + name)
                plugin = row.get('plugin_input', False)
                if type(plugin) is not bool or (plugin and archived_kind == 'report'):
                    raise ValueError('invalid native compiler inventory: ' + name)
                module = Module('', Path(), Path(row['build']), extra_builds, plugin_input=plugin)
                if len(set((module.build, *extra_builds))) != 1 + len(extra_builds):
                    raise ValueError('duplicate compiler build path: ' + name)
                if name in wanted:
                    expected = modules[name]
                    # Identical checkout inputs may yield just the official variant when
                    # the producer read a Mathlib-only marker from the compiled closure.
                    if expected.conditional_backends and not extra_builds:
                        expected = replace(expected, extra_builds=())
                    if kind != 'report' and (module.build, extra_builds, plugin) != (expected.build, expected.extra_builds, expected.plugin_input):
                        raise ValueError('module build path differs from the checkout: ' + name)
                    selected[name] = replace(expected, report=False) if name in seeds else expected
                allowed = lean_artifact_paths(name, module)
                if plugin and name not in wanted:
                    # A portable sibling may be requested from a publisher on another platform.
                    for platform in ('linux', 'darwin', 'win32'):
                        allowed.update(plugin_paths(name, module, platform))
                paths = {safe_path(p) for p in row['paths']}
                reports = set(report_paths(name)) if has_report else set()
                if (not paths or len(paths) != len(row['paths']) or not reports <= paths
                        or (archived_kind == 'report' and paths != reports)
                        or not paths <= allowed | reports
                        or owners.keys() & paths):
                    raise ValueError('invalid compiler file inventory: ' + name)
                if kind == 'report':
                    grouped[name] = reports
                else:
                    grouped[name] = paths - reports if name in seeds else paths
                owners.update(dict.fromkeys(paths, name))
            for entry in entries:
                path = safe_path(entry.name)
                owner = owners.pop(path, None)
                if owner is None or entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError('invalid archived compiler artifact: ' + str(path))
                if owner in wanted and path in grouped[owner]:
                    target = stage / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(entry) as source, target.open('xb') as output:
                        if copy_chunks(source, output, None) != entry.size:
                            raise ValueError('truncated compiler artifact')
                    target.chmod(entry.mode & 0o777)
            if owners:
                raise ValueError('shard omits indexed compiler artifacts')

        if kind != 'report':
            check_outputs(stage, selected, grouped)
        invalid_local = verify_backend_hashes(root, stage, selected, grouped, destination=destination,
                                              reports_only=kind == 'report')
        install = set()
        restored = set()
        partial = set()
        for name, module in selected.items():
            missing = set()
            conflicts = set()
            reports = set(report_paths(name)) if module.report else set()
            for path in grouped[name]:
                live = destination / path
                if live.is_symlink() or has_unsafe_parent(destination, path):
                    raise ValueError('unsafe local compiler artifact: ' + str(path))
                if not live.exists():
                    missing.add(path)
                elif not live.is_file():
                    conflicts.add(path)
                elif (module.extra_builds or module.conditional_backends or module.plugin_input or module.report) and path.suffix == '.trace':
                    if live in invalid_local or not traces_match(live, stage / path):
                        conflicts.add(path)
                elif archive_sha(live, None) != archive_sha(stage / path, None):
                    conflicts.add(path)
            if not conflicts:
                install.update(missing)
                restored.add(name)
            elif kind == 'combined' and compiler_hits is not None and conflicts <= reports:
                install.update(missing - reports)
                partial.add(name)
        install_scoped(destination, stage, install, None)
        # A compiler-only seed can omit the report this checkout still needs.
        # Retain that obligation while adopting the authenticated backend layout.
        if kind != 'report':
            for name in restored | partial:
                modules[name] = replace(modules[name], extra_builds=selected[name].extra_builds)
        if compiler_hits is not None:
            compiler_hits.update(partial)
    return restored
