"""Release shards shared by immutable version indexes and their Actions cache copies."""

from collections import Counter
from functools import cache
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode

from lean_cache import publication_mathlib
import lean_cache_release as host
from lean_generation_checkpoints import publisher
from lean_generation_index import MAX_INDEX, read
from lean_generation_objects import GitHubHTTP
from lean_release_index import valid_digest

PREFIX = 'lean-modules-'
MARKER = 'heads/lean-modules-published'
INDEX = 'index.json'
LAYOUT = 'publication-layout.json'
COMPILER_PLAN = 'compiler-plan-v1.json'
PROMOTED = 'published.json'
TAG = re.compile(r'lean-modules-([0-9a-f]{40})(-report)?(?:-part-([2-9]|[1-9][0-9]+))?')
CAPACITY = 900
UNIT_PART_START = 10000000
ACTIONS_CACHE = 'actions-cache'
UNIT_ASSET = re.compile(r'compiler-unit-([0-9a-f]{64})\.tar\.gz')
CLAIM_GC_SECONDS = 30
CLAIM_GC_LIMIT = 2048
CLAIM_GC_BATCH = 128
UNIT_RECEIPT = r'done/([0-9a-f]{64})/([0-9a-f]{64})-([1-9][0-9]{0,15})(?:-part-([1-9][0-9]*))?'
UNIT_CATALOG = re.compile(r'compiler-units-([0-9a-f]{64})\.json')


class CompilerCacheMiss(FileNotFoundError):
    """The exact compiler cache entry was evicted after discovery."""


