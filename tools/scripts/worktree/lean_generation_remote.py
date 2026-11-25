"""GitHub transport for immutable Lean generation objects and their single publication ref."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import lean_cache_release as host
import lean_release_volume_remote as remote
from lean_generation_objects import Objects
from lean_generation_index import MAX_INDEX, SCHEMA, encode, read, sha
from lean_release_index import valid_digest

MARKER = "heads/lean-cache-published"
INDEX_PREFIX = "lean-generation-v1-"


class InvalidIndexBody(ValueError):
    """The downloaded bytes do not encode the immutable index at their address."""


class MissingCheckpoint(FileNotFoundError):
    """The advertised checkpoint body is no longer present in its object bucket."""


def decode_marker(commit):
    marker = json.loads(commit["message"])
    if (marker["schema"] != SCHEMA or not valid_digest(marker["index"])
            or not re.fullmatch("[0-9a-f]{40}", marker["source"])):
        raise ValueError("invalid published generation marker")
    return marker


class GitHub:
    def __init__(self, root, deadline):
        self.root = root
        self.deadline = deadline
        self.objects = Objects(deadline)

    def api(self, path, method='GET', **fields):
        return self.objects.http.api(path, method=method, payload=json.dumps(fields).encode() if fields else None,
                                     headers={'Content-Type': 'application/json'} if fields else None)

    def current(self):
        # The initial v1 marker probe also serves legacy CLI-authenticated consumers.
        path = "git/ref/" + MARKER
        try:
            ref = json.loads(host.gh(self.deadline, 'api', 'repos/' + host.REPO + '/' + path))
        except host.GitHubCommandError as error:
            try:
                failure = json.loads(error.output or '{}')
            except ValueError:
                failure = {}
            if isinstance(failure, dict) and failure.get('status') == '404':
                return "", None
            if not re.search(r'HTTP 429|rate limit|HTTP 403', error.stderr or '', re.I):
                raise
            # Read the actual response headers so a throttled probe can wait for reset.
            ref = self.api(path)
        commit = ref["object"]["sha"]
        if ref["ref"] != "refs/" + MARKER or not re.fullmatch("[0-9a-f]{40}", commit):
            raise ValueError("invalid published generation ref")
        return commit, decode_marker(self.api("git/commits/" + commit))

    def published(self, digest):
        revision, marker = self.current()
        while marker:
            if marker["index"] == digest:
                return marker
            row = self.api("git/commits/" + revision)
            parent, = row["parents"]
            revision = parent["sha"]
            if revision == marker["source"]:
                break  # The first marker's parent is its protected dev source.
            marker = decode_marker(self.api("git/commits/" + revision))
        raise ValueError("version is outside published generation history")

    def ensure_volume(self, digest, size, path):
        return self.objects.ensure(digest, digest, size, path)

    def has_volume(self, digest, size):
        _, asset = self.objects.locate(digest, digest)
        return self.objects.checked(asset, digest, size) is not None

    def confirmed_volumes(self, sizes):
        # Cache one paginated metadata inventory per physical bucket.
        return {digest: size for digest, size in sizes.items() if self.has_volume(digest, size)}

    def volume(self, digest, size):
        tag, asset = self.objects.locate(digest, digest)
        if not self.objects.checked(asset, digest, size):
            raise ValueError("missing immutable generation volume: " + digest)
        return {**asset, 'tag': tag}

    def blocks(self, asset, start, size):
        return self.objects.http.asset(asset, asset['tag'], start, size)

    def ci(self, operation, digest, path):
        adapter = self.root / ".github/actions/lean-cache-shards/run.mjs"
        subprocess.run(["node", adapter, "generation-" + operation, digest, path.resolve()],
                       cwd=self.root, check=True)

    def stage_indexes(self, digest, path, *, checkpoint=False):
        from lean_generation_checkpoints import publisher
        publisher()
        if checkpoint:
            self.objects.ensure(digest, "checkpoint-" + digest, path.stat().st_size, path)
        else:
            remote.ensure_asset(host, INDEX_PREFIX + digest, digest, path.stat().st_size, path, self.deadline,
                                serialized=True, target=read(path)["source"])
            self.ci("put", digest, path)

    def get_index(self, digest, path, transport):
        if transport == "ci":
            self.ci("get", digest, path)
        elif transport == "checkpoint":
            name = "checkpoint-" + digest
            tag, asset = self.objects.locate(digest, name)
            if asset is None:
                raise MissingCheckpoint("missing durable checkpoint index: " + digest)
            size = asset.get("size")
            if type(size) is not int or not 0 < size <= MAX_INDEX:
                raise InvalidIndexBody("invalid checkpoint asset length: " + digest)
            try:
                self.objects.checked(asset, digest, size)
            except ValueError as error:
                raise InvalidIndexBody("invalid checkpoint asset metadata: " + digest) from error
        else:
            tag = INDEX_PREFIX + digest
            metadata = host.existing_release(tag, self.deadline)
            if metadata is None or metadata["draft"]:
                raise ValueError("missing published generation index")
            assets = metadata["assets"]
            if len(assets) != 1 or not 0 < assets[0]["size"] <= MAX_INDEX:
                raise ValueError("invalid generation index asset")
            asset = remote.checked_asset(metadata, tag, digest, assets[0]["size"])
        if transport != 'ci':
            with tempfile.TemporaryDirectory(dir=path.parent) as directory, (Path(directory) / 'index').open('wb') as temp:
                try:
                    for block in self.objects.http.asset(asset, tag):
                        temp.write(block)
                except host.GitHubCommandError as error:
                    if transport == 'checkpoint' and (error.stderr or '').startswith('HTTP 404:'):
                        raise MissingCheckpoint('checkpoint asset disappeared: ' + digest) from error
                    raise
                temp.close()
                if Path(temp.name).stat().st_size != asset['size']:
                    raise InvalidIndexBody('index body differs from its recorded length: ' + digest)
                os.replace(temp.name, path)
        if path.stat().st_size > MAX_INDEX or host.archive_sha(path, self.deadline) != digest:
            raise InvalidIndexBody("generation index readback digest mismatch")
        try:
            index = read(path)
            canonical = sha(encode(index))
        except ValueError as error:
            raise InvalidIndexBody("generation index body is malformed") from error
        if canonical != digest:
            raise InvalidIndexBody("generation index encoding differs from its canonical digest")
        return index

    def advance(self, expected, marker):
        from lean_generation_checkpoints import publisher
        publisher()
        current, old = self.current()
        if marker == old:
            return current
        if current != expected:
            raise ValueError("stale generation publisher")
        if old is not None and old["source"] != marker["source"]:
            comparison = self.api("compare/" + old["source"] + "..." + marker["source"])
            if comparison["status"] != "ahead":
                raise ValueError("generation would move protected dev backwards")
        elif old is not None:
            raise ValueError("source commit already has a different published generation")
        tree = self.api("git/commits/" + marker["source"])["tree"]["sha"]
        child = self.api("git/commits", "POST", message=encode(marker).decode(), tree=tree,
                         parents=[current or marker["source"]])["sha"]
        if current:
            # The new commit's only parent is the observed marker. A racing
            # sibling can never be its ancestor, so Git rejects the stale swap.
            ref = self.api("git/refs/" + MARKER, "PATCH", sha=child, force=False)
        else:
            ref = self.api("git/refs", "POST", ref="refs/" + MARKER, sha=child)
        if ref["object"]["sha"] != child:
            raise ValueError("generation marker did not advance to its verified index")
        return child
