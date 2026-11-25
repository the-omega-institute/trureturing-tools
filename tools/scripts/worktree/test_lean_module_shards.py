"""Namespace layout and transport of scoped Lake compiler artifacts."""

from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from lean_artifact_inventory import Module, PROJECT_BUILD
from lean_module_shards import DEFAULT_CAPACITY, lookup_shard, pack_shard, plan_shards, restore_shard


class NamespaceLayout(unittest.TestCase):
    def test_split_stability_single_oversize_and_explicit_owner(self):
        sizes = {'D5.Foo': 1, 'D5.Foo.Big': 20, 'D5.Foo.Big.Small': 2,
                 'D5.Foobar': 3, 'D5.Else': 1, 'D5.Leaf.Only': 15}
        layout = plan_shards(sizes, 8)
        self.assertEqual(layout, {'D5': ['D5.Else', 'D5.Foobar'], 'D5.Foo': ['D5.Foo'],
                                  'D5.Foo.Big': ['D5.Foo.Big'], 'D5.Foo.Big.Small': ['D5.Foo.Big.Small'],
                                  'D5.Leaf.Only': ['D5.Leaf.Only']})
        owners = {n: shard for shard, names in layout.items() for n in names}
        for name, owner in owners.items():
            self.assertEqual(lookup_shard(name, owners, layout), owner)
        self.assertEqual(plan_shards(dict(reversed(sizes.items())), 8), layout)
        reduced = plan_shards({name: 1 for name in sizes}, 8, previous=layout)
        self.assertEqual(reduced, layout)
        layout['D5'].append('External.Small')
        owners['External.Small'] = 'D5'
        self.assertEqual(lookup_shard('External.Small', owners, layout), 'D5')
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            lookup_shard('D5.Foo.Big', dict(owners, **{'D5.Foo.Big': 'D5'}), layout)

    def test_default_capacity_splits_large_groups_and_keeps_indivisible_modules(self):
        self.assertEqual(DEFAULT_CAPACITY, 256 * 1024**2)
        sizes = {'D5.A': 150 * 1024**2, 'D5.B': 110 * 1024**2, 'Reg.Large': 300 * 1024**2}
        self.assertEqual(plan_shards(sizes), {'D5': ['D5.B'], 'D5.A': ['D5.A'], 'Reg.Large': ['Reg.Large']})

    def test_two_small_siblings_share_one_named_shard(self):
        self.assertEqual(plan_shards({'Math.A': 1, 'Math.B': 1}, 2), {'Math': ['Math.A', 'Math.B']})


class ShardCompilerTransport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copyfile(Path(__file__).resolve().parents[3] / 'lean-toolchain', self.root / 'lean-toolchain')
        (self.root / 'lakefile.toml').write_text('name = "ns_fixture"\n[[lean_lib]]\nname = "Demo"\n')
        (self.root / 'Demo').mkdir()
        self.modules = {}
        for name in ('A', 'B'):
            source = Path('Demo', name + '.lean')
            (self.root / source).write_text('def Demo.value' + name + ' : Nat := 3\n')
            self.modules['Demo.' + name] = Module('ns_fixture', source, Path('.'))
        subprocess.run(['lake', 'build', '+Demo.A:leanArts', '+Demo.B:leanArts'], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL)
        self.build = self.root / PROJECT_BUILD
        path = self.build / 'lean-inspector/modules/Demo.A.zip'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'excluded report')
        self.archive = self.root / 'rev-Demo.tar.gz'
        self.descriptor = pack_shard(self.root, self.modules, self.modules, self.archive, 'c'*64)

    def test_real_compiler_archives_are_stable_scoped_and_full(self):
        again = self.root / 'second.tar.gz'
        self.assertEqual(pack_shard(self.root, self.modules, reversed(self.modules), again, 'c'*64), self.descriptor)
        self.assertEqual(self.archive.read_bytes(), again.read_bytes())
        with self.archive.open('rb') as archive:
            header = archive.read(75)
        self.assertEqual(header[:4], b'\x1f\x8b\x08\x08')
        self.assertEqual(header[10:], b'c'*64 + b'\0')
        with tarfile.open(self.archive) as bundle:
            self.assertFalse(any('lean-inspector' in p for p in bundle.getnames()))
        for names in (['Demo.A'], ['Demo.B'], list(self.modules)):
            dest = self.root / ('consumer-' + ''.join(names))
            active = {n: self.modules[n] for n in names}
            self.assertEqual(restore_shard(self.archive, self.descriptor, names, active, dest), set(names))
            self.assertFalse((dest / 'lean-inspector').exists())
            self.assertEqual({p.stem for p in (dest / 'lib/lean/Demo').glob('*.olean')}, {n[-1] for n in names})
            for artifact in dest.rglob('*'):
                if artifact.is_file():
                    self.assertEqual(artifact.read_bytes(), (self.build / artifact.relative_to(dest)).read_bytes())
        self.assertTrue(all(self.descriptor['module_sizes'][n] > 0 for n in self.modules))

    def test_conflicting_module_is_skipped_and_malformed_input_cannot_install(self):
        dest = self.root / 'consumer'
        existing = dest / 'lib/lean/Demo/A.olean'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'local')
        self.assertEqual(restore_shard(self.archive, self.descriptor, self.modules, self.modules, dest), {'Demo.B'})
        self.assertEqual(existing.read_bytes(), b'local')
        self.assertFalse(existing.with_suffix('.ilean').exists())
        self.assertTrue(existing.with_name('B.olean').is_file())
        for patch in ({'members': ['Demo.A']}, {'sha256': '0' * 64}):
            fresh = self.root / 'fresh'
            with self.assertRaises(ValueError):
                restore_shard(self.archive, {**self.descriptor, **patch}, ['Demo.A'], self.modules, fresh)
            self.assertFalse(fresh.exists())


if __name__ == '__main__':
    unittest.main()
