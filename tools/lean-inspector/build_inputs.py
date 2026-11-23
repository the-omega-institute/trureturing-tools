"""Prepare compiler inputs and module reports for Lean consumers."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

import build_work
import materials
import publication

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ['leanInspector/inputDiscovery', 'leanInspector/reportInspector']


def selected_programs():
    defaults = ['leanInspector/LeanInformationAudit', 'leanInspector/reportInspector',
                'leanInspectorInterface/LeanInformationAuditInterface', 'reg/Reg']
    targets = json.loads(os.environ.get('STRATALINT_LEAN_BUILD_TARGETS', json.dumps(defaults)))
    if not isinstance(targets, list) or any(not isinstance(item, str) or not re.fullmatch(
            r'[A-Za-z][A-Za-z0-9_.]*(/[A-Za-z][A-Za-z0-9_.]*)?', item) for item in targets):
        raise ValueError('invalid preparation targets')
    return targets


def prepare_producer(repository=ROOT):
    producer = os.environ.get('STRATALINT_LEAN_PRODUCER_DLL')
    if not producer:
        subprocess.run(['dotnet', 'build', 'tools/StrataLint.Lean/StrataLint.Lean.csproj',
                        '-c', 'Release', '--nologo'], cwd=repository, check=True)
        producer = str(repository / 'tools/StrataLint.Lean/bin/Release/net10.0/StrataLint.Lean.dll')
    if not Path(producer).is_absolute() or not Path(producer).is_file():
        raise ValueError('missing prebuilt Lean producer DLL: ' + producer)
    os.environ['STRATALINT_LEAN_PRODUCER_DLL'] = producer
    return producer


def prepare_scope(roots, directory, *, repository=ROOT, save_scope=False):
    start = time.monotonic_ns()
    status = 'failed'
    try:
        prepare_producer(repository)
        command = ['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build-report-scope', roots]
        if save_scope:
            command.append(str(directory))
        run_preparation([command], directory, repository=repository)
        status = 'completed'
    finally:
        elapsed = time.monotonic_ns() - start
        names = build_work.built_modules(directory, 'programs') if status == 'completed' else None
        print('LEAN_INSPECTOR_SCOPE_PREPARE ' + json.dumps(dict(
            status=status, elapsed_ns=elapsed, compiled_modules=None if names is None else len(names),
            compiled_module_names=names), sort_keys=True), flush=True)


def prepare(roots, directory, need_inputs=True):
    if roots:
        prepare_scope(roots, directory)
        return
    if os.environ.get('FULL_BUILD') != '1':
        raise ValueError('full report preparation requires FULL_BUILD=1 or explicit LEAN_TARGETS')
    programs = selected_programs()
    if not need_inputs and not programs:
        return
    producer = prepare_producer()
    targets = [*programs, *TOOLS] if need_inputs else programs
    commands = [['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build-report-programs',
                 *sorted(set(targets))]]
    if need_inputs:
        modules = publication.selection.Selection(ROOT).modules()
        reported_paths = set(modules.values())
        utilities = json.loads(subprocess.check_output(['dotnet', producer, 'lean-utility-input'], cwd=ROOT))
        required = {entry['claimModule'] for entry in utilities if entry['modulePath'] in reported_paths}
        # Selected registrations refer to the registry through the compiler environment.
        required.add('LeanInformationAudit.TemplateEnrollment')
        manifest = directory / 'compile-modules.json'
        manifest.write_text(json.dumps(sorted(required | modules.keys())))
        commands.append(['bash', 'tools/scripts/worktree/lean-cache-run.sh',
                         '--build-report-modules', str(manifest)])
    run_preparation(commands, directory)


def run_preparation(commands, directory, *, repository=ROOT):
    out = directory / 'programs.stdout.log'
    err = directory / 'programs.stderr.log'
    with out.open('w') as stdout, err.open('w') as stderr:
        for command in commands:
            result = subprocess.run(command, cwd=repository, stdout=stdout, stderr=stderr)
            if result.returncode:
                break
    (directory / 'programs.exit.log').write_text(str(result.returncode) + '\n')
    if result.returncode:
        for path in (out, err):
            with path.open() as stream:
                for line in stream:
                    print(line, end='', file=sys.stderr)
        result.check_returncode()


class SparseFallback(Exception):
    """The authenticated predecessor cannot cover this invocation."""


def sparse_programs():
    """Resolve concrete program obligations; the installer covers library compiler modules."""
    executables = {'leanInspector/reportInspector': 'Inspector',
                   'leanInspector/inputDiscovery': 'LeanInformationAudit.Contract.InputDiscovery'}
    directories = ('tools/lean-inspector-reg', 'tools/lean-inspector-interface', 'Reg')
    inherited_libraries = {'leanInspector/LeanInformationAudit'}
    for directory in directories:
        config = tomllib.loads((ROOT / directory / 'lakefile.toml').read_text())
        package = config['name'] + '/'
        executables.update({package + item['name']: item.get('root', item['name'])
                            for item in config.get('lean_exe', [])})
        defaults = set(config['defaultTargets'])
        inherited_libraries.update(package + item['name'] for item in config.get('lean_lib', [])
                                   if item['name'] in defaults)
    selected = set(selected_programs()) | set(TOOLS)
    module_targets = {target.partition('/')[0] + '/' + root for target, root in executables.items()}
    result = {}
    for name in sorted(selected):
        if name in executables:
            result[name] = executables[name]
        elif name not in inherited_libraries and name not in module_targets:
            raise SparseFallback('unrecognized sparse program obligation ' + name)
    return result


def prepare_sparse_inputs(logs, plan, regenerate):
    import native
    import scoped

    if regenerate:
        raise SparseFallback('explicit regeneration requires every module')
    changed = set(plan['rebuild_modules']) | set(plan['removed_modules'])
    if changed.intersection({'Inspector', 'LeanInformationAudit.Contract.InputDiscovery',
                             'LeanInformationAudit.TemplateEnrollment'}):
        raise SparseFallback('shared inspector inputs changed')
    prepare_producer()
    entry = scoped.Entry(ROOT, logs, logs)
    entry.provision()
    programs = [name for name, module in sparse_programs().items() if module in changed]
    if programs:
        entry.phase('programs', ['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build', *programs])
    # The predecessor's residual frames carry the individual Lake artifacts
    # and the utility inputs used to construct each module report.
    entry.phase('inputs', ['bash', 'tools/scripts/worktree/lean-cache-run.sh',
        sys.executable, '-B', 'tools/lean-inspector/native.py', 'prepare', str(ROOT)])
    state = native.state(ROOT)
    records = publication.read_json((state / 'module-inputs.json').read_bytes())['records']
    targets = []
    for name, encoded in records.items():
        before = state / 'inputs' / (name + '.json')
        artifact = state / 'modules' / (name + '.zip')
        if (name in changed or not artifact.is_file() or not before.is_file()
                or before.read_bytes() != encoded.encode('ascii')
                or changed.intersection(json.loads(encoded)['claims'])):
            targets.append(name)
    if len(' '.join(targets).encode()) > 65536:
        raise SparseFallback('changed modules exceed the preparation argument limit')
    if targets:
        prepare(' '.join(targets), logs)
    return ['+' + name + ':report' for name in targets]


def prepare_cache(logs, regenerate):
    work_file = Path(os.environ.get('STRATALINT_LEAN_BUILD_WORK_FILE', ROOT / 'build/lean-cache/build-work.json'))
    work_file.unlink(missing_ok=True)
    publication.selection.Selection(ROOT).validate('lean-report')
    targets = [':moduleReports']
    if configured := os.environ.get('LEAN_CACHE_SPARSE_PLAN'):
        plan = publication.read_json(Path(configured).read_bytes())
        try:
            targets = prepare_sparse_inputs(logs, plan, regenerate)
        except SparseFallback as error:
            output = Path(os.environ['LEAN_CACHE_SPARSE_FALLBACK_FILE'])
            output.write_text(json.dumps(dict(target_commit=plan['target_commit'],
                base_manifest_sha256=plan['base_manifest_sha256'], reason=str(error))))
            raise ValueError(str(error)) from error
        if not targets:
            return
    else:
        prepare('', logs)
    activity = logs / 'native-work.jsonl'
    phases = logs / 'native-phases.jsonl'
    for path in (activity, phases):
        path.write_text('')
    environment = dict(os.environ, STRATALINT_INSPECTOR_ACTIVITY=str(activity),
                       STRATALINT_INSPECTOR_PHASES=str(phases))
    command = ['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--prebuilt']
    if regenerate:
        command.extend([sys.executable, '-B', 'tools/lean-inspector/native.py', 'regenerate', str(ROOT)])
    lake = os.environ.get('LAKE_BIN') or shutil.which('lake')
    if not lake:
        raise ValueError('an installed Lake toolchain is required (LAKE_BIN)')
    command.extend([lake, '--log-level=error', '-d', str(ROOT / 'tools/lean-inspector-reg'),
                    'build', *targets])
    with (logs / 'report.stdout.log').open('w') as stdout, (logs / 'report.stderr.log').open('w') as stderr:
        result = subprocess.run(command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr)
    (logs / 'report.exit.log').write_text(str(result.returncode) + '\n')
    if result.returncode:
        for name in ('report.stdout.log', 'report.stderr.log'):
            with (logs / name).open() as stream:
                for line in stream:
                    print(line, end='', file=sys.stderr)
        result.check_returncode()
    build_work.record(ROOT, logs, work_file, 'programs', 'report')


def compile_scope(roots, report_inputs):
    # The scoped cache writer and common build lock cover discovery and compilation.
    import scoped
    roots = scoped.parse_targets(roots)
    with ExitStack() as stack:
        temporary = report_inputs or Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='lean-scope-prepare.')))
        logs = temporary / 'logs' if report_inputs is not None else temporary
        entry = scoped.Entry(ROOT, temporary, logs)
        entry.provision()
        scope, utilities = entry.discover(roots, writer_owned=True)
        if report_inputs is not None:
            before = scoped.capture(ROOT, scope, utilities)
        subprocess.run(entry.lake_workspace('--log-level=error', '--no-ansi', 'build',
                       *TOOLS, *scoped.preparation_targets(scope)),
                       cwd=ROOT, env=entry.environment, check=True)
        if report_inputs is not None:
            (temporary / 'prepared-inputs.json').write_bytes(materials.canonical_json(before))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'prepare-cache', 'compile-scope-with-writer'])
    parser.add_argument('--targets', default='')
    parser.add_argument('--report-inputs', type=Path)
    parser.add_argument('--regenerate', action='store_true')
    args = parser.parse_args()
    if args.action == 'compile-scope-with-writer':
        compile_scope(args.targets, args.report_inputs)
        return
    if args.report_inputs is not None:
        if args.action != 'prepare' or not args.targets:
            parser.error('capturing scoped preparation requires explicit targets')
        args.report_inputs.mkdir()
        (args.report_inputs / 'logs').mkdir()
        prepare_scope(args.targets, args.report_inputs, save_scope=True)
        return
    if args.action == 'prepare-cache' and (args.targets or os.environ.get('FULL_BUILD') != '1'):
        parser.error('preparing the project cache requires FULL_BUILD=1')
    with tempfile.TemporaryDirectory(prefix='lean-programs.') as logs:
        if args.action == 'prepare-cache':
            prepare_cache(Path(logs), args.regenerate)
        else:
            prepare(args.targets, Path(logs))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 128 - error.returncode)
    except (OSError, ValueError) as error:
        print('lean-report build: ' + str(error), file=sys.stderr)
        sys.exit(2)
