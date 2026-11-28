"""Versioned module Release assets, mutable Actions keys, retries and independent readback."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import subprocess
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import lean_module_cache as cache
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

    def request(self, url, *, method='GET', path=None, headers=None, accept=None, authenticate=True, redirect=False, payload=None):
        if method == 'HEAD' and '/releases/download/' in url:
            assert not authenticate and redirect
            tag, name = urlsplit(url).path.split('/')[-2:]
            if (tag, name) not in self.files:
                raise transport.host.GitHubCommandError(1, ['HEAD'], stderr='HTTP 404: missing')
            response = io.BytesIO(b'')
            response.status = 302
            return response
        if '/releases/expanded_assets/' in url:
            assert method == 'GET' and not authenticate and accept == 'text/html'
            tag = urlsplit(url).path.split('/')[-1]
            assets = self.releases.get(tag, {}).get('assets', [])
            body = ''.join(f'<li><a href="/{transport.host.REPO}/releases/download/{tag}/{a["name"]}"></a>'
                           f'<clipboard-copy value="{a["digest"]}"></clipboard-copy></li>'
                           for a in assets if a['state'] == 'uploaded').encode()
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            if headers.get('If-None-Match') == etag:
                raise transport.host.GitHubCommandError(1, ['GET'], stderr='HTTP 304: ')
            response = io.BytesIO(gzip.compress(body))
            response.headers = {'ETag': etag, 'Content-Encoding': 'gzip'}
            return response
        if method == 'DELETE':
            asset_id = int(urlsplit(url).path.split('/')[-1])
            for tag, release in self.releases.items():
                for asset in release['assets']:
                    if asset['id'] == asset_id:
                        release['assets'].remove(asset)
                        self.files.pop((tag, asset['name']), None)
                        return io.BytesIO(b'')
            raise AssertionError('missing asset to delete')
        params = parse_qs(urlsplit(url).query)
        rid = int(urlsplit(url).path.split('/')[-2])
        tag, release = next((tag, r) for tag, r in self.releases.items() if r['id'] == rid)
        payload = path.read_bytes()
        name = params['name'][0]
        if any(a['name'] == name for a in release['assets']):
            raise transport.host.GitHubCommandError(1, ['gh'], stderr='HTTP 422: already_exists')
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
        self.checkpoints = {}
        def compiler_cache(operation, kind, *args):
            if operation == 'list':
                args[0].write_text(json.dumps([dict(id=k, sha256=d, bytes=b) for t, k, d, b in self.checkpoints
                                               if t == kind]))
                return True
            key, sha, size, path = args
            address = kind, key, sha, int(size)
            if operation == 'put':
                self.checkpoints[address] = path.read_bytes()
                return True
            if address in self.checkpoints:
                path.write_bytes(self.checkpoints[address])
                return True
            return False
        self.remote.compiler_cache = compiler_cache
        guard = patch.object(transport, 'publisher', return_value=dict(run='9', attempt='1'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_pinned_compiler_plan_is_reused_before_analyzing_the_checkout(self):
        import lean_module_cache as cache

        source = 'a' * 40
        version = transport.PREFIX + source
        plan = dict(schema=cache.SCHEMA, kind='compiler', source=source, version=version,
                    lanes=[[0]], shards={}, compiler_dag={'units': {}})
        output = self.root / 'compiler-plan.json'
        argv = ['lean_module_cache', 'plan', '--kind', 'compiler', '--compiler-dag',
                '--prepare-release', '--output', str(output)]
        with patch.object(cache.sys, 'argv', argv), patch.object(cache.compiler, 'checkout', return_value=source), \
                patch.object(transport, 'Remote', return_value=self.remote), \
                patch.object(cache, 'plan_publication', side_effect=[plan, AssertionError('re-analyzed pinned source')]) as analyze:
            self.assertEqual(cache.run(), 0)
            first = output.read_bytes()
            output.unlink()
            self.assertEqual(cache.run(), 0)
            self.assertEqual(output.read_bytes(), first)
        analyze.assert_called_once()
        self.assertIn((version, transport.COMPILER_PLAN), self.service.files)

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

    def test_child_workflow_authority_is_scoped_to_compiler_workers(self):
        from lean_generation_checkpoints import publisher
        repo = transport.host.REPO
        env = dict(GITHUB_REPOSITORY=repo, GITHUB_REF='refs/heads/dev', GITHUB_REF_PROTECTED='true',
                   GITHUB_WORKFLOW_REF=f'{repo}/.github/workflows/ci-lean-generation-compile.yml@refs/heads/dev',
                   GITHUB_EVENT_NAME='workflow_dispatch', STRATALINT_CACHE_WRITES='true',
                   GITHUB_RUN_ID='7', GITHUB_RUN_ATTEMPT='1')
        with patch.dict(transport.os.environ, env):
            self.assertEqual(publisher('compiler-worker'), dict(run='7', attempt='1'))
            for kind in ('compiler', 'report'):
                with self.assertRaises(ValueError):
                    publisher(kind)
            with patch.dict(transport.os.environ, GITHUB_EVENT_NAME='push'), self.assertRaises(ValueError):
                publisher('compiler-worker')

    def test_units_and_catalogs_use_only_actions_and_recover_surviving_members(self):
        from lean_module_dag import unit_key, unit_row
        plan = self.plan('a' * 40)
        inputs = {'Small': 'compiler1', 'Other': 'compiler2'}
        key = unit_key(plan['mathlib'], inputs)
        row = unit_row(key, inputs)
        plan.update(shards={key: row}, lanes=[[]], targets={'Small': {'key': 'compiler1'}})
        self.remote.save_compiler_catalog(plan['version'], dict(mathlib=plan['mathlib'], units={key: inputs}))
        path = self.root / 'unit.tar.gz'
        with path.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename=key, mode='wb', mtime=0) as archive:
            archive.write(b'checkpoint')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.remote.compiler_receipts(dict(plan, compiler_dag={'units':{key:row}})), {})
        done = self.remote.save(plan, key, path, dict(row, sha256=sha, bytes=path.stat().st_size))
        self.assertEqual(done['version'], transport.ACTIONS_CACHE)
        old_row = dict(row, part=transport.UNIT_PART_START)
        old = self.remote.save(dict(plan, shards={key: old_row}), key, path,
                               dict(old_row, sha256=sha, bytes=path.stat().st_size))
        self.assertEqual(old['version'], transport.ACTIONS_CACHE)
        for filename in (row['asset'], 'compiler-units-' + sha + '.json'):
            with self.assertRaisesRegex(ValueError, 'Actions cache'):
                self.remote.stage(plan['version'], filename, path, sha)
        self.assertEqual(self.service.releases, {})
        self.assertEqual(self.ci, {})
        self.assertEqual(self.remote.compiler_receipts(dict(plan, compiler_dag={'units':{key:row}})), {key: done})
        output = self.root / 'copy.gz'
        self.remote.download(done, output)
        self.assertEqual(output.read_bytes(), path.read_bytes())
        later = dict(plan, version=transport.PREFIX+'b'*40)
        recovered = self.remote.compiler_seeds(later)
        self.assertEqual(set(recovered['targets']), {'Small'})
        self.assertEqual(recovered['shards'][key]['members'], ['Other', 'Small'])
        addr = 'unit', key, sha, path.stat().st_size
        self.checkpoints[addr] = b'bad'
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.remote.download(done, output)
        self.checkpoints.pop(addr)
        with self.assertRaises(transport.CompilerCacheMiss):
            self.remote.download(done, output)
        self.assertEqual(self.remote.compiler_seeds(later)['targets'], {})
        self.checkpoints[addr] = path.read_bytes()
        lookup = self.remote.compiler_cache
        def evict_catalog(op, kind, *args):
            return False if (op, kind) == ('get', 'catalog') else lookup(op, kind, *args)
        with patch.object(self.remote, 'compiler_cache', side_effect=evict_catalog):
            self.assertEqual(self.remote.compiler_seeds(later)['targets'], {})

    def test_compiler_bridge_distinguishes_eviction_from_service_failure(self):
        remote = transport.Remote(self.root)
        args = ['unit', 'a'*64, 'b'*64, '123', self.root/'unit.gz']
        with patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 4)) as run:
            self.assertFalse(remote.compiler_cache('get', *args))
            self.assertEqual(run.call_args.args[0][2:], ['compiler-cache-get', *args])
            with self.assertRaises(subprocess.CalledProcessError):
                remote.compiler_cache('put', *args)
        with patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(subprocess.CalledProcessError):
                remote.compiler_cache('get', *args)

    def test_historical_release_catalog_and_overflow_are_readable(self):
        from lean_module_dag import unit_key, unit_row
        plan = self.plan('a' * 40)
        inputs = {'Small': 'compiler1'}
        key = unit_key(plan['mathlib'], inputs)
        row = dict(unit_row(key, inputs), part=transport.UNIT_PART_START)
        plan.update(shards={key: row}, targets={'Small': {'key': 'compiler1'}})
        catalog = transport.json_body(dict(mathlib=plan['mathlib'], units={key: inputs}))
        sha = hashlib.sha256(catalog).hexdigest()
        part = f'{plan["version"]}-part-{row["part"] + 1}'
        for tag, name, data in ((plan['version'], 'compiler-units-' + sha + '.json', catalog),
                                (part, row['asset'], b'historical unit')):
            self.remote.catalog(tag, create=True)
            asset = dict(name=name, state='uploaded', digest='sha256:' + hashlib.sha256(data).hexdigest(),
                         size=len(data))
            self.service.releases[tag]['assets'].append(asset)
            self.service.files[tag, name] = data
        self.remote.inventory.clear()
        self.assertEqual(self.remote.compiler_receipts(dict(plan, compiler_dag={'units':{key:row}}))[key]['version'], part)
        recovered = self.remote.compiler_seeds(dict(plan, version=transport.PREFIX + 'b'*40))
        self.assertEqual(set(recovered['targets']), {'Small'})
        output = self.root / 'copy.gz'
        self.remote.download(recovered['shards'][key], output)
        self.assertEqual(output.read_bytes(), b'historical unit')

    def test_typed_compiler_preserves_promotion_evidence_for_its_legacy_report_source(self):
        first = self.plan('a' * 40)
        old = dict(first, shards={'Small': self.save(first, b'mixed')})
        self.remote.publish(old)
        self.assertNotIn((old['version'], transport.PROMOTED), self.service.files)
        second = self.plan('b' * 40, fingerprint='e')
        index = dict(second, kind='compiler', combined_report_version=old['version'],
                     shards={'Small': self.save(second, b'compiler')})
        self.remote.publish(index)
        self.assertEqual(self.remote.current(), second['source'])
        self.assertEqual(self.remote.published(old['version']), old)

    def test_late_promotion_of_an_unpublished_ancestor_is_superseded(self):
        ancestor = self.plan('a' * 40)
        old = dict(ancestor, shards={'Small': self.save(ancestor, b'old')})
        self.remote.stage_index(old)
        with patch.object(self.remote, 'present', side_effect=InterruptedError('metadata')):
            with self.assertRaises(InterruptedError):
                self.remote.promote(old['version'])
        newer = self.plan('b' * 40, fingerprint='e')
        current = dict(newer, shards={'Small': self.save(newer, b'new')})
        self.remote.publish(current)
        api = self.remote.api
        def compare(path, *args, **fields):
            if path.startswith('compare/'):
                return {'status': 'behind', 'merge_base_commit': {'sha': old['source']}}
            return api(path, *args, **fields)
        with patch.object(self.remote, 'api', side_effect=compare):
            self.assertEqual(self.remote.promote(old['version']), 'superseded')
        self.assertIsNone(self.remote.published(old['version']))
        self.assertEqual(self.remote.latest(), current)
        def divergent(path, *args, **fields):
            if path.startswith('compare/'):
                return {'status': 'diverged', 'merge_base_commit': {'sha': 'f' * 40}}
            return api(path, *args, **fields)
        with patch.object(self.remote, 'api', side_effect=divergent), self.assertRaisesRegex(ValueError, 'divergent'):
            self.remote.promote(old['version'])
        self.assertEqual(self.remote.latest(), current)

    def test_independent_report_release_and_promotion(self):
        report = transport.Remote(self.root, kind='report')
        report.http = self.service
        reports = []
        report.current = lambda: reports[-1] if reports else None
        def save_ref(path, method='GET', **fields):
            if path.startswith('git/refs'):
                self.assertEqual(fields.get('ref', 'refs/' + report.marker), 'refs/heads/lean-modules-published-report')
                reports.append(fields['sha'])
            return self.service.api(path, method=method, payload=transport.json_body(fields))
        report.api = save_ref
        a = 'a' * 40
        b = 'b' * 40
        def index(source):
            return dict(schema='lean-module-shards-v1', kind='report', source=source, mathlib='abc',
                        version=report.version(source), compiler_version=self.remote.version(source),
                        targets={}, shards={})
        first = index(a)
        self.assertIsNone(report.published(first['version']))
        self.assertFalse(self.remote.accepts_tag(first['version']))
        self.assertFalse(report.accepts_tag(first['compiler_version']))
        report.publish(first)
        self.assertEqual(report.published(first['version']), first)
        second = index(b)
        report.stage_index(second)
        self.assertEqual(report.staged_index(second['version']), second)
        self.assertIsNone(report.published(second['version']))
        self.assertEqual(report.latest(), first)
        def break_metadata(index, **kwargs):
            if index['source'] == b:
                raise OSError('final release metadata failed')
        with patch.object(report, 'clear_indexed_labels', side_effect=break_metadata):
            with self.assertRaisesRegex(OSError, 'metadata failed'):
                report.promote(second['version'])
        self.assertIsNone(report.published(second['version']))
        self.assertEqual(report.latest(), first)
        self.assertNotIn((second['version'], transport.PROMOTED), self.service.files)
        report.promote(second['version'])
        self.assertEqual(report.published(first['version']), first)
        self.assertEqual(report.published(second['version']), second)
        report.promote(first['version'])
        report.promote(second['version'])
        self.assertEqual(reports, [a, b])

    def marker(self, version, filename, contents):
        path = self.root / filename
        data = transport.json_body(contents)
        path.write_bytes(data)
        self.remote.stage(version, filename, path, hashlib.sha256(data).hexdigest())
        return next(a for a in self.service.releases[version]['assets'] if a['name'] == filename)

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

    def test_measured_marker_follows_its_owner_when_cold_boundaries_move(self):
        members = [f'D5.A.X{i:03}' for i in range(250)]
        inputs = {n: {'key': n, 'deps': [], 'report_inputs': []} for n in [*members, 'D5.B']}
        fp = cache.shard_fingerprint('abcd', inputs, members)
        layout = {n: [n] for n in members}
        legacy = self.plan('a' * 40)['version']
        self.marker(legacy, 'split-' + fp + '.json', {'group': 'D5', 'fingerprint': fp, 'layout': layout})
        def plan():
            return cache.make_plan('b' * 40, 'abcd', inputs, None, self.remote,
                                   cache.DEFAULT_CAPACITY, 2, mathlib_version='v1')
        initial = plan()
        self.assertEqual(initial['shards']['D5.A']['members'], members)
        self.remote.save_split(initial, 'D5.A', layout)
        self.assertEqual(set(plan()['shards']), {'D5', *members})

        qualified = transport.split_filename('D5.A', fp)
        for field, value in [('group', 'D5'), ('fingerprint', '0' * 64), ('layout', {'D5.A': members[:-1]})]:
            with self.subTest(field=field):
                bad = {'group': 'D5.A', 'fingerprint': fp, 'layout': layout, field: value}
                version = self.plan('c' * 40)['version']
                self.service.releases.pop(version, None)
                self.remote.inventory.clear()
                self.marker(version, qualified, bad)
                with self.assertRaisesRegex(ValueError, 'split (members|group) differ'):
                    self.remote.splits(dict(initial, version=version))

    def test_cold_child_inherits_only_a_confirmed_published_base_shard(self):
        self.remote.tags = lambda current, base: [current]
        def plan(source, inputs, base=None):
            return cache.make_plan(source * 40, 'abcd', inputs, base, self.remote,
                                   cache.DEFAULT_CAPACITY, 2, mathlib_version='v1')
        stable = {'D5.Stable': {'key': 'stable', 'deps': [], 'report_inputs': []}}
        first = plan('a', stable)
        self.save(first, b'stable')
        base = cache.finish(first, self.remote)
        extra = {f'D5.New.X{i:03}': {'key': str(i), 'deps': [], 'report_inputs': []} for i in range(251)}
        inputs = stable | extra
        next_plan = plan('b', inputs, base)
        self.assertEqual(next_plan['inherited'], base['shards'])
        self.assertNotIn('D5', [n for lane in next_plan['lanes'] for n in lane])
        self.service.releases[base['version']]['assets'].remove(next(
            row for row in self.service.releases[base['version']]['assets'] if row['name'] == base['shards']['D5']['asset']))
        self.remote.inventory.clear()
        missing = plan('b', inputs, base)
        self.assertEqual(missing['inherited'], {})
        self.assertIn('D5', [n for lane in missing['lanes'] for n in lane])

    def test_same_source_resumes_immutable_groups_after_the_base_advances(self):
        self.remote.tags = lambda current, base: [current]
        source = 'c' * 40
        inputs = {n: {'key': n, 'deps': [], 'report_inputs': []} for n in
                  [*(f'D5.A.{branch}.X{i:03}' for branch in ('Left', 'Right') for i in range(125)), 'D5.B']}
        def plan(revision, inputs, base, *, capacity=cache.DEFAULT_CAPACITY, prepare=False):
            return cache.make_plan(revision, 'abcd', inputs, base, self.remote, capacity, 2,
                                   mathlib_version='v1', prepare_release=prepare)
        def save(plan, name):
            return self.save(dict(plan, shards={name: plan['shards'][name]}), name.encode())
        # The original published base covers only a different report.
        old = plan('a' * 40, {'Old': {'key': 'old', 'deps': []}}, None)
        save(old, 'Old')
        base = cache.finish(old, self.remote)
        dry = plan(source, inputs, base)
        self.assertNotIn(dry['version'], self.service.releases)
        first = plan(source, inputs, base, prepare=True)
        self.assertEqual(first, dry)
        self.assertEqual(set(first['shards']), {'D5', 'D5.A'})
        self.assertEqual(first['shards']['D5']['members'], ['D5.B'])
        completed = save(first, 'D5')
        # The next published ancestor covers 250 reports with different shard boundaries.
        warm = {n: row for n, row in inputs.items() if n != 'D5.B'}
        newer = plan('b' * 40, warm, base, capacity=125 * cache.DEFAULT_MODULE_BYTES)
        self.assertNotEqual(newer['shards'].keys(), first['shards'].keys())
        for name in newer['shards']:
            save(newer, name)
        advanced = cache.finish(newer, self.remote)
        retry = plan(source, inputs, advanced, prepare=True)
        self.assertEqual(retry['shards'], first['shards'])
        self.assertEqual(retry['inherited'], {'D5': completed})
        self.assertEqual(retry['lanes'], [['D5.A']])
        self.assertEqual(retry['base_version'], base['version'])
        save(retry, 'D5.A')
        index = cache.finish(retry, self.remote)
        self.assertEqual(cache.finish(first, self.remote), index)
        self.assertEqual(self.remote.latest(), index)
        self.assertEqual(index['shards']['D5'], completed)

    def test_identical_planners_share_the_first_layout_and_workers_can_split_it(self):
        source = 'a' * 40
        inputs = {n: {'key': n, 'deps': []} for n in ('D5.A', 'D5.B')}
        start = Barrier(2)
        def prepare(_):
            remote = transport.Remote(self.root)
            remote.http = self.service
            remote.tags = lambda current, base: [current]
            save = remote.save_publication_layout
            def together(version, coordinates):
                start.wait(timeout=5)
                save(version, coordinates)
            remote.save_publication_layout = together
            return cache.make_plan(source, 'abcd', inputs, None, remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version='v1', prepare_release=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(prepare, range(2))
        self.assertEqual(first, second)
        self.assertEqual(self.service.uploads, 1)
        coordinates = self.service.files[first['version'], transport.LAYOUT]
        self.remote.save_split(first, 'D5', {n: [n] for n in inputs})
        retry = cache.make_plan(source, 'abcd', inputs, None, self.remote, cache.DEFAULT_CAPACITY,
                                2, mathlib_version='v1', prepare_release=True)
        self.assertEqual(set(retry['shards']), set(inputs))
        self.assertEqual(self.service.files[first['version'], transport.LAYOUT], coordinates)
        self.assertEqual(self.service.uploads, 2)

    def test_planner_replaces_an_interrupted_layout_reservation(self):
        source = 'a' * 40
        version = transport.PREFIX + source
        self.remote.catalog(version, create=True)
        starter = {'id': 100, 'name': transport.LAYOUT, 'state': 'starter', 'size': 0}
        self.service.releases[version]['assets'].append(starter)
        inputs = {'D5.X': {'key': 'x', 'deps': []}}
        result = cache.make_plan(source, 'abcd', inputs, None, self.remote, cache.DEFAULT_CAPACITY,
                                 2, mathlib_version='v1', prepare_release=True)
        self.assertEqual(result['lanes'], [['D5']])
        assets = self.service.releases[version]['assets']
        self.assertEqual([(a['name'], a['state']) for a in assets], [(transport.LAYOUT, 'uploaded')])
        self.assertIsNone(self.remote.publication_layout(version)['base_version'])

    def test_retry_restores_300_reports_from_the_original_base_or_keeps_an_empty_base(self):
        def plan(source, inputs, base=None, prepare=False):
            return cache.make_plan(source * 40, 'abcd', inputs, base, self.remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version='v1', prepare_release=prepare)
        names = [f'D5.X{i:03}' for i in range(300)]
        warm = {n: {'key': n, 'deps': [], 'report_inputs': []} for n in names}
        inputs = warm | {'D5.New': {'key': 'new', 'deps': [], 'report_inputs': []}}
        no_base = plan('e', inputs, prepare=True)
        # The first published base stores the 300 reports together.
        with patch.object(cache, 'MAX_COLD_REPORTS_PER_SHARD', 300):
            before = plan('a', warm)
        self.save(before, b'warm')
        base = cache.finish(before, self.remote)
        original = plan('c', inputs, base, prepare=True)
        self.assertEqual(set(original['shards']), {'D5'})
        different = {'D5.Else': {'key': 'other', 'deps': [], 'report_inputs': []}}
        other = plan('b', different, base)
        self.save(other, b'different')
        newer = cache.finish(other, self.remote)
        retry = plan('c', inputs, newer, prepare=True)
        self.assertEqual((retry['shards'], retry['base_version']), (original['shards'], base['version']))
        modules = {n: cache.inventory.Module('trureturing', Path(*n.split('.')).with_suffix('.lean'), Path('.'))
                   for n in inputs}
        with tempfile.TemporaryDirectory() as dest, patch.object(cache, 'restore_shard',
                side_effect=lambda archive, row, members, *args, **kw: set(members)):
            self.assertEqual(cache.restore_artifacts(self.root, modules, inputs, self.remote.latest(retry['base_version']),
                                                     set(inputs), self.remote, Path(dest), 'compiler'), set(warm))
        empty_retry = plan('e', inputs, newer, prepare=True)
        self.assertEqual(empty_retry['shards'], no_base['shards'])
        self.assertIsNone(empty_retry['base_version'])

    def test_retries_the_staged_index_after_interrupted_promotion_and_a_new_base(self):
        self.remote.tags = lambda current, base: [current]
        def plan(source, inputs, base=None, prepare=False):
            return cache.make_plan(source * 40, 'abcd', inputs, base, self.remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version='v1', prepare_release=prepare)
        def save(plan, name):
            return self.save(dict(plan, shards={name: plan['shards'][name]}), name.encode())
        old_inputs = {'D5.X': {'key': 'x', 'deps': []}}
        old = plan('a', old_inputs)
        save(old, 'D5')
        base = cache.finish(old, self.remote)
        inputs = old_inputs | {'Other.Y': {'key': 'y', 'deps': []}}
        # This source predates publication-layout.json. Its staged index is sufficient to resume.
        first = plan('c', inputs, base)
        self.assertEqual(first['inherited'], base['shards'])
        save(first, 'D5')  # An earlier source-local upload has the same payload.
        save(first, 'Other')
        with patch.object(self.remote, 'present', side_effect=InterruptedError('promotion')):
            with self.assertRaisesRegex(InterruptedError, 'promotion'):
                cache.finish(first, self.remote)
        index_bytes = self.service.files[first['version'], transport.INDEX]
        self.assertEqual(self.service.ref, base['source'])
        newer = plan('b', {'D5.Z': {'key': 'z', 'deps': []}}, base)
        save(newer, 'D5')
        advanced = cache.finish(newer, self.remote)
        self.assertEqual(self.service.ref, advanced['source'])
        retry = plan('c', inputs, advanced, True)
        self.assertEqual(retry['lanes'], [])
        result = cache.finish(retry, self.remote)
        self.assertEqual(self.service.ref, first['source'])
        self.assertEqual(transport.json_body(result), index_bytes)
        self.assertEqual(result['shards']['D5'], base['shards']['D5'])

    def test_older_finish_resumes_the_index_staged_by_a_later_plan(self):
        self.remote.tags = lambda current, base: [current]
        def plan(source, inputs, base=None):
            return cache.make_plan(source * 40, 'abcd', inputs, base, self.remote,
                                   cache.DEFAULT_CAPACITY, 2, mathlib_version='v1')
        def save(plan, name):
            return self.save(dict(plan, shards={name: plan['shards'][name]}), name.encode())
        stable = {'D5.X': {'key': 'x', 'deps': []}}
        inputs = stable | {'Other.Y': {'key': 'y', 'deps': []}}
        early = plan('c', inputs)
        save(early, 'D5')
        save(early, 'Other')
        older_base = plan('a', stable)
        save(older_base, 'D5')
        base = cache.finish(older_base, self.remote)
        later = plan('c', inputs, base)
        self.assertEqual(later['inherited']['D5']['version'], base['version'])
        with patch.object(self.remote, 'present', side_effect=InterruptedError('promotion')):
            with self.assertRaisesRegex(InterruptedError, 'promotion'):
                cache.finish(later, self.remote)
        stored = self.service.files[later['version'], transport.INDEX]
        staged = json.loads(stored)
        for changed in (dict(staged, source='d' * 40),
                        dict(staged, targets={**staged['targets'], 'D5.X': dict(staged['targets']['D5.X'], key='other')}),
                        dict(staged, shards={})):
            with patch.object(self.remote, 'staged_index', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'staged module'):
                    cache.finish(early, self.remote)
        self.assertEqual(self.service.ref, older_base['source'])
        result = cache.finish(early, self.remote)
        self.assertEqual(transport.json_body(result), stored)
        self.assertEqual(self.remote.latest(), result)
        self.assertEqual(self.service.ref, early['source'])

    def test_retry_keeps_the_original_address_of_an_unpublished_sibling_shard(self):
        inputs = {'D5.X': {'key': 'x', 'deps': []}}
        def plan(source, base=None, prepare=False):
            return cache.make_plan(source * 40, 'abcd', inputs, base, self.remote, cache.DEFAULT_CAPACITY,
                                   2, mathlib_version='v1', prepare_release=prepare)
        sibling = plan('a')
        adopted = self.save(sibling, b'first sibling')
        original = plan('c', prepare=True)
        self.assertEqual(original['lanes'], [])
        self.assertEqual(original['inherited'], {'D5': adopted})
        newer = plan('b')
        newer_row = self.save(newer, b'newer sibling')
        advanced = {**newer, 'shards': {'D5': newer_row}}
        self.remote.publish(advanced)
        retry = plan('c', advanced, prepare=True)
        self.assertEqual(retry['inherited'], original['inherited'])
        self.assertEqual(cache.finish(retry, self.remote)['shards'], {'D5': adopted})

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
        asset = self.marker(plan['version'], 'split-' + fingerprint + '.json', {
            'group': 'D5', 'fingerprint': fingerprint, 'layout': {n: [n] for n in members},
        })
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
