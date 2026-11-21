#!/usr/bin/env python3
"""Warm only a strict descendant covered by the newest cache Release."""
from contextlib import contextmanager
import json
import os
import math
import time
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from landing import Landing
from common import entry, fail, main_lean_lock, main_warm_stamp, sha, uint

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'worktree'))
from lean_cache_release_io import PRODUCTION_PREFIX


@contextmanager
def warm_lock(op, lock, rounds):
    # The lock owner also survives an interrupted caller. EOF releases the directory
    # once the caller and any running checkout/build command have both exited.
    script = r'''set -e
. "$1"
op_init
op_lock "$2" "$3"
trap 'rmdir "$2"' EXIT
echo locked
cat >/dev/null
'''
    with subprocess.Popen(['/bin/bash', '-c', script, 'warm-lock', Path(__file__).with_name('common.sh'),
                           lock, rounds], cwd=op.ctx.main, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          start_new_session=True) as holder:
        try:
            if holder.stdout.readline() != b'locked\n':
                fail(15, 'MAIN_WARM_LOCK_FAILED')
            yield holder.stdin.fileno()
        finally:
            holder.stdin.close()


def write_atomic(path, value):
    with tempfile.TemporaryDirectory(dir=path.parent) as stage:
        temporary = Path(stage) / path.name
        temporary.write_text(value)
        temporary.replace(path)


def selected_release(op):
    pages = json.loads(op.run('gh', 'api', 'repos/' + op.ctx.slug + '/releases?per_page=100',
                              '--paginate', '--slurp').stdout)
    releases = [r for page in pages for r in page if not r.get('draft')
                and r['tag_name'].startswith(tuple(PRODUCTION_PREFIX.values()))]
    if not releases:
        return None
    tag = max(releases, key=lambda release: release['created_at'])['tag_name']
    check_tag(tag)
    with tempfile.TemporaryDirectory(prefix='op-warm __manifest__ ') as tmp:
        op.gh('release', 'download', tag, '--pattern', 'manifest.json', '--dir', tmp)
        producer = sha(json.loads((Path(tmp) / 'manifest.json').read_text())['producer_commit_sha'])
    return tag, producer


def check_tag(tag):
    for indexed, prefix in PRODUCTION_PREFIX.items():
        if tag.startswith(prefix) and re.fullmatch(r'[A-Za-z0-9._-]+', tag[len(prefix):]):
            return bool(indexed)
    fail(13, 'invalid cache tag')


def clean_dev(op):
    return (op.run('git', 'branch', '--show-current').stdout.strip() == 'dev'
            and not op.run('git', 'status', '--porcelain').stdout.strip())


def warm_round(op, lock_fd):
    if not clean_dev(op):
        print('MAIN_WARM_SKIP not a clean dev checkout', flush=True)
        return
    head = sha(op.run('git', 'rev-parse', 'HEAD').stdout.strip())
    gitdir = Path(op.run('git', 'rev-parse', '--absolute-git-dir').stdout.strip())
    pending = gitdir / 'main-warm-pending.json'
    if pending.is_file():
        record = json.loads(pending.read_text())
        base, producer, tag, phase = sha(record['base']), sha(record['producer']), record['tag'], record['phase']
        check_tag(tag)
        if phase not in ('restore', 'lean', 'stamp'):
            fail(13, 'invalid pending warm phase')
        if head not in (base, producer) or (head == base and phase != 'restore'):
            # A different checkout update superseded this job.
            pending.unlink()
        else:
            print('MAIN_WARM_RESUME', producer, tag, phase, flush=True)
    if not pending.is_file():
        op.run('git', 'fetch', '-q', 'origin', 'dev')
        selected = selected_release(op)
        if selected is None:
            print('MAIN_WARM_NO_RELEASE', flush=True)
            return
        tag, producer = selected
        if op.run('git', 'cat-file', '-e', producer, check=False).returncode:
            op.run('git', 'fetch', '-q', 'origin', producer)
        ancestry = op.run('git', 'merge-base', '--is-ancestor', head, producer, check=False)
        if ancestry.returncode not in (0, 1):
            fail(16, 'cannot check cache producer ancestry')
        if head == producer or ancestry.returncode == 1:
            print('MAIN_WARM_KEEP', head, producer, flush=True)
            return
        phase = 'restore'
        record = dict(base=head, tag=tag, producer=producer, phase=phase)
        write_atomic(pending, json.dumps(record))

    if not clean_dev(op) or op.run('git', 'rev-parse', 'HEAD').stdout.strip() != head:
        fail(14, 'MAIN_WARM_CHECKOUT_CHANGED')
    if head != producer:
        subprocess.run(['git', 'merge', '--ff-only', '-q', producer], cwd=op.ctx.main,
                       pass_fds=(lock_fd,), check=True)
        print('MAIN_WARM_ADVANCE', head, producer, tag, flush=True)
    commands = [('restore', ['make', 'lean-cache-from-github-without-mathlib', 'REFRESH_STALE=1',
                             'LEAN_CACHE_APPROVED_TAG=' + tag, 'LEAN_CACHE_APPROVED_PRODUCER=' + producer,
                             'LEAN_CACHE_INDEXED=' + str(int(check_tag(tag)))], 'lean'),
                ('lean', ['make', 'lean', 'FULL_BUILD=1', 'LEAN_TARGETS=', 'STRATALINT_WARM_DONOR=1'], 'stamp')]
    for label, command, next_phase in commands:
        if phase != label:
            continue
        op.ctx.state.mkdir(parents=True, exist_ok=True)
        with (op.ctx.state / ('main-warm-' + label + '.log')).open('w') as out:
            result = subprocess.run(command, cwd=op.ctx.main, stdout=out, stderr=subprocess.STDOUT,
                                    pass_fds=(lock_fd,))
        print('MAIN_WARM_' + label.upper() + '_EXIT=' + str(result.returncode), flush=True)
        if result.returncode:
            fail(16, 'MAIN_WARM_' + label.upper() + '_FAILED')
        phase = record['phase'] = next_phase
        write_atomic(pending, json.dumps(record))
    if not clean_dev(op) or op.run('git', 'rev-parse', 'HEAD').stdout.strip() != producer:
        fail(14, 'MAIN_WARM_CHECKOUT_CHANGED')
    built = main_warm_stamp(op.ctx.main)
    built.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(built, producer + ' ' + datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z') + '\n')
    pending.unlink()


def main(argv):
    if argv[1:] == ['--selftest']:
        from landing_selftest import run
        return run('main-warm')
    if len(argv) > 3:
        fail(10, 'main-warm.sh [HOURS [INTERVAL_SECONDS]]')
    hours = argv[1] if len(argv) > 1 else '48'
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', hours): fail(13, 'invalid hours')
    duration = float(hours) * 3600
    if not math.isfinite(duration) or duration <= 0: fail(13, 'positive finite hours required')
    interval = argv[2] if len(argv) > 2 else '1800'
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', interval): fail(13, 'invalid interval')
    op = Landing()
    lock = main_lean_lock()
    lock_rounds = str(uint(os.environ.get('OP_REFRESH_LOCK_ROUNDS', '480'), 'lock rounds'))
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        with warm_lock(op, lock, lock_rounds) as lock_fd:
            warm_round(op, lock_fd)
        remaining = deadline - time.monotonic()
        if remaining > 0: op.pause(min(float(interval), remaining))
    print('MAIN_WARM_LOOP_END', flush=True)


if __name__ == '__main__':
    sys.exit(main(sys.argv) if sys.argv[1:] == ['--selftest'] else entry(main))
