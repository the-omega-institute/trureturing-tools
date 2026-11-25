"""Versioned module Release assets, mutable Actions keys, retries and independent readback."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import lean_module_remote as transport


class Service:
    def __init__(self):
        self.releases = {}
        self.creates = Counter()
        self.files = {}
        self.ref = None
        self.patches = []
        self.uploads = 0
        self.reads = []
        self.page_updates = []

    def api(self, path, **args):
        body = json.loads(args['payload']) if args.get('payload') else {}
        if path.startswith('releases/tags/'):
            tag = path.split('/')[-1]
            if tag not in self.releases:
                raise transport.host.GitHubCommandError(1, ['gh'], stderr='HTTP 404: missing')
            return self.releases[tag]
        if path == 'releases':
            tag = body['tag_name']
            self.creates[tag] += 1
            release = {'id': len(self.releases) + 1, 'tag_name': tag, 'draft': False, 'assets': []}
            self.releases[tag] = release
            return release
        if path.startswith('releases/assets/'):
            assert args['method'] == 'PATCH' and body == {'label': ''}
            asset_id = int(path.split('/')[-1])
            asset = next(a for r in self.releases.values() for a in r['assets'] if a['id'] == asset_id)
            asset.update(body)
            self.patches.append(asset_id)
            return asset
        if path.startswith('releases/') and args['method'] == 'PATCH':
            rid = int(path.split('/')[1])
            release = next(r for r in self.releases.values() if r['id'] == rid)
            release.update(body)
            self.page_updates.append(rid)
            return release
        if path.startswith('releases/'):
            rid = int(path.split('/')[1])
            assets = next(r['assets'] for r in self.releases.values() if r['id'] == rid)
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            return assets[(page-1)*100:page*100]
        if path.startswith('git/matching-refs/tags/'):
            prefix = path.removeprefix('git/matching-refs/tags/')
            return [{'ref': 'refs/tags/' + tag} for tag in self.releases if tag.startswith(prefix)]
        if path.startswith('git/refs'):
            self.ref = body['sha']
            return {}
        if path.startswith('compare/'):
            return {'merge_base_commit': {'sha': self.ref}}
        raise ValueError(path)

    def request(self, url, *, method, path):
        params = parse_qs(urlsplit(url).query)
        rid = int(urlsplit(url).path.split('/')[-2])
        tag, release = next((tag, r) for tag, r in self.releases.items() if r['id'] == rid)
        payload = path.read_bytes()
        name = params['name'][0]
        if any(a['name'] == name for a in release['assets']):
            raise AssertionError('duplicate upload')
        assert 'label' not in params
        asset = {'name': name, 'label': None, 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
                 'id': self.uploads + 1, 'size': len(payload), 'state': 'uploaded'}
        self.files[tag, name] = payload
        release['assets'].append(asset)
        self.uploads += 1
        return io.BytesIO(json.dumps(asset).encode())

    def asset(self, asset, tag, offset=0, length=None):
        self.reads.append((asset['name'], offset, length))
        data = self.files[tag, asset['name']]
        yield data[offset:offset+length] if length is not None else data[offset:]


class VersionedShards(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = Service()
        self.ci = {}
        self.hits = []
        self.remote = transport.Remote(self.root)
        self.remote.http = self.service
        self.remote.current = lambda: self.service.ref
        self.remote.tags = lambda current, base: [current, *(t for t in self.service.releases if t != current)]
        def actions(operation, key, digest, path):
            if operation == 'put':
                self.ci[key] = path.read_bytes()
                return True
            if key in self.ci:
                self.hits.append(key)
                path.write_bytes(self.ci[key])
                return True
            return False
        self.remote.actions = actions
        guard = patch.object(transport, 'publisher', return_value={})
        guard.start()
        self.addCleanup(guard.stop)

    def plan(self, source, name='Small', fingerprint='c'):
        version = transport.PREFIX + source
        key = 'abcd-' + name
        row = {'key': key, 'members': [name], 'fingerprint': fingerprint*64, 'asset': key+'.tar.gz'}
        return {'schema':'lean-module-shards-v1', 'source':source, 'shards':{name:row},
                'version':version, 'mathlib':'abcd', 'targets':{name:{'shard':name}}}

    def save(self, plan, content, *, legacy=False):
        name = next(iter(plan['shards']))
        row = plan['shards'][name]
        path = self.root / row['asset']
        with path.open('wb') as out, gzip.GzipFile(fileobj=out, filename='' if legacy else row['fingerprint'],
                                                   mode='wb', mtime=0) as archive:
            archive.write(content)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        saved = self.remote.save(plan, name, path, {**row, 'bytes': path.stat().st_size, 'sha256': digest})
        if legacy:
            self.service.releases[saved['version']]['assets'][-1]['label'] = row['fingerprint']
            self.remote.inventory.clear()
        return saved

    def test_two_versions_stable_key_fallback_and_interrupted_sibling(self):
        plan = self.plan('a'*40)
        self.assertIsNone(self.remote.latest())
        old = self.save(plan, b'old')
        self.assertIsNone(self.remote.latest())
        self.assertEqual(self.remote.partials(plan), {'Small':old})
        self.assertEqual(self.save(plan, b'old'), old)
        self.assertEqual(self.service.uploads, 1)
        first = {**plan, 'shards': {'Small': old}}
        self.remote.publish(first)
        self.assertEqual(self.remote.latest(), first)
        next_plan = self.plan('b'*40, fingerprint='d')
        new = self.save(next_plan, b'updated')
        self.assertNotEqual(old['version'], new['version'])
        restored = self.root / 'restored'
        self.remote.download(old, restored)
        self.assertEqual(gzip.decompress(restored.read_bytes()), b'old')
        self.remote.download(new, restored)
        self.assertEqual(gzip.decompress(restored.read_bytes()), b'updated')
        self.assertEqual(self.remote.latest(), first)
        next_index = {**next_plan, 'shards':{'Small':new}}
        self.remote.publish(next_index)
        self.assertEqual(self.remote.latest(), next_index)
        self.assertEqual(self.remote.latest(first['version']), first)

    def test_overflow_and_cross_run_split_checkpoints(self):
        plan = self.plan('a'*40)
        with patch.object(transport, 'CAPACITY', 1):
            self.remote.save_split(plan, 'Small', {'Small':['Small']})
            shard = self.save(plan, b'payload')
            self.assertEqual(shard['version'], plan['version']+'-part-2')
            later = self.plan('d'*40)
            self.assertEqual(self.remote.splits(later), {'Small':{'Small':['Small']}})
            self.service.reads.clear()
            self.assertEqual(self.remote.partials(later), {'Small':shard})
            self.assertEqual(self.service.reads, [(shard['asset'], 0, 75)])
            self.assertEqual(self.remote.partials(self.plan('e'*40, fingerprint='f')), {})
            index = {**later, 'shards':{'Small':shard}}
            self.remote.publish(index)
            self.assertEqual(self.remote.latest(), index)
            self.assertNotIn((shard['version'], 'index.json'), self.service.files)

    def test_planner_owns_primary_and_parallel_workers_own_sparse_overflow(self):
        version = transport.PREFIX + 'a' * 40
        payload = self.root / 'payload'
        payload.write_bytes(b'payload')
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()

        def worker(lane):
            remote = transport.Remote(self.root, lane=lane, lanes=4)
            remote.http = self.service
            return remote

        with self.assertRaisesRegex(ValueError, 'planner did not prepare'):
            worker(1).stage(version, 'early', payload, digest)
        self.assertFalse(self.service.creates)
        self.remote.catalog(version, create=True)
        start = Barrier(2)

        def upload_primary(lane):
            start.wait(timeout=5)
            return worker(lane).stage(version, str(lane), payload, digest)[0]

        def overflow(lane):
            remote = worker(lane)
            start.wait(timeout=5)
            return [remote.stage(version, f'{lane}-{n}', payload, digest)[0] for n in range(2)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(upload_primary, (1, 3))), [version, version])
            with patch.object(transport, 'CAPACITY', 1):
                parts = list(pool.map(overflow, (1, 3)))
        self.assertEqual(parts, [[f'{version}-part-{n}' for n in seq] for seq in [(3, 7), (5, 9)]])
        self.assertEqual(self.service.creates, {tag: 1 for tag in [version, *parts[0], *parts[1]]})
        self.remote.tags = lambda current, base: [current]
        self.assertEqual(set(dict(self.remote.candidates({'version': version}))), set(self.service.creates))

    def test_unfinished_raw_revision_checkpoint_keeps_its_download_address(self):
        old = self.plan('a'*40, name='D5.S0')
        old['mathlib'] = '1'*40
        previous = old['shards']['D5.S0']
        previous['key'] = old['mathlib'] + '-D5.S0'
        previous['asset'] = previous['key'] + '.tar.gz'
        saved = self.save(old, b'compiled', legacy=True)
        new = {**old, 'source': 'b'*40, 'version': transport.PREFIX + 'b'*40,
               'shards': {'D5.S0': dict(previous, key='v4.35.0-D5.S0.tar.gz', asset='v4.35.0-D5.S0.tar.gz')}}
        found = self.remote.partials(new)['D5.S0']
        self.assertEqual(found, saved)
        self.assertEqual(self.service.reads, [])
        self.assertEqual(self.service.uploads, 1)
        output = self.root / 'consumer'
        self.remote.download(found, output)
        self.assertEqual(gzip.decompress(output.read_bytes()), b'compiled')

    def test_completed_legacy_shard_inherits_and_downloads_after_its_label_is_cleared(self):
        plan = self.plan('a'*40)
        row = self.save(plan, b'legacy', legacy=True)
        index = {**plan, 'shards': {'Small': row}}
        self.remote.publish(index)
        for asset in self.service.releases[plan['version']]['assets']:
            asset['label'] = ''
        self.service.reads.clear()
        self.assertEqual(self.remote.confirmed(index['shards']), index['shards'])
        self.assertEqual(self.service.reads, [])
        self.ci.clear()
        self.remote.download(row, self.root/'restored')
        self.assertEqual(gzip.decompress((self.root/'restored').read_bytes()), b'legacy')
        self.remote.publish(index)
        self.assertEqual(self.service.uploads, 2)
        self.assertEqual(self.remote.latest(), index)

    def test_split_migration_binds_the_layout_to_the_complete_index(self):
        from lean_module_cache import shard_fingerprint
        plan = self.plan('a'*40, name='D5')
        members = ['D5.A', 'D5.B']
        plan['targets'] = {n: {'key': n, 'shard': n} for n in members}
        fingerprint = shard_fingerprint(plan['mathlib'], plan['targets'], members)
        plan['shards']['D5'].update(fingerprint=fingerprint, members=members)
        self.remote.save_split(plan, 'D5', {n: [n] for n in members})
        asset = self.service.releases[plan['version']]['assets'][0]
        asset['label'] = fingerprint
        rows = {}
        for n in members:
            expected = self.plan(plan['source'], name=n)
            rows[n] = self.save(expected, n.encode())
        index = {**plan, 'shards': rows}
        self.remote.inventory.clear()
        self.remote.publish(index)
        self.assertEqual(self.service.patches, [asset['id']])
        self.assertEqual(self.remote.splits(plan), {'D5': {n: [n] for n in members}})
        self.remote.publish(index)
        self.assertEqual(self.service.patches, [asset['id']])

    def test_previous_release_label_migration_resumes_after_a_failed_patch(self):
        plan = self.plan('a'*40)
        row = self.save(plan, b'previous')
        self.remote.publish({**plan, 'shards': {'Small': row}})
        primary = self.service.releases[plan['version']]['assets']
        previous, previous_index = primary
        previous['label'] = row['fingerprint']
        previous_index['label'] = previous_index['digest'][7:]
        unrelated = {**previous, 'name': 'orphan.tar.gz', 'id': 999}
        primary.append(unrelated)
        next_plan = self.plan('b'*40, fingerprint='d')
        updated = self.save(next_plan, b'updated')
        index = {**next_plan, 'shards': {'Small':updated}}
        api = self.service.api

        def interrupted(path, **kwargs):
            if path == f'releases/assets/{previous_index["id"]}':
                raise OSError('API offline')
            return api(path, **kwargs)

        with patch.object(self.service, 'api', side_effect=interrupted), self.assertRaisesRegex(OSError, 'offline'):
            self.remote.publish(index)
        self.assertEqual(self.service.ref, plan['source'])
        self.assertEqual(self.service.patches, [previous['id']])
        self.remote.publish(index)
        self.assertEqual(self.service.ref, next_plan['source'])
        self.assertEqual(self.service.patches, [previous['id'], previous_index['id']])
        self.assertEqual(unrelated['label'], row['fingerprint'])
        self.remote.publish(index)
        self.assertEqual(len(self.service.patches), 2)
        self.assertEqual(self.service.uploads, 4)

    def test_cache_failure_preserves_release_checkpoint_and_index(self):
        plan = self.plan('a'*40)
        self.remote.actions = transport.Remote.actions.__get__(self.remote)
        with patch.dict(transport.os.environ, {'LEAN_MODULE_ACTIONS_CACHE':'1'}), \
                patch.object(transport.subprocess, 'run', side_effect=OSError('cache offline')), \
                patch.object(transport.sys, 'stderr', new_callable=io.StringIO) as warnings:
            row = self.save(plan, b'payload')
            self.assertEqual(self.service.uploads, 1)
            self.assertEqual(self.save(plan, b'payload'), row)
            self.assertEqual(self.service.uploads, 1)
            index = {**plan, 'shards':{'Small':row}}
            self.remote.publish(index)
            self.assertEqual(self.service.uploads, 2)
            self.assertEqual(self.remote.latest(), index)
            self.remote.download(row, self.root/'response')
            self.assertEqual(gzip.decompress((self.root/'response').read_bytes()), b'payload')
            self.assertIn('cache offline', warnings.getvalue())
            self.remote.publish(index)
            self.assertEqual(self.service.uploads, 2)
            with self.assertRaisesRegex(ValueError, 'local shard'):
                self.remote.save(plan, 'Small', self.root/'response', {**row, 'sha256':'0'*64})

    def test_removed_shard_cannot_be_inherited_in_a_new_version(self):
        plan = self.plan('a'*40)
        row = self.save(plan, b'payload')
        self.assertEqual(self.remote.confirmed({'Small': row}), {'Small': row})
        self.service.releases[plan['version']]['assets'].clear()
        fresh = self.plan('b'*40)
        with self.assertRaisesRegex(ValueError, 'unavailable shard'):
            self.remote.publish({**fresh, 'shards': {'Small': row}})
        self.assertIsNone(self.service.ref)

    def test_release_page_links_to_local_overflow_and_inherited_shards(self):
        source = 'a'*40
        tag = transport.PREFIX + source
        old = transport.PREFIX + 'b'*40
        sites = {'A': tag, 'B': tag + '-part-2', 'C': old}
        index = {'version': tag, 'source': source, 'targets': dict.fromkeys(sites),
                 'shards': {n: {'version': v, 'asset': f'v4.35.0-{n}.tar.gz', 'members': [n], 'bytes': 1024**2}
                            for n, v in sites.items()}}
        self.remote.catalog(tag, create=True)
        self.remote.present(index)
        release = self.service.releases[tag]
        self.assertIn('mathlib v4.35.0', release['name'])
        for link in (f'{tag}/index.json', f'{tag}/v4.35.0-A.tar.gz', f'{tag}-part-2/v4.35.0-B.tar.gz',
                     f'/releases/tag/{old}'):
            self.assertIn(link, release['body'])
        self.assertIn('本版发布 **2** 个，复用历史分片 **1** 个', release['body'])
        self.remote.present(index)
        self.assertEqual(self.service.page_updates, [release['id']])
        for row in index['shards'].values():
            row['version'] = old
        self.remote.present(index)
        self.assertIn('本版发布 **0** 个', release['body'])
        self.assertNotIn('### 本版下载', release['body'])

    def test_release_page_names_mathlib_for_legacy_only_index(self):
        plan = self.plan('a'*40)
        self.remote.catalog(plan['version'], create=True)
        revision = 'a'*40
        manifest = {'packages': [{'name': 'mathlib', 'inputRev': 'v4.35.0', 'rev': revision}]}
        (self.root/'lake-manifest.json').write_text(json.dumps(manifest))
        self.remote.present(dict(plan, mathlib=revision, shards={}))
        self.assertIn('mathlib v4.35.0', self.service.releases[plan['version']]['name'])
        self.remote.present(dict(plan, mathlib='d'*40, shards={}))
        self.assertNotIn('v4.35.0', self.service.releases[plan['version']]['name'])

    def test_manifest_readback_failure_blocks_advance(self):
        plan = self.plan('a'*40)
        row = self.save(plan, b'payload')
        index = {**plan, 'shards':{'Small':row}}
        with patch.object(self.service, 'asset', return_value=[b'incorrect']):
            with self.assertRaisesRegex(ValueError, 'readback'):
                self.remote.publish(index)
        self.assertIsNone(self.service.ref)


if __name__ == '__main__':
    unittest.main()
