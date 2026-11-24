"""Append-only Release asset buckets shared by full versions and partial checkpoints."""

import json
import os
from pathlib import Path
import tempfile

import lean_cache_release as host
from lean_release_index import valid_digest

BUCKET = "lean-cache-objects-v1"
# Leave room for the concurrent final writes of workers with the same bucket view.
CAPACITY = 980


class Objects:
    def __init__(self, deadline):
        self.deadline = deadline
        self.buckets = {}

    def assets(self, tag):
        if tag not in self.buckets:
            release = host.existing_release(tag, self.deadline)
            if release is None:
                self.buckets[tag] = None
            else:
                if release["draft"]:
                    raise ValueError("generation object bucket is unpublished: " + tag)
                url = f"repos/{host.REPO}/releases/{release['id']}/assets?per_page=100"
                output = host.gh(self.deadline, "api", url, "--paginate", "--jq",
                                 ".[]|{name,id,size,digest,state}")
                rows = [json.loads(line) for line in output.splitlines() if line]
                self.buckets[tag] = {row["name"]: row for row in rows}
                if len(self.buckets[tag]) != len(rows):
                    raise ValueError("duplicate immutable asset names")
        return self.buckets[tag]

    def locate(self, digest, name):
        if not valid_digest(digest) or name not in (digest, "checkpoint-" + digest):
            raise ValueError("invalid generation object address")
        # Once a prefix bucket fills, append to its deterministic children.
        # Retained buckets are never pruned or renumbered.
        for depth in range(len(digest) + 1):
            tag = BUCKET + ("-" + digest[:depth] if depth else "")
            assets = self.assets(tag)
            if assets and name in assets and assets[name]["state"] == "starter" and assets[name]["size"] == 0:
                # A prior upload may have stopped after reserving the name.
                # A child bucket permits recovery without deleting a live upload.
                continue
            if assets is None or name in assets or len(assets) < CAPACITY:
                return tag, (assets or {}).get(name)
        raise ValueError("generation object address exhausted")

    @staticmethod
    def checked(asset, digest, size):
        if asset is not None and (asset.get("state") != "uploaded" or asset.get("digest") != "sha256:" + digest
                                  or asset.get("size") != size or type(asset.get("id")) is not int or asset["id"] <= 0):
            raise ValueError("immutable object differs from requested digest or length: " + digest)
        return asset

    def ensure(self, digest, name, size, path):
        from lean_generation_checkpoints import publisher
        publisher()
        for attempt in range(2):
            tag, asset = self.locate(digest, name)
            if self.checked(asset, digest, size):
                return False
            if self.buckets[tag] is None:
                try:
                    host.gh(self.deadline, "release", "create", tag, "--repo", host.REPO,
                            "--prerelease", "--latest=false", "--title", tag,
                            "--notes", "Content-addressed Lean build objects shared by published generations.")
                except host.GitHubCommandError:
                    if host.existing_release(tag, self.deadline) is None:
                        raise
            with tempfile.TemporaryDirectory(dir=path.parent) as temp:
                alias = Path(temp) / name
                os.link(path, alias)
                try:
                    host.gh(self.deadline, "release", "upload", tag, str(alias), "--repo", host.REPO)
                except host.GitHubCommandError:
                    self.buckets.pop(tag, None)
                    if attempt == 0:
                        continue
                    raise
            self.buckets.pop(tag, None)
            _, asset = self.locate(digest, name)
            if not self.checked(asset, digest, size):
                raise ValueError("uploaded generation object is unavailable")
            return True
        raise ValueError("unable to stage generation object")
