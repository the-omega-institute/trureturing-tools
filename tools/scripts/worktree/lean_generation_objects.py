"""Append-only Release asset buckets shared by full versions and partial checkpoints."""

from datetime import timezone
from email.utils import parsedate_to_datetime
from functools import cached_property
import json
import http.client
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import lean_cache_release as host
from lean_release_index import valid_digest


BUCKET = "lean-cache-objects-v1"
# Leave room for the concurrent final writes of workers with the same bucket view.
CAPACITY = 980


class RateLimitTimeout(TimeoutError):
    """The server's earliest allowed retry is beyond the operation's deadline."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def timestamp(value):
    try:
        date = parsedate_to_datetime(value)
        return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def seconds(value):
    return int(value) if value and re.fullmatch(r'[0-9]{1,18}', value) else None


def retry_delay(status, headers, message, attempt):
    if status not in (403, 429):
        return None
    delay = headers.get('Retry-After')
    exhausted = headers.get('X-RateLimit-Remaining') == '0'
    if status == 403 and not (exhausted or delay or 'rate limit' in message.lower()):
        return None
    now = timestamp(headers.get('Date')) or time.time()
    waits = []
    if delay:
        numeric = seconds(delay)
        if numeric is not None:
            waits.append(numeric)
        elif (date := timestamp(delay)) is not None:
            waits.append(date - now)
        else:
            raise ValueError('invalid GitHub Retry-After header')
    if exhausted and (raw := headers.get('X-RateLimit-Reset')):
        reset = seconds(raw)
        if reset is None:
            raise ValueError('invalid GitHub X-RateLimit-Reset header')
        waits.append(reset - now + 1)
    return max(1, math.ceil(max(waits))) if waits else 60 * 2 ** min(attempt, 3)


class GitHubHTTP:
    def __init__(self, deadline):
        self.deadline = deadline
        self.opener = urllib.request.build_opener(NoRedirect())
        self.signed = {}

    @cached_property
    def token(self):
        return os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or host.gh(
            self.deadline, 'auth', 'token', '--hostname', 'github.com').strip()

    def request(self, url, *, method='GET', accept='application/vnd.github+json', headers=None,
                path=None, payload=None, redirect=False, authenticate=True):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('invalid HTTPS GitHub asset address')
        if authenticate and parsed.hostname not in ('api.github.com', 'uploads.github.com'):
            raise ValueError('invalid authenticated GitHub API origin')
        fields = {'Accept': accept, 'User-Agent': 'trureturing-generation', **(headers or {})}
        if authenticate:
            fields['Authorization'] = 'Bearer ' + self.token
        if path is not None:
            fields.update({'Content-Length': str(path.stat().st_size), 'Content-Type': 'application/octet-stream'})
        for attempt in range(8):
            file = path.open('rb') if path is not None else None
            try:
                request = urllib.request.Request(url, headers=fields, data=file or payload, method=method)
                return self.opener.open(request, timeout=min(120, host.remaining(self.deadline)))
            except urllib.error.HTTPError as error:
                if redirect and error.code == 302:
                    return error
                with error:
                    message = error.read(8192).decode(errors='replace') if authenticate else ''
                    delay = retry_delay(error.code, error.headers, message, attempt)
                failure = host.GitHubCommandError(1, ['GitHub', method], stderr=f'HTTP {error.code}: {message}', output=message)
                if delay is None or attempt == 7:
                    raise failure from None
                if delay >= host.remaining(self.deadline):
                    raise RateLimitTimeout(f'GitHub rate limit requires {delay}s beyond the remaining Release budget') from None
                print(f'GitHub rate limit: waiting {delay}s before retrying {method}', file=sys.stderr, flush=True)
                time.sleep(delay)
            except (http.client.HTTPException, urllib.error.URLError, TimeoutError) as error:
                raise host.GitHubCommandError(1, ['GitHub', method], stderr='GitHub connection interrupted') from error
            finally:
                if file is not None:
                    file.close()

    def api(self, path, **options):
        with self.request('https://api.github.com/repos/' + host.REPO + '/' + path, **options) as response:
            return json.load(response)

    def upload(self, release_id, name, path):
        url = f'https://uploads.github.com/repos/{host.REPO}/releases/{release_id}/assets?name={name}'
        with self.request(url, method='POST', path=path) as response:
            return json.load(response)

    def open_asset(self, asset, tag, headers):
        asset_id = asset['id']
        api = f'https://api.github.com/repos/{host.REPO}/releases/assets/{asset_id}'
        public = f'https://github.com/{host.REPO}/releases/download/{tag}/{asset["name"]}'
        for attempt in range(2):
            if asset_id not in self.signed:
                try:
                    response = self.request(public, headers=headers, redirect=True, authenticate=False)
                except host.GitHubCommandError as error:
                    if not re.match(r'HTTP (401|403|404):', error.stderr or ''):
                        raise
                    response = self.request(api, headers=headers, accept='application/octet-stream', redirect=True)
                if response.status != 302:
                    return response
                with response:
                    location = response.headers['Location']
                    hostname = urllib.parse.urlsplit(location).hostname or ''
                    if not hostname.endswith('.githubusercontent.com'):
                        raise ValueError('unexpected GitHub Release download origin')
                    self.signed[asset_id] = location
            try:
                return self.request(self.signed[asset_id], headers=headers, accept='application/octet-stream',
                                    authenticate=False)
            except host.GitHubCommandError as error:
                if not re.match(r'HTTP (401|403|404|618):', error.stderr or ''):
                    raise
                self.signed.pop(asset_id, None)
                if attempt:
                    raise

    def asset(self, asset, tag, offset=0, length=None):
        asset_id, size = asset['id'], asset['size']
        if type(asset_id) is not int or asset_id <= 0 or type(size) is not int or size <= 0:
            raise ValueError('invalid Release asset identity')
        end = size - 1 if length is None else offset + length - 1
        if not 0 <= offset <= end < size:
            raise ValueError('invalid Release asset range')
        cursor = offset
        for attempt in range(5):
            ranged = length is not None or cursor != 0
            headers = {'Range': f'bytes={cursor}-{end}'} if ranged else {}
            remaining = end - cursor + 1
            try:
                host.remaining(self.deadline)
                with self.open_asset(asset, tag, headers) as response:
                    expected = f'bytes {cursor}-{end}/{size}' if ranged else None
                    actual = response.headers.get('Content-Range')
                    if response.status != (206 if ranged else 200) or actual != expected:
                        raise ValueError(f'Release asset range mismatch (HTTP {response.status}, Content-Range {actual!r})')
                    declared = response.headers.get('Content-Length')
                    if declared is not None and seconds(declared) != remaining:
                        raise ValueError('Release asset Content-Length mismatch')
                    while remaining:
                        host.remaining(self.deadline)
                        block = response.read(min(1024 * 1024, remaining))
                        if not block:
                            raise EOFError('truncated Release asset')
                        remaining -= len(block)
                        if remaining < 0 or (remaining == 0 and response.read(1)):
                            raise ValueError('Release asset exceeded requested length')
                        cursor += len(block)
                        yield block
                    return
            except RateLimitTimeout:
                raise
            except (EOFError, ConnectionError, TimeoutError, ssl.SSLEOFError,
                    http.client.HTTPException, host.GitHubCommandError) as error:
                if isinstance(error, host.GitHubCommandError) and not (
                        error.stderr == 'GitHub connection interrupted' or re.match(r'HTTP (502|503|504):', error.stderr or '')):
                    raise
                delay = 2 ** attempt
                if attempt == 4:
                    raise
                if host.remaining(self.deadline) <= delay:
                    raise TimeoutError('remaining Release budget exhausted before asset retry') from error
                print(f'Retrying Release asset {asset_id} at byte {cursor} in {delay}s', file=sys.stderr, flush=True)
                time.sleep(delay)


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
