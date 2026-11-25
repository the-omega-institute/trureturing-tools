import copy
import io
import json
from pathlib import Path
import tarfile
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lean_cache_generation as generation
import lean_generation_checkpoints as checkpoints
from lean_generation_objects import BUCKET, CAPACITY, Objects
from lean_generation_remote import GitHub, INDEX_PREFIX, InvalidIndexBody, MissingCheckpoint
from lean_generation_index import encode, read, sha, validate


class Store:
    deadline = None

    def __init__(self):
        self.revision = ""
        self.marker = None
        self.history = {}
        self.indexes = {"ci": {}, "release": {}}
        self.volumes = {}
        self.block_bytes = 0
        self.range_requests = 0
        self.fail = None

    def ensure_volume(self, digest, size, path):
        assert path.stat().st_size == size
        if digest in self.volumes:
            return False
        data = path.read_bytes()
        assert sha(data) == digest
        self.volumes[digest] = data
        return True

    def confirmed_volumes(self, sizes):
        return {v: n for v, n in sizes.items() if len(self.volumes[v]) == n}

    def volume(self, digest, size):
        assert len(self.volumes[digest]) == size
        return digest

    def blocks(self, asset, start, size):
        self.range_requests += 1
        self.block_bytes += size
        yield self.volumes[asset][start:start + size]

    def current(self):
        return self.revision, self.marker

    def stage_indexes(self, digest, path, *, checkpoint=False):
        if checkpoint:
            self.indexes.setdefault("checkpoint", {})[digest] = path.read_bytes()
            return
        for transport in ("ci", "release"):
            if self.fail == transport:
                raise ConnectionError(transport)
            self.indexes[transport][digest] = path.read_bytes()

    def get_index(self, digest, path, transport):
        path.write_bytes(self.indexes[transport][digest])
        assert sha(path.read_bytes()) == digest
        return read(path)

    def published(self, digest):
        if digest not in self.history:
            raise ValueError("version is outside published generation history")
        return self.history[digest]

    def advance(self, expected, marker):
        assert self.revision == expected
        self.marker = marker
        self.history[marker["index"]] = marker
        self.revision = sha(encode(marker))[:40]
        return self.revision


