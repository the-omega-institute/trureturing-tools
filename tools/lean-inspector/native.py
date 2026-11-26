#!/usr/bin/env python3
"""Production actions for Lake's Inspector facets; Lake owns module invalidation."""
from __future__ import annotations

import json
from contextlib import contextmanager, ExitStack
from functools import lru_cache
import io
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import materials
import publication as public

selection = public.selection
ROW_SUFFIXES = ('', '.materials.zip', '.provenance.json')
# Each process reads one bounded compiler-artifact closure and assesses its
# typed input owners directly from the compiled data.
NATIVE_BATCH_MODULES = 100
UTILITY_FIELDS = {'modulePath', 'claimGid', 'claimModule', 'claimSelector', 'claimSourcePath',
                  'claimSourceSha256', 'resultGid', 'resultModule', 'resultSelector'}


def check_workspace_producer(root):
    project = Path(root) / 'tools/StrataLint.Lean/StrataLint.Lean.csproj'
    result = subprocess.run(['dotnet', 'msbuild', str(project), '-question',
                             '-p:Configuration=Release', '-nologo', '-v:quiet'])
    if result.returncode:
        raise ValueError('prebuilt utility reader needs a build; run make lean-report-prepare '
                         'with the same LEAN_TARGETS or FULL_BUILD=1')


def batch_modules():
    """Runtime capacity only: keep report identities and reusable rows unchanged."""
    value = os.environ.get('STRATALINT_NATIVE_BATCH_MODULES', str(NATIVE_BATCH_MODULES))
    if not re.fullmatch(r'[1-9][0-9]{0,2}', value) or int(value) > NATIVE_BATCH_MODULES:
        raise ValueError(f'STRATALINT_NATIVE_BATCH_MODULES requires an integer from 1 to {NATIVE_BATCH_MODULES}')
    return int(value)


def activity(kind, count):
    """Invocation-local work counts; these never participate in reuse decisions."""
    path = os.environ.get('STRATALINT_INSPECTOR_ACTIVITY')
    if path:
        with Path(path).open('a', encoding='utf-8') as target:
            target.write(json.dumps({'kind': kind, 'count': count}) + '\n')


def module_work(operation, names):
    """Live module work, separate from Lake's replayable build logs."""
    path = os.environ.get('STRATALINT_INSPECTOR_MODULE_WORK')
    if path:
        with Path(path).open('a', encoding='utf-8') as target:
            for name in names:
                target.write(json.dumps({'operation': operation, 'module': name}) + '\n')


def diagnostic_failure(label, error):
    try:
        print(f'LEAN_INSPECTOR_DIAGNOSTIC_UNAVAILABLE {label}: {error}', file=sys.stderr)
    except (OSError, UnicodeError):
        pass


