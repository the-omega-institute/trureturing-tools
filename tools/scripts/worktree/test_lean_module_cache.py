"""End-to-end compiler shard selection, incremental publication and restart with real Lake artifacts."""

from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import lean_module_cache as cache
from lean_module_dag import assign_queues
from lean_artifact_inventory import Module, PROJECT_BUILD
from lean_module_shards import pack_shard, restore_shard


class CompilerQueuePlanning(unittest.TestCase):
    def test_dependency_handoff_balances_chains_and_wide_fanout(self):
        paths = {
            'producer': (3, [], 2),
            'independent': (1, [], 1),
            'left': (2, ['producer'], 10),
            'right': (2, ['producer'], 1),
            'join': (1, ['left', 'right'], 1),
        }
        def run(workers):
            units = {key: dict(rank=rank, deps=deps, members=range(count))
                     for key, (rank, deps, count) in paths.items()}
            assign_queues(units, workers)
            return {key: unit['queue'] for key, unit in units.items()}

        queues = run(2)
        self.assertNotEqual(queues['independent'], queues['producer'])
        self.assertEqual(queues['left'], queues['producer'])
        self.assertNotEqual(queues['right'], queues['left'])
        self.assertEqual(queues['join'], queues['left'])
        self.assertEqual(set(run(1).values()), {0})


class MemoryRemote:
    def __init__(self):
        self.indexes = {}
        self.rows = {}
        self.assets = {}
        self.deadline = None
        self.layouts = {}
        self.publications = {}
        self.saves = []
        self.fail = None
        self.catalogs = []
        self.queues = {}

    def confirmed(self, rows):
        return rows

    def latest(self, version=None):
        return self.indexes.get(version)

    def partials(self, plan):
        return {n: self.rows[r['fingerprint']] for n, r in plan['shards'].items() if r['fingerprint'] in self.rows}

    def save_compiler_catalog(self, version, catalog):
        self.catalogs.append(catalog)

    def compiler_seeds(self, plan):
        from lean_module_dag import recovery_index
        assets = {name: (tag, dict(digest='sha256:' + hashlib.sha256(data).hexdigest(),
                                  state='uploaded', size=len(data)))
                  for (tag, name), data in self.assets.items()}
        return recovery_index(plan, self.catalogs, assets)

    def compiler_progress(self, plan, pending):
        return {k: self.rows[k] for k in pending if k in self.rows}

    def retry_checkpoint(self, plan, key):
        return self.rows.get(key)

    def claim_compiler_queue(self, plan, queue, lane, key):
        owner = self.queues.setdefault((plan['source'], queue), lane if queue == lane else None)
        return (self.queues.setdefault((plan['source'], key), lane) == lane) if owner is None else owner == lane

    def wait_compiler_progress(self, plan):
        raise AssertionError('a single live worker must make progress')

    def save(self, plan, name, path, row):
        if self.fail == name:
            raise InterruptedError(name)
        key = row['version'], row['asset']
        assert key not in self.assets
        self.assets[key] = path.read_bytes()
        self.rows[row['fingerprint']] = row
        self.saves.append(name)
        return row

    def staged_index(self, version):
        return self.latest(version)

    def publication_layout(self, version, name=None):
        return self.publications.get((version, name) if name else version)

    def save_publication_layout(self, version, layout):
        assert self.publications.setdefault(version, layout) == layout

    def splits(self, plan):
        return {n: self.layouts[n, r['fingerprint']] for n, r in plan['shards'].items() if (n, r['fingerprint']) in self.layouts}

    def save_split(self, plan, name, layout):
        fingerprint = plan['shards'][name]['fingerprint']
        assert self.layouts.setdefault((name, fingerprint), layout) == layout

    def download(self, row, output):
        output.write_bytes(self.assets[row['version'], row['asset']])

    def publish(self, index):
        assert self.indexes.setdefault(index['version'], index) == index


class ColdReportShards(unittest.TestCase):
    def test_saved_parents_and_split_children_survive_replanning(self):
        # Warm is an unchanged report. Compiler has no report to extract.
        targets = {n: {'key': n, 'deps': [], 'report_inputs': [n]} for n in
                   ('D5.A.One', 'D5.A.Two', 'D5.B', 'D5.C', 'D5.Warm',
                    'Reg.A', 'Reg.B', 'Reg.C', 'Edge.A', 'Edge.B', 'Edge.Warm')}
        targets['D5.Compiler'] = {'key': 'compiler', 'deps': []}
        remote = MemoryRemote()

        def plan(source, inputs, base=None):
            return cache.make_plan(source, 'm', inputs, base, remote, cache.DEFAULT_CAPACITY, 2,
                                   mathlib_version='v1')

        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 100):
            original = plan('old', targets)
            for row in original['shards'].values():
                remote.rows[row['fingerprint']] = dict(row, bytes=128, version=original['version'])
            base = cache.finish(original, remote)
        current = {n: dict(row, key='changed-' + n) if 'report_inputs' in row and not n.endswith('.Warm')
                   else row for n, row in targets.items()}
        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 100):
            unsplit = plan('new', current, base)
        # Reg was already uploaded by a worker from the old plan.
        reg = dict(unsplit['shards']['Reg'], bytes=256, version=unsplit['version'])
        remote.rows[reg['fingerprint']] = reg
        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 2):
            first = plan('new', current, base)
            self.assertEqual(first['inherited'], {'Reg': reg})
            self.assertEqual(first['shards'].keys(), {'D5', 'D5.A', 'Reg', 'Edge'})
            self.assertEqual(first['shards']['Edge'], unsplit['shards']['Edge'])
            self.assertEqual(first['shards']['D5']['members'], ['D5.B', 'D5.C', 'D5.Compiler', 'D5.Warm'])
            self.assertEqual(len(first['lanes']), 2)
            self.assertEqual(first, plan('new', dict(reversed(current.items())), base))

            # The byte-size check further split D5.A. One child completed before interruption.
            members = first['shards']['D5.A']['members']
            remote.save_split(first, 'D5.A', {n: [n] for n in members})
            parts = cache.expanded(first, remote.splits(first))
            done = parts['shards']['D5.A.One']
            remote.rows[done['fingerprint']] = dict(done, bytes=128, version=first['version'])
            retry = plan('new', current, base)
            self.assertEqual(retry['shards'], parts['shards'])
            self.assertEqual(retry['inherited'].keys(), {'Reg', 'D5.A.One'})
            self.assertCountEqual([n for lane in retry['lanes'] for n in lane], ['Edge', 'D5', 'D5.A.Two'])
            with self.assertRaisesRegex(ValueError, 'planned shards unavailable'):
                cache.finish(first, remote)
            for row in retry['shards'].values():
                remote.rows.setdefault(row['fingerprint'], dict(row, bytes=128, version=first['version']))
            finished = cache.finish(first, remote)
            self.assertEqual(cache.finish(retry, remote), finished)
            self.assertCountEqual([n for r in finished['shards'].values() for n in r['members']], targets)
            self.assertEqual(plan('later', current, finished)['lanes'], [])


