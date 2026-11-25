"""Deadline-bounded GitHub API I/O and credential-free Release asset ranges."""

from datetime import timezone
from email.utils import parsedate_to_datetime
from functools import cached_property
import json
import http.client
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import lean_cache_release as host


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
                    raise TimeoutError(f'GitHub rate limit requires {delay}s beyond the remaining Release budget') from None
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

    def asset(self, asset, tag, offset=0, length=None):
        asset_id, size = asset['id'], asset['size']
        if type(asset_id) is not int or asset_id <= 0 or type(size) is not int or size <= 0:
            raise ValueError('invalid Release asset identity')
        end = size - 1 if length is None else offset + length - 1
        if not 0 <= offset <= end < size:
            raise ValueError('invalid Release asset range')
        headers = {} if length is None else {'Range': f'bytes={offset}-{end}'}
        api = f'https://api.github.com/repos/{host.REPO}/releases/assets/{asset_id}'
        public = f'https://github.com/{host.REPO}/releases/download/{tag}/{asset["name"]}'
        for attempt in range(2):
            if asset_id not in self.signed:
                try:
                    response = self.request(public, headers=headers, redirect=True, authenticate=False)
                except host.GitHubCommandError as error:
                    if not re.search(r'HTTP (401|403|404):', error.stderr):
                        raise
                    response = self.request(api, headers=headers, accept='application/octet-stream', redirect=True)
                if response.status != 302:
                    break
                with response:
                    location = response.headers['Location']
                    hostname = urllib.parse.urlsplit(location).hostname or ''
                    if not hostname.endswith('.githubusercontent.com'):
                        raise ValueError('unexpected GitHub Release download origin')
                    self.signed[asset_id] = location
            try:
                response = self.request(self.signed[asset_id], headers=headers, accept='application/octet-stream',
                                        authenticate=False)
                break
            except host.GitHubCommandError as error:
                self.signed.pop(asset_id, None)
                if attempt or not re.search(r'HTTP (401|403|404|618):', error.stderr or ''):
                    raise
        with response:
            if length is None:
                if response.status != 200:
                    raise ValueError('unexpected GitHub asset response')
            elif response.status != 206 or response.headers.get('Content-Range') != f'bytes {offset}-{end}/{size}':
                raise ValueError(f'Release asset range mismatch (HTTP {response.status}, Content-Range {response.headers.get("Content-Range")!r})')
            remaining = end - offset + 1
            while remaining:
                host.remaining(self.deadline)
                block = response.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('truncated Release asset')
                remaining -= len(block)
                yield block
            if response.read(1):
                raise ValueError('Release asset exceeded requested length')
