"""Complete the shared compiler inventory with individually reusable reports and executables."""

import argparse
import json
from graphlib import TopologicalSorter
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

from lean_artifact_inventory import Artifact, PROJECT_BUILD
from lean_cache import binary_platform
from lean_cache_generation import restore, stage_frames
from lean_cache_release import report_selection
from lean_generation_checkpoints import discover, remote_for, save
from lean_generation_index import matches, read
from lean_generation_plan import checked_hits, digest, module_targets, write
from lean_release_index import write_frame


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lean-inspector"))
import publication as report_publication


# These Lake executables are required by the CI report and audit consumers.
PROGRAMS = {
    'reportInspector': ('leanInspector', 'Inspector', 'producer'),
    'inputDiscovery': ('leanInspector', 'LeanInformationAudit.Contract.InputDiscovery', 'producer'),
    'compiledJudgeTests': ('regInspector', 'LeanInformationAuditRegTests.CompiledDiscovery', 'reg'),
}


def facet_paths(name):
    kind, target = name.split('/', 1)
    if kind == 'report':
        path = f'lean-inspector/modules/{target}.zip'
    elif kind == 'native':
        path = f'lean-inspector/{PROGRAMS[target][2]}/bin/{target}'
    else:
        raise ValueError('unknown cache facet: ' + name)
    return path, path + '.hash', path + '.trace'


def utility_inputs(root):
    project = 'tools/StrataLint.Lean/StrataLint.Lean.csproj'
    # Run the snapshot's own utility parser to bind claims outside Lean imports.
    subprocess.run(['dotnet', 'build', project, '-c', 'Release', '--nologo',
                    '--verbosity', 'quiet'], cwd=root, check=True, stdout=subprocess.DEVNULL)
    dll = 'tools/StrataLint.Lean/bin/Release/net10.0/StrataLint.Lean.dll'
    rows = json.loads(subprocess.check_output(['dotnet', dll, 'lean-utility-input'], cwd=root))
    return {row['modulePath']: row for row in rows}


def all_targets(root, module_rows=None):
    targets = dict(module_targets(root) if module_rows is None else module_rows)
    selection = report_selection.Selection(root)
    # The module facet traces its compiler exports, claim inputs and report
    # format. Aggregate configuration and production provenance have their
    # own checks when the report is consumed.
    report_format = report_selection.REPORT_FORMAT
    utilities = utility_inputs(root)
    for exe, (package, main, _) in PROGRAMS.items():
        name = 'native/' + exe
        deps = [main]
        targets[name] = {'key': digest(['native-v1', binary_platform(), package, exe, targets[main]['key']]), 'deps': deps}
    # A module cannot contain typed registration inputs without the contract
    # types in its import closure. These are the same runtime roots used by
    # scoped report preparation.
    contracts = {'LeanInformationAuditInterface.Contract.Catalog',
                 'LeanInformationAuditInterface.Contract.Registration'}
    graph = {name: row['deps'] for name, row in targets.items()}
    may_register = set()
    for name in TopologicalSorter(graph).static_order():
        if name in contracts or any(dep in may_register for dep in graph[name]):
            may_register.add(name)
    for module, path in selection.modules().items():
        utility = utilities.get(path)
        deps = {module}
        if utility:
            deps.add(utility['claimModule'])

        def report_target(dependencies):
            names = sorted(dependencies)
            return {'key': digest(['report-v1', module, report_format, utility,
                                   [(n, targets[n]['key']) for n in names]]), 'deps': names}

        unconditional = report_target(deps)
        registry = 'LeanInformationAudit.TemplateEnrollment'
        if module in may_register and registry not in deps:
            target = report_target(deps | {registry})
            # This candidate is admissible only with a producer-checked empty
            # compiled input_projection in the module's report bundle.
            target['empty_projection'] = unconditional
        else:
            target = unconditional
        targets['report/' + module] = target
    return targets


def report_has_inputs(path, module):
    public = report_publication
    with zipfile.ZipFile(path) as bundle:
        members = public.bundle_members(bundle, ('', '.materials.zip', '.provenance.json'))
        with public.open_zip_member(bundle, members[public.RAW]) as data:
            report = public.read_json(data.read())
        with public.open_zip_member(bundle, members[public.RAW + '.provenance.json']) as data:
            origin = public.read_json(data.read())
    if (report['schema'] != public.materials.REPORT_SCHEMA or len(report['modules']) != 1
            or report['modules'][0]['module'] != module):
        raise ValueError('unexpected module in report projection: ' + module)
    public.check_origin(origin, report['modules'][0])
    return bool(origin['input_projection']['inputs'])


def use_known_projections(targets, snapshots):
    """Only a prior authenticated row with the exact module/dependency key may specialize a report."""
    for snapshot in snapshots:
        for name, row in snapshot['targets'].items():
            target = targets.get(name, {})
            alternate = target.get('empty_projection')
            if alternate and matches(row, alternate):
                targets[name] = alternate


