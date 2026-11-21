"""Explicit compilation and regression checks for consumers that request a built report."""
from collections import defaultdict
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import build_work
import materials
import native
import publication
import reuse

ROOT = Path(__file__).resolve().parents[2]
PRODUCER = ROOT / 'tools/StrataLint.Lean/bin/Release/net10.0/StrataLint.Lean.dll'
TOOLS = ['leanInspector/inputDiscovery', 'leanInspector/reportInspector']


def selected_programs():
    defaults = ['leanInspector/LeanInformationAudit', 'leanInspector/reportInspector',
                'leanInspectorInterface/LeanInformationAuditInterface', 'reg/Reg']
    targets = json.loads(os.environ.get('STRATALINT_LEAN_BUILD_TARGETS', json.dumps(defaults)))
    if not isinstance(targets, list) or any(not isinstance(item, str) or not re.fullmatch(
            r'[A-Za-z][A-Za-z0-9_.]*(/[A-Za-z][A-Za-z0-9_.]*)?', item) for item in targets):
        raise ValueError('invalid preparation targets')
    return targets


def prepare_producer():
    producer = os.environ.get('STRATALINT_LEAN_PRODUCER_DLL')
    if not producer:
        subprocess.run(['dotnet', 'build', 'tools/StrataLint.Lean/StrataLint.Lean.csproj',
                        '-c', 'Release', '--nologo'], cwd=ROOT, check=True)
        producer = str(PRODUCER)
    if not Path(producer).is_absolute() or not Path(producer).is_file():
        raise ValueError('missing prebuilt Lean producer DLL: ' + producer)
    os.environ['STRATALINT_LEAN_PRODUCER_DLL'] = producer
    return producer


def prepare(roots, directory, need_inputs=True):
    if not roots and os.environ.get('FULL_BUILD') != '1':
        raise ValueError('full report preparation requires FULL_BUILD=1 or explicit LEAN_TARGETS')
    programs = [] if roots else selected_programs()
    if not need_inputs and not programs:
        return
    producer = prepare_producer()
    if roots:
        commands = [['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build-report-scope', roots]]
    else:
        targets = programs
        if need_inputs:
            targets = [*targets, *TOOLS, 'regInspector/auricFibAnalysis']
        commands = [['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build-report-programs',
                     *sorted(set(targets))]]
    if not roots and need_inputs:
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
    out = directory / 'programs.stdout.log'
    err = directory / 'programs.stderr.log'
    with out.open('w') as stdout, err.open('w') as stderr:
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr)
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


