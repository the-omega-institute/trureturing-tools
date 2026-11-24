import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lean_actions_shards as legacy
from lean_artifact_inventory import Artifact, PROJECT_BUILD, Module, required_compiler_paths
from lean_cache_release_io import archive_sha
from lean_generation_index import encode, sha
import lean_generation_seed as seed
from lean_release_index import safe_path, write_frame


class Migration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.name = "D5.A"
        self.module = Module("trureturing", Path("D5/A.lean"), Path("."))
        (self.root / self.module.source.parent).mkdir()
        (self.root / self.module.source).write_text("def a := 1\n")
        self.build = self.root / PROJECT_BUILD
        for path in required_compiler_paths(self.name, self.module):
            physical = self.build / path
            physical.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".trace":
                physical.write_text(json.dumps({"outputs": {"o": ["hash.olean"], "i": "hash.ilean", "c": "hash.c"}}))
            else:
                physical.write_bytes(b"compiled")
                physical.with_name(physical.name + ".hash").write_bytes(b"hash")
        self.paths = sorted(str(p.relative_to(self.build)) for p in self.build.rglob("*") if p.is_file())
        self.fingerprint = legacy.digest([(n, (self.build / n).stat().st_mode & 0o777,
                                          archive_sha(self.build / n, None)) for n in self.paths])
        self.digest = legacy.write_payload(self.root, self.build, self.paths)
        self.archive = self.root / legacy.transport_payload(self.digest)

    def test_migrated_frame_identical_to_worker_and_missing_trace_rejected(self):
        directory = self.root / "frames"
        directory.mkdir()
        scratch = self.root / "scratch"
        destination = scratch / PROJECT_BUILD
        destination.mkdir(parents=True)
        available = seed.extract_shard(self.archive, self.digest, self.fingerprint, self.paths,
                                       set(self.paths), destination)
        frame = seed.pack_target(scratch, self.name, set(self.paths), available,
                                 {self.name: self.module}, directory)
        artifacts = []
        for path in sorted(map(safe_path, self.paths)):
            stat = (self.build / path).stat()
            artifacts.append(Artifact(path, stat.st_mode, stat.st_size, self.name, "lean"))
        with (self.root / "worker-frame").open("wb") as stream:
            worker = write_frame(self.root, {self.name: self.module}, self.name, artifacts, stream, None,
                                 stable_timestamps=True)
        self.assertEqual(frame["sha256"], worker["sha256"])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            seed.pack_target(scratch, self.name, set(self.paths), {p for p in available if not p.endswith(".trace")},
                             {self.name: self.module}, directory)

    def test_fingerprint_and_missing_required_path(self):
        for fingerprint, expected in [(sha(b"stale"), self.paths), (self.fingerprint, ["not-in-the-archive"])]:
            with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, "fingerprint or required"):
                seed.extract_shard(self.archive, self.digest, fingerprint, expected, set(self.paths), Path(temp))

    def test_index_is_bound_to_exact_run_attempt_source_and_cache_key(self):
        identity = {"schema": legacy.SCHEMA, "mathlib": "b"*40, "os": "linux", "arch": "arm64",
                    "config": sha(b"config"), "execution": sha(b"execution"), "runtime": sha(b"runtime")}
        owner = {"ref": "refs/heads/dev", "run": "23", "attempt": "2", "commit": "c"*40}
        group = [[self.name]]
        group_id = legacy.digest(group)
        raw = {"state": "complete", "binding": identity, "owner": owner, "layout": {group_id: group},
               "entries": {group_id: self.digest}, "sources": {self.name: sha(b"source")},
               "required": {group_id: self.paths}, "fingerprints": {group_id: self.fingerprint}}
        index = self.root / "index.json"
        index.write_bytes(encode(raw))
        addresses = legacy.index_addresses(identity)
        descriptor = {**owner, "key": addresses["snapshot_prefix"] + "23-2", "prefix": addresses["prefix"],
                      "index_sha256": archive_sha(index, None)}
        file = self.root / "seed.json"
        file.write_bytes(encode(descriptor))
        with patch.object(legacy, "binding", return_value=identity), patch.object(
                seed.compiler, "checkout", return_value=owner["commit"]):
            self.assertEqual(seed.pinned_seed(self.root, self.root)[1], raw)
            file.write_bytes(encode({**descriptor, "attempt": "3"}))
            with self.assertRaisesRegex(ValueError, "protected-dev producer"):
                seed.pinned_seed(self.root, self.root)
            file.write_bytes(encode(descriptor))
            index.write_bytes(index.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "digest"):
                seed.pinned_seed(self.root, self.root)


