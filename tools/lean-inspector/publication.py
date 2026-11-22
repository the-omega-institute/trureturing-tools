#!/usr/bin/env python3
"""Inspector-owned canonical bundle validation and private publication.

Validates native module artifacts and publishes scoped bundles.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

import materials
from materials import read_json, unique_object

_selection_spec = importlib.util.spec_from_file_location('report_selection',
    Path(__file__).resolve().parent.parent / 'scripts/report/lean-report-selection.py')
selection = importlib.util.module_from_spec(_selection_spec)
_selection_spec.loader.exec_module(selection)

# This Selection module is private to the inspector. Only the pure grammar
# computation is memoized; safe_file still checks every current path component.
_compile_glob = selection.compile_glob
_compiled_glob = lru_cache(maxsize=16384)(_compile_glob)


def compile_glob(pattern, location):
    if not isinstance(pattern, str) or not isinstance(location, str):
        return _compile_glob(pattern, location)
    return _compiled_glob(pattern, location)


selection.compile_glob = compile_glob

RAW = 'raw-lean-report.json'
SUFFIXES = ('', '.sha256', '.input.attestation', '.provenance.json', '.materials.zip')
SHA = re.compile(r'sha256:[0-9a-f]{64}')
HEX = re.compile(r'[0-9a-f]{64}')
KINDS = {'axiom', 'def', 'theorem', 'opaque', 'quotient', 'constructor', 'recursor', 'inductive'}


def report_rows(report, schema, *, canonical=False):
    """Consume one module at a time; only the current JSON value is buffered."""
    expected = hashlib.sha256(b'{"modules": [') if canonical else None
    count = 0
    with Path(report).open(encoding='utf-8') as source:
        buffered = ''
        decoder = json.JSONDecoder(object_pairs_hook=unique_object)

        def token():
            nonlocal buffered
            while True:
                buffered = buffered.lstrip()
                if buffered:
                    result, buffered = buffered[0], buffered[1:]
                    return result
                buffered = source.read(materials.BUFFER_BYTES)
                if not buffered:
                    return ''

        def value(first):
            nonlocal buffered
            if first not in ('{', '"'):
                raise ValueError('invalid report JSON value')
            data, buffered = first + buffered, ''
            while True:
                try:
                    _, end = decoder.raw_decode(data)
                except json.JSONDecodeError as error:
                    extra = source.read(max(materials.BUFFER_BYTES, len(data)))
                    if not extra:
                        raise ValueError('truncated or invalid report JSON value') from error
                    data += extra
                else:
                    buffered = data[end:]
                    return read_json(data[:end])

        if token() != '{':
            raise ValueError('invalid raw report schema')
        fields = set()
        while True:
            key = value(token())
            if key not in ('modules', 'schema') or key in fields or token() != ':':
                raise ValueError('invalid raw report schema')
            fields.add(key)
            if key == 'schema':
                if value(token()) != schema:
                    raise ValueError('invalid raw report schema')
            else:
                if token() != '[':
                    raise ValueError('invalid raw report schema')
                c = token()
                if c != ']':
                    while True:
                        row = value(c)
                        if not isinstance(row, dict):
                            raise ValueError('invalid report module')
                        if expected is not None:
                            if count:
                                expected.update(b', ')
                            expected.update(materials.canonical_json(row)[:-1])
                        count += 1
                        yield row
                        del row
                        c = token()
                        if c == ']':
                            break
                        if c != ',':
                            raise ValueError('invalid report module separator')
                        c = token()
            c = token()
            if c == '}':
                break
            if c != ',':
                raise ValueError('invalid report field separator')
        if fields != {'modules', 'schema'} or token():
            raise ValueError('invalid raw report schema')
    if expected is not None:
        expected.update(b'], "schema": ' + materials.canonical_json(schema)[:-1] + b'}\n')
        if expected.hexdigest() != digest(report):
            raise ValueError('noncanonical raw report bytes')


def member(report, suffix):
    return Path(str(report) + suffix)


def open_zip_member(archive, info):
    # CPython raises RuntimeError before decoding when an optional module is
    # absent. Reject that artifact condition without catching program failures.
    for method, decoder, name in ((zipfile.ZIP_DEFLATED, zipfile.zlib, 'DEFLATE'),
            (zipfile.ZIP_BZIP2, zipfile.bz2, 'BZIP2'), (zipfile.ZIP_LZMA, zipfile.lzma, 'LZMA')):
        if info.compress_type == method and decoder is None:
            raise ValueError('unavailable ZIP decoder: ' + name)
    return archive.open(info)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(materials.BUFFER_BYTES), b''):
            result.update(block)
    return result.hexdigest()


def production_origin(repository, executable, *, inputs=None):
    """Actual generation evidence, never a Lake report dependency or currentness test."""
    inputs = inputs if inputs is not None else selection.Selection(repository)
    inputs.validate('lean-report')
    fingerprint = hashlib.sha256()
    for path in inputs.producer_paths('lean-report'):
        sha = (hashlib.sha256(inputs.projection('lean-report').encode('ascii')).hexdigest()
               if path == selection.MANIFEST else digest(inputs.safe_file(path)))
        fingerprint.update(path.encode('utf-8') + b'\0' + sha.encode('ascii') + b'\n')
    return dict(producer_sources_sha256=fingerprint.hexdigest(),
                inspector_executable_sha256=digest(executable))


def write_origin(report, name, origin, projection):
    validate_input_projection(projection, name)
    record = dict(origin, module=name, report_sha256=digest(report), input_projection=projection)
    member(report, '.provenance.json').write_bytes(materials.canonical_json(record))


def validate_input_projection(projection, module):
    materials.require_keys(projection, {'schema', 'module', 'inputs'}, 'judge input projection')
    if (projection['schema'] != 'stratalint-judge-input-projection-v1'
            or projection['module'] != module or not isinstance(projection['inputs'], list)):
        raise ValueError('contract.discovery:invalid_projection:' + module)
    names = set()
    for entry in projection['inputs']:
        materials.require_keys(entry, {'type', 'owner', 'name'}, 'judge input projection entry')
        if (entry['type'] not in {'LeanInformationAudit.Contract.' + name for name in
                ('Registration', 'TemplateEnrollment', 'RootCatalog', 'Seal')}
                or entry['owner'] != module or not isinstance(entry['name'], str)
                or not entry['name'] or entry['name'] in names):
            raise ValueError('contract.discovery:invalid_projection_entry:' + module)
        names.add(entry['name'])


def check_origin(origin, row, *, check_report=True):
    materials.require_keys(origin, {'module', 'report_sha256', 'input_projection',
        'producer_sources_sha256', 'inspector_executable_sha256'}, 'module production origin')
    validate_input_projection(origin['input_projection'], row['module'])
    if (origin['module'] != row['module']
            or any(not isinstance(origin[k], str) or not HEX.fullmatch(origin[k]) for k in
                   ('report_sha256', 'producer_sources_sha256', 'inspector_executable_sha256'))
            or (check_report and origin['report_sha256'] != hashlib.sha256(materials.canonical_json(
                dict(schema=materials.REPORT_SCHEMA, modules=[row]))).hexdigest())):
        raise ValueError('invalid or incompatible module production origin')


def validate_origin(report, rows):
    if len(rows) != 1:
        raise ValueError('module origin requires exactly one row')
    origin = read_json(member(report, '.provenance.json').read_bytes())
    check_origin(origin, rows[0])
    return origin


def require_module_keys(row):
    keys = {'module', 'source_path', 'source_sha256', 'imports', 'declarations'}
    keys.update(key for key in ('information_registration_errors', 'information_templates', 'utility_refutation') if key in row)
    materials.require_keys(row, keys, 'module')


def _validate_row(row, archive, available, verified_materials, identities):
    """All certificate and declaration references die at this call boundary."""
    references = {}
    require_module_keys(row)
    name, path, sha = row['module'], row['source_path'], row['source_sha256']
    if (not isinstance(name, str) or not name
            or not isinstance(path, str) or not path
            or not isinstance(sha, str) or not SHA.fullmatch(sha)):
        raise ValueError('invalid or duplicate module/source binding')
    materials.require_sorted_strings(row['imports'], 'imports')
    if 'information_registration_errors' in row:
        materials.require_sorted_strings(row['information_registration_errors'], 'registration errors')
    if 'information_templates' in row:
        materials.validate_template_evidence(row['information_templates'])
    if 'utility_refutation' in row:
        evidence = materials.require_keys(row['utility_refutation'], {'claim_gid', 'claim_source_path',
            'claim_source_sha256', 'result_gid', 'is_closed_negation'}, 'utility refutation')
        if (any(not isinstance(evidence[k], str) or not evidence[k] for k in ('claim_gid', 'claim_source_path', 'result_gid'))
                or not isinstance(evidence['claim_source_sha256'], str)
                or not SHA.fullmatch(evidence['claim_source_sha256']) or type(evidence['is_closed_negation']) is not bool):
            raise ValueError('invalid typed refutation')
    if not isinstance(row['declarations'], list):
        raise ValueError('invalid declarations')
    previous_key = None
    for decl in row['declarations']:
        materials.require_keys(decl, {'axioms', 'include_in_statement', 'kind', 'name', 'name_key', 'statement_id', 'type_sha256'}, 'declaration')
        key = decl['name_key']
        if (not isinstance(key, str) or not key or previous_key is not None and key <= previous_key
                or not isinstance(decl['name'], str) or not decl['name'] or decl['kind'] not in KINDS
                or type(decl['include_in_statement']) is not bool
                or any(not isinstance(decl[k], str) or not SHA.fullmatch(decl[k]) for k in ('statement_id', 'type_sha256'))):
            raise ValueError('invalid or duplicate declaration')
        previous_key = key
        materials.require_sorted_strings(decl['axioms'], 'axioms')
        references.setdefault('sha256/' + decl['type_sha256'][7:], []).append((row, decl))
    if set(references) - available:
        raise ValueError('duplicate, missing, or unreferenced material')
    for name in references:
        info = archive.getinfo(name)
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError('nonregular material')
        if info.flag_bits & 1:
            raise ValueError('encrypted material')
        for row, decl in references[name]:
            key = (row['source_path'], decl['kind'], decl['name_key'], decl['type_sha256'])
            with open_zip_member(archive, info) as source:
                if not identities:
                    materials.verify_material(source, decl['type_sha256'])
                    actual = (decl['type_sha256'], decl['statement_id'])
                elif verified_materials is not None and key in verified_materials:
                    # Invocation-local reuse of the expensive canonical
                    # statement encoding. Always read/CRC-check/hash the
                    # actual bytes again; an archive address is no proof.
                    materials.verify_material(source, decl['type_sha256'])
                    actual = (decl['type_sha256'], verified_materials[key])
                else:
                    actual = materials.material_identities(source, row['source_path'], decl['kind'], decl['name_key'])
            if actual != (decl['type_sha256'], decl['statement_id']):
                raise ValueError('material or declaration identity mismatch')
            if identities and verified_materials is not None:
                verified_materials[key] = actual[1]
    return set(references)


def validated_rows(report, archive_path, verified_materials=None, *, identities=True,
                   schema=materials.REPORT_SCHEMA):
    """Validate each module and release its certificate and reference map on advance."""
    previous = None
    paths, used = set(), set()
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate, missing, or unreferenced material')
        available = set(names)
        for row in report_rows(report, schema, canonical=True):
            name, path = row.get('module'), row.get('source_path')
            if (not isinstance(name, str) or not isinstance(path, str)
                    or previous is not None and name <= previous or path in paths):
                raise ValueError('invalid or duplicate module/source binding')
            previous = name
            paths.add(path)
            used.update(_validate_row(row, archive, available, verified_materials, identities))
            yield row
            del row
        if used != available:
            raise ValueError('duplicate, missing, or unreferenced material')


def validate_rows(report, archive_path, verified_materials=None, *, identities=True):
    """Materialize a module artifact; aggregate consumers use validated_rows."""
    return list(validated_rows(report, archive_path, verified_materials, identities=identities))


def validate_template_sources(rows, repository, *, inputs=None):
    """Check evidence structure against the declared report input scope.

    Lake traces own input freshness; evidence contains no raw source hashes.
    """
    if inputs is None:
        inputs = selection.Selection(repository)
    elif inputs.root != Path(repository).resolve():
        raise ValueError('declared-template input owner mismatch')
    for row in rows:
        evidence = row.get('information_templates')
        if evidence is None:
            continue
        materials.validate_template_evidence(evidence)


def _require_bundle_files(report):
    for suffix in SUFFIXES:
        path = member(report, suffix)
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError(f'missing bundle member: {path.name}')


def zip_files(destination, paths):
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, source in paths:
            info = zipfile.ZipInfo(name, materials.ARCHIVE_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with Path(source).open('rb') as reader, archive.open(info, 'w', force_zip64=Path(source).stat().st_size >= zipfile.ZIP64_LIMIT) as writer:
                shutil.copyfileobj(reader, writer, materials.BUFFER_BYTES)


def bundle_members(archive, suffixes=SUFFIXES):
    """The exact regular, unencrypted members of an open bundle, by name."""
    expected = {RAW + suffix for suffix in suffixes}
    if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
        raise ValueError('invalid native artifact members')
    members = {}
    for name in sorted(expected):
        info = archive.getinfo(name)
        if info.is_dir() or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
            raise ValueError('nonregular native artifact member')
        if info.flag_bits & 1:
            raise ValueError('encrypted native artifact member')
        members[name] = info
    return members


def unpack(artifact, directory, suffixes=SUFFIXES):
    with zipfile.ZipFile(artifact) as archive:
        for name, info in bundle_members(archive, suffixes).items():
            with open_zip_member(archive, info) as reader, (Path(directory) / name).open('wb') as writer:
                shutil.copyfileobj(reader, writer, materials.BUFFER_BYTES)
    return Path(directory) / RAW


def publish(report, destination, *, validator):
    """Stage, validate and atomically publish a scoped bundle."""
    report, destination = Path(report), Path(destination)
    _require_bundle_files(report)
    staging_parent = destination.parent
    while not staging_parent.exists():
        staging_parent = staging_parent.parent
    with tempfile.TemporaryDirectory(prefix='.lean-report.', dir=staging_parent) as directory:
        # Keep the incoming basename and every sidecar byte until the complete
        # private snapshot has passed canonical and current repository checks.
        staged = Path(directory) / 'bundle' / report.name
        staged.parent.mkdir()
        for suffix in SUFFIXES:
            shutil.copyfile(member(report, suffix), member(staged, suffix))
        accepted = {suffix: digest(member(staged, suffix)) for suffix in SUFFIXES}
        validator(staged)
        if any(digest(member(staged, suffix)) != sha for suffix, sha in accepted.items()):
            raise ValueError('publication snapshot changed during validation')

        checksum = f'{accepted[""]}  {destination.name}\n'.encode('ascii')
        member(staged, '.sha256').write_bytes(checksum)
        accepted['.sha256'] = hashlib.sha256(checksum).hexdigest()
        _require_bundle_files(staged)
        if any(digest(member(staged, suffix)) != sha for suffix, sha in accepted.items()):
            raise ValueError('publication snapshot changed after validation')
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = Path(directory) / 'previous'
        previous.mkdir()
        backups = {}
        for suffix in SUFFIXES:
            path = member(destination, suffix)
            backup = previous / (destination.name + suffix)
            if os.path.lexists(path):
                # Replacements never write through these links. Keep the old
                # bytes available without copying a second material archive.
                os.link(path, backup, follow_symlinks=False)
                backups[suffix] = backup
        # Publish the validated report last. Concurrent readers fail closed on a
        # transitional sidecar mismatch; they can never accept a mixed bundle.
        replaced = []
        try:
            for suffix in (*SUFFIXES[1:], ''):
                os.replace(member(staged, suffix), member(destination, suffix))
                replaced.append(suffix)
        except BaseException:
            for suffix in reversed(replaced):
                if suffix in backups:
                    os.replace(backups[suffix], member(destination, suffix))
                else:
                    member(destination, suffix).unlink()
            raise

if __name__ == '__main__':
    raise SystemExit('For scoped verification, use lean-report-input.sh verify-scoped.')
