"""The published archive, digest tag, CI transfer and GitHub draft recovery contracts."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import publish


COMMIT = "a" * 40
OCI_REFERENCE = f"{publish.REGISTRY}@sha256:{'b' * 64}"
ARTIFACTS = ("blueprint-index.v1.json", "raw-lean-report.json", "source-snapshot.v1.json",
             "truth-export.v1.json", "truth-graph.v1.json")


def hex_digest(data):
    return hashlib.sha256(data).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.assets = self.root / "assets"
        self.dag = self.root / "DAG.md"
        self.dag.write_bytes(b"# Truth DAG\n")
        for name in ARTIFACTS:
            (self.bundle / name).write_text("{}\n")
        snapshot = dict(source_repo=publish.REPO, source_commit=COMMIT,
                        dag_md_sha256="sha256:" + hex_digest(self.dag.read_bytes()))
        (self.bundle / "source-snapshot.v1.json").write_text(json.dumps(snapshot))
        self.sums = ("".join(f"{hex_digest((self.bundle / n).read_bytes())}  {n}\n"
                             for n in ARTIFACTS)).encode()
        (self.bundle / "SHA256SUMS").write_bytes(self.sums)
        self.digest = "sha256:" + hex_digest(self.sums)
        coordinate = dict(schema="truth-release-publication.v1", bundle_ref=self.digest,
                          release_digest=self.digest, source_commit=COMMIT,
                          source_tree="c" * 40, producer_commit="d" * 40)
        (self.bundle / publish.PUBLICATION).write_text(json.dumps(coordinate))
        (self.bundle / "release-manifest.v1.json").write_text("{}\n")
        self.outputs = self.root / "outputs"
        self.enterContext(patch.dict(os.environ, GITHUB_REPOSITORY=publish.REPO,
                                     SOURCE_COMMIT=COMMIT, GITHUB_OUTPUT=str(self.outputs)))

    def prepare(self):
        publish.prepare(self.bundle, self.dag, self.assets, COMMIT)
        return dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())

    def test_archive_matches_release_coordinate_and_can_be_rebuilt(self):
        outputs = self.prepare()
        archive = self.assets / f"truth-release-{hex_digest(self.sums)}.tar.gz"
        self.assertEqual(outputs["release_digest"], self.digest)
        self.assertEqual(outputs["archive_sha256"], hex_digest(archive.read_bytes()))
        self.assertEqual(outputs["dag_sha256"], hex_digest((self.assets / "DAG.md").read_bytes()))
        self.assertEqual(outputs["publication_sha256"],
                         hex_digest((self.assets / publish.PUBLICATION).read_bytes()))
        with tarfile.open(archive, "r:gz") as opened:
            expected = {f"./{path.name}" for path in self.bundle.iterdir()}
            self.assertEqual(set(opened.getnames()), expected)
            for name in expected:
                self.assertEqual(opened.extractfile(name).read(), (self.bundle / name).read_bytes())
        for path in self.bundle.iterdir():
            os.utime(path, (120, 120))
        self.assertEqual(self.prepare(), outputs)

    def test_rejects_stale_checksums_and_mismatched_dag(self):
        (self.bundle / "SHA256SUMS").write_bytes(self.sums.rstrip(b"\n"))
        with self.assertRaisesRegex(ValueError, "checksums"):
            self.prepare()
        (self.bundle / "SHA256SUMS").write_bytes(self.sums)
        self.dag.write_bytes(b"other graph\n")
        with self.assertRaisesRegex(ValueError, "DAG or source"):
            self.prepare()
        (self.bundle / "truth-graph.v1.json").write_bytes(b"another graph")
        with self.assertRaisesRegex(ValueError, "checksums"):
            self.prepare()

    def test_tampered_download_fails_before_contacting_either_service(self):
        outputs = self.prepare()
        downloaded = self.root / "download"
        shutil.copytree(self.assets, downloaded)
        with patch.dict(os.environ, {k.upper(): v for k, v in outputs.items()}), \
                patch.object(subprocess, "run") as remote:
            for asset in downloaded.iterdir():
                original = asset.read_bytes()
                asset.write_bytes(original + b"!")
                for args in (["publish", "oci", str(downloaded)],
                             ["publish", "mirror", str(downloaded), OCI_REFERENCE]):
                    with self.subTest(asset=asset.name, command=args[1]), patch.object(sys, "argv", args):
                        with self.assertRaisesRegex(ValueError, "transfer checksum mismatch"):
                            publish.main()
                asset.write_bytes(original)
            remote.assert_not_called()

    def test_existing_draft_uploads_only_missing_files_and_publishes_once(self):
        self.prepare()
        hashes = {path.name: hex_digest(path.read_bytes()) for path in self.assets.iterdir()}
        tag = "truth-release-" + self.digest[7:]
        release = {"tagName": tag, "targetCommitish": COMMIT, "isPrerelease": False,
                   "isDraft": True, "assets": []}

        def uploaded(name):
            return {"name": name, "digest": "sha256:" + hashes[name], "state": "uploaded"}

        release["assets"].append(uploaded(publish.PUBLICATION))
        mutations = []

        def gh(command, **options):
            if command[1] == "api":
                self.assertTrue(command[2].endswith("/git/ref/tags/" + tag))
                if release["isDraft"]:
                    return subprocess.CompletedProcess(command, 1, "", "gh: Not Found (HTTP 404)")
                answer = {"object": {"type": "commit", "sha": COMMIT}}
            elif command[2] == "view":
                answer = release
            elif command[2] == "upload":
                names = {path.name for path in command[4:command.index("--repo")]}
                self.assertEqual(names, set(hashes) - {publish.PUBLICATION})
                release["assets"].extend(uploaded(name) for name in names)
                mutations.append("upload")
                answer = None
            elif command[2] == "edit":
                self.assertIn("--draft=false", command)
                self.assertIn("--latest=false", command)
                release["isDraft"] = False
                mutations.append("edit")
                answer = None
            else:
                self.fail(f"unexpected gh command {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(answer), "")

        with patch.object(subprocess, "run", side_effect=gh):
            for _ in range(2):
                publish.mirror(self.assets, hashes, self.digest, COMMIT, OCI_REFERENCE)
            release["assets"][0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "published bytes differ"):
                publish.mirror(self.assets, hashes, self.digest, COMMIT, OCI_REFERENCE)
        self.assertEqual(mutations, ["upload", "edit"])