def json_body(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def split_filename(group, fingerprint):
    identity = hashlib.sha256(json_body([group, fingerprint])).hexdigest()
    return 'split-' + identity + '.json'


def release_page(index, checkout_version=None):
    source = index['source']
    tag = index['version']
    repo = 'https://github.com/' + host.REPO
    shards = index['shards']
    current = []
    historical = Counter()
    version = checkout_version or ''
    for name, row in sorted(shards.items()):
        owner = row['version']
        if owner == tag or owner.startswith(tag + '-part-'):
            current.append(row)
        else:
            historical[owner] += 1
        # Historical indexes can address assets by their original mathlib commit.
        prefix = row['asset'].removesuffix('-report').removesuffix('-' + name + '.tar.gz')
        if re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', prefix):
            version = prefix

    product = 'Lean 模块报告' if index.get('kind') == 'report' else 'Lean 编译缓存'
    title = product + (f' · mathlib {version}' if version else '') + ' · ' + source[:8]
    index_url = f'{repo}/releases/download/{tag}/{INDEX}'
    lines = [f'[下载完整索引（index.json）]({index_url}) · [查看源码（{source[:8]}）]({repo}/tree/{source})', '',
             f'覆盖 **{len(index["targets"]):,}** 个模块，共 **{len(shards):,}** 个分片。'
             f'本版发布 **{len(current):,}** 个，复用历史分片 **{sum(historical.values()):,}** 个。']
    if current:
        lines.extend(['', '### 本版下载', '', '| 文件 | 模块数 | 压缩大小 |', '| --- | ---: | ---: |'])
        for row in current[:32]:
            asset = row['asset']
            url = f'{repo}/releases/download/{row["version"]}/{quote(asset)}'
            lines.append(f'| [{asset}]({url}) | {len(row["members"]):,} | {row["bytes"] / 1024**2:,.2f} MiB |')
        if len(current) > 32:
            lines.extend(['', f'其余 {len(current) - 32:,} 个本版分片可通过[完整索引]({index_url})定位。'])
    if historical:
        lines.extend(['', '### 历史分片来源', '', '| Release | 复用分片数 |', '| --- | ---: |'])
        for owner, count in historical.most_common(16):
            parts = TAG.fullmatch(owner)
            label = parts[1][:8] + (f' / part {parts[3]}' if parts[3] else '')
            lines.append(f'| [{label}]({repo}/releases/tag/{owner}) | {count:,} |')
        if len(historical) > 16:
            lines.extend(['', f'其余 {len(historical) - 16:,} 个来源见[完整索引]({index_url})。'])
    lines.extend(['', '按模块下载时，在索引中查 `targets[模块].shard`，'
                  '再用 `shards[分片].version` 和 `.asset` 定位文件。'])
    return title, '\n'.join(lines)


def missing(error):
    try:
        return str(json.loads(error.output)['status']) == '404'
    except (KeyError, TypeError, ValueError):
        return bool(re.search(r'\bHTTP 404\b', error.stderr or ''))


class Remote:
    def __init__(self, root, deadline=None, *, lane=None, lanes=1, kind='compiler'):
        if lane is not None and not 0 <= lane < lanes <= 256:
            raise ValueError('invalid module Release lane')
        if kind not in ('compiler', 'report'):
            raise ValueError('invalid module Release kind')
        self.kind = kind
        self.suffix = '-report' if kind == 'report' else ''
        self.marker = MARKER + self.suffix
        self.lane = lane
        self.lanes = lanes
        self.root = root
        self.deadline = deadline if deadline is not None else host.operation_deadline()
        self.http = GitHubHTTP(self.deadline)
        self.next_job_check = 0
        self.progress_interval = 60
        self.unit_entries = {}
        self.next_progress = 0
        self.claims = {}
        self.inventory = {}
        self.release_pages = {}

    def accepts_tag(self, tag):
        match = TAG.fullmatch(tag)
        return match is not None and (match[2] or '') == self.suffix

    def version(self, source):
        return PREFIX + source + self.suffix

    def current(self):
        try:
            ref = json.loads(host.gh(self.deadline, 'api', 'repos/' + host.REPO + '/git/ref/' + self.marker))
        except host.GitHubCommandError as error:
            if missing(error):
                return None
            raise
        source = ref['object']['sha']
        if ref['ref'] != 'refs/' + self.marker or not re.fullmatch('[0-9a-f]{40}', source):
            raise ValueError('invalid published module cache ref')
        return source

    def api(self, path, method='GET', **fields):
        return self.http.api(path, method=method, payload=json_body(fields) if fields else None,
                             headers={'Content-Type': 'application/json'} if fields else None)

    def catalog(self, tag, *, refresh=False, create=False):
        if not self.accepts_tag(tag):
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
        assets = {row['name']: row for row in release['assets']}
        self.release_pages[tag] = release.get('name'), release.get('body')
        entry = release['id'], assets
        self.inventory[tag] = entry
        return entry

    def present(self, index):
        tag = index['version']
        release, _ = self.catalog(tag)
        version = None
        if (self.root / 'lake-manifest.json').is_file():
            revision, checkout_version = publication_mathlib(self.root)
            if revision == index['mathlib']:
                version = checkout_version
        title, body = release_page(index, version)
        if self.release_pages[tag] != (title, body):
            updated = self.api(f'releases/{release}', 'PATCH', name=title, body=body)
            if (updated['name'], updated['body']) != (title, body):
                raise ValueError('module Release page update failed: ' + tag)
            self.release_pages[tag] = title, body

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
                if self.accepts_tag(tag) and tag not in seen:
                    seen.append(tag)
            if not refs['pageInfo']['hasNextPage']:
                return tuple(seen)
            cursor = ['-f', 'cursor=' + refs['pageInfo']['endCursor']]

    def candidates(self, plan, *, include_units=False):
        # Current-version writes by sibling jobs may have happened since planning.
        tags = [plan['version']] if plan.get('source_only') else list(self.tags(plan['version'], plan.get('base_version')))
        prefix = 'tags/' + plan['version'] + '-part-'
        for row in self.api('git/matching-refs/' + prefix):
            tag = row['ref'].removeprefix('refs/tags/')
            if self.accepts_tag(tag) and tag.startswith(plan['version'] + '-part-') and tag not in tags:
                tags.append(tag)
        for tag in tags:
            part = TAG.fullmatch(tag)[3]
            if part and int(part) >= UNIT_PART_START and not include_units:
                continue
            entry = self.catalog(tag, refresh=tag.startswith(plan['version']))
            if entry:
                yield tag, entry[1]

    def actions(self, operation, key, digest, path):
        if os.environ.get('LEAN_MODULE_ACTIONS_CACHE') != '1':
            return False
        cmd = ['node', self.root / '.github/actions/lean-cache-shards/run.mjs',
               'module-' + operation, key, digest, path.resolve()]
        try:
            result = subprocess.run(cmd, cwd=self.root, timeout=host.remaining(self.deadline), check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f'Actions cache {operation} unavailable for {key}: {error}', file=sys.stderr)
            return False
        if result.returncode and (operation == 'put' or result.returncode != 4):
            print(f'Actions cache {operation} unavailable for {key} (exit {result.returncode})', file=sys.stderr)
        return result.returncode == 0

    def compiler_cache(self, operation, kind, *args):
        cmd = ['node', self.root / '.github/actions/lean-cache-shards/run.mjs',
               'compiler-cache-' + operation, kind, *args]
        # Inventory is metadata. Archives can take longer on a throttled runner.
        budget = 120 if operation == 'list' else min(1800, 120 + int(args[2]) / 524288)
        result = subprocess.run(cmd, cwd=self.root, timeout=min(budget, host.remaining(self.deadline)))
        if result.returncode == 4 and operation == 'get':
            return False
        result.check_returncode()
        return True

    def cache_entries(self, kind):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'entries.json'
            self.compiler_cache('list', kind, path.resolve())
            entries = read(path)
        if not isinstance(entries, list):
            raise ValueError('invalid compiler cache inventory')
        rows = {}
        for entry in entries:
            key, sha, size = entry['id'], entry['sha256'], entry['bytes']
            if not valid_digest(key) or not valid_digest(sha) or type(size) is not int or size <= 0:
                raise ValueError('invalid compiler cache address')
            if kind == 'catalog' and (key != sha or size > MAX_INDEX):
                raise ValueError('invalid compiler cache catalog')
            # Multiple verified builds may have different archive bytes for the same inputs.
            rows.setdefault(key, dict(version=ACTIONS_CACHE, sha256=sha, bytes=size))
        return rows

    def get_compiler_cache(self, kind, key, row, path):
        if not self.compiler_cache('get', kind, key, row['sha256'], str(row['bytes']), path.resolve()):
            raise CompilerCacheMiss(key)
        if path.stat().st_size != row['bytes'] or host.archive_sha(path, self.deadline) != row['sha256']:
            raise ValueError('compiler cache payload differs from its address: ' + key)

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
                    and asset['digest'] == 'sha256:' + row['sha256']):
                confirmed[name] = row
        return confirmed

    def download(self, row, path):
        digest = row['sha256']
        if row['version'] == ACTIONS_CACHE:
            if not UNIT_ASSET.fullmatch(row['asset']) or row['asset'] != f'compiler-unit-{row["fingerprint"]}.tar.gz':
                raise ValueError('invalid compiler cache unit identity')
            self.get_compiler_cache('unit', row['fingerprint'], row, path)
            return
        if (self.actions('get', row['key'], digest, path) and path.is_file()
                and path.stat().st_size == row['bytes'] and host.archive_sha(path, self.deadline) == digest):
            return
        entry = self.catalog(row['version'])
        asset = entry[1].get(row['asset']) if entry else None
        if (not asset or asset['size'] != row['bytes'] or asset['digest'] != 'sha256:' + digest
                or asset.get('state') != 'uploaded'):
            raise ValueError('versioned shard metadata differs from index: ' + row['asset'])
        self.fetch(row['version'], asset, path)

    def stage(self, version, name, path, digest):
        if UNIT_ASSET.fullmatch(name) or UNIT_CATALOG.fullmatch(name):
            raise ValueError('temporary compiler units belong in Actions cache')
        publisher(self.kind)
        primary_metadata = name in (INDEX, LAYOUT, PROMOTED, COMPILER_PLAN)
        # The planner owns the shared Release. Each worker alone creates its overflow parts.
        first_overflow = 2 + (self.lane or 0)
        parts = chain((1,), range(first_overflow, first_overflow + 9998 * self.lanes, self.lanes))
        for part in parts:
            tag = version if part == 1 else f'{version}-part-{part}'
            catalog = self.catalog(tag, create=self.lane is None or part != 1)
            if catalog is None:
                raise ValueError('planner did not prepare module Release: ' + tag)
            release, assets = catalog
            existing = assets.get(name)
            if not existing and not primary_metadata and len(assets) >= CAPACITY:
                continue
            if not existing:
                params = urlencode({'name': name})
                url = f'https://uploads.github.com/repos/{host.REPO}/releases/{release}/assets?{params}'
                try:
                    with self.http.request(url, method='POST', path=path) as response:
                        existing = json.load(response)
                except host.GitHubCommandError:
                    _, assets = self.catalog(tag, refresh=True)
                    existing = assets.get(name)
                    if not existing and not primary_metadata and len(assets) >= CAPACITY:
                        continue
                    if not existing:
                        raise
            if existing['state'] == 'starter':
                if not primary_metadata:
                    continue
                # The exclusive planner and finalizer can recover their own interrupted reservations.
                if existing['size'] or existing['name'] != name:
                    raise ValueError('invalid incomplete module Release metadata: ' + name)
                url = f'https://api.github.com/repos/{host.REPO}/releases/assets/{existing["id"]}'
                with self.http.request(url, method='DELETE'):
                    pass
                assets.pop(name)
                return self.stage(version, name, path, digest)
            if (existing['state'] != 'uploaded' or existing['digest'] != 'sha256:' + digest
                    or existing['size'] != path.stat().st_size):
                raise ValueError('immutable Release asset conflicts: ' + name)
            assets[name] = existing
            return tag, existing
        raise ValueError('module Release asset capacity exhausted')

    def latest(self, version=None):
        if version is None:
            source = self.current()
            if source is None:
                return None
            version = self.version(source)
        if not self.accepts_tag(version) or TAG.fullmatch(version)[3]:
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
                or data['source'] != TAG.fullmatch(version)[1]
                or data.get('kind', 'compiler') != self.kind):
            raise ValueError('module index identity differs from its version')
        return data

    def published(self, version):
        """Read a promoted source after the rolling marker has already advanced."""
        if not self.accepts_tag(version) or TAG.fullmatch(version)[3]:
            raise ValueError('invalid requested publication version')
        current = self.current()
        if not current:
            return None
        if version == self.version(current):
            return self.latest(version)
        catalog = self.catalog(version, refresh=True)
        promotion = catalog[1].get(PROMOTED) if catalog else None
        index = catalog[1].get(INDEX) if catalog else None
        if not index or not promotion or promotion['state'] != 'uploaded':
            return None
        evidence = json_body({'index_digest': index['digest'], 'version': version})
        if promotion['digest'] != 'sha256:' + hashlib.sha256(evidence).hexdigest():
            raise ValueError('module promotion differs from release index')
        return self.latest(version)

    def record_promotion(self, index):
        data = json_body(index)
        evidence = json_body({'index_digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                              'version': index['version']})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / PROMOTED
            path.write_bytes(evidence)
            self.stage(index['version'], PROMOTED, path, hashlib.sha256(evidence).hexdigest())

    def matches_checkpoint(self, tag, asset, fingerprint):
        if asset.get('label'):
            return asset['label'] == fingerprint
        # RFC 1952 FNAME follows the ten-byte fixed header in archives produced by pack_shard.
        size = 10 + 64 + 1
        if asset['size'] < size:
            return False
        header = b''.join(self.http.asset(asset, tag, 0, size))
        return header[:4] == b'\x1f\x8b\x08\x08' and header[10:] == fingerprint.encode('ascii') + b'\0'

    def partials(self, plan):
        expected = {}
        for name, row in plan['shards'].items():
            expected[row['asset']] = (name, row)
            if self.kind == 'report':
                continue
            legacy_key = plan['mathlib'] + '-' + name
            expected.setdefault(legacy_key + '.tar.gz', (name, dict(row, key=legacy_key, asset=legacy_key + '.tar.gz')))
        result = {}
        for tag, assets in self.candidates(plan):
            for name in expected.keys() & assets.keys():
                shard, row = expected[name]
                asset = assets[name]
                digest = (asset.get('digest') or '').removeprefix('sha256:')
                if (asset.get('state') != 'uploaded' or not valid_digest(digest)
                        or asset['size'] <= 0 or not self.matches_checkpoint(tag, asset, row['fingerprint'])):
                    continue
                candidate = {**row, 'sha256': digest, 'bytes': asset['size'], 'version': tag}
                if result.setdefault(shard, candidate)['sha256'] != digest:
                    raise ValueError('conflicting partials for module shard: ' + shard)
        return result

    def save(self, plan, name, path, row):
        if not self.accepts_tag(plan['version']) or plan['shards'][name]['fingerprint'] != row['fingerprint']:
            raise ValueError('shard does not belong to its plan')
        publisher(self.kind)
        if path.stat().st_size != row['bytes'] or host.archive_sha(path, self.deadline) != row['sha256']:
            raise ValueError('local shard differs from its advertised payload')
        if match := UNIT_ASSET.fullmatch(row['asset']):
            if self.kind != 'compiler' or match[1] != name or name != row['fingerprint']:
                raise ValueError('invalid compiler unit identity')
            self.compiler_cache('put', 'unit', name, row['sha256'], str(row['bytes']), path.resolve())
            saved = dict(row, version=ACTIONS_CACHE)
            self.unit_entries[name] = dict(version=ACTIONS_CACHE, bytes=row['bytes'], sha256=row['sha256'])
            return saved
        tag, _ = self.stage(plan['version'], row['asset'], path, row['sha256'])
        self.actions('put', row['key'], row['sha256'], path)
        return {**row, 'version': tag}

    def compiler_seeds(self, plan):
        from lean_module_dag import recovery_index
        catalogs, assets = [], {}
        for key, row in self.cache_entries('unit').items():
            asset = dict(digest='sha256:' + row['sha256'], size=row['bytes'], state='uploaded')
            assets[f'compiler-unit-{key}.tar.gz'] = ACTIONS_CACHE, asset
        if assets:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'catalog.json'
                for key, row in self.cache_entries('catalog').items():
                    try:
                        self.get_compiler_cache('catalog', key, row, path)
                    except CompilerCacheMiss:
                        continue
                    catalogs.append(read(path))
        for tag, entries in self.candidates(dict(plan, source_only=False), include_units=True):
            for name, asset in entries.items():
                if UNIT_ASSET.fullmatch(name) and asset['state'] == 'uploaded':
                    assets.setdefault(name, (tag, asset))
                if UNIT_CATALOG.fullmatch(name) and asset['state'] == 'uploaded':
                    if not 0 < asset['size'] <= MAX_INDEX or name[15:-5] != asset['digest'].removeprefix('sha256:'):
                        raise ValueError('invalid compiler unit catalog')
                    with tempfile.TemporaryDirectory() as temporary:
                        path = Path(temporary) / 'catalog.json'
                        self.fetch(tag, asset, path)
                        catalogs.append(read(path))
        return recovery_index(plan, catalogs, assets)

    def save_compiler_catalog(self, version, catalog):
        if self.kind != 'compiler' or not self.accepts_tag(version):
            raise ValueError('invalid compiler catalog version')
        publisher('compiler')
        data = json_body(catalog)
        if len(data) > MAX_INDEX:
            raise ValueError('compiler unit catalog exceeds release metadata limit')
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / 'catalog.json'
            path.write_bytes(data)
            self.compiler_cache('put', 'catalog', sha, sha, str(len(data)), path.resolve())

    def compiler_progress(self, plan, pending):
        if self.next_progress == 0:
            # Cross-run checkpoints already exist at startup. This attempt's producers
            # subsequently advertise their verified exact addresses in their receipts.
            self.unit_entries.update(self.cache_entries('unit'))
        if time.monotonic() >= self.next_progress:
            prefix = self.claim_prefix(plan['source'])
            for entry in self.api('git/matching-refs/' + prefix.removeprefix('refs/')):
                ref = entry['ref']
                if not ref.startswith(prefix):
                    raise ValueError('unexpected compiler claim ref')
                target = entry['object']['sha']
                suffix = ref[len(prefix):]
                if receipt := re.fullmatch(UNIT_RECEIPT, suffix):
                    if target != plan['source']:
                        raise ValueError('invalid compiler completion receipt')
                    key, sha, size, part = receipt.groups()
                    address = plan['version'] + '-part-' + part if part else ACTIONS_CACHE
                    self.unit_entries[key] = dict(version=address, sha256=sha, bytes=int(size))
                elif re.fullmatch(r'unit/[0-9a-f]{64}', suffix):
                    self.claims[ref] = target
            self.progress_interval = max(10, min(120, 2 * len(plan['lanes'])))
            self.next_progress = time.monotonic() + self.progress_interval
        return {k: dict(plan['shards'][k], **self.unit_entries[k]) for k in pending & self.unit_entries.keys()}

    def retry_checkpoint(self, plan, key):
        row = plan['shards'][key]
        if key in self.unit_entries:
            return dict(row, **self.unit_entries[key])
        if 'part' not in row:
            return None
        # Old run plans addressed interrupted uploads by their Release part.
        # A fixed download address can become available before both inventories.
        # An interrupted name can overflow into a deterministic sibling part.
        for offset in range(8):
            tag = f'{plan["version"]}-part-{row["part"] + offset}'
            url = f'https://github.com/{host.REPO}/releases/download/{tag}/{row["asset"]}'
            try:
                with self.http.request(url, method='HEAD', redirect=True, authenticate=False) as response:
                    if response.status not in (200, 302):
                        raise ValueError('unexpected compiler checkpoint status')
            except host.GitHubCommandError as error:
                if missing(error):
                    continue
                raise
            return self.resolve_checkpoint(dict(row, version=tag))
        return None

    def resolve_checkpoint(self, row):
        tag = row['version']
        catalog = self.catalog(tag)
        delay = 0
        while True:
            asset = catalog[1].get(row['asset']) if catalog else None
            if asset and asset['state'] == 'uploaded':
                sha = (asset.get('digest') or '').removeprefix('sha256:')
                if asset['size'] <= 0 or not valid_digest(sha) or sha != row.get('public_digest', sha):
                    raise ValueError('compiler progress differs from immutable release metadata')
                return dict(row, bytes=asset['size'], sha256=sha)
            # The public directory can announce an asset before the REST read
            # replica sees the upload. Reconcile within this worker's deadline.
            if delay:
                time.sleep(min(delay, host.remaining(self.deadline)))
            delay = min(30, max(2, delay * 2))
            catalog = self.catalog(tag, refresh=True)

    @cache
    def claim_prefix(self, source):
        run = publisher('compiler')
        if not re.fullmatch(r'[0-9a-f]{40}', source) or not all(
                re.fullmatch(r'[0-9]+', run[k]) for k in ('run', 'attempt')):
            raise ValueError('invalid compiler job identity')
        return f'refs/tags/lean-unit-claims/{source}/{run["run"]}/{run["attempt"]}/'

    @cache
    def claim_identity(self, source, lane):
        # Distinguish a lost successful POST from a competing worker's reservation.
        tag_name = self.claim_prefix(source).removeprefix('refs/tags/') + str(lane)
        tag = self.api('git/tags', 'POST', tag=tag_name, message='Compiler unit worker',
                       object=source, type='commit')
        return tag['sha']

    def compiler_claim(self, ref, owner):
        if ref not in self.claims:
            try:
                self.api('git/refs', 'POST', ref=ref, sha=owner)
                self.claims[ref] = owner
            except host.GitHubCommandError:
                # A lost successful POST and a competing worker are distinguished
                # by the target of the exact, immutable ref.
                for delay in (0, 1, 2, 4, 8):
                    if delay:
                        time.sleep(min(delay, host.remaining(self.deadline)))
                    try:
                        entry = self.api('git/ref/' + ref.removeprefix('refs/'))
                    except host.GitHubCommandError as error:
                        if missing(error) and delay != 8:
                            continue
                        raise
                    if entry['ref'] != ref:
                        raise ValueError('unexpected compiler claim ref')
                    self.claims[ref] = entry['object']['sha']
                    break
        return self.claims[ref]

    def claim_compiler_unit(self, plan, lane, ordered):
        if not ordered:
            return None
        prefix = self.claim_prefix(plan['source']) + 'unit/'
        owner = self.claim_identity(plan['source'], lane)
        collisions = 0
        for key in ordered:
            ref = prefix + key
            if ref in self.claims:
                if self.claims[ref] == owner:
                    return key
                continue
            if self.compiler_claim(ref, owner) == owner:
                return key
            # Concurrent workers may have claimed ready units since the bulk read.
            # Refresh the attempt inventory before making more contested writes.
            collisions += 1
            if collisions == 2:
                self.next_progress = time.monotonic()
                break
        return None

    def complete_compiler_unit(self, plan, lane, key, row):
        source = plan['source']
        prefix = self.claim_prefix(source)
        if self.claims.get(prefix + 'unit/' + key) != self.claim_identity(source, lane):
            raise ValueError('only the compiler unit owner can record its completion')
        receipt = f'done/{key}/{row["sha256"]}-{row["bytes"]}'
        if row['version'] != ACTIONS_CACHE:
            tag = TAG.fullmatch(row['version'])
            if not tag or not tag[3] or row['version'] != f'{plan["version"]}-part-{tag[3]}':
                raise ValueError('invalid historical compiler checkpoint version')
            receipt += '-part-' + tag[3]
        if not re.fullmatch(UNIT_RECEIPT, receipt) or self.compiler_claim(prefix + receipt, source) != source:
            raise ValueError('invalid compiler completion receipt')

    def wait_compiler_progress(self, plan):
        # A dead runner cannot release its reservation. Failed-job reruns use a fresh attempt.
        if time.monotonic() >= self.next_job_check:
            run = publisher('compiler')
            for page in range(1, 5):
                jobs = self.api(f'actions/runs/{run["run"]}/attempts/{run["attempt"]}/jobs?per_page=100&page={page}')['jobs']
                if any(job['conclusion'] in ('cancelled', 'failure', 'timed_out') for job in jobs):
                    raise RuntimeError('compiler job failed; completed unit checkpoints are available for retry')
                if len(jobs) < 100:
                    break
            self.next_job_check = time.monotonic() + max(300, self.progress_interval * 2)
        delay = max(0, self.next_progress - time.monotonic())
        time.sleep(min(delay, host.remaining(self.deadline)))

    def compiler_claims_for_cleanup(self, source, run, deadline):
        if source is not None and not re.fullmatch('[0-9a-f]{40}', source):
            raise ValueError('invalid compiler cleanup source')
        if not all(isinstance(run.get(k), str) and re.fullmatch('[0-9]+', run[k]) for k in ('run', 'attempt')):
            raise ValueError('invalid compiler cleanup attempt')
        prefix = 'refs/tags/lean-unit-claims/' + (source + '/' if source else '')
        path = 'repos/' + host.REPO + '/git/matching-refs/' + prefix.removeprefix('refs/')
        entries = json.loads(host.gh(deadline, 'api', path))
        claims = []
        if not isinstance(entries, list):
            raise ValueError('invalid compiler claim inventory')
        for entry in entries:
            ref = entry.get('ref') if isinstance(entry, dict) else None
            if not isinstance(ref, str):
                raise ValueError('unexpected compiler claim ref')
            match = re.fullmatch(r'refs/tags/lean-unit-claims/([0-9a-f]{40})/([0-9]+)/([0-9]+)/(?:[0-9]+|unit/[0-9a-f]{64}|' + UNIT_RECEIPT + ')', ref)
            if not match or not ref.startswith(prefix):
                raise ValueError('unexpected compiler claim ref')
            if source is None and (match[2], match[3]) == (run['run'], run['attempt']):
                continue
            node = entry.get('node_id')
            obj = entry.get('object')
            oid = obj.get('sha') if isinstance(obj, dict) else None
            if not isinstance(node, str) or not node or not isinstance(oid, str) or not re.fullmatch('[0-9a-f]{40}', oid):
                raise ValueError('invalid compiler claim node or target: ' + ref)
            claims.append((int(match[2]), int(match[3]), ref, oid))
        # The planner drains older attempts first even as new publications add claims.
        return [(ref, oid) for _, _, ref, oid in sorted(claims)]

    def delete_compiler_claim_batch(self, claims, deadline):
        leases = ['--force-with-lease=' + ref + ':' + oid for ref, oid in claims]
        command = ['git', '-C', self.root, '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
                   'push', '--atomic', '--no-follow-tags', *leases, 'https://github.com/' + host.REPO + '.git',
                   *(':' + ref for ref, _ in claims)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=host.remaining(deadline),
                       env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))

    def clear_compiler_claims(self, source=None):
        run = publisher('compiler')
        deleted = 0
        try:
            deadline = time.monotonic() + min(CLAIM_GC_SECONDS, host.remaining(self.deadline))
            claims = self.compiler_claims_for_cleanup(source, run, deadline)
            count = min(CLAIM_GC_LIMIT, len(claims))
            for start in range(0, count, CLAIM_GC_BATCH):
                batch = claims[start:min(count, start + CLAIM_GC_BATCH)]
                self.delete_compiler_claim_batch(batch, deadline)
                deleted += len(batch)
            if count < len(claims):
                print(f'Compiler claim cleanup deferred {len(claims) - count} refs after deleting {deleted}',
                      file=sys.stderr)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            # A failed or unacknowledged push is re-listed by the next publisher.
            # Keep optional maintenance errors inside this operation.
            if isinstance(error, subprocess.TimeoutExpired):
                reason = 'maintenance deadline expired'
            elif isinstance(error, subprocess.CalledProcessError):
                reason = (error.stderr or f'command exited {error.returncode}').strip()
            else:
                reason = str(error)
            print(f'Compiler claim cleanup deferred after {deleted} confirmed deletions: {reason[-500:]}',
                  file=sys.stderr)

    def staged_index(self, version):
        if not self.accepts_tag(version) or TAG.fullmatch(version)[3]:
            raise ValueError('invalid staged module version')
        catalog = self.catalog(version, refresh=True)
        asset = catalog[1].get(INDEX) if catalog else None
        if asset and asset['state'] == 'uploaded':
            return self.latest(version)
        return None

    def publication_layout(self, version, name=LAYOUT):
        catalog = self.catalog(version)
        asset = catalog[1].get(name) if catalog else None
        if not asset or (asset['state'] == 'starter' and asset['size'] == 0):
            return None
        if not 0 < asset['size'] <= MAX_INDEX:
            raise ValueError('invalid module publication layout size')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / name
            self.fetch(version, asset, path)
            return read(path)

    def save_publication_layout(self, version, layout, name=LAYOUT):
        data = json_body(layout)
        if len(data) > MAX_INDEX:
            raise ValueError('invalid module publication layout size')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / name
            path.write_bytes(data)
            self.stage(version, name, path, hashlib.sha256(data).hexdigest())

    def splits(self, plan):
        result = {}
        expected = {}
        for group, row in plan['shards'].items():
            expected[split_filename(group, row['fingerprint'])] = (group, True)
            expected['split-' + row['fingerprint'] + '.json'] = (group, False)
        for tag, assets in self.candidates(plan):
            for filename in sorted(expected.keys() & assets.keys(), key=lambda key: not expected[key][1]):
                group, qualified = expected[filename]
                if group in result:
                    continue
                asset = assets[filename]
                fingerprint = plan['shards'][group]['fingerprint']
                if asset.get('state') != 'uploaded' or not 0 < asset['size'] <= MAX_INDEX:
                    continue
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / filename
                    self.fetch(tag, asset, path)
                    split = read(path)
                contents = [name for members in split['layout'].values() for name in members]
                if split['fingerprint'] != fingerprint or sorted(contents) != sorted(plan['shards'][group]['members']):
                    raise ValueError('split members differ from planned shard')
                if split['group'] != group:
                    owner = split['group']
                    if qualified or not all(n == owner or n.startswith(owner + '.') for n in contents):
                        raise ValueError('split group differs from its address')
                    # Historical filenames identified members only. A new boundary may own the same members.
                    continue
                result[group] = split['layout']
        return result

    def save_split(self, plan, group, layout):
        fingerprint = plan['shards'][group]['fingerprint']
        data = json_body({'group': group, 'fingerprint': fingerprint, 'layout': layout})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'split.json'
            path.write_bytes(data)
            self.stage(plan['version'], split_filename(group, fingerprint), path, hashlib.sha256(data).hexdigest())

    def clear_label(self, asset, fingerprint, size, digest):
        if (not asset or asset.get('label') != fingerprint or asset.get('state') != 'uploaded'
                or asset.get('size') != size or asset.get('digest') != 'sha256:' + digest):
            return
        updated = self.api(f'releases/assets/{asset["id"]}', 'PATCH', label='')
        if (updated.get('label') or any(updated[k] != asset[k] for k in ('id', 'name', 'size', 'digest', 'state'))):
            raise ValueError('Release asset metadata differs after clearing label: ' + asset['name'])
        asset.update(updated)

    def clear_indexed_labels(self, index, *, local_only=False):
        version = index['version']
        local_tags = {version}
        for row in index['shards'].values():
            tag = row['version']
            if tag == version or tag.startswith(version + '-part-'):
                local_tags.add(tag)
            elif local_only:
                continue
            catalog = self.catalog(tag)
            if catalog:
                self.clear_label(catalog[1].get(row['asset']), row['fingerprint'], row['bytes'], row['sha256'])
        for tag in local_tags:
            catalog = self.catalog(tag)
            if not catalog:
                continue
            for name, asset in catalog[1].items():
                fingerprint = asset.get('label')
                if not valid_digest(fingerprint) or name != 'split-' + fingerprint + '.json':
                    continue
                if asset.get('state') != 'uploaded' or not 0 < asset['size'] <= MAX_INDEX:
                    continue
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / 'split.json'
                    self.fetch(tag, asset, path)
                    split = read(path)
                layout = split.get('layout', {})
                if (split.get('fingerprint') != fingerprint or not layout
                        or any(k not in index['shards'] or index['shards'][k]['members'] != v
                               for k, v in layout.items())):
                    continue
                from lean_module_cache import shard_fingerprint
                members = [n for part in layout.values() for n in part]
                if shard_fingerprint(index['mathlib'], index['targets'], members) == fingerprint:
                    self.clear_label(asset, fingerprint, asset['size'], asset['digest'][7:])
        catalog = self.catalog(version)
        if catalog:
            data = json_body(index)
            digest = hashlib.sha256(data).hexdigest()
            self.clear_label(catalog[1].get(INDEX), digest, len(data), digest)

    def stage_index(self, index):
        publisher(self.kind)
        source = index['source']
        if index['version'] != self.version(source) or not re.fullmatch('[0-9a-f]{40}', source):
            raise ValueError('invalid module version for publication')
        self.inventory.clear()
        if self.confirmed(index['shards']).keys() != index['shards'].keys():
            raise ValueError('complete module index references an unavailable shard')
        data = json_body(index)
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / INDEX
            path.write_bytes(data)
            version, asset = self.stage(index['version'], INDEX, path, digest)
            readback = Path(stage) / 'readback'
            self.fetch(version, asset, readback)
            key = 'lean-module-index-' + source + self.suffix
            self.actions('put', key, digest, path)

    def publish(self, index):
        self.stage_index(index)
        self.promote(index['version'])

    def promote(self, version):
        publisher(self.kind)
        if self.published(version) is not None:
            return 'published'
        index = self.staged_index(version)
        if index is None:
            raise ValueError('complete staged module index unavailable: ' + version)
        source = index['source']
        current = self.current()
        if current and current != source:
            comparison = self.api(f'compare/{current}...{source}?per_page=1')
            ancestor = comparison['merge_base_commit']['sha']
            if ancestor == source and comparison.get('status') == 'behind':
                return 'superseded'
            if ancestor != current:
                raise ValueError('divergent module publication cannot advance the marker')
        if self.confirmed(index['shards']).keys() != index['shards'].keys():
            raise ValueError('staged module index references an unavailable shard')
        if current and current != source:
            previous = self.latest(self.version(current))
            if previous.get('kind') or index.get('combined_report_version') == previous['version']:
                self.record_promotion(previous)
            self.clear_indexed_labels(previous, local_only=True)
            self.present(previous)
        self.present(index)
        self.clear_indexed_labels(index)
        if current is None:
            self.api('git/refs', 'POST', ref='refs/' + self.marker, sha=source)
        elif current != source:
            self.api('git/refs/' + self.marker, 'PATCH', sha=source, force=False)
        return 'published'