if __name__ == "__main__":
    unittest.main()


class WarmGeneration(unittest.TestCase):
    def test_complete_seed_reuses_reports_and_compiler_without_opening_frames(self):
        from contextlib import ExitStack
        import lean_cache_generation as generation
        import lean_generation_facets as facets
        import lean_generation_plan as compiler
        from test_lean_cache_generation import Store

        # The files represent an authenticated complete seed. Open the same
        # compiler plan and facet-production coordinator used on a fresh run.
        modules = {'A', *[main for _, main, _ in facets.PROGRAMS.values()]}
        module_rows = {n: {'key': sha(n.encode()), 'deps': []} for n in modules}
        remote = Store()
        source = '3' * 40
        with tempfile.TemporaryDirectory() as directory, ExitStack() as mocks:
            root = Path(directory)
            mocks.enter_context(patch.object(facets.report_selection, 'Selection')).return_value.modules.return_value = {'A': 'A.lean'}
            mocks.enter_context(patch.object(facets, 'utility_inputs', return_value={}))
            mocks.enter_context(patch.object(facets, 'remote_for', return_value=remote))
            planned = facets.all_targets(root, module_rows)
            targets = {}
            sizes = {}
            for name, descriptor in planned.items():
                d = sha(name.encode())
                paths = list(facets.facet_paths(name)) if '/' in name else ['lib/lean/' + name + '.olean']
                targets[name] = {**descriptor, 'paths': paths, 'offset': 0, 'bytes': 1, 'sha256': d, 'volume': d}
                sizes[d] = 1
                remote.volumes[d] = b'x'
            base = {'schema': generation.SCHEMA, 'source': source, 'targets': targets, 'volumes': sizes}
            remote.revision = 'f' * 40
            remote.marker = {'schema': generation.SCHEMA, 'source': source, 'index': sha(encode(base))}
            plan, hits = compiler.plan_layers(source, module_rows, [base])
            plan['domain'] = 'd' * 64
            self.assertFalse(plan['stages'])
            receipts = root / 'receipts'
            receipts.mkdir()
            compiler.write(receipts / 'compiler-hits.json', hits)
            build = mocks.enter_context(patch.object(facets, 'build_facets', side_effect=AssertionError('unexpected build')))
            put = mocks.enter_context(patch.object(remote, 'ensure_volume', side_effect=AssertionError('unexpected physical volume')))
            output = root / 'plan.json'
            facets.run(root, plan, base, receipts, output)
            result = generation.publish(remote, json.loads(output.read_bytes()), base,
                [json.loads(path.read_bytes()) for path in receipts.glob('*.json')], remote.revision)
            self.assertEqual(result['ci_index_sha256'], sha(encode(base)))
            self.assertEqual(remote.block_bytes, 0)
            build.assert_not_called()
            put.assert_not_called()

    def test_report_survives_unrelated_extractor_rebuild(self):
        import lean_generation_facets as facets
        modules = {n: {'key': sha(n.encode()), 'deps': []}
                   for n in ['A', *[main for _, main, _ in facets.PROGRAMS.values()]]}
        with patch.object(facets.report_selection, 'Selection') as registry, patch.object(facets, 'utility_inputs', return_value={}):
            registry.return_value.modules.return_value = {'A': 'A.lean'}
            previous = facets.all_targets(Path('.'), modules)
            modules['Inspector'] = {'key': '0' * 64, 'deps': []}
            current = facets.all_targets(Path('.'), modules)
        self.assertEqual(previous['report/A'], current['report/A'])
        self.assertNotEqual(previous['native/reportInspector'], current['native/reportInspector'])