class Generations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.remote = Store()
        self.plan = {"source": "a"*40, "targets": {
            "A": {"key": sha(b"A"), "deps": []},
            "B": {"key": sha(b"B"), "deps": ["A"]}}}
        self.receipt, self.stats = self.frames(self.plan, {"A": b"library", "B": b"consumer"})

    def frames(self, plan, values):
        metadata = {}
        for name, value in values.items():
            path = "lib/lean/" + name + ".olean"
            frame = self.directory / "frame"
            with tarfile.open(frame, "w:gz") as archive:
                header = tarfile.TarInfo(path)
                header.size = len(value)
                archive.addfile(header, io.BytesIO(value))
            digest = sha(frame.read_bytes())
            frame.rename(self.directory / (digest + ".tar.gz"))
            metadata[name] = {"paths": [path], "sha256": digest}
        (self.directory / "frames.json").write_bytes(encode(metadata))
        return generation.stage_frames(self.remote, plan, self.directory, 4_096)

    def commit(self, plan, base, receipts):
        result = generation.publish(self.remote, plan, base, receipts, self.remote.revision)
        self.assertEqual(result["ci_index_sha256"], result["release_index_sha256"])
        return read_from(self.remote, self.directory, "release")

    def test_two_readers_and_inherited_volumes(self):
        index = self.commit(self.plan, None, [self.receipt])
        self.assertEqual(self.stats["new_volumes"], 1)
        payload = {}
        for transport in ("release", "ci"):
            current = read_from(self.remote, self.directory, transport)
            directory = self.directory / transport
            result = generation.restore(self.remote, current, self.plan, ["B"], directory)
            self.assertEqual(result["restored"], 2)
            self.assertEqual(current["targets"]["B"]["key"], self.plan["targets"]["B"]["key"])
            payload[transport] = sha((directory / "lib/lean/B.olean").read_bytes())
        self.assertEqual(self.remote.range_requests, 4)
        self.assertEqual(payload, {"ci": sha(b"consumer"), "release": sha(b"consumer")})
        body_bytes = self.remote.block_bytes
        second_plan = {**self.plan, "source": "b"*40}
        self.assertEqual(self.commit(second_plan, index, [])["volumes"], index["volumes"])
        self.assertEqual(self.remote.block_bytes, body_bytes)
        self.assertEqual(len(self.remote.volumes), 1)
        self.assertIn(sha(encode(index)), self.remote.indexes["release"])

    def test_interrupted_cutover_stale_callback_and_conflict(self):
        first = self.commit(self.plan, None, [self.receipt])
        old_marker = self.remote.revision
        new_plan = {**self.plan, "source": "c"*40}
        self.remote.fail = "release"
        with self.assertRaises(ConnectionError):
            generation.publish(self.remote, new_plan, first, [], old_marker)
        for transport in ("ci", "release"):
            self.assertEqual(read_from(self.remote, self.directory, transport), first)
        self.remote.fail = None
        self.commit(new_plan, first, [])
        with self.assertRaisesRegex(ValueError, "stale"):
            generation.publish(self.remote, self.plan, first, [self.receipt], old_marker)
        bad, _ = self.frames(new_plan, {"A": b"different"})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            generation.assemble(new_plan, first, [bad])

    def test_explicit_release_pin_restores_old_version(self):
        first = self.commit(self.plan, None, [self.receipt])
        plan = copy.deepcopy(self.plan)
        plan["source"] = "b"*40
        plan["targets"]["B"]["key"] = sha(b"changed key")
        receipt, _ = self.frames(plan, {"B": b"new payload"})
        self.commit(plan, first, [receipt])
        with patch.object(generation, "GitHub", return_value=self.remote), \
                patch.object(generation, "checkout_plan", return_value=self.plan):
            tag = INDEX_PREFIX + sha(encode(first))
            self.assertEqual(generation.fetch_published(self.directory, ["B"], None, refresh_stale=False, tag=tag), 0)
        self.assertEqual((self.directory / ".lake/build/lib/lean/B.olean").read_bytes(), b"consumer")

    def test_uploaded_receipt_and_conflict_refresh(self):
        import lean_generation_http as http
        from unittest.mock import Mock

        path = self.directory / 'payload'
        path.write_bytes(b'body')
        digest = sha(path.read_bytes())
        asset = {'id': 9, 'state': 'uploaded', 'size': 4, 'digest': 'sha256:' + digest, 'name': digest}
        collision = generation.host.GitHubCommandError(1, 'POST', stderr='HTTP 422: validation failed')
        denied = generation.host.GitHubCommandError(1, 'POST', stderr='HTTP 403: Forbidden')
        child = BUCKET + '-' + digest[0]
        def bucket(tag, rows):
            return {'id': 11 if tag == BUCKET else 12, 'tag_name': tag, 'draft': False, 'assets': rows}
        full = [{'name': str(i)} for i in range(CAPACITY)]
        with patch.object(checkpoints, 'publisher'):
            for ambiguous in (collision, ConnectionResetError('lost ACK')):
                objects = Objects(None)
                objects.http = Mock(spec=http.GitHubHTTP)
                objects.http.api.side_effect = [bucket(BUCKET, []), bucket(BUCKET, [asset])]
                objects.http.upload.side_effect = ambiguous
                self.assertFalse(objects.ensure(digest, digest, 4, path))
                objects.http.upload.assert_called_once()
                self.assertEqual(objects.http.api.call_count, 2)
            objects = Objects(None)
            objects.http = Mock(spec=http.GitHubHTTP)
            objects.http.api.side_effect = [bucket(BUCKET, []), bucket(BUCKET, full), bucket(child, [])]
            objects.http.upload.side_effect = [collision, asset]
            self.assertTrue(objects.ensure(digest, digest, 4, path))
            self.assertFalse(objects.ensure(digest, digest, 4, path))
            self.assertEqual(objects.locate(digest, digest), (child, asset))
            self.assertEqual([call.args[0] for call in objects.http.upload.call_args_list], [11, 12])
            self.assertEqual(objects.http.api.call_count, 3)
            fresh = Objects(None)
            fresh.http.api = Mock(side_effect=[bucket(BUCKET, full), bucket(child, [asset])])
            self.assertEqual(fresh.locate(digest, digest), (child, asset))
            blocked = Objects(None)
            blocked.http = Mock(spec=http.GitHubHTTP)
            blocked.http.api.return_value = bucket(BUCKET, [])
            blocked.http.upload.side_effect = denied
            with self.assertRaisesRegex(generation.host.GitHubCommandError, '403'):
                blocked.ensure(digest, digest, 4, path)
            blocked.http.api.assert_called_once()
            blocked.http.upload.assert_called_once()

    def test_rate_limit_headers_and_asset_redirect_boundaries(self):
        import urllib.error
        from unittest.mock import Mock
        from email.message import Message
        from urllib.parse import urlsplit
        import lean_generation_http as http

        now = 'Tue, 24 Nov 2026 23:00:00 GMT'
        date = int(http.timestamp(now))
        limits = {'Date': now, 'Retry-After': '2', 'X-RateLimit-Reset': str(date + 120), 'X-RateLimit-Remaining': '0'}
        self.assertEqual(http.retry_delay(403, limits, '', 0), 121)
        self.assertEqual(http.retry_delay(429, {'Date': now, 'Retry-After': 'Tue, 24 Nov 2026 23:15:00 GMT'}, '', 0), 900)
        self.assertIsNone(http.retry_delay(403, {}, 'Resource not accessible by integration', 0))
        digest = sha(b'material')
        asset = {'id': 42, 'size': 8, 'name': digest}
        client = http.GitHubHTTP(None)
        client.token = 'token-for-this-test'
        requests = []
        def error(url, status, headers=None, message=b''):
            fields = Message()
            for key, value in (headers or {}).items(): fields[key] = value
            return urllib.error.HTTPError(url, status, 'failure', fields, io.BytesIO(message))
        def served(request, timeout):
            hostname = urlsplit(request.full_url).hostname
            requests.append((hostname, request.get_header('Authorization')))
            if hostname == 'github.com': raise error(request.full_url, 404)
            if hostname == 'api.github.com':
                if len(requests) == 2: raise error(request.full_url, 403, limits, b'API rate limit exceeded')
                raise error(request.full_url, 302, {'Location': 'https://release-assets.githubusercontent.com/asset'})
            response = io.BytesIO(b'm')
            response.status, response.headers = 206, {'Content-Range': 'bytes 0-0/8'}
            return response
        with patch.object(client.opener, 'open', side_effect=served), patch.object(http.time, 'sleep') as sleep:
            self.assertEqual(b''.join(client.asset(asset, BUCKET, 0, 1)), b'm')
            self.assertEqual(b''.join(client.asset(asset, BUCKET, 0, 1)), b'm')
            sleep.assert_called_once_with(121)
        self.assertEqual(requests, [('github.com', None), ('api.github.com', 'Bearer token-for-this-test'),
                                   ('api.github.com', 'Bearer token-for-this-test'),
                                   ('release-assets.githubusercontent.com', None), ('release-assets.githubusercontent.com', None)])
        with patch.object(client.opener, 'open', side_effect=error('url', 403, limits)), \
                patch.object(http.host, 'remaining', return_value=20), patch.object(http.time, 'sleep') as sleep:
            with self.assertRaisesRegex(TimeoutError, 'remaining Release budget'):
                client.api('git/ref/tags/x')
            sleep.assert_not_called()
        body = io.BytesIO(b'material'); body.status, body.headers = 200, {}
        with patch.object(client.opener, 'open', return_value=body), self.assertRaisesRegex(ValueError, 'HTTP 200'):
            list(client.asset(asset, BUCKET, 0, 1))

    def test_shared_asset_bucket_overflow_and_interrupted_upload(self):
        objects = Objects(None)
        digest = sha(b"one volume")
        valid = {"id": 5, "name": digest, "size": 4, "digest": "sha256:" + digest, "state": "uploaded"}
        child = BUCKET + "-" + digest[0]
        objects.buckets[BUCKET] = {str(i): {} for i in range(CAPACITY)}
        objects.buckets[child] = {digest: valid}
        self.assertEqual(objects.locate(digest, digest), (child, valid))
        self.assertEqual(objects.checked(valid, digest, 4)["id"], 5)
        with self.assertRaisesRegex(ValueError, "digest or length"):
            objects.checked(valid, digest, 3)
        objects.buckets[BUCKET] = {digest: {"state": "starter", "size": 0}}
        self.assertEqual(objects.locate(digest, digest), (child, valid))

    def test_retry_lost_ack_and_read_only_eviction_fallback(self):
        first = generation.publish(self.remote, self.plan, None, [self.receipt], "")
        proof = first["first_restore"]
        self.assertEqual(proof["ci_artifact_digest"], proof["release_artifact_digest"])
        self.assertGreater(proof["release_bytes"], 0)
        replay = generation.publish(self.remote, self.plan, None, [self.receipt], "")
        self.assertEqual(first["marker"], replay["marker"])
        self.assertEqual(self.remote.range_requests, 2)
        actual = self.remote.get_index
        next_plan = {**self.plan, "source": "c"*40}
        for code in (4, 5):
            def unavailable(digest, path, transport):
                if transport == "ci":
                    raise subprocess.CalledProcessError(code, "get")
                self.assertEqual(digest, first["ci_index_sha256"])
                return actual(digest, path, transport)
            with self.subTest(code=code), patch.object(self.remote, "get_index", side_effect=unavailable):
                self.assertTrue(generation.latest(self.remote, "ci", self.directory / "resolved"))
                with self.assertRaises(subprocess.CalledProcessError):
                    generation.publish(self.remote, next_plan, read(self.directory / "resolved"), [], first["marker"])
        self.assertEqual(first["marker"], self.remote.revision)

    def test_resume_pending_generation_release_by_draft_id(self):
        target = self.plan["source"]
        path = self.directory / "index"
        path.write_bytes(encode({"source": target}))
        digest = sha(path.read_bytes())
        tag = INDEX_PREFIX + digest
        starter = {"name": digest, "id": 142, "state": "starter", "size": 0, "digest": None}
        metadata = {"draft": True, "id": 17, "tag_name": tag, "target_commitish": target, "assets": [starter]}
        writes = []
        def gh(deadline, *args):
            if args[:2] == ("api", "graphql"):
                self.assertIn("tag=" + tag, args)
                return json.dumps({"data": {"repository": {"release": {"databaseId": 17, "isDraft": True}}}})
            if args[:2] == ("api", "repos/" + generation.host.REPO + "/releases/17"):
                return json.dumps(metadata)
            if args[:3] == ("api", "--method", "DELETE"):
                self.assertEqual(args[-1], "repos/" + generation.host.REPO + "/releases/assets/142")
                metadata["assets"].clear()
            elif args[:3] == ("release", "upload", tag):
                self.assertEqual(Path(args[3]).read_bytes(), path.read_bytes())
                metadata["assets"] = [dict(starter, state="uploaded", digest="sha256:" + digest,
                                           size=path.stat().st_size)]
            elif args[:3] == ("release", "edit", tag):
                self.assertIn("--draft=false", args)
                metadata["draft"] = False
            else:
                self.fail(f"unexpected GitHub call: {args}")
            writes.append(args[1:3])
        def by_tag(tag_name, deadline):
            self.assertEqual(tag_name, tag)
            return None if metadata["draft"] else metadata
        remote = GitHub(self.directory, None)
        with patch.object(generation.host, "gh", side_effect=gh), \
                patch.object(generation.host, "existing_release", side_effect=by_tag), \
                patch.object(checkpoints, "publisher"), patch.object(remote, "ci"):
            for field, bad in (("target_commitish", "d"*40), ("tag_name", "wrong"), ("id", 18)):
                with self.subTest(field=field), patch.dict(metadata, {field: bad}), self.assertRaises(ValueError):
                    remote.stage_indexes(digest, path)
                self.assertEqual(writes, [])
            remote.stage_indexes(digest, path)
            self.assertEqual(writes, [("--method", "DELETE"), ("upload", tag), ("edit", tag)])
            remote.stage_indexes(digest, path)
            self.assertEqual(len(writes), 3)

    def test_valid_checkpoint_metadata_with_corrupt_or_vanished_body(self):
        remote = GitHub(self.directory, None)
        digest = sha(b"expected")
        name = "checkpoint-" + digest
        asset = {"id": 5, "name": name, "size": 8, "digest": "sha256:" + digest, "state": "uploaded"}
        for bad in ({"digest": "sha256:" + "a"*64}, {"size": 0}, {"state": "starter"}, {"id": -1}):
            with self.subTest(metadata=bad), patch.object(remote.objects, "locate", return_value=("tag", asset | bad)), \
                    self.assertRaises(InvalidIndexBody):
                remote.get_index(digest, self.directory / "download", "checkpoint")
        def corrupt(asset, tag):
            yield b"corrupt!"
        errors = [(corrupt, InvalidIndexBody),
                  (generation.host.GitHubCommandError(1, "gh", stderr="HTTP 404: Not Found"), MissingCheckpoint),
                  (generation.host.GitHubCommandError(1, "gh", stderr="HTTP 403: Forbidden"),
                   generation.host.GitHubCommandError)]
        for transport, expected in errors:
            with self.subTest(transport=transport), patch.object(remote.objects, "locate", return_value=("tag", asset)), \
                    patch.object(remote.objects.http, "asset", side_effect=transport), self.assertRaises(expected):
                remote.get_index(digest, self.directory / "download", "checkpoint")

    def test_checkpoint_ref_retries_ambiguous_create_only_if_missing(self):
        name = "checkpoint"
        tag = "f" * 40
        ref = {"object": {"sha": tag}}
        missing = generation.host.GitHubCommandError(1, "gh", stderr="gh: Not Found (HTTP 404)")
        for diagnostic, delay in (("unexpected end of JSON input", 2), ("gh: Unavailable (HTTP 503)", 2),
                                  ("gh: API rate limit exceeded (HTTP 403)", 60)):
            error = generation.host.GitHubCommandError(1, "gh", stderr=diagnostic)
            with self.subTest(diagnostic=diagnostic), patch.object(checkpoints.time, "sleep") as sleep, \
                    patch.object(self.remote, "api", create=True, side_effect=[error, missing, ref]) as api:
                self.assertEqual(checkpoints.create_ref(self.remote, name, tag), ref)
                create, lookup, retry = api.call_args_list
                self.assertEqual(create.args, ("git/refs", "POST"))
                self.assertEqual(create.kwargs, {'ref': 'refs/tags/checkpoint', 'sha': tag})
                self.assertEqual(lookup.args, ("git/ref/tags/checkpoint",))
                self.assertEqual(retry, create)
                sleep.assert_called_once_with(delay)
            with patch.object(checkpoints.time, "sleep") as sleep, \
                    patch.object(self.remote, "api", create=True, side_effect=[error, ref]) as api:
                self.assertEqual(checkpoints.create_ref(self.remote, name, tag), ref)
                self.assertEqual(api.call_count, 2)
                sleep.assert_not_called()

    def test_checkpoint_ref_preserves_create_failure_and_conflicting_tag(self):
        tag = "f" * 40
        denied = generation.host.GitHubCommandError(1, "gh", stderr="gh: Forbidden (HTTP 403)")
        ambiguous = generation.host.GitHubCommandError(1, "gh", stderr="unexpected end of JSON input")
        missing = generation.host.GitHubCommandError(1, "gh", stderr="gh: Not Found (HTTP 404)")
        for sequence, expected in (([denied], denied), ([ambiguous, missing] * 3, ambiguous)):
            with self.subTest(expected=expected.stderr), patch.object(checkpoints.time, "sleep"), \
                    patch.object(self.remote, "api", create=True, side_effect=sequence) as api, \
                    self.assertRaises(generation.host.GitHubCommandError) as raised:
                checkpoints.create_ref(self.remote, "checkpoint", tag)
            self.assertIs(raised.exception, expected)
            self.assertEqual(api.call_count, len(sequence))

        exists = generation.host.GitHubCommandError(1, "gh", stderr="gh: Reference already exists (HTTP 422)")
        responses = [{"sha": tag}, exists, {"object": {"sha": "e" * 40}},
                     {"object": {"sha": self.receipt["source"]}, "message": "different targets"}]
        with patch.object(self.remote, "api", create=True, side_effect=responses), \
                patch.object(checkpoints, "publisher", return_value={"run": "123", "attempt": "1"}), \
                self.assertRaisesRegex(ValueError, "checkpoint metadata differs"):
            checkpoints.save(self.remote, sha(b"domain"), self.receipt, self.directory)

    def test_partial_checkpoint_discoverable_before_ci_quota_failure(self):
        remote = self.remote
        discovered = []
        def api(endpoint, method="GET", **fields):
            if endpoint == "git/tags":
                return {"sha": "f"*40}
            if endpoint == "git/refs":
                digest = fields["ref"].rsplit("-", 1)[1]
                advertised = [r["key"] for r in self.receipt["targets"].values()]
                discovered.append((digest, self.receipt["source"], advertised))
                return {"object": {"sha": "f"*40}}
            self.assertEqual(endpoint, "actions/runs/123/attempts/1")
            return {"repository": {"full_name": generation.host.REPO}, "head_branch": "dev",
                    "head_sha": self.receipt["source"], "event": "schedule",
                    "path": ".github/workflows/cache-release-publish.yml"}
        def deny(operation, digest, path):
            self.assertEqual(operation, "put")
            self.assertEqual(discovered[0][0], digest)
            raise subprocess.CalledProcessError(1, "quota")
        with patch.object(remote, "ci", create=True, side_effect=deny), \
                patch.object(remote, "api", create=True, side_effect=api), \
                patch.object(checkpoints, "publisher", return_value={"run": "123", "attempt": "1"}), \
                patch.object(checkpoints, "descriptors", return_value=discovered):
            checkpoints.save(remote, sha(b"domain"), self.receipt, self.directory)
            recovered, = checkpoints.discover(remote, sha(b"domain"))
        self.assertEqual(recovered["targets"], self.receipt["targets"])
        self.assertFalse(remote.indexes["ci"])
        self.assertFalse(remote.marker)

    def test_staged_only_explicit_version_refused_before_artifacts(self):
        index = generation.assemble(self.plan, None, [self.receipt])
        digest = sha(encode(index))
        self.remote.indexes["release"][digest] = encode(index)
        with patch.object(generation, "GitHub", return_value=self.remote), self.assertRaisesRegex(
                ValueError, "published generation history"):
            generation.fetch_published(self.directory, ["B"], None, refresh_stale=False, tag=INDEX_PREFIX + digest)
        self.assertEqual(self.remote.block_bytes, 0)

    def test_digest_failure_and_changed_keys(self):
        first = self.commit(self.plan, None, [self.receipt])
        different = copy.deepcopy(self.plan)
        different["targets"]["B"]["key"] = sha(b"B2")
        destination = self.directory / "filtered"
        self.assertEqual(generation.restore(self.remote, first, different, ["B"], destination)["restored"], 1)
        self.assertFalse((destination / "lib/lean/B.olean").exists())
        first["targets"]["A"]["sha256"] = "0"*64
        with self.assertRaisesRegex(ValueError, "checksum"):
            generation.restore(self.remote, first, self.plan, ["A"], self.directory / "bad")
        self.assertFalse((self.directory / "bad/lib/lean/A.olean").exists())
        first["targets"]["A"]["offset"] = first["volumes"][first["targets"]["A"]["volume"]]
        with self.assertRaisesRegex(ValueError, "extent"):
            validate(first, complete=True)

    def test_unpublished_first_generation_and_local_reader(self):
        marker = self.directory / "marker"
        self.assertFalse(generation.latest(self.remote, "ci", self.directory / "index", marker))
        self.assertEqual(marker.read_bytes(), b"")
        self.commit(self.plan, None, [self.receipt])
        with patch.object(generation, "GitHub", return_value=self.remote), \
                patch.object(generation, "checkout_plan", return_value=self.plan):
            self.assertEqual(generation.fetch_published(self.directory, [], None, refresh_stale=False), 0)
        self.assertEqual((self.directory / ".lake/build/lib/lean/A.olean").read_bytes(), b"library")


def read_from(remote, directory, transport):
    path = directory / "current-index.json"
    assert generation.latest(remote, transport, path)
    return read(path)


if __name__ == "__main__":
    unittest.main()