def verify_sparse_checkout(plan):
    if reuse._git(ROOT, 'rev-parse', '--verify', 'HEAD') != plan['target_commit']:
        raise ValueError('sparse report target differs from HEAD')
    if reuse._git(ROOT, 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('sparse report requires the authenticated clean checkout')


def sparse_plan(path):
    import scoped

    plan = publication.read_json(Path(path).read_bytes())
    if plan.get('schema') != 'lean-release-sparse-plan-v1':
        raise ValueError('unsupported sparse plan schema')
    for key, width in [('target_commit', 40), ('base_commit', 40),
                       ('base_manifest_sha256', 64), ('base_report_sha256', 64)]:
        if not re.fullmatch('[0-9a-f]{' + str(width) + '}', plan.get(key, '')):
            raise ValueError('invalid sparse plan ' + key)
    if not isinstance(plan.get('base_tag'), str) or not plan['base_tag']:
        raise ValueError('sparse plan lacks predecessor tag')
    for key in ('rebuild_modules', 'removed_modules', 'restore_modules'):
        materials.require_sorted_strings(plan.get(key), key)
        if any(not scoped.MODULE.fullmatch(name) for name in plan[key]):
            raise ValueError('invalid sparse module name')
    verify_sparse_checkout(plan)
    if reuse._ancestry(ROOT, plan['base_commit'], plan['target_commit']) is not True:
        raise ValueError('sparse report predecessor is not a verified ancestor')
    return plan


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
        if directory == 'tools/lean-inspector-reg':
            defaults.add('LeanInformationAuditFibTests')
        inherited_libraries.update(package + item['name'] for item in config.get('lean_lib', [])
                                   if item['name'] in defaults)
    selected = set(selected_programs()) | set(TOOLS) | {'regInspector/auricFibAnalysis'}
    module_targets = {target.partition('/')[0] + '/' + root for target, root in executables.items()}
    result = {}
    for name in sorted(selected):
        if name in executables:
            result[name] = executables[name]
        elif name not in inherited_libraries and name not in module_targets:
            raise SparseFallback('unrecognized sparse program obligation ' + name)
    return result


def sparse_graph(entry, modules, programs):
    """Read Lake headers for the registered population and tool roots without building their exports."""
    import scoped

    roots = sorted(set(modules).union(programs.values(), {'LeanInformationAudit.TemplateEnrollment'}))
    request = entry.temporary / 'graph-request.json'
    output = entry.temporary / 'graph.json'
    request.write_bytes(materials.canonical_json(dict(roots=roots, modules=sorted(modules),
                                                      utilities=[], utility_inputs=[])))
    # A sparse seed can have zero compiler work and no local Lake configuration oleans.
    entry.phase('headers', ['bash', 'tools/scripts/worktree/lean-cache-run.sh',
        *entry.lake_workspace('script', 'run', 'leanInspector/reportScope', str(request), str(output))])
    graph = publication.read_json(output.read_bytes())
    if graph['roots'] != roots:
        raise ValueError('incomplete sparse header graph roots')
    scoped.check_scope(entry.root, dict(graph, roots=sorted(modules)), sorted(modules))
    deps = {row['module']: row for row in graph['dependencies']}
    if not set(roots) <= deps.keys():
        raise ValueError('incomplete sparse header graph membership')
    return deps


def utility_matches(row, obligation):
    evidence = row.get('utility_refutation', {})
    return bool(evidence) == bool(obligation) and all(evidence.get(field) == obligation.get(key)
        for key, field in native.UTILITY_BINDINGS)


def sparse_rows(plan, seed, captured, modules, deps, utilities):
    """Decide row reuse from the checked aggregate, current compiler graph and claim inputs."""
    receipt = reuse.read_receipt(seed)
    if receipt['completed'] != reuse.CHECKED:
        raise SparseFallback('predecessor lacks checked FIB inputs')
    before = receipt['inputs']
    if (not captured.get('eligible') or {k: v for k, v in before.items() if k != 'files'}
            != {k: v for k, v in captured.items() if k != 'files'}):
        raise SparseFallback('report format, execution or registered dependency coverage changed')
    old, current = before['files'], captured['files']
    removed = set(plan['removed_modules'])
    if removed & modules.keys():
        raise ValueError('removed sparse module is present in the target')
    origins = publication.read_json(publication.member(seed, '.provenance.json').read_bytes())['module_origins']
    if set(origins) - modules.keys() != removed:
        raise ValueError('sparse removal set differs from previous report membership')
    changed = {path for path in old.keys() | current.keys() if old.get(path) != current.get(path)}
    tracked = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--no-renames', '--name-only', '-z',
                                      plan['base_commit'], plan['target_commit'], '--']).decode().split('\0')
    old_paths = {name.replace('.', '/') + '.lean' for name in origins}
    report_paths = old_paths | set(modules.values())
    if changed - report_paths or any(path not in report_paths and (
            path.endswith('.lean') or path in ('lean-report-inputs.json', 'Meta/tools-release.json')
            or Path(path).name in ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain')) for path in tracked):
        raise SparseFallback('global report or compiler input changed')
    if not changed <= set(tracked):
        raise ValueError('report input changes do not agree with the pinned Git commits')
    roots = set(plan['rebuild_modules'])
    affected = roots | removed | (modules.keys() - origins.keys())
    reverse = defaultdict(set)
    for name, row in deps.items():
        for parent in row['imports']:
            reverse[parent].add(name)
    by_path = {u['modulePath']: u for u in utilities}
    if len(by_path) != len(utilities):
        raise ValueError('duplicate authoritative utility obligation')
    reported_paths = set(modules.values())
    for utility in utilities:
        materials.require_keys(utility, native.UTILITY_FIELDS, 'authoritative utility input')
        if utility['modulePath'] in reported_paths and utility['claimModule'] not in deps:
            raise SparseFallback('utility claim is outside the discovered compiler graph')
    changed_obligations = set()
    seen = set()
    for row in publication.report_rows(seed, materials.REPORT_SCHEMA):
        name, path = row['module'], row['source_path']
        publication.check_origin(origins[name], row)
        if (name in seen or path != name.replace('.', '/') + '.lean' or path not in old
                or row['source_sha256'] != 'sha256:' + old[path]['sha256']):
            raise ValueError('invalid predecessor module source binding')
        seen.add(name)
        if name in removed:
            continue
        if path != modules[name]:
            raise ValueError('current module source path mismatch')
        if path in changed or row['imports'] != deps[name]['imports']:
            affected.add(name)
        if not utility_matches(row, by_path.get(path, {})):
            changed_obligations.add(name)
    if seen != origins.keys():
        raise ValueError('incomplete predecessor report')
    # Only compiler import changes propagate through successors. External claim
    # and tool changes invalidate the report row that consumes them.
    pending = list(affected)
    while pending:
        for child in reverse[pending.pop()] - affected:
            affected.add(child)
            pending.append(child)
    if affected.intersection({'Inspector', 'LeanInformationAudit.Contract.InputDiscovery'}):
        raise SparseFallback('a report executable depends on changed compiler inputs')
    rebuilt = affected.intersection(modules)
    if rebuilt - roots:
        raise SparseFallback('compiler plan omits a changed module or transitive successor')
    rebuilt.update(changed_obligations)
    for name, path in modules.items():
        obligation = by_path.get(path)
        if obligation and obligation['claimModule'] in affected:
            rebuilt.add(name)
        for item in origins.get(name, {}).get('input_projection', {}).get('inputs', []):
            runtime = ('LeanInformationAuditRegAnalysis.AuricFib.Main'
                       if item['type'] == 'LeanInformationAudit.AuricFib.Contract.Application'
                       else 'LeanInformationAudit.TemplateEnrollment')
            if runtime in affected:
                rebuilt.add(name)
    return sorted(rebuilt), affected


def sparse_candidate(directory, seed, logs, captured, plan):
    import scoped

    prior = directory / 'base' / publication.RAW
    prior.parent.mkdir()
    with reuse.publication_guard(ROOT, seed):
        for suffix in (*publication.SUFFIXES, reuse.SUFFIX):
            source = publication.member(seed, suffix)
            if source.is_symlink() or not source.is_file():
                raise SparseFallback('predecessor report is incomplete')
            os.link(source, publication.member(prior, suffix))
    if reuse.seed_identity(prior) != plan['base_report_sha256']:
        raise ValueError('restored report differs from authenticated predecessor')
    publication.validate_bundle(prior, check_rows=False)
    modules = publication.selection.Selection(ROOT).modules()
    entry = scoped.Entry(ROOT, directory, logs)
    entry.provision()
    programs = sparse_programs()
    deps = sparse_graph(entry, modules, programs)
    utility_path = entry.phase('utility', ['dotnet', entry.producer, 'lean-utility-input'])
    utilities = publication.read_json(utility_path.read_bytes())
    rebuilt, affected = sparse_rows(plan, prior, captured, modules, deps, utilities)
    if len(rebuilt) == len(modules):
        raise SparseFallback('all current report modules require extraction')
    if len(' '.join(rebuilt).encode()) > 65536:
        raise SparseFallback('affected scope exceeds the single-argument preparation interface')
    relink = [name for name, module in programs.items() if module in affected]
    if relink:
        entry.phase('programs', ['bash', 'tools/scripts/worktree/lean-cache-run.sh', '--build', *relink])
    if rebuilt:
        prepare(' '.join(rebuilt), directory)
    for filename in ('module-work.jsonl', 'native-work.jsonl', 'native-phases.jsonl'):
        (logs / filename).write_text('')
    entry.environment.update(STRATALINT_INSPECTOR_MODULE_WORK=str(logs / 'module-work.jsonl'),
        STRATALINT_INSPECTOR_ACTIVITY=str(logs / 'native-work.jsonl'),
        STRATALINT_INSPECTOR_PHASES=str(logs / 'native-phases.jsonl'))
    if rebuilt:
        entry.phase('report', entry.guarded('build', *['+' + name + ':report' for name in rebuilt]))
    else:
        for stream in ('stdout', 'stderr'):
            (logs / ('report.' + stream + '.log')).write_text('')
        (logs / 'report.exit.log').write_text('0\n')
    names = sorted(modules)
    replacements = {name: native.state(ROOT) / 'modules' / (name + '.zip') for name in rebuilt}
    artifact = directory / 'aggregate.zip'
    native.aggregate(ROOT, artifact, *(replacements.get(name) for name in names),
                     config=dict(modules=names, coordinates=publication.coordinates(ROOT)), inherited=prior)
    candidate = publication.unpack(artifact, directory)
    publication.validate_bundle(candidate, publication.coordinates(ROOT), ROOT)
    by_path = {utility['modulePath']: utility for utility in utilities}
    for row in publication.report_rows(candidate, materials.REPORT_SCHEMA):
        path, name = row['source_path'], row['module']
        if (row['source_sha256'] != 'sha256:' + captured['files'][path]['sha256']
                or row['imports'] != deps[name]['imports']):
            raise ValueError('sparse report row does not match current source and imports')
        if not utility_matches(row, by_path.get(path, {})):
            raise ValueError('sparse report row does not match current utility inputs')
    verify_sparse_checkout(plan)
    print(f'LEAN_REPORT_SPARSE modules={len(names)} rebuilt={len(rebuilt)} inherited={len(names)-len(rebuilt)}',
          flush=True)


def checked_report(directory, output, seed, program_logs, work_file, *, try_seed, plan=None):
    captured = reuse.capture(ROOT)
    candidate = directory / publication.RAW
    logs = Path(os.environ.get('STRATALINT_LEAN_REPORT_LOG_DIR', str(output) + '.logs')).resolve()
    reused = False
    seed_commit = None
    if try_seed:
        with reuse.publication_guard(ROOT, seed):
            if seed == reuse.canonical_seed(ROOT):
                seed_commit = reuse.read_seed_base(ROOT)
            reused = (captured.get('covers_compiler_dependencies', False)
                      and not reuse._reuse(ROOT, seed, candidate)['needs_lake'])
        if not reused:
            # Seed bytes changed since resource planning. Prepare the compiler inputs for extraction.
            prepare('', program_logs)
    if plan is not None:
        logs.mkdir(parents=True, exist_ok=True)
        sparse_candidate(directory, seed, logs, captured, plan)
    elif not reused:
        env = dict(os.environ, STRATALINT_LEAN_REPORT_LOG_DIR=str(logs),
                   STRATALINT_LEAN_BUILD_WORK_FILE=str(directory / 'analysis-work.json'))
        subprocess.run(['make', 'lean-report', 'LEAN_REPORT_CACHE_MISS_POLICY=reuse-or-build',
                        'LEAN_REPORT=' + str(candidate)], cwd=ROOT, env=env, check=True)
    # Keep stable comparison data across regressions that build transient Lean fixtures.
    previous = directory / 'previous' / publication.RAW
    previous.parent.mkdir()
    observed_members = ('', '.provenance.json', reuse.SUFFIX)
    copied_members = set()
    with reuse.publication_guard(ROOT, output):
        if plan is not None and reuse.seed_identity(output) != plan['base_report_sha256']:
            raise ValueError('published report changed during sparse generation')
        try:
            checked_previous = reuse.read_receipt(output)['completed'] == reuse.CHECKED
        except reuse.INVALID_SEED:
            checked_previous = False
        for suffix in observed_members:
            path = publication.member(output, suffix)
            if path.exists():
                snapshot = publication.member(previous, suffix)
                if path.stat().st_dev == directory.stat().st_dev:
                    os.link(path, snapshot)
                else:
                    shutil.copyfile(path, snapshot)
                    copied_members.add(suffix)
    selected = os.environ.get('STRATALINT_FIB_REGRESSIONS_SELECTED', 'false')
    if selected not in ('true', 'false'):
        raise ValueError('invalid FIB regression selection')
    same = (checked_previous and reused and seed == output and previous.exists()
            and publication.digest(previous) == publication.digest(candidate))
    if not same or selected == 'true':
        subprocess.run([sys.executable, '-B', 'tools/lean-inspector/fib_analysis.py', str(candidate),
                        '--regressions', str(ROOT), '--previous', str(previous),
                        '--selected', selected, *([] if checked_previous else ['--unverified-previous'])], cwd=ROOT, check=True)
    else:
        print('LEAN_INSPECTOR_FIB_REGRESSIONS changed=false selected=false', flush=True)
    with reuse.publication_guard(ROOT, output):
        for suffix in observed_members:
            observed = publication.member(previous, suffix)
            current = publication.member(output, suffix)
            if not current.exists() and not observed.exists():
                continue
            unchanged = current.exists() and observed.exists() and (
                publication.digest(current) == publication.digest(observed) if suffix in copied_members
                else current.samefile(observed))
            if not unchanged:
                raise ValueError('published report changed during regression check')
        if plan is not None:
            verify_sparse_checkout(plan)
        if reuse.capture(ROOT) != captured:
            raise ValueError('inputs changed during report check')
        identity = publication.publish(candidate, output, publication.coordinates(ROOT), ROOT, validate=False)
        publication.member(output, reuse.SUFFIX).unlink(missing_ok=True)
        reuse._seal(ROOT, output, captured, identity, checked=True, reused=reused, seed_commit=seed_commit)
    try:
        logs.mkdir(parents=True, exist_ok=True)
        for path in program_logs.glob('programs.*.log'):
            shutil.copyfile(path, logs / path.name)
        phases = [] if reused else ['report']
        if (program_logs / 'programs.exit.log').exists():
            phases.append('programs')
        build_work.record(ROOT, logs, work_file, *phases)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('LEAN_BUILD_WORK_UNKNOWN ' + str(error), file=sys.stderr)


def compile_scope(roots):
    # The scoped cache writer and common build lock cover discovery and compilation.
    import scoped
    roots = scoped.parse_targets(roots)
    with tempfile.TemporaryDirectory(prefix='lean-scope-prepare.') as directory:
        temporary = Path(directory)
        entry = scoped.Entry(ROOT, temporary, temporary)
        entry.provision()
        scope, _ = entry.discover(roots, writer_owned=True)
        subprocess.run(entry.lake_workspace('--log-level=error', 'build', *TOOLS, *scoped.preparation_targets(scope)),
                       cwd=ROOT, env=entry.environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'check', 'compile-scope-with-writer'])
    parser.add_argument('--targets', default='')
    parser.add_argument('--output', type=Path, default=reuse.canonical_seed(ROOT))
    args = parser.parse_args()
    if args.action == 'compile-scope-with-writer':
        compile_scope(args.targets)
        return
    if args.action == 'check' and (args.targets or os.environ.get('FULL_BUILD') != '1'):
        raise ValueError('checking the full report requires FULL_BUILD=1')
    plan_path = os.environ.get('LEAN_CACHE_SPARSE_PLAN') if args.action == 'check' else None
    plan = sparse_plan(plan_path) if plan_path else None
    try:
        run_entry(args, plan)
    except SparseFallback as error:
        marker = os.environ.get('LEAN_CACHE_SPARSE_FALLBACK_FILE')
        if marker and plan is not None:
            native.write_if_changed(Path(marker), materials.canonical_json(dict(target_commit=plan['target_commit'],
                base_manifest_sha256=plan['base_manifest_sha256'], reason=str(error))))
        raise


def run_entry(args, plan):
    work_file = Path(os.environ.get('STRATALINT_LEAN_BUILD_WORK_FILE', ROOT / 'build/lean-cache/build-work.json'))
    if args.action == 'check':
        work_file.unlink(missing_ok=True)
        # A reusable report can coexist with missing project compiler artifacts.
        if plan is None and os.environ.get('PARTIAL_INDEXED_PROJECT') == 'true':
            subprocess.run(['make', 'lean', 'FULL_BUILD=1'], cwd=ROOT, check=True)
    output = args.output.resolve()
    seed = Path(os.environ.get('STRATALINT_LEAN_REPORT_REUSE', output)).resolve()
    force = (os.environ.get('REBUILD_REPORT_CACHE') == '1'
             or os.environ.get('REGENERATE_REPORT_ARTIFACTS') == '1')
    if plan is not None and (force or seed != output):
        raise SparseFallback('full regeneration or an alternate report seed was requested')
    if (plan is None and args.action == 'check' and not force
            and os.environ.get('LEAN_REPORT_CACHE_MISS_POLICY') == 'fetch-or-fail'):
        with reuse.compatible_seed(ROOT, seed) as compatible:
            seed = compatible
    need_inputs = (plan is not None or args.action == 'prepare' or force
                   or reuse.probe(ROOT, seed, require_covered_dependencies=True)['needs_lake'])
    with tempfile.TemporaryDirectory(prefix='lean-programs.') as logs:
        program_logs = Path(logs)
        if plan is not None:
            prepare_producer()
        else:
            prepare(args.targets, program_logs, need_inputs)
        if args.action == 'check':
            # Release publishers may archive the build directory while FIB checks run.
            # Keep all candidate bytes and prior-generation links outside that archive.
            staging_parent = ROOT / '.lake' if output.is_relative_to(ROOT / '.lake/build') else output.parent
            staging_parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.lean-report-build.', dir=staging_parent) as staging:
                checked_report(Path(staging), output, seed, program_logs, work_file, try_seed=not need_inputs,
                               plan=plan)


if __name__ == '__main__':
    try:
        main()
    except SparseFallback as error:
        print('LEAN_REPORT_SPARSE_FALLBACK ' + str(error), file=sys.stderr)
        sys.exit(75)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 128 - error.returncode)
    except (OSError, ValueError) as error:
        print('lean-report build: ' + str(error), file=sys.stderr)
        sys.exit(2)