class ModuleReleases(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pin = '1' * 40
        self.version = 'v4.35.0'
        self.source = 'a' * 40
        self.remote = MemoryRemote()
        self.modules = {}
        shutil.copyfile(Path(__file__).resolve().parents[3] / 'lean-toolchain', self.root / 'lean-toolchain')
        self.cfg = self.root / 'lakefile.toml'
        self.cfg.write_text('name = "trureturing"\n[[lean_lib]]\nname = "D5"\n[[lean_lib]]\nname = "Other"\n')
        for name, body in [('D5.A', 'def valueA := 1'), ('D5.B', 'def valueB := 7'),
                           ('D5.C', 'import D5.A\ndef valueC := valueA'),
                           ('Other.Auditor', 'def irrelevant := 0')]:
            source = Path(*name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', source, Path('.'))
            path = self.root / source
            path.parent.mkdir(exist_ok=True)
            path.write_text(body + '\n')
        self.lake('update')
        self.enterContext(patch.object(cache.inventory, 'workspace_modules', return_value=self.modules))
        self.enterContext(patch.object(cache, 'seed_partitions', side_effect=lambda _: [self.pin + '/linux-arm64']))
        self.enterContext(patch.object(cache.compiler, 'checkout', side_effect=lambda _: self.source))
        self.enterContext(patch.object(cache.compiler, 'compile_roots', side_effect=self.compile))
        self.enterContext(patch.object(cache.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 8*1024**3)))
        self.compiled = []

    def lake(self, *args):
        return subprocess.run(['lake', *args], cwd=self.root, check=True, text=True, capture_output=True)

    def compile(self, _, modules, names):
        self.compiled.append(names)
        self.lake('build', *['+' + n + ':leanArts' for n in names])

    def plan(self, base=None, capacity=cache.DEFAULT_CAPACITY):
        _, targets = cache.checkout_targets(self.root)
        return cache.make_plan(self.source, self.pin, targets, base, self.remote, capacity, 2, mathlib_version=self.version, kind='compiler')

    def workers(self, plan):
        return [cache.work(self.root, plan, i, self.remote, self.root / 'receipts') for i in range(len(plan['lanes']))]

    def dag_plan(self, workers=2, base=None):
        modules, targets = cache.checkout_targets(self.root)
        return cache.make_plan(self.source, self.pin, targets, base, self.remote, cache.DEFAULT_CAPACITY,
                               workers, mathlib_version=self.version, kind='compiler', compiler_dag=True,
                               prepare_release=True, modules=modules, root=self.root)

    def test_static_plan_keeps_cold_ancestor_below_a_warm_intermediary(self):
        from lean_module_dag import decode_scope
        graph = {'D5.A': [], 'D5.B': ['D5.A'], 'D5.C': ['D5.B'], 'D5.D': []}
        targets = {n: dict(key=n, deps=parents) for n, parents in graph.items()}
        modules = dict(self.modules, **{'D5.D': Module('trureturing', Path('D5/D.lean'), Path('.'))})
        # A prior partial unit supplies just the intermediary.
        recovery = dict(targets={'D5.B': dict(key='D5.B', shard='warm')}, shards={
            'warm': dict(version='old', asset='warm', members=['D5.B'])})
        with patch.object(self.remote, 'compiler_seeds', return_value=recovery):
            plan = cache.make_plan(self.source, self.pin, targets, None, self.remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version=self.version, kind='compiler', compiler_dag=True,
                                   modules=modules, root=self.root)
        dag = plan['compiler_dag']
        owner = {n: k for k, unit in dag['units'].items() for n in unit['members']}
        self.assertEqual(dag['units'][owner['D5.C']]['deps'], [owner['D5.A']])
        restore = dag['units'][owner['D5.C']]['restore']
        self.assertEqual(decode_scope(restore, dag['names']), {'D5.A', 'D5.B'})

    def test_diamond_single_runner_retry_split_from_fresh_worktree(self):
        # Two cold branches share A. The last runner starts before either of the others.
        (self.root / 'D5/B.lean').write_text('import D5.A\ndef valueB := valueA+2\n')
        (self.root / 'D5/D.lean').write_text('import D5.B\nimport D5.C\ndef valueD := valueB+valueC\n')
        self.modules['D5.D'] = Module('trureturing', Path('D5/D.lean'), Path('.'))
        plan = self.dag_plan(workers=3)
        units = plan['compiler_dag']['units']
        own = {n: k for k, row in units.items() for n in row['members']}
        self.assertEqual(set(units[own['D5.D']]['deps']), {own['D5.B'], own['D5.C']})
        self.assertEqual(units[own['D5.B']]['deps'], [own['D5.A']])
        self.assertEqual(units[own['D5.C']]['deps'], [own['D5.A']])
        self.remote.fail = own['D5.D']
        from contextlib import ExitStack
        import lean_module_dag as dag
        with ExitStack() as guards:
            for target, name in ((cache, 'checkout_targets'), (dag, 'TopologicalSorter'),
                                 (cache.compiler, 'terminal'), (cache.inventory, 'import_closure')):
                guards.enter_context(patch.object(target, name, side_effect=AssertionError('worker reanalyzed DAG')))
            with self.assertRaises(InterruptedError):
                cache.work(self.root, plan, 2, self.remote, self.root / 'receipts')
        self.assertNotIn('D5', self.remote.saves)
        with self.assertRaisesRegex(ValueError, 'planned shards unavailable'):
            cache.finish(plan, self.remote)
        self.compiled.clear()
        self.remote.fail = None
        self.remote.queues.clear()
        shutil.rmtree(self.root / PROJECT_BUILD)
        # An Actions failed-job retry reuses the original plan artifact.
        self.assertEqual(cache.work(self.root, plan, 0, self.remote, self.root / 'retry')['compiled_modules'], 1)
        self.assertEqual(self.compiled, [['D5.D']])
        index = cache.finish(plan, self.remote)
        destination = self.root / 'restored'
        self.assertEqual(cache.restore(self.root, index, ['D5.D'], self.remote, destination,
                                       kind='compiler')['restored'], 4)
        self.assertEqual(set(index['shards']), {'D5'})
        self.assertEqual(set(self.dag_plan(base=index)['lanes']), set())

    def test_simultaneous_workers_publish_each_diamond_module_once(self):
        self.run_diamond(staggered=False)

    def test_late_worker_joins_natural_fanout_after_first_worker_starts_a_branch(self):
        self.run_diamond(staggered=True)

    def run_diamond(self, *, staggered):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Condition, Event
        from time import monotonic
        (self.root / 'D5/B.lean').write_text('import D5.A\ndef valueB := valueA+2\n')
        (self.root / 'D5/D.lean').write_text('import D5.B\nimport D5.C\ndef valueD := valueB+valueC\n')
        self.modules['D5.D'] = Module('trureturing', Path('D5/D.lean'), Path('.'))
        plan = self.dag_plan(workers=2)
        queues = {n: u['queue'] for u in plan['compiler_dag']['units'].values() for n in u['members']}
        self.assertNotEqual(queues['D5.B'], queues['D5.C'])
        branch_started = Event()
        condition = Condition()
        branches = Barrier(2, timeout=20)
        ready = Barrier(2, timeout=20)
        checkouts = [self.root / 'runner0', self.root / 'runner1']
        for root in checkouts:
            root.mkdir()
            for f in ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain'):
                shutil.copyfile(self.root / f, root / f)
            shutil.copytree(self.root / 'D5', root / 'D5')
        build_calls = []

        def compile_at(root, modules, roots):
            build_calls.extend(roots)
            if roots in (['D5.B'], ['D5.C']):
                branch_started.set()
                branches.wait()
            subprocess.run(['lake', 'build', *['+' + n + ':leanArts' for n in roots]],
                           cwd=root, check=True, capture_output=True)

        def save(plan, key, path, row):
            with condition:
                result = MemoryRemote.save(self.remote, plan, key, path, row)
                condition.notify_all()
                return result

        def run_lane(lane):
            if staggered:
                if lane == queues['D5.A']:
                    self.assertTrue(branch_started.wait(timeout=20))
            else:
                ready.wait()
            return cache.work(checkouts[lane], plan, lane, self.remote, checkouts[lane] / 'receipts')

        deadline = monotonic() + 35
        def wait(plan):
            if monotonic() > deadline:
                raise AssertionError('ready compiler queues stalled')
            with condition:
                condition.wait(timeout=0.05)

        with patch.object(cache.compiler, 'compile_roots', side_effect=compile_at), \
             patch.object(self.remote, 'save', side_effect=save), \
             patch.object(self.remote, 'wait_compiler_progress', side_effect=wait), \
             ThreadPoolExecutor(max_workers=2) as runners:
            results = list(runners.map(run_lane, (1, 0)))
        self.assertCountEqual(build_calls, ['D5.A', 'D5.B', 'D5.C', 'D5.D'])
        self.assertEqual(sum(row['compiled_modules'] for row in results), 4)
        self.assertEqual(set(cache.finish(plan, self.remote)['shards']), {'D5'})

    def test_checkpoints_seen_child_first_release_descendants_after_ancestors(self):
        from collections import deque
        import lean_module_dag as dag
        (self.root / 'D5/B.lean').write_text('import D5.A\ndef valueB := valueA+2\n')
        (self.root / 'D5/C.lean').write_text('import D5.B\ndef valueC := valueB+1\n')
        with patch.object(dag, 'UNIT_LIMIT', 1):
            plan = self.dag_plan(workers=2)
        owner = {n: k for k, unit in plan['compiler_dag']['units'].items() for n in unit['members']}
        self.remote.fail = owner['D5.C']
        with self.assertRaises(InterruptedError):
            cache.work(self.root, plan, 0, self.remote, self.root / 'first')
        a, b = owner['D5.A'], owner['D5.B']
        observed = deque([{b: self.remote.rows[b]}, {a: self.remote.rows[a]}])
        self.compiled.clear()
        self.remote.fail = None
        shutil.rmtree(self.root / PROJECT_BUILD)
        self.remote.queues.clear()

        with patch.object(self.remote, 'compiler_progress', side_effect=lambda *args: observed.popleft()), \
             patch.object(self.remote, 'wait_compiler_progress', return_value=None):
            result = cache.work(self.root, plan, 0, self.remote, self.root / 'retry')
        self.assertEqual(result['compiled_modules'], 1)
        self.assertEqual(self.compiled, [['D5.C']])
        self.assertEqual(set(cache.finish(plan, self.remote)['targets']), set(owner))

    def test_retry_recovers_uploads_omitted_by_both_inventories(self):
        plan = self.dag_plan()
        self.remote.fail = 'D5'
        with self.assertRaises(InterruptedError):
            self.workers(plan)
        self.remote.queues.clear()
        self.remote.fail = None
        self.compiled.clear()
        shutil.rmtree(self.root / PROJECT_BUILD)
        with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2'), \
             patch.object(self.remote, 'compiler_progress', return_value={}):
            cache.work(self.root, plan, 0, self.remote, self.root / 'retry')
        self.assertEqual(self.compiled, [])
        self.assertEqual(set(cache.finish(plan, self.remote)['targets']), {'D5.A', 'D5.B', 'D5.C'})

    def test_dag_adopts_available_first_publication_frames(self):
        plan = self.dag_plan(workers=1)
        self.lake('build', '+D5.A:leanArts')
        plan['legacy'] = {'targets': {'D5.A': {}}}
        def restore(remote, index, names, destination):
            return names & index['targets'].keys()
        with patch('lean_generation_checkpoints.remote_for', return_value=object()), \
             patch.object(cache, 'restore_legacy', side_effect=restore):
            result = self.workers(plan)
        self.assertEqual(sum(r['compiled_modules'] for r in result), 2)
        self.assertCountEqual([n for call in self.compiled for n in call], ['D5.B', 'D5.C'])
        self.assertEqual(len(cache.finish(plan, self.remote)['targets']), 3)

    def test_checkpoint_rejects_modified_ordinary_olean_before_upload(self):
        plan = self.dag_plan()
        def corrupt(root, modules, names):
            self.compile(root, modules, names)
            artifact = root / PROJECT_BUILD / 'lib/lean' / Path(*names[0].split('.')).with_suffix('.olean')
            with artifact.open('r+b') as stream:
                stream.seek(-1, os.SEEK_END)
                last = stream.read(1)[0]
                stream.seek(-1, os.SEEK_END)
                stream.write(bytes([last ^ 1]))
        with patch.object(cache.compiler, 'compile_roots', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'artifact differs from its Lake trace'):
                cache.work(self.root, plan, 0, self.remote, self.root / 'receipts')
        self.assertEqual(self.remote.saves, [])

    def test_exact_compiler_verifier_uses_the_frozen_source_plan_on_cold_and_warm_releases(self):
        from lean_module_remote import COMPILER_PLAN
        plan = self.dag_plan()
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        for snapshot in (plan, self.dag_plan(base=index)):
            self.remote.publications[index['version'], COMPILER_PLAN] = snapshot
            with patch.object(cache, 'checkout_targets', side_effect=AssertionError('verifier reanalyzed source')):
                result = cache.restore_published(self.root, index, ['D5.C'], self.remote,
                                                 self.root / 'verified', 'compiler', exact=True)
            self.assertEqual((result['requested'], result['restored']), (2, 2))

    def test_older_unit_catalog_does_not_block_trace_verified_publication(self):
        import lean_module_dag as dag
        inputs = {'D5.A': 'x'}
        old = dag.digest(['lean-compiler-unit-v1', self.pin, sorted(inputs.items())])
        self.remote.catalogs.append(dict(mathlib=self.pin, units={old: inputs}))
        self.remote.assets['old', dag.unit_row(old, inputs)['asset']] = b'older unit'
        plan = self.dag_plan()
        self.assertNotIn('D5.A', plan['compiler_dag']['seeds']['owners'])
        self.remote.catalogs.append(dict(mathlib=self.pin, units={'0' * 64: inputs}))
        with self.assertRaisesRegex(ValueError, 'inputs differ'):
            self.dag_plan()

    def test_cross_source_reuses_matching_member_of_interrupted_unit(self):
        # A and B fit in one unit. A later edit invalidates B while A and its consumer C still match.
        original = self.dag_plan(workers=1)
        self.remote.fail = 'D5'
        with self.assertRaises(InterruptedError):
            self.workers(original)
        self.assertEqual(self.remote.compiler_seeds(original)['targets'].keys(), {'D5.A', 'D5.B', 'D5.C'})
        shutil.rmtree(self.root / PROJECT_BUILD)
        self.compiled.clear()
        self.source = 'b' * 40
        self.remote.fail = None
        (self.root / 'D5/B.lean').write_text('def valueB := 70\n')
        later = self.dag_plan()
        self.assertEqual(set(later['compiler_dag']['seeds']['owners']), {'D5.A', 'D5.C'})
        self.workers(later)
        self.assertEqual(self.compiled, [['D5.B']])
        index = cache.finish(later, self.remote)
        self.assertEqual(cache.restore(self.root, index, None, self.remote, self.root / 'restored',
                                       kind='compiler')['restored'], 3)

    def test_compiler_and_report_inputs_restore_independently(self):
        from lean_cache_release import report_selection
        import lean_generation_facets as facets

        old = self.plan()
        self.workers(old)
        index = cache.finish(old, self.remote)
        (self.root / 'lean-report-inputs.json').write_text('{}')
        selection = self.enterContext(patch.object(report_selection, 'Selection')).return_value
        selection.modules.return_value = {'D5.A': 'D5/A.lean'}
        utility = {'modulePath': 'D5/A.lean', 'claimModule': 'D5.B'}
        self.enterContext(patch.object(facets, 'utility_inputs', return_value={'D5/A.lean': utility}))

        with patch.object(report_selection, 'Selection', side_effect=AssertionError('compiler read report settings')):
            _, compiler_only = cache.checkout_targets(self.root)
        modules, with_reports = cache.checkout_targets(self.root, kind='report')
        self.assertTrue(modules['D5.A'].report)
        self.assertEqual(sorted(with_reports['D5.A']['report_inputs']), ['D5.A', 'D5.B'])
        self.assertEqual(index['targets']['D5.A']['key'], with_reports['D5.A']['compiler_input'])
        self.assertNotEqual(index['targets']['D5.A']['key'], with_reports['D5.A']['key'])
        self.assertEqual(index['targets']['D5.C']['key'], with_reports['D5.C']['key'])
        self.assertEqual(index['targets']['D5.A']['key'], compiler_only['D5.A']['key'])
        destination = self.root / 'restored'
        result = cache.restore(self.root, index, ['D5.C'], self.remote, destination)
        self.assertEqual((result['requested'], result['restored'], result['compiler_restored']), (2, 1, 2))
        self.assertEqual((result['reports_requested'], result['reports_restored']), (1, 0))
        self.assertEqual((result['supporting_compilers_requested'], result['supporting_compilers_restored']), (1, 1))
        self.assertTrue((destination/'lib/lean/D5/B.olean').is_file())
        self.assertTrue((destination/'lib/lean/D5/A.olean').is_file())
        self.assertFalse((destination/'lean-inspector/modules/D5.A.zip').exists())

        # The example is a consumer of the requested source and cannot be found through its imports.
        def example_scope(root, subset, *, scope_inputs):
            scope_inputs['D5/A.lean'] = ['D5.C']
            return {'D5/A.lean': utility}
        with patch.object(facets, 'utility_inputs', side_effect=example_scope):
            expanded = cache.restore(self.root, index, ['D5.A'], self.remote, self.root / 'example-scope')
            self.assertEqual(expanded['requested'], 2)
            self.assertTrue((self.root / 'example-scope/lib/lean/D5/C.olean').is_file())

        with patch.object(facets, 'utility_inputs', side_effect=FileNotFoundError('dotnet')) as reader:
            plain = cache.restore(self.root, index, ['D5.B'], self.remote, self.root / 'ordinary')
            self.assertEqual(plain['restored'], 1)
            reader.assert_not_called()
            cold = cache.restore(self.root, index, ['D5.A'], self.remote, self.root / 'compiler-seed')
            self.assertEqual((cold['reports_restored'], cold['compiler_restored']), (0, 1))

        (self.root / 'D5/B.lean').write_text('def valueB := 8')
        _, changed = cache.checkout_targets(self.root, kind='report')
        self.assertNotEqual(changed['D5.A']['key'], with_reports['D5.A']['key'])
        self.assertEqual(changed['D5.A']['compiler_input'], with_reports['D5.A']['compiler_input'])
        self.assertEqual(changed['D5.C']['key'], with_reports['D5.C']['key'])
        with patch.object(report_selection, 'REPORT_FORMAT', 'changed'):
            _, new_format = cache.checkout_targets(self.root, kind='report')
        self.assertNotEqual(new_format['D5.A']['key'], changed['D5.A']['key'])
        self.assertEqual(new_format['D5.B']['key'], changed['D5.B']['key'])

    def test_reports_publish_and_retry_from_compiler_only_artifacts(self):
        from lean_cache_release import report_selection
        import lean_generation_facets as facets
        from test_lean_module_shards import ShardCompilerTransport
        (self.root / 'lean-report-inputs.json').write_text('{}')
        selection = self.enterContext(patch.object(report_selection, 'Selection')).return_value
        selection.modules.return_value = {'D5.A': 'D5/A.lean'}
        self.enterContext(patch.object(facets, 'utility_inputs', return_value={}))
        with patch.object(facets, 'materialize', side_effect=AssertionError('compiler extracted a report')):
            original = self.plan()
            self.workers(original)
            compiler_index = cache.finish(original, self.remote)
        selection.modules.assert_not_called()
        self.assertEqual(compiler_index['kind'], 'compiler')
        report_remote = MemoryRemote()
        modules, targets = cache.checkout_targets(self.root, kind='report')
        selected = {n: r for n, r in targets.items() if 'report_inputs' in r}
        def plan():
            return cache.make_plan(self.source, self.pin, selected, None, report_remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version=self.version, kind='report', compiler_index=compiler_index)
        reports = plan()
        self.assertEqual(reports['version'], original['version'] + '-report')
        expected = compiler_index['shards']['D5']['key'] + '-report'
        self.assertEqual(reports['shards']['D5']['key'], expected)
        self.assertEqual(reports['shards']['D5']['asset'], expected)
        self.assertNotEqual(reports['shards']['D5']['fingerprint'], compiler_index['shards']['D5']['fingerprint'])
        self.assertFalse(report_remote.indexes)
        shutil.rmtree(self.root / PROJECT_BUILD)
        def produce(root, names, inputs, *, prebuilt):
            self.assertTrue(prebuilt)
            self.assertEqual(set(inputs), {'D5.A'})
            self.assertTrue((root / PROJECT_BUILD / 'lib/lean/D5/A.olean').is_file())
            for name in names:
                # Generate the signed report bundle and its actual Lake hash/trace.
                ShardCompilerTransport.prepare_report(SimpleNamespace(root=root, build=root / PROJECT_BUILD,
                                                                     modules=modules), name)
        self.remote.published = self.remote.latest
        with patch.object(cache.compiler, 'compile_roots', side_effect=AssertionError('report compiled project modules')):
            report_remote.fail = 'D5'
            with patch.object(facets, 'materialize', side_effect=produce):
                with self.assertRaises(InterruptedError):
                    cache.work(self.root, reports, 0, report_remote, self.root / 'report-worker', compiler_remote=self.remote)
            self.assertEqual(self.remote.latest(original['version']), compiler_index)
            fresh = self.root / 'consumer-before-reports'
            self.assertEqual(cache.restore(self.root, compiler_index, ['D5.A'], self.remote, fresh)['compiler_restored'], 1)
            report_remote.fail = None
            shutil.rmtree(self.root / PROJECT_BUILD)
            with patch.object(facets, 'materialize', side_effect=produce):
                retry = plan()
                result = cache.work(self.root, retry, 0, report_remote, self.root / 'report-worker', compiler_remote=self.remote)
            self.assertEqual(result['compiled_modules'], 0)
            self.assertEqual(result['prepared_reports'], 1)
        index = cache.finish(retry, report_remote)
        self.assertEqual(index['compiler_version'], compiler_index['version'])
        self.assertEqual(index['kind'], 'report')
        self.assertEqual(index['shards']['D5']['key'], expected)
        self.assertEqual(plan()['lanes'], [])
        fresh = self.root / 'report-only'
        result = cache.restore(self.root, compiler_index, ['D5.A'], self.remote, fresh, kind='report',
                               report_index=index, report_remote=report_remote)
        self.assertEqual(result['reports_restored'], 1)
        self.assertTrue((fresh / 'lean-inspector/modules/D5.A.zip').is_file())
        self.assertFalse((fresh / 'lib/lean/D5/A.olean').exists())

    def test_rolling_report_marker_can_lag_the_compiler_and_restore_only_matching_inputs(self):
        import lean_module_remote
        from lean_cache_release import report_selection
        import lean_generation_facets as facets
        from test_lean_module_shards import ShardCompilerTransport
        (self.root / 'lean-report-inputs.json').write_text('{}')
        selection = self.enterContext(patch.object(report_selection, 'Selection')).return_value
        selection.modules.return_value = {n: 'D5/' + n[-1] + '.lean' for n in ('D5.A', 'D5.B')}
        self.enterContext(patch.object(facets, 'utility_inputs', return_value={}))
        first = self.plan()
        self.workers(first)
        compiler_a = cache.finish(first, self.remote)
        modules, targets = cache.checkout_targets(self.root, kind='report')
        selected = {n: r for n, r in targets.items() if 'report_inputs' in r}
        report_remote = MemoryRemote()
        plan = cache.make_plan(self.source, self.pin, selected, None, report_remote, cache.DEFAULT_CAPACITY,
                               2, mathlib_version=self.version, kind='report', compiler_index=compiler_a)
        self.remote.published = self.remote.latest
        def produce(root, names, inputs, *, prebuilt):
            for name in names:
                ShardCompilerTransport.prepare_report(SimpleNamespace(root=root, build=root / PROJECT_BUILD,
                                                                     modules=modules), name)
        with patch.object(facets, 'materialize', side_effect=produce):
            cache.work(self.root, plan, 0, report_remote, self.root / 'worker-A', compiler_remote=self.remote)
        reports_a = cache.finish(plan, report_remote)
        report_remote.published = report_remote.latest
        report_remote.latest = lambda: reports_a

        (self.root / 'D5/A.lean').write_text('def valueA := 67')
        self.source = 'b' * 40
        shutil.rmtree(self.root / PROJECT_BUILD)
        second = self.plan(compiler_a)
        self.workers(second)
        compiler_b = cache.finish(second, self.remote)
        with patch.object(lean_module_remote, 'Remote', return_value=report_remote):
            rolling = self.root / 'rolling'
            result = cache.restore_published(self.root, compiler_b, None, self.remote, rolling)
            self.assertEqual((result['compiler_restored'], result['reports_requested'], result['reports_restored']),
                             (3, 2, 1))
            self.assertTrue((rolling / 'lean-inspector/modules/D5.B.zip').is_file())
            self.assertFalse((rolling / 'lean-inspector/modules/D5.A.zip').exists())
            exact = cache.restore_published(self.root, compiler_b, None, self.remote, self.root / 'exact', exact=True)
            self.assertEqual((exact['compiler_restored'], exact['reports_restored']), (3, 0))
            bad = {**reports_a, 'compiler_version': compiler_b['version']}
            report_remote.latest = lambda: bad
            fallback = cache.restore_published(self.root, compiler_b, None, self.remote, self.root / 'mispaired')
            self.assertEqual((fallback['compiler_restored'], fallback['reports_restored']), (3, 0))
            self.assertIn('report publication differs', fallback['report_error'])
            with self.assertRaisesRegex(ValueError, 'report publication differs'):
                cache.restore_published(self.root, compiler_b, None, self.remote, self.root / 'strict', kind='report')
            missing = self.root / 'report-index-unavailable'
            def unavailable():
                self.assertTrue((missing / 'lib/lean/D5/B.olean').is_file())
                raise OSError('report metadata unavailable')
            report_remote.latest = unavailable
            with patch.object(cache.sys, 'stderr', new_callable=io.StringIO) as diagnostic:
                fallback = cache.restore_published(self.root, compiler_b, None, self.remote, missing)
            self.assertEqual((fallback['compiler_restored'], fallback['reports_restored']), (3, 0))
            self.assertEqual(fallback['report_error'], 'report metadata unavailable')
            self.assertIn('report metadata unavailable', diagnostic.getvalue())

    def test_first_report_publication_migrates_unchanged_zips_after_two_compiler_versions(self):
        from lean_cache_release import report_selection
        import lean_generation_facets as facets
        from test_lean_module_shards import ShardCompilerTransport
        (self.root / 'lean-report-inputs.json').write_text('{}')
        self.enterContext(patch.object(report_selection, 'Selection')).return_value.modules.return_value = {
            n: 'D5/' + n[-1] + '.lean' for n in ('D5.A', 'D5.B')}
        self.enterContext(patch.object(facets, 'utility_inputs', return_value={}))
        modules, historical = cache.checkout_targets(self.root, kind='report')
        self.compile(self.root, modules, ['D5.A', 'D5.B', 'D5.C'])
        def generate(names):
            fixture = SimpleNamespace(root=self.root, build=self.root / PROJECT_BUILD, modules=modules)
            for name in names:
                ShardCompilerTransport.prepare_report(fixture, name)
        generate(['D5.A', 'D5.B'])
        # The original producer marked compiler-only siblings in a mixed namespace.
        row = historical['D5.C']
        historical['D5.C'] = dict(row, compiler_input=row['key'], key=cache.digest(['module-report-shard', row['key']]))
        mixed_plan = cache.make_plan(self.source, self.pin, historical, None, self.remote, cache.DEFAULT_CAPACITY,
                                     2, mathlib_version=self.version)
        shard = mixed_plan['shards']['D5']
        archive = self.root / 'mixed.tar.gz'
        packed = pack_shard(self.root, modules, historical, archive, shard['fingerprint'])
        self.remote.save(mixed_plan, 'D5', archive, {**shard, **packed, 'version': mixed_plan['version']})
        mixed = cache.finish(mixed_plan, self.remote)
        self.assertNotIn('kind', mixed)
        self.remote.published = self.remote.latest
        base = mixed
        for revision in ('b', 'c'):
            self.source = revision * 40
            if revision == 'c':
                (self.root / 'D5/A.lean').write_text('def valueA := 67')
            shutil.rmtree(self.root / PROJECT_BUILD, ignore_errors=True)
            compiler = self.plan(base)
            self.workers(compiler)
            base = cache.finish(compiler, self.remote)
            self.assertEqual(base['combined_report_version'], mixed['version'])
        modules, current = cache.checkout_targets(self.root, kind='report')
        targets = {n: r for n, r in current.items() if 'report_inputs' in r}
        reports = MemoryRemote()
        plan = cache.make_plan(self.source, self.pin, targets, None, reports, cache.DEFAULT_CAPACITY,
                               2, mathlib_version=self.version, kind='report', compiler_index=base)
        shutil.rmtree(self.root / PROJECT_BUILD)
        import lean_module_remote
        with patch.object(lean_module_remote, 'Remote', return_value=reports):
            stats = cache.restore_published(self.root, base, None, self.remote, self.root / 'pre-migration-consumer')
            self.assertEqual((stats['compiler_restored'], stats['reports_restored']), (3, 1))
        calls = []
        def produce(root, names, inputs, *, prebuilt):
            calls.extend(names)
            generate(names)
        with patch.object(facets, 'materialize', side_effect=produce):
            result = cache.work(self.root, plan, 0, reports, self.root / 'worker', compiler_remote=self.remote)
        self.assertEqual(calls, ['D5.A'])
        self.assertEqual(result['compiled_modules'], 0)
        self.assertEqual(result['prepared_reports'], 1)
        self.assertEqual(cache.finish(plan, reports)['targets'].keys(), {'D5.A', 'D5.B'})

    def test_report_cli_reads_the_explicit_report_and_paired_compiler_tags(self):
        import lean_module_remote
        compiler_remote, report_remote = MemoryRemote(), MemoryRemote()
        compiler_tag = cache.VERSION_PREFIX + self.source
        report_tag = compiler_tag + '-report'
        compiler = {'version': compiler_tag}
        reports = {'compiler_version': compiler_tag}
        compiler_remote.published = lambda version: compiler if version == compiler_tag else self.fail(version)
        report_remote.published = lambda version: reports if version == report_tag else self.fail(version)
        def remote(root, *, kind='compiler'):
            return report_remote if kind == 'report' else compiler_remote
        args = ['test', '--repository', str(self.root), 'restore', '--kind', 'report', '--version', report_tag,
                '--targets', 'D5.A']
        with patch.object(lean_module_remote, 'Remote', side_effect=remote), patch.object(cache.sys, 'argv', args), \
             patch.object(cache, 'restore_published', return_value={'requested': 1, 'restored': 1}) as restore:
            self.assertEqual(cache.run(), 0)
            restore.assert_called_once_with(self.root.resolve(), compiler, ['D5.A'], compiler_remote,
                                            self.root.resolve() / PROJECT_BUILD, 'report', exact=True, report_remote=report_remote,
                                            report_index=reports)
            restore.return_value = {'requested': 1, 'restored': 0}
            self.assertEqual(cache.run(), 1)
            report_remote.published = lambda version: None
            self.assertEqual(cache.run(), 3)

    def test_report_suffix_requires_published_reports_and_staged_checks_are_explicit(self):
        import lean_module_remote
        compiler_reader, report_reader = MemoryRemote(), MemoryRemote()
        compiler_tag = cache.VERSION_PREFIX + self.source
        report_tag = compiler_tag + '-report'
        index = {'source': self.source, 'version': compiler_tag}
        reports = {'source': self.source, 'compiler_version': compiler_tag}
        report_reader.indexes[report_tag] = reports
        compiler_reader.published = lambda version: index if version == compiler_tag else self.fail(version)
        report_reader.published = lambda version: None
        def reader(root, deadline=None, *, kind='compiler'):
            return report_reader if kind == 'report' else compiler_reader
        args = ['test', '--repository', str(self.root), 'restore', '--version', report_tag, '--targets', 'D5.A']
        with patch.object(lean_module_remote, 'Remote', side_effect=reader), \
             patch.object(cache, 'restore_published', return_value={'requested': 1, 'restored': 1}) as restore:
            with patch.object(cache.sys, 'argv', args):
                self.assertEqual(cache.run(), 3)
            self.assertEqual(cache.fetch_published(self.root, ['D5.A'], None, refresh_stale=False, version=report_tag), 1)
            with patch.object(cache.sys, 'argv', [*args, '--kind', 'report', '--staged']):
                self.assertEqual(cache.run(), 0)
            self.assertEqual(restore.call_args.args[5], 'report')
            self.assertIs(restore.call_args.kwargs['report_index'], reports)
            report_reader.published = lambda version: reports if version == report_tag else self.fail(version)
            with patch.object(cache.sys, 'argv', args):
                self.assertEqual(cache.run(), 0)
            self.assertEqual(cache.fetch_published(self.root, ['D5.A'], None, refresh_stale=False, version=report_tag), 0)
            self.assertEqual(restore.call_args.args[5], 'report')

    def test_incremental_releases_and_scoped_consumers(self):
        plan = self.plan()
        self.assertEqual(set(plan['targets']), {'D5.A', 'D5.B', 'D5.C'})
        self.workers(plan)
        first = cache.finish(plan, self.remote)
        self.assertEqual(self.remote.saves, ['D5'])
        old_archive = self.remote.assets[first['version'], self.version + '-D5.tar.gz']
        for subset in (None, ['D5.A'], ['D5.C']):
            destination = self.root / ('consumer-' + str(subset))
            result = cache.restore(self.root, first, subset, self.remote, destination)
            self.assertEqual(result['restored'], {None: 3, 'D5.A': 1, 'D5.C': 2}[subset[0] if subset else None])
            self.assertFalse((destination / 'lean-inspector/modules').exists())
        cfg = self.cfg.read_text()
        self.cfg.write_text(cfg + '[[lean_exe]]\nname = "auditor"\nroot = "Other.Auditor"\n')
        (self.root / self.modules['Other.Auditor'].source).write_text('def irrelevant := 99\n')
        self.source = 'b' * 40
        self.assertEqual(self.plan(first)['lanes'], [])
        (self.root / self.modules['D5.A'].source).write_text('def valueA := 9\n')
        second_plan = self.plan(first)
        self.assertEqual({n for n, row in second_plan['targets'].items()
                          if row['key'] != first['targets'][n]['key']}, {'D5.A', 'D5.C'})
        shutil.rmtree(self.root / PROJECT_BUILD)
        self.assertEqual(self.workers(second_plan), [{'compiled_modules': 2, 'saved_shards': 1}])
        second = cache.finish(second_plan, self.remote)
        self.assertEqual(first['shards']['D5']['asset'], second['shards']['D5']['asset'])
        self.assertEqual(old_archive, self.remote.assets[first['version'], first['shards']['D5']['asset']])
        self.assertNotEqual(first['shards']['D5']['sha256'], second['shards']['D5']['sha256'])
        self.assertEqual(self.workers(self.plan(second)), [])

    def omega_fixture(self):
        for name in ('OmegaInit', 'OmegaMathlib', 'OmegaChecks'):
            with self.cfg.open('a') as config:
                config.write(f'[[lean_lib]]\nname = "{name}"\nsrcDir = "src"\n')
        for name, code in [('OmegaInit', 'module\n@[expose] public def omegaBase := 1\n'),
                           ('OmegaMathlib', 'import OmegaInit\ndef omegaValue := omegaBase + 1\n'),
                           ('OmegaChecks.Portable.Client', 'import OmegaMathlib\ndef checked := omegaValue\n')]:
            path = Path('src', *name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', path, Path('.'))
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(code)
        strict = self.root / 'strict'
        strict.mkdir()
        (strict / 'OmegaInit.lean').write_text('module\n@[expose] public def omegaBase := 7\n')
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 1\n')
        (self.root / 'D5/A.lean').write_text('import OmegaMathlib\ndef valueA := omegaValue\n')
        checks = self.root / 'src/OmegaChecks'
        for helper in ('strict_lake.py', 'portable.py', 'Portable/Exports.lean'):
            (checks / helper).write_text('')
        (checks / 'Portable/ArtifactHashes.lean').write_text('''import Lake.Build.Trace

def main : IO Unit := do
  for path in (← (← IO.getStdin).readToEnd).splitOn "\\n" do
    IO.println (← Lake.computeBinFileHash path).hex
''')
        common = self.root / '.lake/omega-common-src'

        def layout(strict_backend, with_reg, common_dir=None):
            self.assertFalse(with_reg)
            if not strict_backend and common_dir is None:
                return ['lake'], {'': self.root / PROJECT_BUILD / 'lib/lean'}
            label = 'omega-common' if common_dir else 'omega-strict'
            source = common_dir or 'strict'
            text = self.cfg.read_text()
            for name in ('OmegaInit', 'OmegaMathlib'):
                text = text.replace(f'name = "{name}"\nsrcDir = "src"', f'name = "{name}"\nsrcDir = "{source}"')
            build = PROJECT_BUILD / label
            config = self.root / '.lake' / (label + '.toml')
            config.write_text(f'buildDir = "{build}"\n' + text)
            return ['lake', '-f', str(config)], {'': self.root / build / 'lib/lean'}

        def derived_facades(*args):
            names = args[2]
            normal = {n: (self.root / self.modules[n].source).read_bytes() for n in names}
            generated = dict(normal, OmegaInit=b'module\n@[expose] public def omegaBase := 11\n')
            return normal, generated

        strict_api = SimpleNamespace(ROOT=self.root.resolve(), COMMON_SRC=common, lake_layout=layout)
        portable_api = SimpleNamespace(ROOT=self.root.resolve(), derived_facades=derived_facades,
                                       output=lambda *args: self.lake(*args[1:]).stdout)
        self.enterContext(patch.object(cache.importlib, 'import_module', side_effect=lambda name: {
            'strict_lake': strict_api, 'portable': portable_api}[name]))
        return portable_api

    def test_strict_foundation_restores_the_ordinary_native_plugin(self):
        from lean_artifact_inventory import plugin_outputs

        self.omega_fixture()
        plugin = cache.inventory.INIT_PLUGIN
        helpers = {plugin + '.Layout', plugin + '.Proofs'}
        names = helpers | {plugin}
        for name in sorted(names):
            source = Path('src', *name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', source, Path('.'))
            path = self.root / source
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text(('import ' + '\nimport '.join(sorted(helpers)) + '\n') if name == plugin
                            else 'namespace ' + name + '\ndef value : Nat := 1\nend ' + name + '\n')
        with self.cfg.open('a') as file:
            file.write(f'\n[[lean_lib]]\nname = "{plugin}"\nsrcDir="src"\n')
        modules, original = cache.checkout_targets(self.root)
        self.assertIn(plugin, original['OmegaInit']['deps'])
        self.assertEqual(set(original[plugin]['deps']), helpers)
        for n in names:
            self.assertEqual(modules[n].extra_builds, ())
            self.assertTrue(modules[n].plugin_input)
        with patch.object(cache, 'binary_platform', return_value=('linux', 'other-architecture')):
            _, other_host = cache.checkout_targets(self.root)
        self.assertNotEqual(original[plugin + '.Layout']['key'], other_host[plugin + '.Layout']['key'])
        self.assertNotEqual(original['OmegaInit']['key'], other_host['OmegaInit']['key'])
        self.assertEqual(original['D5.B']['key'], other_host['D5.B']['key'])
        archive = self.root / 'plugin.tar.gz'
        cache.compile_omega_outputs(self.root, modules, names, False)
        descriptor = pack_shard(self.root, modules, names, archive, 'c'*64, kind='compiler')
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / 'checkout'
            shutil.copytree(self.root, other, ignore=shutil.ignore_patterns('.lake', '*.tar.gz'))
            dest = other / PROJECT_BUILD
            self.assertEqual(restore_shard(archive, descriptor, names, modules, dest,
                                           root=other, kind='compiler'), names)
            cache.materialize_shared(dest, modules, names)
            config = other / 'strict-plugin.toml'
            text = (other / 'lakefile.toml').read_text().replace('name = "OmegaInit"\n',
                f'name = "OmegaInit"\nneeds=["{plugin}:shared"]\nplugins=["{plugin}:shared"]\n')
            config.write_text('buildDir=".lake/build/omega-strict"\n' + text)
            subprocess.run(['lake', '-f', config, '--no-build', 'build', plugin + ':shared'], cwd=other, check=True)
            built = subprocess.run(['lake', '-f', config, 'build', '+OmegaInit:olean'], cwd=other,
                                   check=True, capture_output=True, text=True)
            self.assertNotIn('Built ' + plugin, built.stdout + built.stderr)
        # A different-platform library in the same archive does not exclude a portable sibling.
        self.compile(self.root, modules, ['D5.B'])
        mixed = self.root / 'mixed-native.tar.gz'
        mixed_row = pack_shard(self.root, modules, names | {'D5.B'}, mixed, 'd'*64, kind='compiler')
        other_platform = 'darwin' if cache.sys.platform != 'darwin' else 'linux'
        with patch.object(cache.sys, 'platform', other_platform):
            self.assertEqual(restore_shard(mixed, mixed_row, ['D5.B'], modules, self.root / 'portable',
                                           root=self.root, kind='compiler'), {'D5.B'})
        path, = [p for p in plugin_outputs(plugin, modules[plugin]) if p.parent.name == 'lib']
        (self.root / PROJECT_BUILD / path).write_bytes(b'invalid native plugin')
        with self.assertRaisesRegex(ValueError, 'differs from its Lake trace'):
            pack_shard(self.root, modules, names, archive, 'c'*64, kind='compiler')
        helper = modules[plugin + '.Layout'].source
        with (self.root / helper).open('a') as source:
            source.write('def extraPluginLayout := 2\n')
        _, updated = cache.checkout_targets(self.root)
        for name in (plugin + '.Layout', plugin, 'OmegaInit', 'OmegaMathlib', 'D5.A'):
            self.assertNotEqual(original[name]['key'], updated[name]['key'], name)
        self.assertEqual(original['D5.B']['key'], updated['D5.B']['key'])

    def test_omega_backends_share_module_identity_and_restore_together(self):
        self.omega_fixture()
        strict = self.root / 'strict'
        checks = self.root / 'src/OmegaChecks'
        plan = self.plan()
        modules, _ = cache.checkout_targets(self.root)
        self.assertEqual(modules['OmegaMathlib'].extra_builds, (Path('omega-strict'), Path('omega-common')))
        self.assertEqual(modules['D5.A'].extra_builds, (Path('omega-strict'),))
        self.assertEqual(modules['D5.B'].extra_builds, ())
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'OmegaChecks.Portable.Client'} <= plan['targets'].keys())
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        self.assertFalse(self.plan(index)['lanes'])
        build = self.root / PROJECT_BUILD
        paths = ['lib/lean/OmegaInit.olean', 'omega-strict/lib/lean/OmegaInit.olean',
                 'omega-common/lib/lean/OmegaInit.olean']
        outputs = {path: (build / path).read_bytes() for path in paths}
        self.assertEqual(len(set(outputs.values())), 3)
        shutil.rmtree(build)
        result = cache.restore(self.root, index, ['D5.C', 'OmegaChecks.Portable.Client'], self.remote, build)
        self.assertEqual(result['restored'], 5)
        self.assertEqual({path: (build / path).read_bytes() for path in paths}, outputs)
        self.assertFalse((build / 'lib/lean/D5/B.olean').exists())
        for backend in (None, 'omega-strict', 'omega-common'):
            options = ['-f', str(self.root / '.lake' / (backend + '.toml'))] if backend else []
            self.lake(*options, '--no-build', 'build', '+OmegaChecks.Portable.Client:olean')

        name = 'OmegaInit'
        row = index['shards'][index['targets'][name]['shard']]
        archive = self.root / 'omega.tar.gz'
        self.remote.download(row, archive)

        # An independent official build has the same outputs but local paths in its trace.
        warm = self.root / 'warm'
        (warm / 'src').mkdir(parents=True)
        for source in ('lean-toolchain', 'lakefile.toml', 'src/OmegaInit.lean'):
            shutil.copyfile(self.root / source, warm / source)
        subprocess.run(['lake', 'build', '+OmegaInit:olean'], cwd=warm, check=True, capture_output=True)
        warm_build = warm / PROJECT_BUILD
        official_trace = Path(paths[0]).with_suffix('.trace')
        local_trace = (warm_build / official_trace).read_bytes()
        self.assertNotEqual(local_trace, (build / official_trace).read_bytes())
        for missing_backends in (('omega-strict', 'omega-common'), ('omega-strict',), ('omega-common',)):
            with self.subTest(missing_backends=missing_backends):
                for backend in missing_backends:
                    if (warm_build / backend).exists():
                        shutil.rmtree(warm_build / backend)
                result = cache.restore(self.root, index, [name], self.remote, warm_build)
                self.assertEqual(result['restored'], 1)
                self.assertEqual((warm_build / official_trace).read_bytes(), local_trace)
                self.assertEqual({path: (warm_build / path).read_bytes() for path in paths}, outputs)

        # Wrong bytes with the original trace and sidecar cannot be published or restored.
        for crossed_path in paths:
            with self.subTest(crossed_path=crossed_path):
                path = build / crossed_path
                wrong_bytes = outputs[paths[1] if crossed_path == paths[0] else paths[0]]
                path.write_bytes(wrong_bytes)
                try:
                    rejected = self.root / 'unpublishable.tar.gz'
                    with self.assertRaisesRegex(ValueError, 'artifact differs from its Lake trace'):
                        pack_shard(self.root, modules, [name], rejected, 'a' * 64)
                    self.assertFalse(rejected.exists())
                finally:
                    path.write_bytes(outputs[crossed_path])

                # Give the altered archive a valid transport checksum so restoration reaches Lake verification.
                crossed_archive = self.root / 'crossed.tar.gz'
                with tarfile.open(archive) as original, tarfile.open(crossed_archive, 'w:gz') as tampered:
                    for entry in original:
                        data = original.extractfile(entry).read()
                        if entry.name == crossed_path:
                            data = wrong_bytes
                        entry.size = len(data)
                        tampered.addfile(entry, io.BytesIO(data))
                payload = crossed_archive.read_bytes()
                crossed_row = dict(row, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
                fresh = self.root / 'unrestorable'
                with self.assertRaisesRegex(ValueError, 'artifact differs from its Lake trace'):
                    restore_shard(crossed_archive, crossed_row, [name], modules, fresh, root=self.root, kind='compiler')
                self.assertFalse(fresh.exists())

        # A module with a different backend inventory cannot consume this member.
        crossed = {name: replace(modules[name], extra_builds=tuple(reversed(modules[name].extra_builds)))}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], crossed, self.root / 'crossed', root=self.root, kind='compiler')
        conflict = self.root / 'conflict'
        conflicting = conflict / paths[1]
        conflicting.parent.mkdir(parents=True)
        conflicting.write_bytes(b'local strict artifact')
        self.assertEqual(restore_shard(archive, row, [name], modules, conflict, root=self.root, kind='compiler'), set())
        self.assertEqual(conflicting.read_bytes(), b'local strict artifact')
        self.assertFalse((conflict / paths[0]).exists())
        self.assertFalse((conflict / paths[2]).exists())
        official_only = {name: replace(modules[name], extra_builds=())}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], official_only, self.root / 'legacy', kind='compiler')
        legacy = self.root / 'legacy.tar.gz'
        legacy_row = pack_shard(self.root, official_only, [name], legacy, 'a' * 64)
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(legacy, legacy_row, [name], modules, self.root / 'missing-backends')
        (build / paths[1]).unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete compiler artifacts'):
            pack_shard(self.root, modules, [name], self.root / 'incomplete.tar.gz', 'a' * 64)

        def changed():
            _, current = cache.checkout_targets(self.root)
            return {n for n in current if current[n]['key'] != index['targets'][n]['key']}

        # A direct OmegaMathlib consumer changes even without importing OmegaMath.
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 2\n')
        self.assertTrue({'OmegaMathlib', 'D5.A', 'D5.C', 'OmegaChecks.Portable.Client'} <= changed())
        self.assertNotIn('D5.B', changed())
        stale = cache.restore(self.root, index, ['D5.C'], self.remote, self.root / 'stale')
        self.assertEqual(stale['restored'], 0)
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 1\n')
        self.assertFalse(changed())
        (checks / 'Portable/Exports.lean').write_text('-- changed common generator\n')
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'OmegaChecks.Portable.Client', 'D5.A', 'D5.C'} <= changed())
        self.assertNotIn('D5.B', changed())
        (checks / 'strict_lake.py').write_text('# changed strict configuration\n')
        self.assertTrue({'OmegaInit', 'OmegaMathlib', 'D5.A', 'D5.C'} <= changed())

    def test_strict_uses_the_omegainit_family_and_shares_ordinary_prerequisites(self):
        self.omega_fixture()
        leaf = 'OmegaInit.Leaf'
        path = Path('src/OmegaInit/Leaf.lean')
        for directory, value in [('src', 3), ('strict', 4)]:
            source = self.root / directory / 'OmegaInit/Leaf.lean'
            source.parent.mkdir(exist_ok=True)
            source.write_text(f'def leafValue := {value}\n')
        self.modules[leaf] = Module('trureturing', path, Path('.'))
        (self.root / 'D5/A.lean').write_text('import OmegaInit.Leaf\nimport D5.B\ndef valueA := leafValue+valueB\n')
        plan = self.dag_plan(workers=2)
        dag = plan['compiler_dag']
        from lean_module_dag import worker_modules
        modules = worker_modules(self.root, dag)
        self.assertEqual(modules['D5.A'].extra_builds, (Path('omega-strict'),))
        self.assertEqual(modules['D5.C'].extra_builds, (Path('omega-strict'),))
        ordinary = modules['D5.B']
        self.assertEqual(ordinary.extra_builds, ())
        self.assertEqual(ordinary.shared_builds, (Path('omega-strict'),))
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        build = self.root / PROJECT_BUILD
        shutil.rmtree(build)
        cache.restore(self.root, index, ['D5.C'], self.remote, build, kind='compiler')
        official = build / 'lib/lean/D5/B.trace'
        shared = build / 'omega-strict/lib/lean/D5/B.trace'
        self.assertEqual(official.read_bytes(), shared.read_bytes())
        config = self.root / '.lake/omega-strict.toml'
        self.lake('-f', str(config), '--no-build', 'build', '+D5.C:olean')
        self.assertFalse((build / 'omega-common/lib/lean/D5/B.olean').exists())

    def test_compiled_backend_selection_survives_scoped_cache_and_marker_removal(self):
        api = self.omega_fixture()
        with self.cfg.open('a') as config:
            config.write('[[lean_lib]]\nname = "OmegaBuild"\nsrcDir = "src"\n')
        name = 'OmegaBuild.Commands'
        source = Path('src/OmegaBuild/Commands.lean')
        self.modules[name] = Module('trureturing', source, Path('.'))
        (self.root / source).parent.mkdir(parents=True)
        (self.root / source).write_text('def commandProvider := true\n')
        for reader_name, code in [('OmegaBuild.Plan', 'import OmegaBuild.Parts'),
                                  ('OmegaBuild.Parts', 'def planSetting := 1')]:
            source = Path('src', *reader_name.split('.')).with_suffix('.lean')
            self.modules[reader_name] = Module('trureturing', source, Path('.'))
            (self.root / source).write_text(code + '\n')
        marked = self.root / 'D5/A.lean'
        marked.write_text('import OmegaBuild.Commands\nimport OmegaMathlib\ndef valueA := omegaValue\n')
        (self.root / 'D5/B.lean').write_text('import OmegaBuild.Commands\ndef valueB := commandProvider\n')
        (self.root / 'src/OmegaChecks/Portable/Client.lean').write_text('import D5.A\ndef portable := valueA\n')
        reasons = {'D5.A': ['D5.A'], 'D5.C': ['D5.C', 'D5.A'],
                   'OmegaChecks.Portable.Client': ['OmegaChecks.Portable.Client', 'D5.A']}
        read = set()

        def backend_plan(search_path, names):
            for module in names:
                artifact = Path(*module.split('.')).with_suffix('.olean')
                self.assertTrue(any((Path(d) / artifact).is_file() for d in search_path.split(os.pathsep)))
            read.update(names)
            return {'mathlibOnly': {n: reasons[n] for n in names if n in reasons}}

        api.backend_plan = backend_plan
        plan = self.plan()
        self.assertFalse(read)
        self.workers(plan)
        index = cache.finish(plan, self.remote)
        self.assertEqual(read, set(reasons))
        build = self.root / PROJECT_BUILD
        shutil.rmtree(build)
        read.clear()
        result = cache.restore(self.root, index, ['D5.C', 'D5.B', 'OmegaChecks.Portable.Client'],
                               self.remote, build)
        self.assertGreater(result['restored'], 0)
        self.assertFalse(read)
        for n in reasons:
            base = Path('lib/lean', *n.split('.')).with_suffix('.olean')
            self.assertTrue((build / base).is_file())
            self.assertFalse((build / 'omega-strict' / base).exists())
            self.assertFalse((build / 'omega-common' / base).exists())
        self.assertFalse((build / 'omega-strict/lib/lean/D5/B.olean').exists())
        for filename in ('src/OmegaBuild/Plan.lean', 'src/OmegaBuild/Parts.lean', 'src/OmegaChecks/portable.py'):
            reader_file = self.root / filename
            original = reader_file.read_bytes()
            reader_file.write_bytes(original + b'\n')
            changed = self.plan(index)
            self.assertEqual(changed['targets']['D5.B']['key'], plan['targets']['D5.B']['key'])
            for name in reasons:
                self.assertNotEqual(changed['targets'][name]['key'], plan['targets'][name]['key'])
            stale = self.root / ('stale-' + reader_file.name)
            cache.restore(self.root, index, ['D5.C'], self.remote, stale)
            self.assertFalse((stale / 'lib/lean/D5/C.olean').exists())
            reader_file.write_bytes(original)
        self.source = 'b' * 40
        marked.write_text(marked.read_text().replace('def valueA', '-- removed marker\ndef valueA'))
        reasons.clear()
        next_plan = self.plan(index)
        self.assertNotEqual(plan['targets']['D5.C']['key'], next_plan['targets']['D5.C']['key'])
        self.workers(next_plan)
        for n in ('D5.A', 'D5.C', 'OmegaChecks.Portable.Client'):
            self.assertTrue((build / 'omega-strict/lib/lean' / Path(*n.split('.')).with_suffix('.olean')).exists())
        self.assertTrue((build / 'omega-common/lib/lean/OmegaChecks/Portable/Client.olean').exists())

    def test_new_names_use_the_mathlib_release_and_inherit_original_addresses(self):
        manifest = self.root / 'lake-manifest.json'
        manifest.write_text(json.dumps({'packages': [
            {'name': 'mathlib', 'rev': self.pin, 'inputRev': self.version}]}))
        self.assertEqual(cache.publication_mathlib(self.root), (self.pin, 'v4.35.0'))
        manifest.write_text(json.dumps({'packages': [{'name': 'mathlib', 'rev': self.pin, 'inputRev': self.pin}]}))
        with self.assertRaisesRegex(ValueError, 'mathlib release version'):
            cache.publication_mathlib(self.root)

        _, targets = cache.checkout_targets(self.root)
        criteria = cache.shard_row(self.pin, self.version, targets, 'D5', list(targets))
        self.assertEqual(criteria['asset'], 'v4.35.0-D5.tar.gz')
        self.assertEqual(criteria['key'], criteria['asset'])
        historical = dict(criteria, key=self.pin + '-D5', asset=self.pin + '-D5.tar.gz',
                          version='lean-modules-' + self.source, bytes=123, sha256='c'*64)
        base = {'source': self.source, 'version': historical['version'], 'targets': targets,
                'shards': {'D5': historical}}
        plan = cache.make_plan('b'*40, self.pin, targets, base, self.remote, cache.DEFAULT_CAPACITY, 2,
                               mathlib_version=self.version)
        self.assertEqual(plan['lanes'], [])
        self.assertEqual(cache.finish(plan, self.remote)['shards']['D5'], historical)
        self.assertEqual(self.remote.saves, [])

    def test_first_plan_uses_verified_compressed_sizes_for_layout(self):
        _, targets = cache.checkout_targets(self.root)
        legacy = {'targets': {n: {'bytes': 12} for n in targets}}
        guessed = self.plan(capacity=40)
        self.assertEqual(len(guessed['shards']), 3)
        measured = cache.make_plan(self.source, self.pin, targets, None, self.remote, 40, 2, mathlib_version=self.version, legacy=legacy)
        self.assertEqual(measured['shards'].keys(), {'D5'})
        self.assertEqual(measured['legacy'], legacy)

    def test_actual_oversize_split_survives_interruption_and_upgrade(self):
        # Start with a cold underestimate so real compressed bytes trigger the split in the worker.
        with patch.object(cache, 'DEFAULT_MODULE_BYTES', 1):
            initial = self.plan(capacity=200)
            self.assertEqual(list(initial['shards']), ['D5'])
            self.remote.fail = 'D5.B'
            with self.assertRaises(InterruptedError):
                self.workers(initial)
            self.assertEqual(self.remote.saves, ['D5.A'])
            self.assertEqual(self.remote.indexes, {})
            self.remote.fail = None
            shutil.rmtree(self.root / PROJECT_BUILD)
            retry = self.plan(capacity=200)
            self.assertEqual(set(retry['inherited']), {'D5.A'})
            self.workers(retry)
            first = cache.finish(retry, self.remote)
            self.assertCountEqual(self.remote.saves, ['D5.A', 'D5.B', 'D5.C'])
            self.assertEqual(self.plan(first, capacity=200)['lanes'], [])
            self.pin = '2' * 40
            self.source = 'c' * 40
            upgraded = self.plan(first, capacity=200)
            self.assertFalse(upgraded['inherited'])
            self.assertTrue(all(row['key'].startswith(self.version + '-') for row in upgraded['shards'].values()))


if __name__ == '__main__':
    unittest.main()
