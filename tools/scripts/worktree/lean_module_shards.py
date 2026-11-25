"""Plan stable namespace shards and transport the compiler outputs of their member modules."""

from collections import defaultdict
from dataclasses import replace
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from lean_artifact_inventory import Module, PROJECT_BUILD, compiler_path, compiler_variants, lean_artifact_paths
from lean_cache_release import has_unsafe_parent, install_scoped
from lean_cache_release_io import archive_sha, copy_chunks
from lean_compile_layers import check_outputs
from lean_release_index import safe_path, valid_digest

DEFAULT_CAPACITY = 256 * 1024**2
MANIFEST = 'shard-members.json'
NAME = re.compile(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*')
ARTIFACT_HASH_READER = Path('src/OmegaChecks/Portable/ArtifactHashes.lean')


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


def verify_backend_hashes(root, build, modules, grouped):
    """Check every compiler variant of paired modules using the portable reader's Lake hasher."""
    hashes = {}
    for name, module in modules.items():
        if not (module.extra_builds or module.conditional_backends):
            continue
        for variant in compiler_variants(module):
            trace = compiler_path(name, variant, 'lib/lean', 'trace')
            outputs = json.loads((build / trace).read_bytes())['outputs']
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
                hashes[path] = digest
            if (grouped[name] & lean_artifact_paths(name, variant)) != traced:
                raise ValueError(f'untraced compiler artifacts: {name} in {variant.build}')

    if hashes:
        reader = (root / ARTIFACT_HASH_READER).absolute()
        observed = subprocess.run(['lean', '--run', str(reader)], cwd=root, check=True, text=True,
                                  input='\n'.join(str((build / path).absolute()) for path in hashes),
                                  stdout=subprocess.PIPE).stdout.splitlines()
        for (path, expected), actual in zip(hashes.items(), observed, strict=True):
            if actual != expected:
                raise ValueError(f'artifact differs from its Lake trace: {path}')


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def traces_match(live, staged):
    # Lake records checkout-specific paths in the diagnostic inputs and log.
    try:
        current = json.loads(live.read_bytes())
    except ValueError:
        return False
    published = json.loads(staged.read_bytes())
    return (isinstance(current, dict) and isinstance(published.get('depHash'), str)
            and all(current.get(key) == published.get(key) for key in ('schemaVersion', 'depHash', 'outputs')))


def pack_shard(root, modules, members, output, fingerprint):
    if not valid_digest(fingerprint):
        raise ValueError('invalid module shard fingerprint')
    build = root / PROJECT_BUILD
    if build.is_symlink() or build.parent.is_symlink():
        raise ValueError('unsafe build directory')
    selected = {name: modules[name] for name in members}
    if not selected:
        raise ValueError('empty module shard')
    grouped = {}
    verified_files = {}
    for name, module in selected.items():
        paths = set()
        for path in lean_artifact_paths(name, module):
            file = build / path
            if file.is_symlink() or has_unsafe_parent(build, path):
                raise ValueError('unsafe compiler artifact: ' + str(path))
            if file.is_file():
                paths.add(path)
                if module.extra_builds or module.conditional_backends:
                    verified_files[path.as_posix()] = file_identity(file.stat())
        grouped[name] = paths
    check_outputs(build, selected, grouped)
    verify_backend_hashes(root, build, selected, grouped)
    index = {n: {'build': selected[n].build.as_posix(), 'paths': sorted(map(str, grouped[n]))}
             for n in sorted(selected)}
    for name, row in index.items():
        if selected[name].extra_builds:
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
        candidate.replace(output)
    return descriptor


def restore_shard(archive, descriptor, members, modules, destination, *, root=Path('.')):
    """Check the entire archive, install requested modules, and preserve conflicting live artifacts."""
    wanted = set(members)
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
                module = Module('', Path(), Path(row['build']), extra_builds)
                if len(set((module.build, *extra_builds))) != 1 + len(extra_builds):
                    raise ValueError('duplicate compiler build path: ' + name)
                if name in wanted:
                    expected = modules[name]
                    # Identical checkout inputs may yield just the official variant when
                    # the producer read a Mathlib-only marker from the compiled closure.
                    if expected.conditional_backends and not extra_builds:
                        expected = replace(expected, extra_builds=())
                    if (module.build, extra_builds) != (expected.build, expected.extra_builds):
                        raise ValueError('module build path differs from the checkout: ' + name)
                    selected[name] = expected
                paths = {safe_path(p) for p in row['paths']}
                if (not paths or len(paths) != len(row['paths']) or not paths <= lean_artifact_paths(name, module)
                        or owners.keys() & paths):
                    raise ValueError('invalid compiler file inventory: ' + name)
                grouped[name] = paths
                owners.update(dict.fromkeys(paths, name))
            for entry in entries:
                path = safe_path(entry.name)
                owner = owners.pop(path, None)
                if owner is None or entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError('invalid archived compiler artifact: ' + str(path))
                if owner in wanted:
                    target = stage / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(entry) as source, target.open('xb') as output:
                        if copy_chunks(source, output, None) != entry.size:
                            raise ValueError('truncated compiler artifact')
                    target.chmod(entry.mode & 0o777)
            if owners:
                raise ValueError('shard omits indexed compiler artifacts')

        check_outputs(stage, selected, grouped)
        verify_backend_hashes(root, stage, selected, grouped)
        install = set()
        restored = set()
        for name, module in selected.items():
            missing = set()
            for path in grouped[name]:
                live = destination / path
                if live.is_symlink() or has_unsafe_parent(destination, path):
                    raise ValueError('unsafe local compiler artifact: ' + str(path))
                if not live.exists():
                    missing.add(path)
                elif not live.is_file():
                    break
                elif (module.extra_builds or module.conditional_backends) and path.suffix == '.trace':
                    if not traces_match(live, stage / path):
                        break
                elif archive_sha(live, None) != archive_sha(stage / path, None):
                    break
            else:
                install.update(missing)
                restored.add(name)
        install_scoped(destination, stage, install, None)
        modules.update({n: selected[n] for n in restored})
    return restored
