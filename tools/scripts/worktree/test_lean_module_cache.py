"""End-to-end compiler shard selection, incremental publication and restart with real Lake artifacts."""

from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lean_module_cache as cache
from lean_artifact_inventory import Module, PROJECT_BUILD


class MemoryRemote:
    def __init__(self):
        self.indexes = {}
        self.rows = {}
        self.assets = {}
        self.layouts = {}
        self.saves = []
        self.fail = None

    def confirmed(self, rows):
        return rows

    def latest(self, version=None):
        return self.indexes.get(version)

    def partials(self, plan):
        return {n: self.rows[r['fingerprint']] for n, r in plan['shards'].items() if r['fingerprint'] in self.rows}

    def save(self, plan, name, path, row):
        if self.fail == name:
            raise InterruptedError(name)
        key = row['version'], row['asset']
        assert key not in self.assets
        self.assets[key] = path.read_bytes()
        self.rows[row['fingerprint']] = row
        self.saves.append(name)
        return row

    def splits(self, plan):
        return {n: self.layouts[r['fingerprint']] for n, r in plan['shards'].items() if r['fingerprint'] in self.layouts}

    def save_split(self, plan, name, layout):
        fingerprint = plan['shards'][name]['fingerprint']
        assert self.layouts.setdefault(fingerprint, layout) == layout

    def download(self, row, output):
        output.write_bytes(self.assets[row['version'], row['asset']])

    def publish(self, index):
        assert self.indexes.setdefault(index['version'], index) == index


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
        return cache.make_plan(self.source, self.pin, targets, base, self.remote, capacity, 2, mathlib_version=self.version)

    def workers(self, plan):
        return [cache.work(self.root, plan, i, self.remote, self.root / 'receipts') for i in range(len(plan['lanes']))]

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
