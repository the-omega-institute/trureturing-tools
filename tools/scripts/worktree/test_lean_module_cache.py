"""End-to-end compiler shard selection, incremental publication and restart with real Lake artifacts."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lean_module_cache as cache
from lean_artifact_inventory import Module, PROJECT_BUILD
from lean_module_shards import pack_shard, restore_shard


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

    def test_omega_backends_share_module_identity_and_restore_together(self):
        for name in ('OmegaInit', 'OmegaMathlib', 'OmegaChecks'):
            with self.cfg.open('a') as config:
                config.write(f'[[lean_lib]]\nname = "{name}"\nsrcDir = "src"\n')
        for name, code in [('OmegaInit', 'def omegaBase := 1\n'),
                           ('OmegaMathlib', 'import OmegaInit\ndef omegaValue := omegaBase + 1\n'),
                           ('OmegaChecks.Portable.Client', 'import OmegaMathlib\ndef checked := omegaValue\n')]:
            path = Path('src', *name.split('.')).with_suffix('.lean')
            self.modules[name] = Module('trureturing', path, Path('.'))
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(code)
        strict = self.root / 'strict'
        strict.mkdir()
        (strict / 'OmegaInit.lean').write_text('def omegaBase := 7\n')
        (strict / 'OmegaMathlib.lean').write_text('import OmegaInit\ndef omegaValue := omegaBase + 1\n')
        (self.root / 'D5/A.lean').write_text('import OmegaMathlib\ndef valueA := omegaValue\n')
        checks = self.root / 'src/OmegaChecks'
        for helper in ('strict_lake.py', 'portable.py', 'Portable/Exports.lean'):
            (checks / helper).write_text('')
        common = self.root / '.lake/omega-common-src'

        def layout(strict_backend, with_reg, common_dir=None):
            self.assertFalse(with_reg)
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
            generated = dict(normal, OmegaInit=b'def omegaBase := 11\n')
            return normal, generated

        strict_api = SimpleNamespace(ROOT=self.root.resolve(), COMMON_SRC=common, lake_layout=layout)
        portable_api = SimpleNamespace(ROOT=self.root.resolve(), derived_facades=derived_facades,
                                       output=lambda *args: self.lake(*args[1:]).stdout)
        with patch.object(cache.importlib, 'import_module', side_effect=lambda name: {
            'strict_lake': strict_api, 'portable': portable_api}[name]):
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
        outputs = {(build / path).read_bytes() for path in paths}
        self.assertEqual(len(outputs), 3)
        shutil.rmtree(build)
        result = cache.restore(self.root, index, ['D5.C', 'OmegaChecks.Portable.Client'], self.remote, build)
        self.assertEqual(result['restored'], 5)
        self.assertEqual({(build / path).read_bytes() for path in paths}, outputs)
        self.assertFalse((build / 'lib/lean/D5/B.olean').exists())
        for backend in (None, 'omega-strict', 'omega-common'):
            options = ['-f', str(self.root / '.lake' / (backend + '.toml'))] if backend else []
            self.lake(*options, '--no-build', 'build', '+OmegaChecks.Portable.Client:olean')

        # A module with a different backend inventory cannot consume this member.
        name = 'OmegaInit'
        row = index['shards'][index['targets'][name]['shard']]
        archive = self.root / 'omega.tar.gz'
        self.remote.download(row, archive)
        crossed = {name: replace(modules[name], extra_builds=tuple(reversed(modules[name].extra_builds)))}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], crossed, self.root / 'crossed')
        conflict = self.root / 'conflict'
        conflicting = conflict / paths[1]
        conflicting.parent.mkdir(parents=True)
        conflicting.write_bytes(b'local strict artifact')
        self.assertEqual(restore_shard(archive, row, [name], modules, conflict), set())
        self.assertEqual(conflicting.read_bytes(), b'local strict artifact')
        self.assertFalse((conflict / paths[0]).exists())
        self.assertFalse((conflict / paths[2]).exists())
        official_only = {name: replace(modules[name], extra_builds=())}
        with self.assertRaisesRegex(ValueError, 'build path differs'):
            restore_shard(archive, row, [name], official_only, self.root / 'legacy')
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