@contextmanager
def phase(name, **context):
    """Flush invocation phase boundaries independently of buffered process IO."""
    path = os.environ.get('STRATALINT_INSPECTOR_PHASES')

    def emit(boundary, **fields):
        if path:
            try:
                with Path(path).open('a', encoding='utf-8') as target:
                    target.write(json.dumps(dict(phase=name, boundary=boundary,
                        monotonic_ms=time.monotonic_ns() // 1_000_000, **context, **fields)) + '\n')
            except (OSError, ValueError) as error:
                diagnostic_failure('native phases', error)

    emit('start')
    success = False
    try:
        yield
        success = True
    finally:
        emit('finish', success=success)


def retain_request(root, executable, arguments, utilities, origin=None):
    """Optional request evidence, never an input to report/cache acceptance.

    Keep the actual argument/utility order. Inspector.main derives its imports
    from these inputs; its source digest identifies that derivation, without
    maintaining an independent Python approximation of the import list.
    """
    phase_path = os.environ.get('STRATALINT_INSPECTOR_PHASES')
    if not phase_path:
        return None
    temporary = None
    try:
        payload = dict(cwd=str(root), executable=str(executable), arguments=arguments,
            utilities=utilities, origin=origin,
            inspector_source_sha256=public.digest(Path(root) / 'tools/lean-inspector/Inspector.lean'),
            inspector_executable_sha256=(origin['inspector_executable_sha256'] if origin
                else public.digest(executable)),
            lean_toolchain=(Path(root) / 'lean-toolchain').read_text(encoding='utf-8'))
        # Unique names preserve multiple native calls in one diagnostic directory.
        # Only the phase's request_capture identifies this call; old files alone
        # are not evidence that a later invocation captured or ran a request.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                prefix='native-request-', suffix='.json.tmp',
                dir=Path(phase_path).parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(payload, target)
            target.write('\n')
        capture = temporary.with_suffix('')
        os.replace(temporary, capture)
        return str(capture)
    except (OSError, UnicodeError, ValueError) as error:
        diagnostic_failure('native request capture', error)
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                diagnostic_failure('native request temporary cleanup', error)


def state(root):
    return Path(root) / '.lake/build/lean-inspector'


def prebuilt_workspace(root, *command):
    """Run the report against Lake's existing configuration oleans inside the cache guard."""
    host = Path(root).resolve() / 'tools/lean-inspector-reg'
    args = list(command)
    directory = args.index('-d') + 1
    if Path(args[directory]).resolve() != host:
        raise ValueError('expected the report Lake workspace')

    # Keep the same relative paths to Reg, source packages and their real build outputs.
    with tempfile.TemporaryDirectory(prefix='.report-workspace-', dir=host.parent) as temp:
        view = Path(temp)
        for member in host.iterdir():
            if member.name != '.lake':
                (view / member.name).symlink_to(member)
        cache = view / '.lake'
        cache.mkdir()
        original = host / '.lake'
        if original.is_dir():
            for member in original.iterdir():
                if member.name != 'config':
                    (cache / member.name).symlink_to(member)
        manifest = json.loads((host / 'lake-manifest.json').read_text())
        entries = manifest['packages']
        overrides = original / 'package-overrides.json'
        if overrides.exists():
            entries = entries + json.loads(overrides.read_text())['packages']
        root_config = host / 'lakefile.lean'
        names = {'lakefile.olean', root_config.resolve().with_suffix('.olean').name}
        packages = host / manifest.get('packagesDir', '.lake/packages')
        for entry in entries:
            package = (host / entry['dir'] if entry['type'] == 'path' and not entry.get('copy')
                       else packages / entry['name'] / (entry.get('subDir') or ''))
            source = package / entry.get('configFile', 'lakefile')
            if not source.suffix:
                source = source.with_suffix('.lean')
            if source.suffix == '.lean':
                names.update((source.with_suffix('.olean').name, source.resolve().with_suffix('.olean').name))
        # Lake 4.35 loads at most the root and one package per manifest name. On
        # a cache miss it opens the trace for creation or a lock for writing BEFORE
        # elaboration. Directories at these paths reject both operations, even as root.
        for index in range(len(entries) + 1):
            target = cache / 'config' / str(index)
            target.mkdir(parents=True)
            for name in names:
                saved = original / 'config' / str(index) / name
                trace = saved.with_suffix('.olean.trace')
                if trace.is_file():
                    (target / trace.name).symlink_to(trace)
                else:
                    (target / trace.name).mkdir()
                if saved.is_file():
                    (target / name).symlink_to(saved)
                (target / (name + '.lock')).mkdir()
        args[directory] = str(view)
        status = subprocess.run(args).returncode
        if status:
            print('For missing or stale Lake configurations, run make lean-report-prepare '
                  'with the same LEAN_TARGETS or FULL_BUILD=1.',
                  file=sys.stderr)
            raise SystemExit(status if status >= 0 else 128 - status)


def regenerate(root, *command):
    """Explicit report-only rebuild, called inside lean-cache-run's writer guard.

    Remove generated report artifacts and traces, retaining native
    program directories and compiler input projections. Mathematical Lean and
    dependency build directories lie outside this report state. Only report ZIP
    facets bypass content-store lookup during regeneration.
    """
    if not command:
        raise ValueError('report regeneration requires a build command')
    generated = state(root)
    if generated.is_symlink():
        raise ValueError('report state must not be a symlink')
    if generated.exists():
        for path in generated.iterdir():
            if path.name in {'producer', 'interface', 'reg', 'judge-inputs'}:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    subprocess.run(command, cwd=root, check=True,
                   env=dict(os.environ, STRATALINT_INSPECTOR_REGENERATE_REPORTS='1'))


def write_if_changed(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    # Never mutate a possible restored hard link in place.
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


@phase('native-inputs')
def prepare(root):
    root = Path(root).resolve()
    with phase('native-input-selection'):
        inputs = selection.Selection(root)
        inputs.validate('lean-report')
        modules = inputs.modules()
        scope_file = os.environ.get('STRATALINT_INSPECTOR_SCOPE_FILE')
        if scope_file:
            scope_data = public.read_json(Path(scope_file).read_bytes())
            selected = {row['module']: row['source_path'] for row in scope_data['modules']}
            if not selected or any(modules.get(name) != path for name, path in selected.items()):
                raise ValueError('invalid scoped report membership')
            modules = selected
    producer = os.environ.get('STRATALINT_LEAN_PRODUCER_DLL')
    if producer:
        if not Path(producer).is_absolute() or not Path(producer).is_file():
            raise ValueError('candidate Lean producer must be an existing absolute path')
        command = ['dotnet', producer]
    else:
        command = ['dotnet', 'run', '--project', str(root / 'tools/StrataLint.Lean/StrataLint.Lean.csproj'),
            '--configuration', 'Release', '--no-build', '--no-restore', '--no-launch-profile', '--']
    with phase('native-utility-input'):
        utility_input = os.environ.get('STRATALINT_INSPECTOR_UTILITY_INPUT') if scope_file else None
        if utility_input:
            utility_bytes = Path(utility_input).read_bytes()
        else:
            result = subprocess.run([*command, 'lean-utility-input'],
                cwd=root, stdout=subprocess.PIPE, check=True)
            utility_bytes = result.stdout
    with phase('native-input-files'):
        utilities = public.read_json(utility_bytes)
        if not isinstance(utilities, list):
            raise ValueError('utility input must be an array')
        by_path = {}
        for utility in utilities:
            if scope_file and utility.get('modulePath') not in modules.values():
                continue
            materials.require_keys(utility, UTILITY_FIELDS, 'authoritative utility input')
            if any(not isinstance(value, str) or not value for value in utility.values()):
                raise ValueError('incomplete utility input')
            path = utility['modulePath']
            if path in by_path:
                raise ValueError('duplicate utility obligation')
            claim = inputs.safe_file(utility['claimSourcePath'])
            if utility['claimSourceSha256'] != 'sha256:' + public.digest(claim):
                raise ValueError('stale authoritative claim source')
            by_path[path] = utility
        records = {}
        for name, path in sorted(modules.items()):
            utility = [by_path[path]] if path in by_path else []
            records[name] = dict(utilities=utility,
                claims=sorted({u['claimModule'] for u in utility}), source_path=path)
        write_if_changed(state(root) / 'report-format',
            (selection.REPORT_FORMAT + '\n').encode('ascii'))
        # Lake writes only the records of modules whose facets it fetches.
        write_if_changed(state(root) / 'module-inputs.json', materials.canonical_json(dict(
            modules=sorted(modules), configs=inputs.expand('config_inputs'),
            records={name: materials.canonical_json(record).decode('ascii')
                     for name, record in records.items()})))


@lru_cache(maxsize=None)
def source_inventory(root):
    inputs = selection.Selection(root)
    return set(inputs.dependency_sources())


def validate_dependency_paths(root, utility_path, *, inputs=None):
    allowed = (set(inputs.dependency_sources()) if inputs is not None
               else source_inventory(str(root)))
    paths = public.read_json(Path(str(utility_path) + '.sources.json').read_bytes())
    materials.require_sorted_strings(sorted(set(paths)), 'native dependency sources')
    if set(paths) - allowed:
        raise ValueError('unregistered native dependency sources: ' + ', '.join(sorted(set(paths) - allowed)))


def input_projection(root, name):
    projection = public.read_json((state(root) / 'judge-inputs' / (name + '.json')).read_bytes())
    public.validate_input_projection(projection, name)
    return projection


@contextmanager
def inspector_child_output():
    """Retain this native child's bytes while Lake buffers its process output."""
    streams, options = [], {}
    for channel, variable in [('stdout', 'STRATALINT_INSPECTOR_CHILD_STDOUT'),
                              ('stderr', 'STRATALINT_INSPECTOR_CHILD_STDERR')]:
        path = os.environ.get(variable)
        if path:
            try:
                target = Path(path).open('a+b')
                start = target.tell()
                streams.append((channel, target, start))
                options[channel] = target
            except OSError as error:
                diagnostic_failure('native child ' + channel, error)
    try:
        yield options
    finally:
        for channel, target, start in streams:
            try:
                target.seek(start)
                output = getattr(sys, channel)
                while data := target.read(65536):
                    if hasattr(output, 'buffer'):
                        output.buffer.write(data)
                    else:
                        output.write(data.decode('utf-8', 'replace'))
                output.flush()
            except (OSError, ValueError) as error:
                diagnostic_failure('native child forwarding ' + channel, error)
            finally:
                try:
                    target.close()
                except OSError as error:
                    diagnostic_failure('native child close ' + channel, error)


def run_inspector(root, executable, arguments, request_file=None):
    try:
        command = ['--request-file', str(request_file)] if request_file else arguments
        with inspector_child_output() as output:
            subprocess.run([str(executable), *command], cwd=root, check=True, **output)
    except subprocess.CalledProcessError as error:
        raise ValueError(f'raw.reader_failed:exit={error.returncode}') from error


def row_binding(rows, root, module_name, utility_path, *, template_inputs=None):
    if len(rows) != 1 or rows[0]['module'] != module_name:
        raise ValueError('native module binding mismatch')
    row = rows[0]
    public.validate_template_sources(rows, root, inputs=template_inputs)
    record = public.read_json(Path(utility_path).read_bytes())
    path = record['source_path']
    if row['source_path'] != path:
        raise ValueError('native module source path mismatch')
    obligations = record['utilities']
    if not obligations:
        if 'utility_refutation' in row:
            raise ValueError('unexpected utility evidence')
    else:
        utility = obligations[0]
        evidence = row.get('utility_refutation', {})
        for raw, field in [('claimGid', 'claim_gid'), ('claimSourcePath', 'claim_source_path'),
                           ('claimSourceSha256', 'claim_source_sha256'), ('resultGid', 'result_gid')]:
            if evidence.get(field) != utility[raw]:
                raise ValueError('native utility binding mismatch')


def module(root, name, source, utility_path, executable, output):
    root, source, output = Path(root), Path(source), Path(output)
    template_inputs = selection.Selection(root)
    record = public.read_json(Path(utility_path).read_bytes())
    validate_dependency_paths(root, utility_path, inputs=template_inputs)
    projection = input_projection(root, name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.module.', dir=output.parent) as directory:
        directory = Path(directory)
        utility = directory / 'utility.json'
        utility.write_bytes(materials.canonical_json(record['utilities']))
        spool = directory / 'spool'
        spool.mkdir()
        spool_report = directory / 'spool.json'
        arguments = ['--output', str(spool_report), '--material-spool', str(spool),
            '--utility-input', str(utility), name, record['source_path'], 'sha256:' + public.digest(source)]
        capture = retain_request(root, executable, arguments, record['utilities'])
        with phase('native-inspect', request_capture=capture):
            run_inspector(root, executable, arguments)
        report = directory / public.RAW
        with phase('native-materials', module=name):
            materials.compact(spool_report, spool, report)
            public.write_origin(report, name,
                public.production_origin(root, executable, inputs=template_inputs), projection)
        with phase('native-validation', module=name):
            rows, _ = validate_module(report, root, name, utility_path, template_inputs=template_inputs)
        with phase('native-package', module=name):
            artifact = directory / 'module.zip'
            public.zip_files(artifact, [(public.RAW + suffix, public.member(report, suffix)) for suffix in ROW_SUFFIXES])
            os.replace(artifact, output)
    if projection['inputs']:
        print('LEAN_INSPECTOR_ASSESS module=' + name)
        module_work('assess', [name])
    module_work('extract', [name])
    activity('extract', 1)
    print(f'LEAN_INSPECTOR_EXTRACT module={name} declarations={len(rows[0]["declarations"])}')


def produce_row(row, directory, spool, root, origin, utility_path, output, template_inputs):
    """Keep spooled rows, compacted rows and validation certificates target-local."""
    name = row['module']
    row_dir = directory / name
    row_dir.mkdir()
    row_spool = row_dir / 'spool'
    row_spool.mkdir()
    for decl in row['declarations']:
        source = materials.regular_spool_file(spool, decl['material_file'])
        os.replace(source, row_spool / source.name)
    spool_report = row_dir / 'spool.json'
    with phase('native-materials', module=name):
        spool_report.write_bytes(materials.canonical_json({'schema': materials.SPOOL_SCHEMA, 'modules': [row]}))
        report = row_dir / public.RAW
        materials.compact(spool_report, row_spool, report)
        public.write_origin(report, name, origin, input_projection(root, name))
    with phase('native-validation', module=name):
        validate_module(report, root, name, utility_path, template_inputs=template_inputs)
    with phase('native-package', module=name):
        output.parent.mkdir(parents=True, exist_ok=True)
        artifact = row_dir / 'module.zip'
        public.zip_files(artifact, [(public.RAW + suffix, public.member(report, suffix)) for suffix in ROW_SUFFIXES])
        os.replace(artifact, output)


def produce_batch_chunk(requests, *, generated_index=None):
    inspect_batch_chunk(requests, generated_index=generated_index)


def inspect_batch_chunk(requests, *, check_constants=False, generated_index=None):
    root = Path(requests[0][0])
    template_inputs = selection.Selection(root)
    executable = requests[0][4]
    origin = public.production_origin(root, executable, inputs=template_inputs)
    if any(Path(row[0]) != root or row[4] != executable for row in requests):
        raise ValueError('mixed native batch owners')
    scratch = os.environ.get('STRATALINT_NATIVE_WINDOW_SCRATCH') or state(root)
    with tempfile.TemporaryDirectory(prefix='.inspection.', dir=scratch) as directory:
        directory = Path(directory)
        spool = directory / 'spool'
        spool.mkdir()
        utilities = []
        triples = []
        bindings = {}
        for request in requests:
            _, name, source, utility_path, _, output = request
            if name in bindings:
                raise ValueError('duplicate native batch module')
            record = public.read_json(Path(utility_path).read_bytes())
            utilities.extend(record['utilities'])
            triples.extend([name, record['source_path'], 'sha256:' + public.digest(source)])
            validate_dependency_paths(root, utility_path)
            bindings[name] = (utility_path, Path(output))
        utility_file = directory / 'utility.json'
        utility_file.write_bytes(materials.canonical_json(utilities))
        arguments = ['--output', str(directory / 'spool.json'), '--material-spool', str(spool),
                     '--utility-input', str(utility_file), *triples]
        if check_constants:
            arguments.insert(0, '--check-batch-constants')
        if generated_index is not None:
            arguments[0:0] = ['--generated-index', str(generated_index)]
        argument_file = directory / 'arguments.json'
        argument_file.write_text(json.dumps(arguments))
        capture = retain_request(root, executable, arguments, utilities, origin)
        with phase('native-constant-check' if check_constants else 'native-inspect', request_capture=capture):
            run_inspector(root, executable, arguments, argument_file)
        if check_constants:
            return
        declarations = 0
        completed = []
        for row in public.report_rows(directory / 'spool.json', materials.SPOOL_SCHEMA):
            if len(completed) >= len(bindings) or row['module'] != sorted(bindings)[len(completed)]:
                raise ValueError('incomplete native inspection batch')
            completed.append(row['module'])
            declarations += len(row['declarations'])
            utility_path, output = bindings[row['module']]
            produce_row(row, directory, spool, root, origin, utility_path, output, template_inputs)
            del row
        if completed != sorted(bindings):
            raise ValueError('incomplete native inspection batch')
        if list(spool.iterdir()):
            raise ValueError('unreferenced batch materials')
        for name in bindings:
            print('LEAN_INSPECTOR_EXTRACT module=' + name)
            if input_projection(root, name)['inputs']:
                print('LEAN_INSPECTOR_ASSESS module=' + name)
                module_work('assess', [name])
        module_work('extract', list(bindings))
        activity('extract', len(requests))
        print(f'LEAN_INSPECTOR_EXTRACT modules={len(requests)} declarations={declarations}')


def produce_split_batch(requests, size):
    """Keep the legacy generated-name index and publication boundary across caps.

    The index contains the Inspector's existing identity strings, keyed by its
    injective Name encoding. It is private to this partition, never a cache or
    report input. ConstantInfo comparisons remain entirely in the raw reader.
    """
    root = Path(requests[0][0])
    scratch = os.environ.get('STRATALINT_NATIVE_WINDOW_SCRATCH') or state(root)
    with tempfile.TemporaryDirectory(prefix='.partition.', dir=scratch) as directory:
        directory = Path(directory)
        generated_index = directory / 'generated.json'
        generated_index.write_text('[]')
        staged = []
        for index, request in enumerate(requests):
            copy = list(request)
            copy[5] = str(directory / f'{index}.zip')
            staged.append(copy)
        for offset in range(0, len(staged), size):
            produce_batch_chunk(staged[offset:offset + size], generated_index=generated_index)
        for request, copy in zip(requests, staged):
            Path(request[5]).parent.mkdir(parents=True, exist_ok=True)
            os.replace(copy[5], request[5])


def produce_batch(requests):
    size = batch_modules()
    requests = sorted(requests, key=lambda row: row[1])
    if not requests:
        return
    root, executable = Path(requests[0][0]), requests[0][4]
    if any(Path(row[0]) != root or row[4] != executable for row in requests):
        raise ValueError('mixed native batch owners')
    if any(left[1] == right[1] for left, right in zip(requests, requests[1:])):
        raise ValueError('duplicate native batch module')
    # Compatibility belongs to the original fixed windows and raw/judged
    # partitions. A smaller extraction capacity must not shrink that scope.
    for start in range(0, len(requests), NATIVE_BATCH_MODULES):
        chunk = requests[start:start + NATIVE_BATCH_MODULES]
        raw, judged = [], []
        for row in chunk:
            (judged if input_projection(root, row[1])['inputs'] else raw).append(row)
        for group in (raw, judged):
            if len(group) > size:
                # Complete legacy comparison scope, with one target closure
                # resident at a time and only detached owner/name state retained.
                inspect_batch_chunk(group, check_constants=True)
                produce_split_batch(group, size)
            elif group:
                produce_batch_chunk(group)


@phase('native-batch')
def batch(request_file):
    requests = public.read_json(Path(request_file).read_bytes())
    if any(kind != 'produce' for kind, _ in requests):
        raise ValueError('unknown native batch operation')
    produce = [args for _, args in requests]
    if produce:
        with phase('native-produce'):
            produce_batch(produce)


def stop_window_group(process):
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    process.wait()


def supervise_windows(groups, worker):
    """Stage disjoint groups privately and advance the Lake publication prefix on acknowledgement."""
    if os.name != 'posix':
        raise ValueError('parallel native report groups require POSIX process groups')
    if len(groups) != 2 or any(not group for group in groups):
        raise ValueError('expected two nonempty native report groups')
    if any(kind != 'produce' or len(args) != 6 for group in groups for kind, args in group):
        raise ValueError('invalid native report request')
    targets = [Path(args[5]) for group in groups for _, args in group]
    if len(set(targets)) != len(targets) or len({p.parent for p in targets}) != 1:
        raise ValueError('native report groups need disjoint outputs under one directory')
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    events = queue.SimpleQueue()
    completed = {}

    def receive():
        # The caller owns the only write end. EOF also covers an interrupted Lake.
        with os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0) as source:
            for line in source:
                events.put(('reply', line))
        events.put(('stop', 'Lake input closed'))

    def join(index, process):
        events.put(('worker', (index, process.wait())))

    def await_event(index, acknowledgement=False):
        while acknowledgement or index not in completed:
            kind, value = events.get()
            if kind == 'worker':
                completed[value[0]] = value[1]
            elif kind == 'stop':
                raise InterruptedError(str(value))
            elif acknowledgement and value == f'committed {index}\n'.encode():
                return
            else:
                raise ValueError('invalid native report publication acknowledgement')
        return completed[index]

    def settle_worker(process, index, log):
        if process.poll() != 0:
            stop_window_group(process)
        if process.returncode:
            print(f'native report group {index} exited {process.returncode}', file=sys.stderr, flush=True)
        with log.open('rb') as source:
            shutil.copyfileobj(source, sys.stderr.buffer)

    pending = []
    with ExitStack() as cleanup:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous = signal.signal(number, lambda n, _: events.put(('stop', f'signal {n}')))
            cleanup.callback(signal.signal, number, previous)
        threading.Thread(target=receive, daemon=True).start()
        private = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
            prefix='.report-windows.', dir=targets[0].parent)))
        def discard_pending():
            for path in pending:
                path.unlink(missing_ok=True)

        cleanup.callback(discard_pending)
        for index, group in enumerate(groups):
            directory = private / str(index)
            directory.mkdir()
            staged = [[kind, [*args[:5], str(directory / f'{n}.zip')]]
                      for n, (kind, args) in enumerate(group)]
            request = directory / 'request.json'
            request.write_text(json.dumps(staged))
            log = directory / 'worker.log'
            with log.open('wb') as output:
                process = subprocess.Popen([*worker, str(request)], stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                    env=dict(os.environ, STRATALINT_NATIVE_WINDOW_SCRATCH=str(directory)))
            cleanup.callback(settle_worker, process, index, log)
            threading.Thread(target=join, args=(index, process), daemon=True).start()
        for index, group in enumerate(groups):
            status = await_event(index)
            if status:
                raise ValueError(f'native report group {index} failed with exit {status}')
            for n, (_, args) in enumerate(group):
                target = Path(args[5])
                (private / str(index) / f'{n}.zip').replace(target)
                pending.append(target)
            print(f'ready {index}', flush=True)
            await_event(index, acknowledgement=True)
            pending.clear()


def windows(request_file):
    groups = public.read_json(Path(request_file).read_bytes())
    supervise_windows(groups, [sys.executable, '-B', str(Path(__file__).resolve()), 'batch'])


def aggregate_module(name, artifact, report_stream, material_spool, material_offsets):
    """Consume and destroy one artifact before opening the next target."""
    # Read members in place; a module artifact is never unpacked to disk.
    with zipfile.ZipFile(artifact) as bundle:
        data = {}
        for member, info in public.bundle_members(bundle, ROW_SUFFIXES).items():
            with public.open_zip_member(bundle, info) as reader:
                data[member[len(public.RAW):]] = reader.read()
        module_report = materials.require_keys(public.read_json(data['']), {'schema', 'modules'},
                                               'native module report')
        current = module_report['modules']
        origin = public.read_json(data['.provenance.json'])
        if (module_report['schema'] != materials.REPORT_SCHEMA
                or not isinstance(current, list) or len(current) != 1):
            raise ValueError('invalid native module report')
        row = current[0]
        public.require_module_keys(row)
        if row['module'] != name:
            raise ValueError('native aggregate membership mismatch')
        public.check_origin(origin, row, check_report=False)
        report_stream.write(materials.canonical_json(row)[:-1])
        declarations = len(row['declarations'])
        del module_report, current, row
        # Produced materials matched their content addresses; move their
        # deflated bytes without decompressing or compressing again.
        with zipfile.ZipFile(io.BytesIO(data['.materials.zip'])) as archive:
            for entry in archive.infolist():
                shape = (entry.CRC, entry.file_size)
                if entry.filename in material_offsets:
                    if material_offsets[entry.filename][2:] != shape:
                        raise ValueError('statement material address collision')
                    continue
                data = compressed_member(archive, entry)
                material_spool.seek(0, os.SEEK_END)
                material_offsets[entry.filename] = (material_spool.tell(), len(data), *shape)
                material_spool.write(data)
    return origin, declarations


def aggregate(root, output, *artifacts, config, schema, write_sidecars):
    """Concatenate module artifacts that were validated when produced."""
    root, output = Path(root), Path(output)
    if len(artifacts) != len(config['modules']):
        raise ValueError('native aggregate membership mismatch')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.aggregate.', dir=output.parent) as directory, \
            tempfile.TemporaryFile(dir=output.parent) as material_spool, \
            (Path(directory) / public.RAW).open('wb') as report_stream:
        directory = Path(directory)
        material_offsets = {}
        report = directory / public.RAW
        report_stream.write(b'{"modules": [')
        declarations = 0
        origins = {}
        for name, artifact in zip(config['modules'], artifacts):
            if name != config['modules'][0]:
                report_stream.write(b', ')
            origin, count = aggregate_module(name, artifact, report_stream, material_spool, material_offsets)
            origins[name] = origin
            declarations += count
        report_stream.write(b'], "schema": ' + materials.canonical_json(schema)[:-1] + b'}\n')
        report_stream.close()
        with zipfile.ZipFile(public.member(report, '.materials.zip'), 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            for name, (offset, size, crc, file_size) in sorted(material_offsets.items()):
                info = zipfile.ZipInfo(name, materials.ARCHIVE_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.CRC, info.file_size = crc, file_size
                material_spool.seek(offset)
                data = material_spool.read(size)
                if len(data) != size:
                    raise ValueError('truncated private material spool')
                append_compressed(archive, info, data)
        write_sidecars(report, config['coordinates'], origins)
        artifact = directory / 'report.zip'
        public.zip_files(artifact, [(public.RAW + suffix, public.member(report, suffix)) for suffix in public.SUFFIXES])
        os.replace(artifact, output)
        activity('aggregate', 1)
        print(f'LEAN_INSPECTOR_AGGREGATE modules={len(config["modules"])} declarations={declarations}')


def compressed_member(archive, info):
    """The stored deflate stream of one member, exactly as written."""
    if info.compress_type != zipfile.ZIP_DEFLATED or info.flag_bits & 1:
        raise ValueError('statement material is not a plain deflated member')
    archive.fp.seek(info.header_offset)
    header = archive.fp.read(30)
    if len(header) != 30 or header[:4] != b'PK\x03\x04':
        raise ValueError('invalid statement material header')
    name_length, extra_length = struct.unpack('<HH', header[26:30])
    archive.fp.seek(info.header_offset + 30 + name_length + extra_length)
    data = archive.fp.read(info.compress_size)
    if len(data) != info.compress_size:
        raise ValueError('truncated statement material')
    return data


def append_compressed(archive, info, data):
    """Write a member whose deflated bytes and final sizes are already known."""
    info.compress_size = len(data)
    info.flag_bits = 0
    archive.fp.seek(archive.start_dir)
    info.header_offset = archive.fp.tell()
    archive._writecheck(info)
    archive._didModify = True
    zip64 = info.file_size > zipfile.ZIP64_LIMIT or info.compress_size > zipfile.ZIP64_LIMIT
    archive.fp.write(info.FileHeader(zip64))
    archive.fp.write(data)
    archive.start_dir = archive.fp.tell()
    archive.filelist.append(info)
    archive.NameToInfo[info.filename] = info


def validate_module(report, root, name, utility, *, verified_materials=None, template_inputs=None):
    rows = public.validate_rows(report, public.member(report, '.materials.zip'), verified_materials)
    row_binding(rows, root, name, utility, template_inputs=template_inputs)
    origin = public.validate_origin(report, rows)
    return rows, origin


def main():
    if sys.argv[1:] == ['batch-size']:
        print(f'LEAN_INSPECTOR_BATCH_MODULES {batch_modules()}')
        return
    actions = {'prepare': prepare,
               'module': module,
               'batch': batch, 'windows': windows,
               'regenerate': regenerate, 'prebuilt-workspace': prebuilt_workspace,
               'check-workspace-producer': check_workspace_producer}
    if len(sys.argv) < 2 or sys.argv[1] not in actions:
        raise ValueError('expected a native module report operation')
    with phase('native-' + sys.argv[1]):
        actions[sys.argv[1]](*sys.argv[2:])


if __name__ == '__main__':
    try:
        main()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f'lean-inspector-native: {error}', file=sys.stderr)
        raise SystemExit(1)
