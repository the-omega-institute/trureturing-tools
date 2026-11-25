"""Versioned module Release assets, mutable Actions keys, retries and independent readback."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import lean_module_remote as transport


class Service:
    def __init__(self):
        self.releases = {}
        self.files = {}
        self.ref = None
        self.uploads = 0

    def api(self, path, **args):
        body = json.loads(args['payload']) if args.get('payload') else {}
        if path.startswith('releases/tags/'):
            tag = path.split('/')[-1]
            if tag not in self.releases:
                raise transport.host.GitHubCommandError(1, ['gh'], stderr='HTTP 404: missing')
            return self.releases[tag]
        if path == 'releases':
            tag = body['tag_name']
            release = {'id': len(self.releases) + 1, 'tag_name': tag, 'draft': False, 'assets': []}
            self.releases[tag] = release
            return release
        if path.startswith('releases/'):
            rid = int(path.split('/')[1])
            assets = next(r['assets'] for r in self.releases.values() if r['id'] == rid)
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            return assets[(page-1)*100:page*100]
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
        asset = {'name': name, 'label': params['label'][0], 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
                 'id': self.uploads + 1, 'size': len(payload), 'state': 'uploaded'}
        self.files[tag, name] = payload
        release['assets'].append(asset)
        self.uploads += 1
        return io.BytesIO(json.dumps(asset).encode())

    def asset(self, asset, tag):
        yield self.files[tag, asset['name']]


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

    def save(self, plan, content):
        name = next(iter(plan['shards']))
        row = plan['shards'][name]
        path = self.root / row['asset']
        path.write_bytes(content)
        return self.remote.save(plan, name, path,
                                {**row, 'bytes':len(content), 'sha256':hashlib.sha256(content).hexdigest()})

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
        self.assertEqual(restored.read_bytes(), b'old')
        self.remote.download(new, restored)
        self.assertEqual(restored.read_bytes(), b'updated')
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
            self.assertEqual(self.remote.partials(later), {'Small':shard})
            self.assertEqual(self.remote.partials(self.plan('e'*40, fingerprint='f')), {})
            index = {**later, 'shards':{'Small':shard}}
            self.remote.publish(index)
            self.assertEqual(self.remote.latest(), index)
            self.assertNotIn((shard['version'], 'index.json'), self.service.files)

    def test_removed_shard_cannot_be_inherited_in_a_new_version(self):
        plan = self.plan('a'*40)
        row = self.save(plan, b'payload')
        self.assertEqual(self.remote.confirmed({'Small': row}), {'Small': row})
        self.service.releases[plan['version']]['assets'].clear()
        fresh = self.plan('b'*40)
        with self.assertRaisesRegex(ValueError, 'unavailable shard'):
            self.remote.publish({**fresh, 'shards': {'Small': row}})
        self.assertIsNone(self.service.ref)

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