def build_facets(root, names):
    programs = [PROGRAMS[n[7:]][0] + '/' + n[7:] for n in names if n.startswith('native/')]
    reports = [n[7:] for n in names if n.startswith('report/')]
    wrapper = ['bash', 'tools/scripts/worktree/lean-cache-run.sh']
    if programs:
        subprocess.run([*wrapper, '--build-report-programs', *programs], cwd=root, check=True)
    if reports:
        subprocess.run([*wrapper, 'lake', '-d', 'tools/lean-inspector-reg', 'build',
                        *['+' + name + ':report' for name in reports]], cwd=root, check=True)


def stage(root, remote, plan, names, directory):
    frames = {}
    for name in names:
        target = plan['targets'][name]
        bundle = root / PROJECT_BUILD / facet_paths(name)[0]
        if 'empty_projection' in target and not report_has_inputs(bundle, name.removeprefix('report/')):
            plan['targets'][name] = target['empty_projection']
        artifacts = []
        for label in facet_paths(name):
            path = root / PROJECT_BUILD / label
            if not path.is_file() or path.is_symlink():
                raise ValueError('required output missing: ' + label)
            info = path.stat()
            artifacts.append(Artifact(PurePosixPath(label), info.st_mode, info.st_size, None, 'other'))
        with (directory / 'frame').open('wb') as fp:
            frame = write_frame(root, {}, None, artifacts, fp, None, stable_timestamps=True)
        (directory / 'frame').rename(directory / (frame['sha256'] + '.tar.gz'))
        frames[name] = {'paths': list(facet_paths(name)), 'sha256': frame['sha256']}
    write(directory / 'frames.json', frames)
    receipt, counts = stage_frames(remote, plan, directory, 64 * 1024**2)
    save(remote, plan['domain'], receipt, directory)
    print(json.dumps(counts), flush=True)
    return receipt


def run(root, compiler, base, receipts, output):
    remote = remote_for(root)
    targets = all_targets(root, compiler['targets'])
    plan = {**compiler, 'targets': targets}
    snapshots = [base, *(read(p) for p in sorted(receipts.glob('*.json')))]
    use_known_projections(targets, snapshots)
    rows, sizes = checked_hits(targets, snapshots)
    missing_keys = set()
    for name in targets.keys() - rows.keys():
        target = targets[name]
        missing_keys.add(target['key'])
        if 'empty_projection' in target:
            missing_keys.add(target['empty_projection']['key'])
    if missing_keys:
        partials = list(discover(remote, plan['domain'], missing_keys))
        use_known_projections(targets, partials)
        rows, sizes = checked_hits(targets, [*snapshots, *partials])
    # Include only verified, currently addressable inherited entries.
    available = remote.confirmed_volumes(sizes)
    rows, sizes = checked_hits(targets, [{'volumes': available, 'targets':
        {k: row for k, row in rows.items() if row['volume'] in available}}])
    if not compiler['targets'].keys() <= rows.keys():
        raise ValueError('compiler receipts omit required inputs')
    index = {'source': plan['source'], 'targets': dict(rows), 'volumes': sizes.copy()}
    write(receipts / 'inherited.json', index)
    missing = sorted(targets.keys() - rows.keys())
    if not missing:
        write(output, plan)
        return
    subprocess.run(['bash', '.github/actions/lean-compile-layer/prepare-dependencies.sh'], cwd=root, check=True)
    native = [name for name in missing if name.startswith('native/')]
    reports = [name for name in missing if name.startswith('report/')]
    restored = set()
    batches = ([native] if native else []) + [reports[i:i+32] for i in range(0, len(reports), 32)]
    if reports:
        subprocess.run(['python3', 'tools/lean-inspector/native.py', 'prepare', str(root)], cwd=root, check=True)
    for number, names in enumerate(batches):
        needed = {dep for name in names for dep in targets[name]['deps']}
        if names[0].startswith('report/'):
            needed.update(('native/inputDiscovery', 'native/reportInspector'))
        pending = list(needed)
        while pending:
            extra = set(targets[pending.pop()]['deps']) - needed
            needed.update(extra)
            pending.extend(extra)
        new = needed - restored
        if new:
            subset = {key: index['targets'][key] for key in new}
            subset_index = dict(source=index['source'], targets=subset,
                                volumes={r['volume']: index['volumes'][r['volume']] for r in subset.values()})
            restore(remote, subset_index, plan, sorted(new), root / PROJECT_BUILD)
            restored.update(new)
        build_facets(root, names)
        with tempfile.TemporaryDirectory() as temp:
            receipt = stage(root, remote, plan, names, Path(temp))
        write(receipts / f'facets-{number}.json', receipt)
        index['targets'].update(receipt['targets'])
        index['volumes'].update(receipt['volumes'])
        restored.update(names)
        if shutil.disk_usage(root).free < 4 * 1024**3:
            for target in restored:
                for relative in index['targets'][target]['paths']:
                    (root / PROJECT_BUILD / relative).unlink(missing_ok=True)
            restored.clear()
    write(output, plan)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('plan', 'base', 'receipts', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    args = parser.parse_args()
    run(Path.cwd(), read(args.plan), read(args.base), args.receipts, args.output)
