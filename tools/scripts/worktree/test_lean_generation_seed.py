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

    def test_only_reusable_missing_seed_outputs_trigger_downloads(self):
        import zipfile
        import lean_generation_facets as facets
        from test_lean_cache_generation import Store

        target = self.root / "target"
        target.mkdir()
        producer = self.root / "tools/lean-inspector/materials.py"
        producer.parent.mkdir(parents=True)
        public = facets.report_publication
        report = "report/" + self.name
        native = "native/reportInspector"
        report_paths = list(facets.facet_paths(report))
        bundle, *sidecars = [self.build / path for path in report_paths]
        bundle.parent.mkdir(parents=True)
        for path in sidecars:
            path.write_bytes(b"trace")
        raw = self.root / public.RAW
        raw.write_bytes(public.materials.canonical_json(dict(schema=public.materials.REPORT_SCHEMA,
                                                            modules=[{"module": self.name}])))
        public.write_origin(raw, self.name, {"producer_sources_sha256": "a"*64, "inspector_executable_sha256": "b"*64},
                            {"schema": "stratalint-judge-input-projection-v1", "module": self.name, "inputs": []})
        with zipfile.ZipFile(bundle, "w") as output:
            output.write(raw, public.RAW)
            output.write(public.member(raw, ".provenance.json"), public.RAW + ".provenance.json")
            output.writestr(public.RAW + ".materials.zip", b"")
        native_paths = list(facets.facet_paths(native))
        for path in native_paths:
            file = self.build / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"executable")
        (self.build / native_paths[0]).chmod(0o755)

        modules = {self.name: self.module}
        graph = {self.name: set()}
        planned = {n: {"key": sha(n.encode()), "deps": []} for n in
                   [self.name, *[main for _, main, _ in facets.PROGRAMS.values()]]}
        selection = self.enterContext(patch.object(facets.report_selection, "Selection"))
        selection.return_value.modules.return_value = {self.name: str(self.module.source)}
        self.enterContext(patch.object(facets, "utility_inputs", return_value={}))
        for host in [facets, seed]:
            self.enterContext(patch.object(host, "module_targets", return_value=planned))
        self.enterContext(patch.object(seed.inventory, "workspace_modules", return_value=modules))
        self.enterContext(patch.object(seed.inventory, "project_imports", return_value=graph))
        self.enterContext(patch.object(seed.compiler, "checkout", return_value="c"*40))
        self.enterContext(patch.object(seed, "package_inputs", return_value=("domain", {})))
        self.enterContext(patch.object(seed.checkpoints, "remote_for", return_value=Store()))
        self.enterContext(patch.object(seed.checkpoints, "save"))
        checkpoint = self.enterContext(patch.object(seed, "checkpoint_rows"))
        record = {"layout": {"group": [[self.name]]}, "sources": {self.name: archive_sha(self.root / self.module.source, None)},
                  "required": legacy.required_outputs(self.root, modules, graph, {self.name: "group"}),
                  "entries": {}, "fingerprints": {}}
        self.enterContext(patch.object(seed, "pinned_seed", return_value=({}, record)))
        for group, paths in [("group", self.paths + report_paths), (legacy.EXTRAS, native_paths)]:
            record["entries"][group] = legacy.write_payload(self.root, self.build, paths)
            record["fingerprints"][group] = legacy.digest([
                (p, (self.build / p).stat().st_mode & 0o777, archive_sha(self.build / p, None)) for p in sorted(paths)])

        def migrate(schema, hits):
            hits = set(hits) | {"native/" + exe for exe in facets.PROGRAMS if "native/" + exe != native}
            producer.write_text(f"REPORT_SCHEMA = {schema!r}\n")
            checkpoint.return_value = ({n: {} for n in hits}, {})
            requests = []
            def download(digests):
                requests.extend(digests)
                for digest in digests:
                    relative = legacy.transport_payload(digest)
                    (target / relative).parent.mkdir(parents=True, exist_ok=True)
                    (target / relative).write_bytes((self.root / relative).read_bytes())
                return digests
            result = seed.migrate(target, self.root, target, download)
            indexed = seed.read(target / "base.json")["targets"]
            return requests, result["seed_bytes_read"], set(indexed) - set(hits)

        old = "stratalint-raw-lean-report-v3"
        self.assertEqual(migrate(old, [self.name, native]), ([], 0, set()))
        group = record["entries"]["group"]
        extras = record["entries"][legacy.EXTRAS]
        size = (self.root / legacy.transport_payload(group)).stat().st_size
        native_size = (self.root / legacy.transport_payload(extras)).stat().st_size
        self.assertEqual(migrate(old, [native]), ([group], size, {self.name}))
        self.assertEqual(migrate(old, [self.name]), ([extras], native_size, {native}))
        self.assertEqual(migrate(public.materials.REPORT_SCHEMA, [self.name, native]), ([group], size, {report}))

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
    def test_exclusive_report_workers_resume_batches_and_publish_one_index(self):
        import io
        import tarfile
        from contextlib import ExitStack
        import lean_cache_generation as generation
        import lean_generation_facets as facets
        from test_lean_cache_generation import Store

        count = 641
        names = [f'report/D5.M{i:04d}' for i in range(count)]
        readers = ['native/inputDiscovery', 'native/reportInspector']
        targets = {name: {'key': sha(name.encode()), 'deps': []} for name in ['D5.A', *readers]}
        for name in names:
            targets[name] = {'key': sha(name.encode()), 'deps': ['D5.A']}
        remote = Store()
        base = {'source': 'a' * 40, 'targets': {}, 'volumes': {}}
        for name in ['D5.A', *readers]:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                entry = tarfile.TarInfo(name)
                entry.size = len(name)
                archive.addfile(entry, io.BytesIO(name.encode()))
            data = stream.getvalue()
            volume = sha(data)
            remote.volumes[volume] = data
            base['volumes'][volume] = len(data)
            base['targets'][name] = dict(targets[name], paths=[name], bytes=len(data), offset=0,
                                         sha256=volume, volume=volume)
        compiler = {'source': base['source'], 'domain': 'b' * 64, 'targets': {'D5.A': targets['D5.A']}}
        runs, checkpoints = [], []
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as mocks:
            root = Path(tmp)
            receipts = root / 'receipts'
            receipts.mkdir()
            def build(_, batch, __):
                if len(runs) == 32 and fail:
                    raise RuntimeError('interrupted shard')
                for name in batch:
                    self.assertNotIn(name, runs)
                    runs.append(name)
                    for relative in facets.facet_paths(name):
                        path = root / facets.PROJECT_BUILD / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(relative.encode())

            mocks.enter_context(patch.object(facets, 'all_targets', return_value=targets))
            mocks.enter_context(patch.object(facets, 'discover', side_effect=lambda *args: iter(checkpoints)))
            mocks.enter_context(patch.object(facets, 'save', side_effect=lambda _, __, r, ___: checkpoints.append(r)))
            mocks.enter_context(patch.object(facets, 'restore'))
            mocks.enter_context(patch.object(facets.subprocess, 'run'))
            mocks.enter_context(patch.object(facets, 'build_facets', side_effect=build))
            layout = facets.plan_facets(root, remote, compiler, base, receipts)
            self.assertEqual(len(layout['shards']), 20)
            self.assertEqual(sorted(n for shard in layout['shards'] for n in shard), names)
            fail = True
            with self.assertRaisesRegex(RuntimeError, 'interrupted shard'):
                facets.build_shard(root, remote, layout, 0, resume=False)
            self.assertEqual(len(checkpoints), 1)
            fail = False
            for shard in reversed(range(len(layout['shards']))):
                receipt = facets.build_shard(root, remote, layout, shard, resume=shard == 0)
                facets.write(receipts / f'report-{shard}.json', receipt)
                if shard == 1:
                    with self.assertRaisesRegex(ValueError, 'missing or unexpected'):
                        facets.finish(layout, receipts, root / 'plan')
            self.assertCountEqual(runs, names)
            facets.finish(layout, receipts, root / 'plan')
            indexed = facets.assemble(facets.read(root / 'plan'), None,
                                      [facets.read(p) for p in receipts.glob('*.json')])
            self.assertEqual(indexed['targets'].keys(), targets.keys())
            verified = generation.restore(remote, indexed, facets.read(root / 'plan'), [names[-1]], root / 'reader')
            self.assertEqual(verified['restored'], 2)
            report = facets.facet_paths(names[-1])[0]
            self.assertEqual((root / 'reader' / report).read_text(), report)

    def test_report_batch_prepares_its_planned_inputs_before_reading_exports(self):
        import lean_generation_facets as facets

        root = Path('checkout')
        targets = {
            'report/D5.A': {'deps': ['D5.A', 'External.Claim']},
            'report/Reg.B': {'deps': ['Reg.B', 'External.Claim', 'LeanInformationAudit.TemplateEnrollment']},
            'report/Unrelated': {'deps': ['Unrelated']},
        }
        with patch.object(facets.subprocess, 'run', autospec=True) as run:
            facets.build_facets(root, ['report/D5.A', 'report/Reg.B'], targets)
        commands = [call.args[0] for call in run.call_args_list]
        lake = ['bash', 'tools/scripts/worktree/lean-cache-run.sh', 'lake', '-d', 'tools/lean-inspector-reg', 'build']
        self.assertEqual(commands, [
            [*lake, 'leanInspector/inputDiscovery', 'leanInspector/reportInspector',
             '+D5.A:olean', '+External.Claim:olean', '+LeanInformationAudit.TemplateEnrollment:olean',
             '+Reg.B:olean'],
            [*lake, '+D5.A:report', '+Reg.B:report'],
        ])
        self.assertTrue(all(call.kwargs == {'cwd': root, 'check': True} for call in run.call_args_list))

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

    def test_compiled_empty_judge_projection_reuses_report_across_registry_change(self):
        from contextlib import ExitStack
        import tempfile
        import zipfile
        import lean_cache_generation as generation
        import lean_generation_facets as facets
        from test_lean_cache_generation import Store

        contract = 'LeanInformationAuditInterface.Contract.Catalog'
        enrollment = 'LeanInformationAudit.TemplateEnrollment'
        modules = {n: {'key': sha(n.encode()), 'deps': []} for n in
                   [contract, enrollment, 'A', 'B', *[main for _, main, _ in facets.PROGRAMS.values()]]}
        modules['A']['deps'] = modules['B']['deps'] = [contract]
        remote = Store()
        public = facets.report_publication
        with tempfile.TemporaryDirectory() as directory, ExitStack() as mocks:
            root = Path(directory)
            mocks.enter_context(patch.object(facets.report_selection, 'Selection')).return_value.modules.return_value = {
                'A': 'A.lean', 'B': 'B.lean'}
            mocks.enter_context(patch.object(facets, 'utility_inputs', return_value={}))
            mocks.enter_context(patch.object(facets, 'module_targets', return_value=modules))
            mocks.enter_context(patch.object(facets, 'save'))
            plan = {'source': 'a' * 40, 'domain': 'b' * 64, 'targets': facets.all_targets(root, modules)}
            for module in ['A', 'B']:
                label = 'report/' + module
                self.assertIn(enrollment, plan['targets'][label]['deps'])
                output = root / facets.PROJECT_BUILD / facets.facet_paths(label)[0]
                output.parent.mkdir(parents=True, exist_ok=True)
                row = {'module': module}
                report = root / public.RAW
                report.write_bytes(public.materials.canonical_json({'schema': public.materials.REPORT_SCHEMA,
                                                                     'modules': [row]}))
                inputs = [] if module == 'A' else [{'type': 'LeanInformationAudit.Contract.Registration',
                                                    'owner': module, 'name': 'B.registration'}]
                public.write_origin(report, module, {'producer_sources_sha256': 'a'*64,
                                    'inspector_executable_sha256': 'b'*64},
                                    {'schema': 'stratalint-judge-input-projection-v1', 'module': module, 'inputs': inputs})
                with zipfile.ZipFile(output, 'w') as bundle:
                    bundle.write(report, public.RAW)
                    bundle.write(public.member(report, '.provenance.json'), public.RAW + '.provenance.json')
                    bundle.writestr(public.RAW + '.materials.zip', b'')
                for sidecar in facets.facet_paths(label)[1:]:
                    (root / facets.PROJECT_BUILD / sidecar).write_bytes(b'trace')
            # Production establishes the dependency from the authenticated bundle.
            receipt = facets.stage(root, remote, plan, ['report/A', 'report/B'], root)
            self.assertNotIn(enrollment, receipt['targets']['report/A']['deps'])
            self.assertIn(enrollment, receipt['targets']['report/B']['deps'])
            modules[enrollment] = {'key': 'e' * 64, 'deps': []}
            candidate = generation.checkout_plan(root, receipt)['targets']
            self.assertEqual(candidate['report/A']['key'], receipt['targets']['report/A']['key'])
            self.assertNotEqual(candidate['report/B']['key'], receipt['targets']['report/B']['key'])
            self.assertEqual(remote.block_bytes, 0)
            # A changed module cannot inherit the original projection.
            modules['A'] = {'key': 'd' * 64, 'deps': [contract]}
            candidate = generation.checkout_plan(root, receipt)['targets']
            self.assertIn(enrollment, candidate['report/A']['deps'])
            self.assertNotEqual(candidate['report/A']['key'], receipt['targets']['report/A']['key'])
