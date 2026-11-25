"""Offline coverage for the unattended resume flow."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'
ROOT = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))
import procs  # noqa: E402

NOW = datetime(2026, 11, 25, 16, tzinfo=timezone.utc).timestamp()


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.base / 'home'
        self.product = self.home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        self.config = self.base / 'config'
        self.config.mkdir()
        self.env = dict(os.environ, KOROLEV_HOME=str(self.home), KOROLEV_CONFIG_DIR=str(self.config))
        self.clock = NOW
        self.notifications = []
        self.calls = self.base / 'calls.jsonl'
        self.response = self.base / 'response'
        self.response.write_text('{"type":"result","subtype":"success","result":"OK","is_error":false}')
        self.release = self.base / 'release'
        self.fake = self.base / 'claude.py'
        self.fake.write_text(
            'import json, os, sys, time\n'
            'from pathlib import Path\n'
            f'with Path({str(self.calls)!r}).open("a") as calls:\n'
            '    calls.write(json.dumps([os.getcwd(), os.getpid(), sys.argv[1:]]) + "\\n")\n'
            'if sys.argv[1:3] == ["-p", "Reply OK"]:\n'
            f'    print(Path({str(self.response)!r}).read_text())\n'
            'else:\n'
            f'    for _ in range(500):\n'
            f'        if Path({str(self.release)!r}).exists(): break\n'
            '        time.sleep(0.01)\n'
        )

    def heartbeat(self, *args):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'heartbeat.py'), *args],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def record(self, status='working', age=1800, **extra):
        data = {'product': 'demo', 'status': status, 'doing': 'package 13',
                'updated_at': datetime.fromtimestamp(NOW - age, timezone.utc).isoformat(), **extra}
        (self.home / 'coordinator.json').write_text(json.dumps(data))
        return data

    def read(self):
        return json.loads(self.heartbeat('show'))

    def watch(self, **options):
        import heartbeat
        import resume_watch

        def launch(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            self.addCleanup(process.wait, 10)
            self.addCleanup(self.release.touch)
            self.process = process
            return process

        with (patch.object(heartbeat, 'HOME', self.home), patch.object(procs, 'HOME', self.home),
              patch.object(resume_watch, 'HOME', self.home)):
            return resume_watch.watch(claude=(sys.executable, str(self.fake)), clock=lambda: self.clock,
                                      notify=lambda product, body: self.notifications.append((product, body)),
                                      launch=launch, **options)

    def test_heartbeat_cli_round_trips_and_clears_retry_when_idle(self):
        self.heartbeat('set', '--product', 'demo', '--status', 'waiting_limit', '--doing', 'package 13',
                       '--session', 'my-session', '--retry-after', '2026-11-25T17:00:00Z')
        waiting = self.read()
        self.assertEqual(waiting['session'], 'my-session')
        self.assertEqual(waiting['retry_after'], '2026-11-25T17:00:00Z')
        self.assertIsNotNone(datetime.fromisoformat(waiting['updated_at'].replace('Z', '+00:00')).tzinfo)
        self.heartbeat('set', '--product', 'demo', '--status', 'idle', '--doing', 'finished')
        self.assertEqual(self.read()['status'], 'idle')
        self.assertNotIn('retry_after', self.read())

    def test_fresh_or_idle_heartbeat_does_not_probe(self):
        for status, age in [('working', 1199), ('idle', 1800)]:
            with self.subTest(status=status):
                self.record(status, age)
                self.watch()
                self.assertFalse(self.calls.exists())

    def test_running_coordinator_or_other_watcher_prevents_probe(self):
        self.record()
        with patch.object(procs, 'HOME', self.home):
            for lock in ('coordinator', 'resume'):
                with self.subTest(lock=lock), procs.run_lock(lock):
                    self.watch()
                    self.assertFalse(self.calls.exists())

    def test_probe_limit_sets_retry_and_waits_until_reset(self):
        self.record()
        self.response.write_text('{"is_error":true,"result":"You hit your usage limit · resets 5pm (UTC)"}')
        self.watch()
        waiting = self.read()
        self.assertEqual(waiting['status'], 'waiting_limit')
        self.assertEqual(waiting['retry_after'], '2026-11-25T17:00:00Z')
        self.clock += 3599
        self.watch()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(self.read(), waiting)
        self.assertEqual(self.notifications, [])

    def test_unknown_limit_reset_waits_thirty_minutes(self):
        self.record()
        self.response.write_text('rate_limit_exceeded')
        self.watch()
        self.assertEqual(self.read()['retry_after'], '2026-11-25T16:30:00Z')

    def test_reset_formats_and_timezone(self):
        from resume_watch import limit_details

        cases = [
            ('You hit your limit; reset at 17:00', '2026-11-25T17:00:00Z'),
            ('You are rate limited', None),
            ('Rate limit reached; resets 12pm (America/New_York)', '2026-11-25T17:00:00Z'),
            ('Usage limit reached; try again at 2026-11-25T19:00:00+02:00', '2026-11-25T17:00:00Z'),
            (f'Insufficient credits; resets {(int(NOW) + 3600) * 1000}', '2026-11-25T17:00:00Z'),
        ]
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, {'TZ': 'UTC'}):
            time.tzset()
            for output, retry in cases:
                with self.subTest(output=output):
                    self.assertEqual(limit_details(output, NOW), (True, retry))

    def test_configured_stale_age(self):
        self.record(age=180)
        self.watch(stale_seconds=120)
        self.assertEqual(len(self.notifications), 1)

    def test_non_limit_probe_failure_does_not_resume(self):
        previous = self.record()
        self.response.write_text('{"is_error":true,"result":"Authentication failed"}')
        self.watch()
        self.assertEqual(self.read(), previous)
        self.assertEqual(self.notifications, [])

    def test_success_resumes_one_session_and_notifies_once(self):
        self.record('waiting_limit', retry_after='2026-11-25T16:00:00Z')
        self.watch()
        self.clock += 1800
        self.watch()
        resumed = self.read()
        self.assertEqual(resumed['status'], 'working')
        self.assertNotIn('retry_after', resumed)
        self.assertEqual(resumed['pid'], self.process.pid)
        UUID(resumed['session'])
        self.assertEqual(self.notifications, [('demo', 'Resumed after your usage limit reset: continuing package 13')])
        self.release.touch()
        self.process.wait(10)
        probe, start = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(probe[2][:4], ['-p', 'Reply OK', '--output-format', 'json'])
        self.assertEqual(start[0], str(self.home))
        self.assertEqual(start[1], resumed['pid'])
        self.assertEqual(start[2][0], '-p')
        self.assertIn(resumed['session'], start[2])
        self.assertIn('dontAsk', start[2])
        for expected in ('korolev skill', 'products/demo/STATE.md', 'Session handoff',
                         'products/demo/DECISIONS.md', 'inbox.py --show --project demo', 'Telegram'):
            self.assertIn(expected, start[2][1])

    def test_manual_launcher_holds_coordinator_lock(self):
        self.record()
        process = subprocess.Popen([sys.executable, '-I', str(SCRIPTS / 'heartbeat.py'), 'run',
                                    '--', sys.executable, str(self.fake)], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait, 10)
        self.addCleanup(self.release.touch)
        deadline = time.monotonic() + 3
        while not self.calls.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(process.poll())
        self.watch()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(self.notifications, [])

    def run_codex(self, output, code):
        codex = self.base / 'codex'
        codex.write_text(f'#!{sys.executable}\nimport sys\nsys.stdin.read()\n'
                         f'print({output!r}, file=sys.stderr)\nsys.exit({code})\n')
        codex.chmod(0o700)
        (self.config / 'config.json').write_text('{"worker_sandbox":false,"isolate_codex_home":false}')
        prompt = self.product / 'TASK.md'
        prompt.write_text('# A fake task')
        result = subprocess.run(
            [sys.executable, '-I', str(SCRIPTS / 'run_codex.py'), '--role', 'analyst', '--product', 'demo',
             '--cwd', str(self.base), '--prompt', str(prompt), '--out', 'HANDOFF.md'],
            env=dict(self.env, PATH=str(self.base)), capture_output=True, text=True, timeout=10,
        )
        row = json.loads(result.stdout)
        self.assertEqual(json.loads((self.product / 'USAGE.jsonl').read_text().splitlines()[-1]), row)
        return result.returncode, row

    def test_codex_limit_events_return_75_and_record_reset(self):
        cases = [
            json.dumps({'type': 'turn.failed', 'error': {'message': 'Usage limit reached; reset at 2030-01-02T17:00:00Z'}}),
            f'Credit limit reached. resets {int(datetime(2030, 1, 2, 17, tzinfo=timezone.utc).timestamp())}',
            json.dumps({'type': 'error', 'code': 'usage_limit_reached', 'resets_at': '2030-01-02T17:00:00Z'}),
        ]
        for output in cases:
            with self.subTest(output=output):
                code, row = self.run_codex(output, 1)
                self.assertEqual(code, 75)
                self.assertEqual(row['exit'], 75)
                self.assertIs(row['limit_hit'], True)
                self.assertEqual(row['retry_after'], '2030-01-02T17:00:00Z')

    def test_codex_agent_text_does_not_turn_an_unrelated_failure_into_limit(self):
        code, row = self.run_codex(json.dumps({'type': 'item.completed',
                                              'item': {'type': 'agent_message', 'text': 'Usage limit reached'}}), 7)
        self.assertEqual(code, 7)
        self.assertNotIn('limit_hit', row)

    def test_installer_only_copies_units_when_requested(self):
        stub = self.base / 'python3'
        stub.write_text('#!/bin/sh\nexit 0\n')
        stub.chmod(0o700)
        config = self.base / 'xdg-config'
        env = dict(self.env, HOME=str(self.base / 'user'), XDG_CONFIG_HOME=str(config),
                   PATH=f'{self.base}:/usr/bin:/bin')
        units = config / 'systemd' / 'user'
        command = ['/bin/bash', str(ROOT / 'install.sh')]
        subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=10)
        self.assertFalse(units.exists())
        result = subprocess.run([*command, '--with-resume'], env=env, capture_output=True,
                                text=True, check=True, timeout=10)
        self.assertEqual({path.name for path in units.iterdir()},
                         {'korolev-resume.service', 'korolev-resume.timer'})
        self.assertIn('systemctl --user enable --now korolev-resume.timer', result.stdout)

    def test_doctor_reports_timer_enablement(self):
        (self.config / 'config.json').write_text('{"worker_sandbox":false,"isolate_codex_home":false}')
        for name in ('codex', 'git'):
            stub = self.base / name
            stub.write_text('#!/bin/sh\nexit 0\n')
            stub.chmod(0o700)
        systemctl = self.base / 'systemctl'
        env = dict(self.env, HOME=str(self.base / 'user'), PATH=str(self.base))
        for state, code, level in [('enabled', 0, 'PASS'), ('disabled', 1, 'WARN')]:
            with self.subTest(state=state):
                systemctl.write_text(f'#!/bin/sh\necho {state}\nexit {code}\n')
                systemctl.chmod(0o700)
                result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'doctor.py'), '--offline'],
                                        env=env, capture_output=True, text=True, check=True, timeout=10)
                self.assertIn(f'{level} resume timer: {state}', result.stdout)
