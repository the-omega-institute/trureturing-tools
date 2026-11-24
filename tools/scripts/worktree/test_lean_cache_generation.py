import copy
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import lean_cache_generation as generation
from lean_generation_index import encode, read, sha, validate


class Store:
    deadline = None

    def __init__(self):
        self.revision = ""
        self.marker = None
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

    def volume(self, digest, size):
        assert len(self.volumes[digest]) == size
        return digest

    def blocks(self, asset, start, size):
        self.range_requests += 1
        self.block_bytes += size
        yield self.volumes[asset][start:start + size]

    def current(self):
        return self.revision, self.marker

    def stage_indexes(self, digest, path):
        for transport in self.indexes:
            if self.fail == transport:
                raise ConnectionError(transport)
            self.indexes[transport][digest] = path.read_bytes()

    def get_index(self, digest, path, transport):
        path.write_bytes(self.indexes[transport][digest])
        assert sha(path.read_bytes()) == digest
        return read(path)

    def advance(self, expected, marker):
        assert self.revision == expected
        self.marker = marker
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
        self.assertEqual(self.remote.range_requests, 2)
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
