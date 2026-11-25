import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill.scripts import procs


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'
LOCK = 'demo-builder-write'


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temp) / 'home'
        self.env = dict(os.environ, KOROLEV_HOME=str(self.home), KOROLEV_CONFIG_DIR=f'{temp}/config')
        self.enterContext(patch.object(procs, 'HOME', self.home))

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, '-I', str(SCRIPTS / script), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def read_cli(self, *args):
        result = self.run_script('procs.py', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_lock_is_busy_until_released(self):
        self.assertFalse(procs.is_busy(LOCK))
        with procs.run_lock(LOCK):
            self.assertTrue(procs.is_busy(LOCK))
        self.assertFalse(procs.is_busy(LOCK))

    def test_second_holder_is_refused(self):
        with procs.run_lock(LOCK):
            with self.assertRaises(procs.LockBusyError):
                with procs.run_lock(LOCK):
                    self.fail('Acquired an already-held lock')
            self.assertTrue(procs.is_busy(LOCK))

    def test_stale_file_is_free_and_wrong_start_time_does_not_match(self):
        locks = self.home / 'locks'
        locks.mkdir(parents=True)
        (locks / f'{LOCK}.lock').write_text(json.dumps({'pid': os.getpid(), 'start_time': 0}))
        self.assertFalse(procs.is_busy(LOCK))
        status = self.read_cli('status', LOCK)
        self.assertFalse(status['busy'])
        self.assertEqual(status['pid'], os.getpid())
        self.assertEqual(status['start_time'], 0)
        self.assertFalse(status['same_process'])

    def test_cli_shows_holder_pid_and_start_time(self):
        pid = os.getpid()
        stat = Path(f'/proc/{pid}/stat').read_text()
        start_time = int(stat[stat.rindex(')') + 1:].split()[19])
        with procs.run_lock(LOCK):
            status = self.read_cli('status', LOCK)
            self.assertEqual(status, {'name': LOCK, 'busy': True, 'pid': pid,
                                      'start_time': start_time, 'same_process': True})
            self.assertEqual(self.read_cli('list'), [status])

    def test_launcher_exits_75_when_the_builder_lock_is_held(self):
        product = self.home / 'products' / 'demo'
        product.mkdir(parents=True)
        prompt = product / 'TASK.md'
        prompt.write_text('# Test task\n')
        with procs.run_lock(LOCK):
            result = self.run_script('run_codex.py', '--role', 'builder', '--product', 'demo',
                                     '--prompt', str(prompt), '--out', 'HANDOFF.md')
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn('busy', result.stderr)
        self.assertIn(LOCK, result.stderr)
        self.assertFalse((product / '.runs').exists())

    def test_procs_never_launches_pgrep_or_other_commands(self):
        code = f'''\
import sys
sys.path.insert(0, {str(SCRIPTS)!r})

def reject_process_launch(event, args):
    if event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork'):
        raise AssertionError(f'procs launched a command: {{event}}')

sys.addaudithook(reject_process_launch)
import procs

with procs.run_lock({LOCK!r}):
    assert procs.is_busy({LOCK!r})
    procs.main(['status', {LOCK!r}])
    procs.main(['list'])
assert not procs.is_busy({LOCK!r})
'''
        result = subprocess.run([sys.executable, '-I', '-c', code], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
