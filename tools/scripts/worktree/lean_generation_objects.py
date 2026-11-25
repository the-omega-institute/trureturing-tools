"""Append-only Release asset buckets shared by full versions and partial checkpoints."""

import json
import http.client
import re

import lean_cache_release as host
from lean_release_index import valid_digest
from lean_generation_http import GitHubHTTP

BUCKET = "lean-cache-objects-v1"
# Leave room for the concurrent final writes of workers with the same bucket view.
CAPACITY = 980


class Objects:
    def __init__(self, deadline):
        self.deadline = deadline
        self.buckets = {}
        self.ids = {}
        self.http = GitHubHTTP(deadline)

    def remember(self, tag, release):
        if (release.get('tag_name') != tag or release.get('draft') is not False
                or type(release.get('id')) is not int or release['id'] < 1):
            raise ValueError('invalid generation object bucket: ' + tag)
        self.ids[tag] = release['id']
        rows = release['assets']
        self.buckets[tag] = {row['name']: row for row in rows}
        if len(self.buckets[tag]) != len(rows):
            raise ValueError('duplicate immutable asset names')

    def assets(self, tag):
        if tag not in self.buckets:
            try:
                release = self.http.api('releases/tags/' + tag)
            except host.GitHubCommandError as error:
                if not error.stderr.startswith('HTTP 404:'):
                    raise
                self.buckets[tag] = None
            else:
                self.remember(tag, release)
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
        for attempt in range(3):
            tag, asset = self.locate(digest, name)
            if self.checked(asset, digest, size):
                return False
            try:
                if self.buckets[tag] is None:
                    body = {'tag_name': tag, 'name': tag, 'prerelease': True, 'make_latest': 'false',
                            'body': 'Content-addressed Lean build objects shared by published generations.'}
                    self.remember(tag, self.http.api('releases', method='POST', payload=json.dumps(body).encode(),
                                                     headers={'Content-Type': 'application/json'}))
                uploaded = self.http.upload(self.ids[tag], name, path)
                if uploaded['name'] != name or not self.checked(uploaded, digest, size):
                    raise ValueError('unexpected upload receipt for ' + name)
                self.buckets[tag][name] = uploaded
                return True
            except (host.GitHubCommandError, http.client.HTTPException, OSError, json.JSONDecodeError) as error:
                if isinstance(error, host.GitHubCommandError) and error.stderr != 'GitHub connection interrupted' and not re.search(r'HTTP (409|422|5\d\d):', error.stderr):
                    raise
                # Reconcile a concurrent insertion, a full bucket, or a lost acknowledgement.
                self.buckets.pop(tag, None)
                _, actual = self.locate(digest, name)
                if self.checked(actual, digest, size):
                    return False
                if attempt == 2:
                    raise
