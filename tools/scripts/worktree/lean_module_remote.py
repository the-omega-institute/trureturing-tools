"""Release shards shared by immutable version indexes and their Actions cache copies."""

from functools import cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlencode

import lean_cache_release as host
from lean_generation_checkpoints import publisher
from lean_generation_index import MAX_INDEX, read
from lean_generation_objects import GitHubHTTP
from lean_release_index import valid_digest

PREFIX = 'lean-modules-'
MARKER = 'heads/lean-modules-published'
INDEX = 'index.json'
TAG = re.compile(r'lean-modules-([0-9a-f]{40})(?:-part-([2-9]|[1-9][0-9]+))?')
CAPACITY = 900


def json_body(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def missing(error):
    try:
        return str(json.loads(error.output)['status']) == '404'
    except (KeyError, TypeError, ValueError):
        return bool(re.search(r'\bHTTP 404\b', error.stderr or ''))


class Remote:
    def __init__(self, root):
        self.root = root
        self.deadline = host.operation_deadline()
        self.http = GitHubHTTP(self.deadline)
        self.inventory = {}

    def current(self):
        try:
            ref = json.loads(host.gh(self.deadline, 'api', 'repos/' + host.REPO + '/git/ref/' + MARKER))
        except host.GitHubCommandError as error:
            if missing(error):
                return None
            raise
        source = ref['object']['sha']
        if ref['ref'] != 'refs/' + MARKER or not re.fullmatch('[0-9a-f]{40}', source):
            raise ValueError('invalid published module cache ref')
        return source

    def api(self, path, method='GET', **fields):
        return self.http.api(path, method=method, payload=json_body(fields) if fields else None,
                             headers={'Content-Type': 'application/json'} if fields else None)

    def catalog(self, tag, *, refresh=False, create=False):
        if not TAG.fullmatch(tag):
            raise ValueError('invalid module cache release tag')
        if tag in self.inventory and not refresh and (self.inventory[tag] or not create):
            return self.inventory[tag]
        try:
            release = self.api('releases/tags/' + tag)
        except host.GitHubCommandError as error:
            if not missing(error):
                raise
            if not create:
                self.inventory[tag] = None
                return None
            try:
                release = self.api('releases', 'POST', tag_name=tag, name=tag, prerelease=True,
                                   target_commitish=TAG.fullmatch(tag)[1], make_latest='false')
            except host.GitHubCommandError:
                release = self.api('releases/tags/' + tag)
        if release['tag_name'] != tag or release['draft']:
            raise ValueError('unexpected module Release: ' + tag)
        # GitHub paginates the asset endpoint independently from release metadata.
        assets = {}
        page = 1
        while True:
            subset = self.api(f'releases/{release["id"]}/assets?per_page=100&page={page}')
            assets.update({row['name']: row for row in subset})
            if len(subset) < 100:
                break
            page += 1
        entry = release['id'], assets
        self.inventory[tag] = entry
        return entry

    @cache
    def tags(self, current, base):
        """Search unfinished versions up to the most recent completed base, including overflow tags."""
        seen = [current]
        query = """query($owner:String!,$name:String!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            refs(refPrefix:"refs/tags/",query:"lean-modules-",first:100,after:$cursor,
                 orderBy:{field:TAG_COMMIT_DATE,direction:DESC}) {
              nodes{name} pageInfo{endCursor hasNextPage}
            }
          }
        }"""
        owner, name = host.REPO.split('/')
        values = ['-f', 'owner=' + owner, '-f', 'name=' + name]
        cursor = []
        while True:
            response = host.gh(self.deadline, 'api', 'graphql', '-f', 'query=' + query, *values, *cursor)
            refs = json.loads(response)['data']['repository']['refs']
            for row in refs['nodes']:
                tag = row['name']
                if tag == base:
                    return tuple(seen)
                if TAG.fullmatch(tag) and tag not in seen:
                    seen.append(tag)
            if not refs['pageInfo']['hasNextPage']:
                return tuple(seen)
            cursor = ['-f', 'cursor=' + refs['pageInfo']['endCursor']]

    def candidates(self, plan):
        # Current-version writes by sibling jobs may have happened since planning.
        tags = list(self.tags(plan['version'], plan.get('base_version')))
        n = 2
        while self.catalog(f'{plan["version"]}-part-{n}', refresh=True):
            tag = f'{plan["version"]}-part-{n}'
            if tag not in tags:
                tags.append(tag)
            n += 1
        for tag in tags:
            entry = self.catalog(tag, refresh=tag.startswith(plan['version']))
            if entry:
                yield tag, entry[1]

    def actions(self, operation, key, digest, path):
        if os.environ.get('LEAN_MODULE_ACTIONS_CACHE') != '1':
            return False
        cmd = ['node', self.root / '.github/actions/lean-cache-shards/run.mjs',
               'module-' + operation, key, digest, path.resolve()]
        result = subprocess.run(cmd, cwd=self.root, timeout=host.remaining(self.deadline), check=False)
        if result.returncode and operation == 'put':
            raise ValueError('failed to verify Actions cache write: ' + key)
        return result.returncode == 0

    def fetch(self, tag, asset, path):
        digest = (asset.get('digest') or '').removeprefix('sha256:')
        if asset.get('state') != 'uploaded' or not valid_digest(digest):
            raise ValueError('incomplete Release asset: ' + asset['name'])
        with path.open('wb') as output:
            for block in self.http.asset(asset, tag):
                output.write(block)
        if path.stat().st_size != asset['size'] or host.archive_sha(path, self.deadline) != digest:
            raise ValueError('Release asset readback differs from metadata')

    def confirmed(self, rows):
        confirmed = {}
        for name, row in rows.items():
            catalog = self.catalog(row['version'])
            asset = catalog[1].get(row['asset']) if catalog else None
            if (asset and asset['state'] == 'uploaded' and asset['size'] == row['bytes']
                    and asset['digest'] == 'sha256:' + row['sha256'] and asset['label'] == row['fingerprint']):
                confirmed[name] = row
        return confirmed

    def download(self, row, path):
        digest = row['sha256']
        if (self.actions('get', row['key'], digest, path) and path.is_file()
                and path.stat().st_size == row['bytes'] and host.archive_sha(path, self.deadline) == digest):
            return
        entry = self.catalog(row['version'])
        asset = entry[1].get(row['asset']) if entry else None
        if (not asset or asset['size'] != row['bytes'] or asset['digest'] != 'sha256:' + digest
                or asset['label'] != row['fingerprint']):
            raise ValueError('versioned shard metadata differs from index: ' + row['asset'])
        self.fetch(row['version'], asset, path)

    def stage(self, version, name, path, label, digest, *, index=False):
        publisher()
        for part in range(1, 10000):
            tag = version if part == 1 else f'{version}-part-{part}'
            release, assets = self.catalog(tag, create=True)
            existing = assets.get(name)
            if not existing and not index and len(assets) >= CAPACITY:
                continue
            if not existing:
                params = urlencode({'name': name, 'label': label})
                url = f'https://uploads.github.com/repos/{host.REPO}/releases/{release}/assets?{params}'
                try:
                    with self.http.request(url, method='POST', path=path) as response:
                        existing = json.load(response)
                except host.GitHubCommandError:
                    _, assets = self.catalog(tag, refresh=True)
                    existing = assets.get(name)
                    if not existing and not index and len(assets) >= CAPACITY:
                        continue
                    if not existing:
                        raise
            if existing['state'] == 'starter':
                if not index:
                    continue
                # Only the exclusive finalizer writes index.json; recover an interrupted empty reservation.
                if existing['size'] or existing['name'] != INDEX:
                    raise ValueError('invalid incomplete module index')
                url = f'https://api.github.com/repos/{host.REPO}/releases/assets/{existing["id"]}'
                with self.http.request(url, method='DELETE'):
                    pass
                assets.pop(INDEX)
                return self.stage(version, name, path, label, digest, index=True)
            if (existing['state'] != 'uploaded' or existing['digest'] != 'sha256:' + digest
                    or existing['size'] != path.stat().st_size or existing['label'] != label):
                raise ValueError('immutable Release asset conflicts: ' + name)
            assets[name] = existing
            return tag, existing
        raise ValueError('module Release asset capacity exhausted')

    def latest(self, version=None):
        if version is None:
            source = self.current()
            if source is None:
                return None
            version = PREFIX + source
        if not re.fullmatch(PREFIX + '[0-9a-f]{40}', version):
            raise ValueError('invalid module cache version')
        catalog = self.catalog(version)
        asset = catalog[1].get(INDEX) if catalog else None
        if not asset or not 0 < asset['size'] <= MAX_INDEX:
            raise ValueError('missing complete module version index: ' + version)
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / INDEX
            digest = (asset.get('digest') or '').removeprefix('sha256:')
            key = 'lean-module-index-' + version.removeprefix(PREFIX)
            if not (valid_digest(digest) and self.actions('get', key, digest, path) and path.is_file()
                    and path.stat().st_size == asset['size'] and host.archive_sha(path, self.deadline) == digest):
                self.fetch(version, asset, path)
            data = read(path)
        if (data['schema'] != 'lean-module-shards-v1' or data['version'] != version
                or data['source'] != TAG.fullmatch(version)[1]):
            raise ValueError('module index identity differs from its version')
        return data

    def partials(self, plan):
        expected = {row['asset']: (name, row) for name, row in plan['shards'].items()}
        result = {}
        for tag, assets in self.candidates(plan):
            for name in expected.keys() & assets.keys():
                shard, row = expected[name]
                asset = assets[name]
                digest = (asset.get('digest') or '').removeprefix('sha256:')
                if (asset.get('state') != 'uploaded' or not valid_digest(digest)
                        or asset.get('label') != row['fingerprint'] or asset['size'] <= 0):
                    continue
                candidate = {**row, 'sha256': digest, 'bytes': asset['size'], 'version': tag}
                if result.setdefault(shard, candidate)['sha256'] != digest:
                    raise ValueError('conflicting partials for module shard: ' + shard)
        return result

    def save(self, plan, name, path, row):
        if not TAG.fullmatch(plan['version']) or plan['shards'][name]['fingerprint'] != row['fingerprint']:
            raise ValueError('shard does not belong to its plan')
        publisher()
        # Only a durable Release asset with a verified Actions write is advertised as a reusable shard.
        self.actions('put', row['key'], row['sha256'], path)
        tag, _ = self.stage(plan['version'], row['asset'], path, row['fingerprint'], row['sha256'])
        return {**row, 'version': tag}

    def splits(self, plan):
        result = {}
        expected = {'split-' + row['fingerprint'] + '.json': name for name, row in plan['shards'].items()}
        for tag, assets in self.candidates(plan):
            for filename in expected.keys() & assets.keys():
                group = expected[filename]
                if group in result:
                    continue
                asset = assets[filename]
                fingerprint = plan['shards'][group]['fingerprint']
                if asset.get('state') != 'uploaded' or asset.get('label') != fingerprint or not 0 < asset['size'] <= MAX_INDEX:
                    continue
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / filename
                    self.fetch(tag, asset, path)
                    split = read(path)
                contents = [name for members in split['layout'].values() for name in members]
                if (split['fingerprint'] != fingerprint or split['group'] != group
                        or sorted(contents) != sorted(plan['shards'][group]['members'])):
                    raise ValueError('split members differ from planned shard')
                result[group] = split['layout']
        return result

    def save_split(self, plan, group, layout):
        fingerprint = plan['shards'][group]['fingerprint']
        data = json_body({'group': group, 'fingerprint': fingerprint, 'layout': layout})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'split.json'
            path.write_bytes(data)
            self.stage(plan['version'], 'split-' + fingerprint + '.json', path, fingerprint, hashlib.sha256(data).hexdigest())

    def publish(self, index):
        publisher()
        source = index['source']
        if index['version'] != PREFIX + source or not re.fullmatch('[0-9a-f]{40}', source):
            raise ValueError('invalid module version for publication')
        self.inventory.clear()
        if self.confirmed(index['shards']).keys() != index['shards'].keys():
            raise ValueError('complete module index references an unavailable shard')
        data = json_body(index)
        digest = hashlib.sha256(data).hexdigest()
        current = self.current()
        if current != source and current:
            comparison = self.api(f'compare/{current}...{source}?per_page=1')
            if comparison['merge_base_commit']['sha'] != current:
                raise ValueError('stale module cache publisher')
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / INDEX
            path.write_bytes(data)
            version, asset = self.stage(index['version'], INDEX, path, digest, digest, index=True)
            readback = Path(stage) / 'readback'
            self.fetch(version, asset, readback)
            key = 'lean-module-index-' + source
            self.actions('put', key, digest, path)
        if current is None:
            self.api('git/refs', 'POST', ref='refs/' + MARKER, sha=source)
        elif current != source:
            self.api('git/refs/' + MARKER, 'PATCH', sha=source, force=False)
