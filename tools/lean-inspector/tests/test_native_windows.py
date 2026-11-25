"""Exercise the native coordinator through its process and Lake pipe boundaries."""
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parents[1]
WORKER = '''
import json, os, signal, socket, subprocess, sys
from pathlib import Path
kind, args = json.loads(Path(sys.argv[1]).read_bytes())[0]
root, name = Path(args[0]), args[1]
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(str(root/'control'))
connection.sendall((name+'\\n').encode())
command = connection.makefile('rb', buffering=0).readline().strip()
if command == b'block':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    code = 'import fcntl, os, sys, time; f=open(sys.argv[1], "w"); fcntl.flock(f, fcntl.LOCK_EX); os.write(1,b"ready\\\\n"); time.sleep(300)'
    child = subprocess.Popen([sys.executable, '-c', code, root/'held-lock'], stdout=subprocess.PIPE)
    assert child.stdout.readline() == b'ready\\n'
    connection.sendall((str(child.pid)+'\\n').encode())
    child.wait()
if command == b'fail':
    print('invalid-window-' + name, file=sys.stderr)
    sys.exit(7)
assert command == b'ok'
Path(args[5]).write_text(name)
'''


class NativeWindowCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.space = tempfile.TemporaryDirectory(dir='/tmp', prefix='native-window-test-')
        self.addCleanup(self.space.cleanup)
        self.root = Path(self.space.name)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(str(self.root/'control'))
        self.listener.listen(2)
        self.listener.settimeout(10)
        (self.root/'worker.py').write_text(WORKER)

    def start(self, budget=None):
        groups = [[['produce', [str(self.root), name, '', '', '', str(self.root/(name+'.pending'))]]]
                  for name in ('first', 'second')]
        driver = ('import json,sys; from native import supervise_windows; '
                  'supervise_windows(json.loads(sys.argv[1]), [sys.executable, sys.argv[2]])')
        command = [sys.executable, '-B', '-c', driver, json.dumps(groups), str(self.root/'worker.py')]
        env = dict(os.environ, PYTHONPATH=str(HERE))
        if budget is not None:
            remaining = self.root/'budget'
            remaining.write_text(str(budget))
            env['STRATALINT_CI_COMPUTE_BUDGET'] = str(remaining)
            env.pop('STRATALINT_CI_COMPUTE_ACTIVE', None)
            command = [sys.executable, str(HERE.parent/'scripts/workflow/ci_compute.py'), 'run', '--', *command]
        self.proc = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close)
        self.sockets = {}
        for _ in range(2):
            peer, _ = self.listener.accept()
            peer.settimeout(10)
            self.addCleanup(peer.close)
            stream = peer.makefile('rb', buffering=0)
            self.addCleanup(stream.close)
            self.sockets[stream.readline().decode().strip()] = (peer, stream)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=10)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()

    def order(self, name, command):
        self.sockets[name][0].sendall((command+'\n').encode())

    def publish(self, index, name):
        self.assertEqual(self.proc.stdout.readline(), f'ready {index}\n')
        (self.root/(name+'.pending')).rename(self.root/(name+'.zip'))
        self.proc.stdin.write(f'committed {index}\n')
        self.proc.stdin.flush()

    def settle(self, success, expected):
        self.assertEqual(self.proc.wait(timeout=10) == 0, success)
        self.assertEqual(sorted(path.stem for path in self.root.glob('*.zip')), expected)
        self.assertEqual(list(self.root.glob('*.pending')), [])
        self.assertEqual(list(self.root.glob('.report-windows.*')), [])
        return self.proc.stderr.read()

    def test_later_completion_waits_for_earlier_publication(self):
        self.start()
        self.order('second', 'ok')
        self.assertEqual(self.sockets['second'][1].read(), b'')
        self.assertFalse((self.root/'second.pending').exists())
        self.order('first', 'ok')
        self.publish(0, 'first')
        self.publish(1, 'second')
        self.settle(True, ['first', 'second'])

    def test_later_failure_preserves_committed_prefix(self):
        self.start()
        self.order('second', 'fail')
        self.assertEqual(self.sockets['second'][1].read(), b'')
        self.order('first', 'ok')
        self.publish(0, 'first')
        self.assertIn('invalid-window-second', self.settle(False, ['first']))

    def block_second(self):
        self.order('second', 'block')
        return int(self.sockets['second'][1].readline())

    def assert_descendant_stopped(self):
        with (self.root/'held-lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_early_failure_reaps_speculative_tree(self):
        self.start()
        self.block_second()
        self.order('first', 'fail')
        self.assertIn('invalid-window-first', self.settle(False, []))
        self.assert_descendant_stopped()

    def test_lake_eof_cancels_speculative_tree_after_commit(self):
        self.start()
        self.block_second()
        self.order('first', 'ok')
        self.publish(0, 'first')
        self.proc.stdin.close()
        self.settle(False, ['first'])
        self.assert_descendant_stopped()

    def test_interruption_discards_uncommitted_pending_outputs(self):
        self.start()
        self.order('first', 'ok')
        self.assertEqual(self.proc.stdout.readline(), 'ready 0\n')
        self.proc.send_signal(signal.SIGTERM)
        self.settle(False, [])

    @unittest.skipUnless(shutil.which('timeout'), 'GNU timeout is required by the current CI allowance')
    def test_existing_compute_allowance_terminates_both_groups(self):
        self.start(budget=3)
        self.block_second()
        output = self.settle(False, [])
        self.assertEqual(self.proc.returncode, 124, output)
        self.assertEqual(float((self.root/'budget').read_text()), 0)
        self.assert_descendant_stopped()

    def test_independent_failure_messages_survive_cancellation(self):
        self.start()
        self.order('second', 'fail')
        self.assertEqual(self.sockets['second'][1].read(), b'')
        self.order('first', 'fail')
        output = self.settle(False, [])
        for name in ('first', 'second'):
            self.assertIn('invalid-window-' + name, output)


if __name__ == '__main__':
    unittest.main()
