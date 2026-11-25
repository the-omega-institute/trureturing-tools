"""Actions snapshot publication, material restoration, and rollback contracts."""
import contextlib
import importlib
import io
import json
import os
import pathlib
import shutil
import sys
import subprocess
import tarfile
import unittest
from unittest import mock

from cache_fixture import CACHE, REV, CacheFixture
from cache_publication_contract import PublicationCases


class SnapshotContracts(PublicationCases, CacheFixture, unittest.TestCase):
    def test_stream_packs_bounded_batches_and_requires_upload_acknowledgement(self):
        shards = importlib.import_module("lean_actions_shards")
        first, large, last = ("a" * 64, "b" * 64, "c" * 64)
        record = {"entries": {"first": first, "large": large, "last": last}}

        def pack(*args, deliver):
            deliver(first, 1)
            deliver(large, 2 * 1024 ** 3)
            deliver(last, 1)
            return record

        args = (self.root, {}, "complete", None, REV, (), set())
        with mock.patch.object(shards, "prepare", side_effect=pack), \
                mock.patch.object(sys, "stdin", io.StringIO("continue\n" * 3)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(record, shards.prepare_stream(*args))
        frames = [json.loads(line.removeprefix("SHARD_PAYLOAD_BATCH "))
                  for line in stdout.getvalue().splitlines() if line.startswith("SHARD_PAYLOAD_BATCH ")]
        self.assertEqual([[first], [large], [last]], frames)
        with mock.patch.object(shards, "prepare", side_effect=pack), \
                mock.patch.object(sys, "stdin", io.StringIO("")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "stopped before accepting"):
                shards.prepare_stream(*args)

    def test_release_checkpoint_artifact_survives_empty_cache_and_checks_attempt(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        producer = self.root / "producer"
        source = pathlib.Path("lib/lean/D5/Fixture.olean")
        payload_file = producer / source
        payload_file.parent.mkdir(parents=True)
        payload_file.write_bytes(b"compiler output")
        digest = shards.write_payload(self.root, producer, [source.as_posix()])
        group = [["D5.Fixture"]]
        key = shards.group_identity(group)
        identity = {"verification": {"scenario": "release-none", "run": "17"}}
        owner = {"commit": "a" * 40, "ref": "refs/heads/integration-ci-cache-tests", "run": "17", "attempt": "1"}
        record = {"binding": identity, "owner": owner, "state": "resume", "layout": {key: group},
                  "sources": {"D5.Fixture": "b" * 64}, "entries": {key: digest},
                  "fingerprints": {key: "c" * 64}, "required": {}}
        artifact = self.root / "checkpoint"
        with mock.patch.object(shards, "prepare", return_value=record), contextlib.redirect_stdout(io.StringIO()):
            shards.pack_checkpoint(self.root, identity, None, owner["commit"], artifact)
        self.assertFalse((self.root / shards.transport_payload(digest)).exists())
        previous = {**owner, "attempt": "2"}
        with mock.patch.object(shards, "current_owner", return_value=previous), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "different run or attempt"):
                shards.restore_checkpoint(self.root, identity, artifact, 2)
            shards.restore_checkpoint(self.root, identity, artifact, 1)
            build_output = self.root / ".lake/build" / source
            self.assertEqual(b"compiler output", build_output.read_bytes())
            build_output.unlink()
            new_run = {**owner, "run": "18"}
            new_identity = {"verification": {"scenario": "release-none", "run": "18"}}
            with mock.patch.object(shards, "current_owner", return_value=new_run):
                with mock.patch.object(shards, "manual_source_is_valid", return_value=False):
                    with self.assertRaisesRegex(ValueError, "different run or attempt"):
                        shards.restore_checkpoint(self.root, new_identity, artifact, 1, "17")
                with mock.patch.object(shards, "manual_source_is_valid", return_value=True):
                    shards.restore_checkpoint(self.root, new_identity, artifact, 1, "17")
                    self.assertEqual(b"compiler output", build_output.read_bytes())
                    build_output.unlink()
                    with self.assertRaisesRegex(ValueError, "different run or attempt"):
                        shards.restore_checkpoint(self.root, new_identity, artifact, 2, "17")
                    wrong_mode = {"verification": {"scenario": "release-partial", "run": "18"}}
                    with self.assertRaisesRegex(ValueError, "incompatible Actions shard index binding"):
                        shards.restore_checkpoint(self.root, wrong_mode, artifact, 1, "17")
            with mock.patch.object(shards, "current_owner", return_value={**previous, "commit": "e" * 40}):
                with self.assertRaisesRegex(ValueError, "different run or attempt"):
                    shards.restore_checkpoint(self.root, identity, artifact, 1)
            self.assertFalse(build_output.exists())
            (artifact / "payload" / (digest + ".tar.gz")).unlink()
            with self.assertRaisesRegex(ValueError, "missing verified shard material"):
                shards.restore_checkpoint(self.root, identity, artifact, 1)
            self.assertFalse(build_output.exists())

    def test_complete_discovery_survives_lake_edits_and_preserves_runtime_partitions(self):
        actions = self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(shards, "resume_spec", return_value={"identity": "checkpoint"}), \
                mock.patch.object(shards, "source_commit", return_value="a"*40):
            def addresses(state="complete"):
                return shards.index_addresses(shards.binding(self.root, state))

            original = addresses()
            resume = addresses("resume")
            exact = shards.binding(self.root, "complete")
            exact.pop("runtime")
            self.assertEqual(exact["execution"], actions.dependency_inputs(self.root))
            self.assertEqual(original["snapshot_prefix"], original["prefix"] + "snapshot-" + shards.digest(exact) + "-")
            self.assertEqual(resume["snapshot_prefix"], resume["prefix"] + "snapshot-")

            for relative in ("lakefile.toml", "lakefile.lean", "Reg/lakefile.toml", "lake-manifest.json"):
                path = self.root / relative
                path.parent.mkdir(exist_ok=True)
                with path.open("a") as file:
                    file.write("\n")
                with self.subTest(path=relative):
                    after = addresses()
                    self.assertEqual(original["prefix"], after["prefix"])
                    self.assertNotEqual(original["snapshot_prefix"], after["snapshot_prefix"])
                    self.assertNotEqual(resume["prefix"], addresses("resume")["prefix"])
                    original = after
                    resume = addresses("resume")
            for path, data in (("lean-toolchain", "other toolchain"),
                               ("lake-manifest.json", json.dumps({"packages": [{"name": "mathlib", "rev": "b"*40}]}))):
                (self.root / path).write_text(data)
                after = addresses()
                self.assertNotEqual(original["prefix"], after["prefix"])
                original = after
            with mock.patch.dict(os.environ, LEAN_OPTS="changed"):
                self.assertNotEqual(original["prefix"], addresses()["prefix"])
            current = shards.binding(self.root, "complete")
            for field in ("os", "arch"):
                self.assertNotEqual(original["prefix"], shards.index_prefix(dict(current, **{field: "other"})))

    def test_resume_separates_producer_and_verification_runs(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        env = dict(self.env, GITHUB_REF="refs/heads/integration-ci-cache-tests",
                   GITHUB_REF_PROTECTED="true", GITHUB_REPOSITORY="owner/repository")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(shards, "source_commit", return_value="a" * 40):
            ordinary = shards.binding(self.root, "resume")
            with mock.patch.dict(os.environ, {"STRATALINT_SHARD_VERIFICATION": "release-none"}):
                cold = shards.binding(self.root, "resume")
                with mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
                    self.assertEqual(cold, shards.binding(self.root, "resume"))
            with mock.patch.dict(os.environ, {"STRATALINT_SHARD_VERIFICATION": "release-partial"}):
                partial = shards.binding(self.root, "resume")
            with mock.patch.dict(os.environ, {"STRATALINT_SHARD_VERIFICATION": "release-none", "GITHUB_RUN_ID": "18"}):
                separate_run = shards.binding(self.root, "resume")
            with mock.patch.dict(os.environ, {"STRATALINT_SHARD_VERIFICATION": "scoped-eviction"}):
                scoped = shards.binding(self.root, "resume")
            self.assertEqual(5, len({shards.index_prefix(value)
                                    for value in (ordinary, cold, partial, separate_run, scoped)}))
            with mock.patch.dict(os.environ, {"STRATALINT_SHARD_VERIFICATION": "release-complete"}):
                with self.assertRaisesRegex(ValueError, "known scenario"):
                    shards.binding(self.root, "resume")

    def test_warm_repack_drops_the_legacy_aggregate_and_reuses_the_module_payload(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        inventory = importlib.import_module("lean_artifact_inventory")
        name = "D5.Fixture"
        module = inventory.Module("fixture", pathlib.Path("D5/Fixture.lean"), pathlib.Path())
        group = ((name,),)
        key = shards.group_identity(group)
        owner = {"commit": "b" * 40, "ref": "refs/heads/dev", "run": "17", "attempt": "2"}
        old_binding = dict(schema=shards.SCHEMA, os="linux", arch="arm64", mathlib="a"*40,
                           runtime="b"*64, config="c"*64, execution="d"*64)
        new_binding = dict(old_binding, config="1"*64, execution="2"*64)
        source = self.root / module.source
        source.parent.mkdir()
        source.write_text("def fixture := 1\n")
        build = self.root / shards.PROJECT_BUILD
        compiler_paths = set(map(str, inventory.required_compiler_paths(name, module)))
        module_paths = compiler_paths | {"lean-inspector/modules/D5.Fixture.zip"}
        legacy_report = "stratalint/raw-lean-report.json"
        report_paths = {legacy_report + suffix for suffix in ("", ".sha256", ".input.attestation", ".provenance.json", ".materials.zip", ".reuse.json")}
        aggregate_paths = {"lean-inspector/report.zip", "lean-inspector/report.zip.trace",
                           "lean-inspector/report.zip.hash"}
        inspector = "lean-inspector/producer/bin/reportInspector"
        aggregate_paths.add("lean-inspector/report.zip.trace.nobuild")
        extras_paths = {inspector}
        all_paths = module_paths | report_paths | aggregate_paths | extras_paths
        for relative in all_paths:
            file = build / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(relative)
        (build / inspector).chmod(0o755)
        with mock.patch.dict(os.environ, self.env | {"GITHUB_REF_PROTECTED": "true"}, clear=True), \
                mock.patch.object(shards, "workspace_modules", return_value={name: module}), \
                mock.patch.object(shards, "project_imports", return_value={name: set()}), \
                mock.patch.object(shards, "make_layout", return_value=[group]), \
                mock.patch.object(shards, "current_owner", return_value=owner), \
                mock.patch.object(shards, "resume_spec"), \
                mock.patch.object(shards, "project_publication", return_value=(True, "new-work", 1)), \
                mock.patch.object(shards, "actions_keys", return_value={"project_save_allowed": True}), \
                mock.patch.object(shards.report_selection, "Selection") as selection, \
                contextlib.redirect_stdout(io.StringIO()):
            selection.return_value.modules.return_value = {name: {}}
            index = self.root / "shards.json"
            required = {key: sorted(compiler_paths), shards.REPORT: sorted(report_paths)}
            old_groups = {key: module_paths, shards.REPORT: report_paths, shards.EXTRAS: extras_paths | aggregate_paths}
            previous = {
                "binding": old_binding, "state": "complete", "owner": owner, "layout": {key: group}, "required": required,
                "sources": shards.tracked_sources(self.root, {name: module}),
                "entries": {group_key: shards.write_payload(self.root, build, paths)
                            for group_key, paths in old_groups.items()},
                "fingerprints": {
                    group_key: shards.digest([(path, (build / path).stat().st_mode & 0o777,
                                               shards.archive_sha(build / path, None)) for path in sorted(paths)])
                    for group_key, paths in old_groups.items()
                },
            }
            legacy = {field: value for field, value in old_binding.items() if field != "runtime"}
            index.write_text(json.dumps(dict(previous, binding=legacy)))
            self.assertEqual(legacy, shards.read_index(index, old_binding, "complete")["binding"])
            with self.assertRaises(shards.IncompatibleShardBinding):
                shards.read_index(index, new_binding, "complete", layout_candidate=True)
            index.write_text(json.dumps(previous))
            with self.assertRaises(shards.IncompatibleShardBinding):
                shards.read_index(index, new_binding, "complete")
            for field in ("schema", "mathlib", "os", "arch", "runtime"):
                with self.subTest(field=field), self.assertRaises(shards.IncompatibleShardBinding):
                    shards.read_index(index, dict(new_binding, **{field: "other"}), "complete", layout_candidate=True)
            previous = shards.read_index(index, new_binding, "complete", layout_candidate=True)
            shutil.rmtree(build)
            self.assertTrue(shards.install(self.root, previous, require_complete=True)["complete"])
            self.assertEqual(module_paths | extras_paths, {path.relative_to(build).as_posix()
                                         for path in build.rglob("*") if path.is_file()})

            packed = []
            record = shards.prepare(self.root, new_binding, "complete", previous, owner["commit"],
                                    available=set(previous["entries"].values()),
                                    deliver=lambda payload, size: packed.append(payload))
            index.write_text(json.dumps(record))
            record = shards.read_index(index, new_binding, "complete")
            self.assertEqual({key: sorted(module_paths)}, record["required"])
            self.assertEqual(previous["layout"], record["layout"])
            self.assertEqual(previous["entries"][key], record["entries"][key])
            self.assertEqual(previous["fingerprints"][key], record["fingerprints"][key])
            self.assertEqual(packed, [record["entries"][shards.EXTRAS]])
            self.assertNotIn(shards.REPORT, record["entries"])

            expected_groups = {key: module_paths, shards.EXTRAS: extras_paths}
            for group_key, paths in expected_groups.items():
                payload = self.root / shards.transport_payload(record["entries"][group_key])
                with tarfile.open(payload, "r:gz") as archive:
                    self.assertCountEqual(paths, archive.getnames())

            scoped = {key: record["entries"][key], shards.EXTRAS: record["entries"][shards.EXTRAS]}
            self.assertEqual(scoped, shards.selected_keys(self.root, record, [name]))
            for targets, expected in (([name], module_paths | extras_paths), (None, module_paths | extras_paths)):
                with self.subTest(targets=targets):
                    shutil.rmtree(build)
                    restored = shards.install(self.root, record, targets, require_complete=True)
                    count = len(record["entries"] if targets is None else scoped)
                    self.assertEqual({"available": count, "missing": [], "requested": count,
                                      "complete": targets is None}, restored)
                    self.assertEqual(expected, {path.relative_to(build).as_posix()
                                                for path in build.rglob("*") if path.is_file()})
                    self.assertEqual(0o755, (build / inspector).stat().st_mode & 0o777)

            resume = shards.prepare(self.root, {}, "resume", None, owner["commit"])
            index.write_text(json.dumps(resume))
            self.assertEqual({}, shards.read_index(index, {}, "resume")["required"])

    def test_zero_work_index_bootstrap_requires_a_validated_source(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        env = self.env | {"GITHUB_REF_PROTECTED": "true"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(shards, "project_publication", return_value=(False, "no-build-work", 0)), \
                mock.patch.object(shards, "actions_keys", return_value={"project_save_allowed": True}), \
                mock.patch.object(shards, "manual_source_is_valid", return_value=False), \
                mock.patch.object(shards, "resume_verified", return_value=False), \
                mock.patch.object(shards, "check_native_products", side_effect=ValueError("invalid native products")):
            with self.assertRaisesRegex(ValueError, "publication denied: no-build-work"):
                shards.prepare(self.root, {}, "complete", None, "a" * 40)
            with mock.patch.object(shards, "manual_source_is_valid", return_value=True):
                with self.assertRaisesRegex(ValueError, "invalid native products"):
                    shards.prepare(self.root, {}, "complete", None, "a" * 40)

    def test_sharded_resume_recovers_verified_entries_and_reports_eviction(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        root = self.root
        producer = root / "producer"
        names = ["lib/lean/D5/First.olean", "lib/lean/D5/Second.olean"]
        payloads = []
        for name in names:
            compiled = producer / name
            compiled.parent.mkdir(parents=True, exist_ok=True)
            compiled.write_text(name)
            payloads.append(shards.write_payload(root, producer, [name]))
        groups = [[["D5.First"]], [["D5.Second"]]]
        keys = [shards.group_identity(group) for group in groups]
        identity = {"producer_resume": "isolated"}
        record = {
            "binding": identity, "state": "resume", "required": {},
            "owner": {"commit": "b" * 40, "ref": "refs/heads/dev", "run": "17", "attempt": "2"},
            "layout": dict(zip(keys, groups, strict=True)),
            "entries": dict(zip(keys, payloads, strict=True)),
            "fingerprints": {key: "c" * 64 for key in keys},
            "sources": {name: "d" * 64 for name in ("D5.First", "D5.Second")},
        }
        index = root / "shard-index.json"
        index.write_text(json.dumps(record))
        decoded = shards.read_index(index, identity, "resume")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            shards.read_index(index, identity, "complete")
        (root / shards.transport_payload(payloads[1])).unlink()
        build = root / ".lake/build"
        (build / names[0]).parent.mkdir(parents=True, exist_ok=True)
        (build / names[0]).write_text("earlier first module")
        (build / names[1]).write_text("earlier second module")
        with mock.patch.object(shards, "install_scoped", side_effect=OSError("install interrupted")):
            with self.assertRaisesRegex(OSError, "install interrupted"):
                shards.install(root, decoded)
        self.assertEqual("earlier first module", (build / names[0]).read_text())
        self.assertEqual("earlier second module", (build / names[1]).read_text())

        result = shards.install(root, decoded)
        self.assertEqual(1, result["available"])
        self.assertEqual([payloads[1]], result["missing"])
        self.assertEqual(names[0], (build / names[0]).read_text())
        self.assertEqual("earlier second module", (build / names[1]).read_text())

    def test_complete_shard_index_requires_every_declared_compiler_group(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        group = [["D5.Fixture"]]
        key = shards.group_identity(group)
        record = {
            "binding": {}, "state": "complete",
            "owner": {"commit": "b" * 40, "ref": "refs/heads/dev", "run": "17", "attempt": "2"},
            "layout": {key: group},
            "entries": {key: "a" * 64, "report": "b" * 64},
            "fingerprints": {key: "c" * 64, "report": "d" * 64},
            "sources": {"D5.Fixture": "e" * 64},
            "required": {key: ["lib/lean/D5/Fixture.olean"], "report": ["stratalint/raw-lean-report.json"]},
        }
        index = self.root / "shards.json"
        index.write_text(json.dumps(record))
        self.assertEqual(record, shards.read_index(index, {}, "complete"))
        del record["entries"][key]
        del record["fingerprints"][key]
        index.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "required Actions shard inventory"):
            shards.read_index(index, {}, "complete")

    def test_parallel_shard_install_rejects_cross_payload_collision_before_publication(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        relative = "lib/lean/D5/Fixture.olean"
        source = self.root / "producer" / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"first")
        first = shards.write_payload(self.root, self.root / "producer", [relative])
        source.write_bytes(b"second")
        second = shards.write_payload(self.root, self.root / "producer", [relative])
        target = self.root / ".lake/build"
        target.mkdir(parents=True)
        record = {"state": "complete", "required": {}, "entries": {"first": first, "second": second}}
        with self.assertRaises(FileExistsError):
            shards.install(self.root, record, require_complete=True)
        self.assertEqual([], list(target.iterdir()))
        self.assertEqual([], list(target.parent.glob(".shards-*")))

    def test_complete_index_installs_available_shards_without_claiming_complete(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        producer = self.root / "producer"
        build = self.root / ".lake/build"
        group = shards.group_identity([["D5.Fixture"]])
        paths = {group: "lib/lean/D5/Fixture.olean", shards.EXTRAS: "lean-inspector/modules/D5.Fixture.zip"}
        for path in paths.values():
            source = producer / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(path)
        compiler = shards.write_payload(self.root, producer, [paths[group]])
        report = shards.write_payload(self.root, producer, [paths[shards.EXTRAS]])
        record = {"state": "complete", "entries": {group: compiler, shards.EXTRAS: report},
                  "required": {key: [path] for key, path in paths.items()}}
        report_file = self.root / shards.transport_payload(report)
        report_bytes = report_file.read_bytes()
        report_file.unlink()

        strict = shards.install(self.root, record, require_complete=True)
        self.assertEqual({"available": 0, "missing": [report], "requested": 2, "complete": False}, strict)
        self.assertFalse(build.exists())
        partial = shards.install(self.root, record)
        self.assertEqual(strict | {"available": 1}, partial)
        self.assertEqual(paths[group], (build / paths[group]).read_text())
        self.assertFalse((build / paths[shards.EXTRAS]).exists())
        shutil.rmtree(build)

        report_file.write_bytes(report_bytes)
        complete = shards.install(self.root, record)
        self.assertEqual({"available": 2, "missing": [], "requested": 2, "complete": True}, complete)
        self.assertEqual(paths[shards.EXTRAS], (build / paths[shards.EXTRAS]).read_text())

    def test_restore_plan_accepts_large_and_empty_target_files(self):
        self.restore_owner()
        shards = importlib.import_module("lean_actions_shards")
        targets = [f"D5.ModuleWithALongEnoughNameForTheArgumentLimit.Number{i}" for i in range(5000)]
        base, unrelated = "D5.Base", "D5.Unrelated"
        graph = {name: set() for name in [*targets, base, unrelated]}
        graph[targets[-1]].add(base)
        groups = {shards.group_identity([[name]]): [[name]] for name in [targets[-1], base, unrelated]}
        entries = {key: str(index) * 64 for index, key in enumerate([*groups, shards.REPORT, shards.EXTRAS])}
        record = {"entries": entries, "layout": groups}
        index_path = self.root / "index.json"
        targets_file = self.root / "target file"
        argv = ["lean_actions_shards.py", "restore-plan", "--repository", str(self.root),
                "--index", str(index_path), "--targets-file", str(targets_file)]

        for scope in (targets, []):
            with self.subTest(count=len(scope)):
                targets_file.write_text("\n".join(scope), encoding="utf-8")
                if scope:
                    self.assertGreater(targets_file.stat().st_size, 256 * 1024)
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(shards, "binding"), \
                        mock.patch.object(shards, "read_index", return_value=record), \
                        mock.patch.object(shards, "workspace_modules", return_value=graph), \
                        mock.patch.object(shards, "project_imports", return_value=graph), \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    shards.main()
                expected = {shards.group_identity([[name]]) for name in [targets[-1], base]} | {shards.EXTRAS}
                self.assertEqual(expected if scope else set(), set(json.loads(stdout.getvalue())))
        self.assertEqual(entries.keys() - {shards.REPORT}, shards.selected_keys(self.root, record).keys())
        self.assertEqual({"available": 0, "missing": [], "requested": 0, "complete": False},
                         shards.install(self.root, record, []))
        self.assertFalse((self.root / ".lake/build").exists())

    def test_dependency_restore_receipt_shape_excludes_project_requested_and_miss_keys(self):
        for outcome in ("success", "skipped", "failure"):
            with self.subTest(outcome=outcome):
                owner, keys, _ = self.dependency_seed()
                key = keys["dependency"]["key"]
                with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as result:
                    owner.restore_dependency(self.root, keys, key, outcome)
                entries = [json.loads(line.removeprefix("LEAN_ACTIONS_CACHE "))
                           for line in result.getvalue().splitlines() if line.startswith("LEAN_ACTIONS_CACHE ")]
                self.assertEqual(1, len(entries), result.getvalue())
                entry = entries[0]
                if outcome == "success":
                    self.assertEqual(dict(layer="dependency", status="restored", key=key,
                                          partition=keys["partition"], transport="actions-native"), entry)
                else:
                    reason = ("Actions supplied no cache" if outcome == "skipped" else
                              "Actions restore did not succeed or supplied no matched cache")
                    self.assertEqual(dict(layer="dependency", status="miss", reason=reason), entry)

    def work(self, report=1, programs=0, **changes):
        path = self.root / "build/lean-cache/build-work.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(schema_version=1, run_id="17", run_attempt="2",
                     repository=str(self.root.resolve()), report=report, programs=programs)
        value.update(changes)
        path.write_text(json.dumps(value))
        self.env["STRATALINT_LEAN_BUILD_WORK_FILE"] = str(path)
        return path

    def dependency_seed(self):
        self.work()
        owner = self.restore_owner()
        owner.dependency_restored_record(self.root).unlink(missing_ok=True)
        with mock.patch.dict(os.environ, self.env):
            keys = owner.actions_keys(self.root)
            target = self.root / keys["dependency"]["path"]
            target.mkdir(parents=True, exist_ok=True)
            (target / "module.olean").write_bytes(b"native build material")
            with contextlib.redirect_stdout(io.StringIO()) as result:
                owner.snapshot_dependency(self.root, keys)
            self.assertIn("dependency_ready=true", result.getvalue())
        return owner, keys, target

    def test_native_restore_requires_success_and_the_selected_partition(self):
        for outcome, kind, accepted in (("success", "matching", True), ("failure", "matching", False),
                ("cancelled", "matching", False), ("success", "foreign", False), ("success", "old", False),
                ("success", "missing", False)):
            with self.subTest(outcome=outcome, key=kind):
                owner, keys, target = self.dependency_seed()
                key = keys["dependency"]["key"]
                if kind == "foreign": key = key.replace(REV, "b" * 40)
                if kind == "old": key = key.replace("-v4-", "-v3-")
                if kind == "missing": key = ""
                sibling = self.root / ".lake/build"
                sibling.mkdir(exist_ok=True)
                (sibling / "keep").write_bytes(b"other material")
                with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(accepted, owner.restore_dependency(self.root, keys, key, outcome))
                self.assertEqual(accepted, target.exists())
                self.assertEqual(b"other material", (sibling / "keep").read_bytes())

    def test_actions_retires_whole_project_transfer(self):
        for command in ("keys", "restore", "snapshot"):
            with self.subTest(command=command):
                result = self.run_tool(CACHE, command, "--layers", "project")
                self.assertEqual(2, result.returncode)
                self.assertIn("invalid choice", result.stderr)
        default = self.run_tool(CACHE, "keys")
        dependency = self.run_tool(CACHE, "keys", "--layers", "dependency")
        self.assertEqual(0, default.returncode, default.stderr)
        self.assertEqual(0, dependency.returncode, dependency.stderr)
        self.assertEqual(default.stdout, dependency.stdout)
        self.assertNotIn("project_", default.stdout)

    def test_dependency_retention_uses_only_registered_small_inputs_after_successful_restore(self):
        owner, keys, target = self.dependency_seed()
        original_key = keys["dependency"]["key"]
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
            owner.restore_dependency(self.root, keys, original_key, "success")
        (self.root / "unrelated.lean").write_text("theorem changed := True.intro\n")
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(pathlib.Path, "rglob", side_effect=AssertionError("native directory scan")), \
                contextlib.redirect_stdout(io.StringIO()) as result:
            owner.snapshot_dependency(self.root, keys)
        self.assertIn("retained-restored-dependency", result.getvalue())
        self.assertIn("dependency_ready=false", result.getvalue())
        for file in ("lake-manifest.json", "lean-toolchain", "lakefile.toml"):
            path = self.root / file
            before = path.read_bytes()
            path.write_bytes(before + b"\n")
            with self.subTest(file=file), mock.patch.dict(os.environ, self.env), \
                    contextlib.redirect_stdout(io.StringIO()) as result:
                self.assertEqual(original_key, owner.actions_keys(self.root)["dependency"]["key"])
                owner.snapshot_dependency(self.root, keys)
            self.assertIn("dependency_ready=true", result.getvalue())
            path.write_bytes(before)
        with mock.patch.dict(os.environ, dict(self.env, LEAN_OPTS="-DmaxRecDepth=1024")), \
                contextlib.redirect_stdout(io.StringIO()) as result:
            owner.snapshot_dependency(self.root, keys)
        self.assertIn("dependency_ready=true", result.getvalue())

    def test_dependency_retention_requires_this_round_and_an_accepted_small_receipt(self):
        owner, keys, target = self.dependency_seed()
        marker = target / ".stratalint-actions-inputs.json"
        original = marker.read_bytes()
        for defect in ("missing", "invalid", "wrong-type", "stale-round"):
            with self.subTest(defect=defect), mock.patch.dict(os.environ, self.env):
                marker.write_bytes(original)
                if defect == "missing": marker.unlink()
                elif defect == "invalid": marker.write_text("broken")
                elif defect == "wrong-type": marker.write_text("[]")
                with contextlib.redirect_stdout(io.StringIO()) as restored:
                    owner.restore_dependency(self.root, keys, keys["dependency"]["key"], "success")
                self.assertIn('"status": "restored"', restored.getvalue())
                if defect == "stale-round":
                    changed = dict(keys, dependency=dict(keys["dependency"], key=keys["dependency"]["key"] + "0"))
                else: changed = keys
                with contextlib.redirect_stdout(io.StringIO()) as saved:
                    owner.snapshot_dependency(self.root, changed)
                self.assertIn("dependency_ready=true", saved.getvalue())

    def test_native_metadata_save_failure_keeps_produced_material(self):
        owner, keys, target = self.dependency_seed()
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(owner, "write_small_record", side_effect=OSError("save unavailable")), \
                contextlib.redirect_stdout(io.StringIO()) as result:
            owner.snapshot_dependency(self.root, keys)
        self.assertIn("dependency_ready=false", result.getvalue())
        self.assertEqual(b"native build material", (target / "module.olean").read_bytes())

    def test_elan_inventory_lists_installed_toolchains_without_modifying_them(self):
        home = self.root / "elan cache with spaces"
        binary = home / "bin/elan"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/bash\n[[ \"$*\" == 'toolchain list' ]] || exit 23\n"
                          "[[ \"${ELAN_HOME:-}\" == \"$EXPECTED_INVENTORY_HOME\" ]] || exit 24\n"
                          "printf '%s\\n' 'leanprover/lean4:v4.33.0 (default)' 'leanprover/lean4:v4.31.0'\n")
        binary.chmod(0o755)
        toolchains = home / "toolchains/stale"
        toolchains.mkdir(parents=True)
        material = toolchains / "keep"
        material.write_bytes(b"installed toolchain")
        result = subprocess.run(["bash", "--noprofile", "--norc",
            str(CACHE.parents[1] / "workflow/elan-cache-inventory.sh"), str(home)],
            cwd=self.root, env=dict(self.env, EXPECTED_INVENTORY_HOME=str(home)), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("ELAN_CACHE_TOOLCHAINS", result.stdout)
        self.assertIn("leanprover/lean4:v4.33.0 (default)", result.stdout)
        self.assertIn("leanprover/lean4:v4.31.0", result.stdout)
        self.assertEqual(b"installed toolchain", material.read_bytes())

    def resume_setup(self):
        owner = self.restore_owner()
        self.env.update(GITHUB_REPOSITORY="owner/repo", GITHUB_REF_PROTECTED="true")
        for args in (() if (self.root / ".git").exists() else (("init", "-q"), ("add", "."),
                ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source"))):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / ".lake/packages/mathlib").mkdir(parents=True)
        (self.root / ".lake/build/lib/lean").mkdir(parents=True)
        (self.root / ".lake/build/lib/lean/Module.olean").write_bytes(b"completed module")
        return owner

    def manual_resume_setup(self):
        owner = self.resume_setup()
        self.env.update(GITHUB_EVENT_NAME="workflow_dispatch", STRATALINT_CACHE_SOURCE_VALIDATED="true",
                        STRATALINT_CACHE_SOURCE_REF="refs/heads/dev", STRATALINT_CACHE_SOURCE_COMMIT="b" * 40,
                        STRATALINT_CACHE_CHECKED_OUT_SHA="b" * 40)
        return owner

    def test_producer_resume_is_separate_and_requires_the_same_ref_and_inputs(self):
        owner = self.resume_setup()
        with mock.patch.dict(os.environ, self.env, clear=True):
            spec = owner.resume_spec(self.root)
            self.assertTrue(spec["restore_prefix"].startswith("lean-producer-resume-v1-"))
            for field, value in (("GITHUB_REF", "refs/heads/integration-other"),
                    ("GITHUB_REPOSITORY", "other/repo"), ("LEAN_OPTS", "-DmaxRecDepth=1000")):
                with mock.patch.dict(os.environ, {field: value}):
                    self.assertNotEqual(spec["restore_prefix"], owner.resume_spec(self.root)["restore_prefix"])
            for file in ("lean-toolchain", "lake-manifest.json"):
                path = self.root / file
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                self.assertNotEqual(spec["restore_prefix"], owner.resume_spec(self.root)["restore_prefix"])
                path.write_bytes(before)
            for relative, text in (("source.lean", "def changed := 1\n"),
                    ("tools/producer.py", "raise SystemExit(1)\n"), ("README.md", "documentation\n")):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                subprocess.run(["git", "-C", str(self.root), "add", relative], check=True)
                subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "changed"], check=True, capture_output=True)
                self.assertEqual(spec["restore_prefix"], owner.resume_spec(self.root)["restore_prefix"])

    def test_release_rebuild_continuation_is_private_to_the_pinned_dispatch(self):
        owner = self.manual_resume_setup()
        with mock.patch.dict(os.environ, self.env, clear=True):
            ordinary = owner.resume_spec(self.root)
            pins = {"STRATALINT_RELEASE_SEED_TAG": "lean-cache-v3-fixture",
                    "STRATALINT_RELEASE_SEED_PRODUCER": "a" * 40}
            with mock.patch.dict(os.environ, pins):
                first = owner.resume_spec(self.root)
                self.assertNotEqual(ordinary["identity"], first["identity"])
                with mock.patch.dict(os.environ, GITHUB_RUN_ATTEMPT="3"):
                    retry = owner.resume_spec(self.root)
                    self.assertTrue(owner.resume_key_matches(retry, first["key"]))
                    self.assertNotEqual(first["key"], retry["key"])
                for override in ({"GITHUB_RUN_ID": "18"}, {"STRATALINT_RELEASE_SEED_TAG": "lean-cache-v2-other"},
                                 {"STRATALINT_RELEASE_SEED_PRODUCER": "c" * 40},
                                 {"STRATALINT_CACHE_SOURCE_COMMIT": "d" * 40,
                                  "STRATALINT_CACHE_CHECKED_OUT_SHA": "d" * 40}):
                    with mock.patch.dict(os.environ, override):
                        self.assertFalse(owner.resume_key_matches(owner.resume_spec(self.root), first["key"]))
                with mock.patch.dict(os.environ, STRATALINT_RELEASE_SEED_PRODUCER=""):
                    with self.assertRaisesRegex(ValueError, "Release continuation"):
                        owner.resume_spec(self.root)

    def test_resume_paths_include_registered_dependency_and_project_material(self):
        owner = self.resume_setup()
        with mock.patch.dict(os.environ, self.env, clear=True):
            paths = owner.resume_spec(self.root)["paths"]
        self.assertIn(".lake/packages", paths)
        self.assertIn(".lake/build/lib", paths)
        self.assertIn(".lake/build/lean-inspector", paths)

    def test_resume_writer_rejects_unprotected_pushes_and_ordinary_prs(self):
        owner = self.resume_setup()
        for defect in ({"GITHUB_REF_PROTECTED": "false"}, {"GITHUB_REF": "refs/heads/topic"},
                {"STRATALINT_CACHE_WRITES": "false"}, {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/1/merge"},
                {"GITHUB_EVENT_NAME": "workflow_dispatch", "STRATALINT_CACHE_SOURCE_VALIDATED": "false"}):
            with self.subTest(defect=defect), mock.patch.dict(os.environ, dict(self.env, **defect), clear=True):
                with self.assertRaises(ValueError): owner.resume_spec(self.root)
        env = dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", STRATALINT_CACHE_SOURCE_VALIDATED="true",
                   STRATALINT_CACHE_SOURCE_REF="refs/heads/dev", STRATALINT_CACHE_SOURCE_COMMIT="b" * 40,
                   STRATALINT_CACHE_CHECKED_OUT_SHA="b" * 40)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(owner.resume_spec(self.root)["key"].endswith("17-2"))

    def test_resume_restore_preserves_partial_native_work_without_certifying_a_report(self):
        owner = self.resume_setup()
        env = dict(self.env, STRATALINT_REPORT_SUCCEEDED="false")
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as receipts:
            spec = owner.resume_spec(self.root)
            owner.resume_snapshot(self.root, spec)
            owner.resume_restore(self.root, spec, spec["key"], "success")
            self.assertTrue(owner.resume_verified(self.root))
            self.assertFalse(owner.actions_keys(self.root)["project_save_allowed"])
        self.assertIn("resume_ready=true", receipts.getvalue())
        self.assertNotIn("STRATALINT_REPORT_SUCCEEDED=true", receipts.getvalue())
        self.assertTrue((self.root / ".lake/.stratalint-lean-cache-stamp.json").is_file())
        self.assertEqual(b"completed module", (self.root / ".lake/build/lib/lean/Module.olean").read_bytes())

    def test_incompatible_or_failed_resume_restore_discards_material_and_cannot_promote(self):
        for defect in ("foreign-key", "foreign-ref", "missing-marker", "transport-failed", "duplicate-marker"):
            with self.subTest(defect=defect):
                owner = self.resume_setup()
                with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                    spec = owner.resume_spec(self.root)
                    owner.resume_snapshot(self.root, spec)
                    key, outcome = spec["key"], "success"
                    marker = owner.resume_marker(self.root)
                    if defect == "foreign-key": key = "foreign-resume-key"
                    elif defect == "foreign-ref": os.environ["GITHUB_REF"] = "refs/heads/integration-other"
                    elif defect == "missing-marker": marker.unlink()
                    elif defect == "transport-failed": outcome = "failure"
                    elif defect == "duplicate-marker": marker.write_text('{"key":"a","key":"b"}')
                    owner.resume_restore(self.root, owner.resume_spec(self.root), key, outcome)
                    self.assertFalse(owner.resume_verified(self.root))
                self.assertFalse((self.root / ".lake/build").exists())
                self.assertFalse((self.root / ".lake/packages").exists())

    def test_empty_matched_key_after_resume_attempt_discards_extracted_material(self):
        owner = self.manual_resume_setup()
        self.work(0, 0)
        env = dict(self.env, STRATALINT_CACHE_PRODUCER_COMPLETE="true")
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as receipts:
            spec = owner.resume_spec(self.root)
            owner.resume_snapshot(self.root, spec)
            owner.resume_restore(self.root, spec, spec["key"], "success")
            self.assertTrue(owner.resume_verified(self.root))
            (self.root / ".lake/packages/mathlib/partial").write_bytes(b"incomplete extraction")
            (self.root / ".lake/build/lib/lean/Module.olean").write_bytes(b"truncated")
            owner.resume_restore(self.root, spec, "", "success")
            self.assertFalse(owner.resume_verified(self.root))
            self.assertEqual((False, "no-build-work", 0), owner.project_publication(self.root))
        self.assertFalse((self.root / ".lake/packages").exists())
        self.assertFalse((self.root / ".lake/build").exists())
        self.assertIn("resume transport failed or identity is incompatible", receipts.getvalue())

    def test_no_resume_extraction_preserves_valid_dependency_seed_without_promoting(self):
        owner = self.manual_resume_setup()
        self.work(0, 0)
        dependency = self.root / ".lake/packages/mathlib/keep"
        dependency.write_bytes(b"valid dependency seed")
        env = dict(self.env, STRATALINT_CACHE_PRODUCER_COMPLETE="true")
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as receipts:
            keys = owner.actions_keys(self.root)
            self.assertTrue(owner.restore_dependency(self.root, keys, keys["dependency"]["key"], "success"))
            stamp = self.root / ".lake/.stratalint-lean-cache-stamp.json"
            stamp_bytes = stamp.read_bytes()
            spec = owner.resume_spec(self.root)
            for outcome in ("", "skipped"):
                with self.subTest(outcome=outcome):
                    owner.resume_snapshot(self.root, spec)
                    owner.resume_restore(self.root, spec, spec["key"], "success")
                    self.assertTrue(owner.resume_verified(self.root))
                    owner.resume_restore(self.root, spec, "", outcome)
                    self.assertFalse(owner.resume_verified(self.root))
                    self.assertEqual((False, "no-build-work", 0), owner.project_publication(self.root))
                    self.assertEqual(b"valid dependency seed", dependency.read_bytes())
                    self.assertEqual(stamp_bytes, stamp.read_bytes())
                    self.assertEqual(b"completed module", (self.root / ".lake/build/lib/lean/Module.olean").read_bytes())
        self.assertEqual(2, receipts.getvalue().count('"reason": "Actions supplied no continuation"'))

    def test_resume_cannot_be_consumed_as_a_dependency_seed(self):
        owner = self.resume_setup()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            spec = owner.resume_spec(self.root)
            self.assertFalse(owner.restore_dependency(self.root, owner.actions_keys(self.root), spec["key"], "success"))

    def test_manual_final_slice_can_promote_verified_resume_with_zero_new_work(self):
        owner = self.manual_resume_setup()
        self.work(0, 0)
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()) as receipts:
            os.environ["STRATALINT_CACHE_PRODUCER_COMPLETE"] = "true"
            self.assertEqual((False, "no-build-work", 0), owner.project_publication(self.root))
            del os.environ["STRATALINT_CACHE_PRODUCER_COMPLETE"]
            spec = owner.resume_spec(self.root)
            owner.resume_snapshot(self.root, spec)
            owner.resume_restore(self.root, spec, spec["key"], "success")
            self.assertEqual((False, "no-build-work", 0), owner.project_publication(self.root))
            os.environ["STRATALINT_CACHE_PRODUCER_COMPLETE"] = "true"
            self.assertEqual((True, "validated-producer-resume", 0), owner.project_publication(self.root))
            os.environ["STRATALINT_REPORT_SUCCEEDED"] = "false"
            self.assertFalse(owner.actions_keys(self.root)["project_save_allowed"])
            os.environ["GITHUB_RUN_ATTEMPT"] = "3"
            self.assertFalse(owner.resume_verified(self.root))

    def test_resume_cli_selects_saves_and_restores_native_continuation(self):
        self.resume_setup()
        result = self.run_tool(CACHE, "resume-keys")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertIn("resume_restore_prefix", values)
        self.assertIn(".lake/packages", json.loads(values["resume_archive_path"]))
        result = self.run_tool(CACHE, "resume-snapshot")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("resume_ready=true", result.stdout)
        result = self.run_tool(CACHE, "resume-restore", "--resume-key", values["resume_key"], "--resume-outcome", "success")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('"status": "restored"', result.stdout)
        self.assertFalse((self.root / "environment").exists())
        result = self.run_tool(CACHE, "resume-keys", env=dict(self.env, STRATALINT_CACHE_WRITES="false"))
        self.assertEqual(2, result.returncode)
        self.assertIn("authorized protected branch", result.stderr)

    def preparation_resume_environment(self):
        return dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/integration-upgrade", GITHUB_REF_PROTECTED="true",
            STRATALINT_CACHE_PR_SOURCE_VALIDATED="true", STRATALINT_CACHE_SOURCE_REF="refs/pull/21/merge",
            STRATALINT_CACHE_TARGET_REF="refs/heads/integration-upgrade",
            STRATALINT_CACHE_SOURCE_COMMIT="b" * 40, STRATALINT_CACHE_CHECKED_OUT_SHA="b" * 40)

    def test_pr_preparation_cannot_write_any_cache_even_on_protected_runtime(self):
        owner = self.resume_setup()
        with mock.patch.dict(os.environ, self.preparation_resume_environment(), clear=True):
            keys = owner.actions_keys(self.root)
            for key in ("project_save_allowed", "save_allowed", "producer_save_allowed"):
                self.assertFalse(keys[key])
            with self.assertRaisesRegex(ValueError, "authorized protected branch"):
                owner.resume_spec(self.root)

    def test_ordinary_pr_cannot_become_a_producer_by_setting_the_legacy_marker(self):
        owner = self.resume_setup()
        env = dict(self.env, GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/21/merge",
            STRATALINT_CACHE_PR_PRODUCER="true", STRATALINT_CACHE_PRODUCER_COMPLETE="true")
        self.work(1, 1)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                owner.resume_spec(self.root)
            self.assertFalse(owner.actions_keys(self.root)["project_save_allowed"])
            self.assertEqual((False, "pull-requests-do-not-publish-project-seeds", None), owner.project_publication(self.root))

    def restore_owner(self):
        sys.path.insert(0, str(CACHE.parent))
        return importlib.import_module("lean_actions")


if __name__ == "__main__":
    unittest.main()
