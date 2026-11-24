"""GitHub transport for immutable Lean generation objects and their single publication ref."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import lean_cache_release as host
import lean_release_volume_remote as remote
from lean_generation_index import MAX_INDEX, SCHEMA, encode, read, sha
from lean_release_index import valid_digest

MARKER = "heads/lean-cache-published"
VOLUME_PREFIX = remote.volume_prefix(SCHEMA)
INDEX_PREFIX = "lean-generation-v1-"


class GitHub:
    def __init__(self, root, deadline):
        self.root = root
        self.deadline = deadline

    def api(self, path, *args):
        return json.loads(host.gh(self.deadline, "api", "repos/" + host.REPO + "/" + path, *args))

    def current(self):
        try:
            ref = self.api("git/ref/" + MARKER)
        except host.GitHubCommandError as error:
            # Never bootstrap after a transport/auth failure.
            if json.loads(error.output).get("status") == "404":
                return "", None
            raise
        commit = ref["object"]["sha"]
        if ref["ref"] != "refs/" + MARKER or not re.fullmatch("[0-9a-f]{40}", commit):
            raise ValueError("invalid published generation ref")
        marker = json.loads(self.api("git/commits/" + commit)["message"])
        if (marker["schema"] != SCHEMA or not valid_digest(marker["index"])
                or not re.fullmatch("[0-9a-f]{40}", marker["source"])):
            raise ValueError("invalid published generation marker")
        return commit, marker

    def ensure_volume(self, digest, size, path):
        return remote.ensure_asset(host, VOLUME_PREFIX + digest, digest, size, path, self.deadline,
                                   serialized=True)

    def has_volume(self, digest, size):
        return remote.asset_for(host, VOLUME_PREFIX + digest, digest, size, self.deadline) is not None

    def volume(self, digest, size):
        asset = remote.asset_for(host, VOLUME_PREFIX + digest, digest, size, self.deadline)
        if asset is None:
            raise ValueError("missing immutable generation volume: " + digest)
        return asset

    def blocks(self, asset, start, size):
        return host.range_blocks(asset, start, size, self.deadline)

    def ci(self, operation, digest, path):
        adapter = self.root / ".github/actions/lean-cache-shards/run.mjs"
        subprocess.run(["node", adapter, "generation-" + operation, digest, path.resolve()],
                       cwd=self.root, check=True)

    def stage_indexes(self, digest, path):
        remote.ensure_asset(host, INDEX_PREFIX + digest, digest, path.stat().st_size, path,
                            self.deadline)
        self.ci("put", digest, path)

    def get_index(self, digest, path, transport):
        if transport == "ci":
            self.ci("get", digest, path)
        else:
            tag = INDEX_PREFIX + digest
            metadata = host.existing_release(tag, self.deadline)
            if metadata is None or metadata["draft"]:
                raise ValueError("missing published generation index")
            assets = metadata["assets"]
            if len(assets) != 1 or not 0 < assets[0]["size"] <= MAX_INDEX:
                raise ValueError("invalid generation index asset")
            remote.checked_asset(metadata, tag, digest, assets[0]["size"])
            with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
                host.gh(self.deadline, "release", "download", tag, "--repo", host.REPO, "--dir",
                        temporary, "--pattern", digest)
                os.replace(Path(temporary) / digest, path)
        if path.stat().st_size > MAX_INDEX or host.archive_sha(path, self.deadline) != digest:
            raise ValueError("generation index readback digest mismatch")
        index = read(path)
        if sha(encode(index)) != digest:
            raise ValueError("generation index encoding differs from its canonical digest")
        return index

    def advance(self, expected, marker):
        current, old = self.current()
        if current != expected:
            raise ValueError("stale generation publisher")
        if marker == old:
            return current
        if old is not None and old["source"] != marker["source"]:
            comparison = self.api("compare/" + old["source"] + "..." + marker["source"])
            if comparison["status"] != "ahead":
                raise ValueError("generation would move protected dev backwards")
        elif old is not None:
            raise ValueError("source commit already has a different published generation")
        tree = self.api("git/commits/" + marker["source"])["tree"]["sha"]
        child = self.api("git/commits", "-X", "POST", "-f", "message=" + encode(marker).decode(),
                         "-f", "tree=" + tree, "-f", "parents[]=" + (current or marker["source"]))["sha"]
        if current:
            # The new commit's only parent is the observed marker. A racing
            # sibling can never be its ancestor, so Git rejects the stale swap.
            ref = self.api("git/refs/" + MARKER, "-X", "PATCH", "-f", "sha=" + child, "-F", "force=false")
        else:
            ref = self.api("git/refs", "-X", "POST", "-f", "ref=refs/" + MARKER, "-f", "sha=" + child)
        if ref["object"]["sha"] != child:
            raise ValueError("generation marker did not advance to its verified index")
        return child
