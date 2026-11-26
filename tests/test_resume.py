"""Offline coverage for the unattended resume flow."""
import json
import os
import shlex
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
import common  # noqa: E402
import procs  # noqa: E402

NOW = datetime(2026, 11, 25, 16, tzinfo=timezone.utc).timestamp()


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.base / 'home'
        self.product = self.home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        config_dir = self.base / 'config'
        config_dir.mkdir()
        # Exercise resume and limit handling with a fake Codex and no worker namespace.
        (config_dir / 'config.json').write_text('{"worker_sandbox": false, "isolate_codex_home": false}\n')
        self.bot = self.base / 'bot'
        self.user = self.base / 'user'
        self.env = dict(os.environ, HOME=str(self.user), KOROLEV_HOME=str(self.home),
                        KOROLEV_CONFIG_DIR=str(config_dir), KOROLEV_BOT_STATE=str(self.bot))
        self.env.pop('KOROLEV_COORDINATOR_SESSION', None)
        self.env.pop('KOROLEV_COORDINATOR_PID', None)
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

    def heartbeat(self, *args, cwd=None):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'heartbeat.py'), *args],
                                cwd=cwd, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def record(self, status='working', age=1800, **extra):
        data = {'product': 'demo', 'status': status, 'doing': 'package 13',
                'updated_at': datetime.fromtimestamp(NOW - age, timezone.utc).isoformat(), **extra}
        (self.home / 'coordinator.json').write_text(json.dumps(data))
        return data

    def read(self):
        return json.loads(self.heartbeat('show'))

    def owner_message(self, text, *, project='demo', date=NOW - 30, message_id=10):
        common.append_line(self.bot / 'INBOX.jsonl',
                           {'project': project, 'date': date, 'message_id': message_id, 'text': text})

    def watch(self, **options):
        import resume_watch

        def launch(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            self.addCleanup(process.wait, 10)
            self.addCleanup(self.release.touch)
            self.process = process
            return process

        with (patch.object(common, 'HOME', self.home), patch.object(procs, 'HOME', self.home),
              patch.object(resume_watch, 'HOME', self.home), patch.object(resume_watch, 'BOT_STATE', self.bot)):
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
        self.assertEqual(self.read()['session'], 'my-session')
        self.assertNotIn('retry_after', self.read())

    def test_auto_session_uses_newest_jsonl_for_current_directory(self):
        cwd = self.base / 'coordinator work'
        cwd.mkdir()
        projects = self.user / '.claude' / 'projects'
        transcript_dir = projects / str(cwd).replace('/', '-')
        transcript_dir.mkdir(parents=True)
        for name, mtime in [('z-older.jsonl', NOW - 20), ('a-newer.jsonl', NOW), ('ignored.json', NOW + 20)]:
            transcript = transcript_dir / name
            transcript.touch()
            os.utime(transcript, (mtime, mtime))
        (projects / 'elsewhere').mkdir()
        (projects / 'elsewhere' / 'unrelated.jsonl').touch()
        self.heartbeat('set', '--product', 'demo', '--status', 'working', '--doing', 'starting',
                       '--session', 'auto', cwd=cwd)
        self.assertEqual(self.read()['session'], 'a-newer')
        self.assertEqual(self.read()['cwd'], str(cwd))
        self.heartbeat('set', '--product', 'demo', '--status', 'idle', '--doing', 'finished', cwd=self.home)
        self.assertEqual(self.read()['session'], 'a-newer')
        self.assertEqual(self.read()['cwd'], str(cwd))

    def test_inbox_show_refreshes_idle_heartbeat_and_keeps_session(self):
        self.record('idle', session='my-session', cwd=str(self.base))
        started = int(time.time())
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'inbox.py'), '--show', '--project', 'demo'],
                                env=self.env, capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('0 message(s) recorded', result.stdout)
        record = self.read()
        self.assertEqual(record['status'], 'working')
        self.assertEqual(record['doing'], 'Reading the owner inbox')
        self.assertGreaterEqual(common.parse_timestamp(record['updated_at']), started)
        self.assertEqual(record['session'], 'my-session')
        self.assertEqual(record['cwd'], str(self.base))

    def test_touch_preserves_waiting_limit_until_one_minute_has_passed(self):
        record = self.record('waiting_limit', age=59, retry_after=common.timestamp(NOW + 600),
                             session='my-session', cwd=str(self.base))
        with patch.object(common, 'HOME', self.home), patch.object(common.time, 'time', return_value=NOW) as clock:
            common.touch_heartbeat('demo', 'reading')
            self.assertEqual(common.read_heartbeat(), record)
            clock.return_value = NOW + 1
            common.touch_heartbeat(None, 'reading')
            updated = common.read_heartbeat()
        self.assertEqual(updated['status'], 'working')
        self.assertEqual(updated['updated_at'], common.timestamp(NOW + 1))
        self.assertEqual(updated['product'], 'demo')
        self.assertEqual(updated['session'], 'my-session')
        self.assertNotIn('retry_after', updated)

    def test_fresh_or_idle_heartbeat_does_not_probe(self):
        for status, age in [('working', 1199), ('idle', 1800)]:
            with self.subTest(status=status):
                self.record(status, age)
                self.watch()
                self.assertFalse(self.calls.exists())

    def test_idle_with_unanswered_owner_message_resumes_same_chat_in_recorded_cwd(self):
        cwd = self.base / 'coordinator work'
        cwd.mkdir()
        self.record('idle', age=60, session='recorded-session', cwd=str(cwd))
        owner_text = 'Please continue ' + 'x' * 100
        self.owner_message(owner_text)
        common.append_line(self.bot / 'SENT.jsonl', {'project': 'demo', 'message_id': 9,
                                                    'time': common.timestamp(NOW - 30)})
        common.append_line(self.bot / 'SENT.jsonl', {'project': 'other', 'message_id': 11,
                                                    'time': common.timestamp(NOW - 15)})
        self.watch()
        self.release.touch()
        self.process.wait(10)
        _, start = [json.loads(line) for line in self.calls.read_text().splitlines()]
        command = start[2]
        self.assertEqual(start[0], str(cwd))
        self.assertEqual(command[:3], ['--resume', 'recorded-session', '-p'])
        self.assertNotIn('--session-id', command)
        self.assertTrue(command[3].startswith('Your usage limit has reset. Continue where you stopped.'))
        self.assertIn(str(self.product / 'STATE.md'), command[3])
        self.assertIn('procs.py status demo-builder-write', command[3])
        self.assertEqual(command[command.index('--setting-sources') + 1], '')
        self.assertIn('--strict-mcp-config', command)
        settings = json.loads(command[command.index('--settings') + 1])
        self.assertTrue(settings['disableAllHooks'])
        self.assertIn(f'Write(/{self.product}/**)', settings['permissions']['allow'])
        self.assertEqual(self.read()['session'], 'recorded-session')
        self.assertEqual(self.read()['cwd'], str(cwd))
        self.assertEqual(self.notifications, [
            ('demo', "Your usage is back. I'm continuing where I stopped: package 13"
             f'\nOwner message: “{owner_text[:79]}…”')])

    def test_idle_with_old_or_answered_messages_does_not_probe(self):
        self.record('idle', age=60)
        self.owner_message('old message', date=NOW - 70)
        self.owner_message('another project', project='other', message_id=11)
        self.watch()
        self.assertFalse(self.calls.exists())
        self.owner_message('answered message', message_id=12)
        common.append_line(self.bot / 'SENT.jsonl', {'project': 'demo', 'message_id': 13,
                                                    'time': common.timestamp(NOW - 30)})
        self.watch()
        self.assertFalse(self.calls.exists())

    def test_unfinished_package_resumes_when_builder_lock_is_free(self):
        self.record('idle', age=60)
        package = self.product / 'packages' / '01-package'
        package.mkdir(parents=True)
        (package / 'TASK.md').touch()
        handoff = package / 'HANDOFF.md'
        handoff.touch()
        self.watch()
        self.assertFalse(self.calls.exists())
        handoff.unlink()
        with patch.object(procs, 'HOME', self.home), procs.run_lock('demo-builder-write'):
            self.watch()
            self.assertFalse(self.calls.exists())
        self.watch()
        self.assertEqual(len(self.notifications), 1)

    def test_running_coordinator_or_other_watcher_prevents_probe(self):
        self.record()
        with patch.object(procs, 'HOME', self.home):
            for lock in ('coordinator', 'resume'):
                with self.subTest(lock=lock), procs.run_lock(lock):
                    self.watch()
                    self.assertFalse(self.calls.exists())

    def test_recorded_process_identity_prevents_resume(self):
        from heartbeat import process_identity

        identity = process_identity(os.getpid())
        self.record(session='my-session', cwd=str(self.home), **identity)
        self.watch()
        self.assertFalse(self.calls.exists())
        self.record(session='my-session', cwd=str(self.home), pid=identity['pid'],
                    start_time=identity['start_time'] + 1)
        self.watch()
        self.assertEqual(len(self.notifications), 1)

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

    def test_probe_limit_preserves_unanswered_message_trigger(self):
        self.record('idle', age=60, session='my-session', cwd=str(self.home))
        self.owner_message('Continue the build')
        self.response.write_text(f'Usage limit reached. Reset at {int(NOW + 300)}')
        self.watch()
        self.clock += 299
        self.watch()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.clock += 1
        self.response.write_text('{"subtype":"success","result":"OK"}')
        self.watch()
        self.assertIn('“Continue the build”', self.notifications[0][1])

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

    def test_no_recorded_session_starts_one_fresh_chat_and_notifies_once(self):
        self.record('waiting_limit', retry_after='2026-11-25T16:00:00Z')
        self.watch()
        with patch.object(procs, 'HOME', self.home):
            self.assertTrue(procs.is_busy('coordinator'))
        self.clock += 1800
        self.watch()
        resumed = self.read()
        self.assertEqual(resumed['status'], 'working')
        self.assertNotIn('retry_after', resumed)
        self.assertEqual(resumed['pid'], self.process.pid)
        UUID(resumed['session'])
        self.assertEqual(self.notifications, [
            ('demo', "Your usage is back. I'm continuing where I stopped: package 13"
             '\nI started a fresh chat because no session was recorded.')])
        self.release.touch()
        self.process.wait(10)
        probe, start = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(probe[2][:4], ['-p', 'Reply OK', '--output-format', 'json'])
        self.assertEqual(start[0], str(self.home))
        self.assertEqual(start[1], resumed['pid'])
        self.assertEqual(start[2][0], '-p')
        self.assertEqual(start[2][2:4], ['--session-id', resumed['session']])
        self.assertNotIn('--resume', start[2])
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

    def test_resume_denies_credential_paths_and_preserves_allow_rules(self):
        from resume_watch import resume_command

        user_home = self.base / 'user'
        with patch.dict(os.environ, {'HOME': str(user_home), 'KOROLEV_HOME': str(self.home)}):
            command = resume_command(('claude',), 'demo', 'test-session')
        permissions = json.loads(command[command.index('--settings') + 1])['permissions']

        paths = [f'/{user_home}/.config/korolev/**', f'/{user_home}/.ssh/**',
                 f'/{user_home}/.codex/auth.json', f'/{user_home}/.gemini/**', '**/*.env']
        self.assertCountEqual(permissions['deny'], [f'{tool}({path})' for path in paths
                                                   for tool in ('Read', 'Glob', 'Grep', 'Write', 'Edit')])
        scripts = [
            ('inbox', '--show --project demo'),
            ('heartbeat', 'set --product demo *'),
            ('handoff', '--product demo --file products/demo/*'),
            ('notify', '--project demo *'),
            ('run_codex', '--role builder --product demo *'),
            ('run_codex', '--role analyst --product demo *'),
            ('procs', 'status *'),
            ('testcount', 'check *'),
        ]
        self.assertEqual(permissions['allow'], [
            'Skill(korolev)', 'Write(./products/demo/**)', 'Edit(./products/demo/**)',
            *(f'Bash(python3 -I {shlex.quote(str(SCRIPTS / (script + ".py")))} {args})' for script, args in scripts),
            'Bash(python3 -m unittest discover -s tests)',
            *(f'Bash(git -C work/demo/repo {cmd} *)' for cmd in ('status', 'log', 'show', 'diff', 'rev-parse')),
        ])
