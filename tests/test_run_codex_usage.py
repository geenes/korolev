import fcntl
import hashlib
import json
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skill.scripts import onboard, run_codex

if __package__:
    from .git_fixture import git_fixture
    from .test_sandbox import EXECUTABLE_FIXTURE
else:
    from git_fixture import git_fixture
    from test_sandbox import EXECUTABLE_FIXTURE


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class RunCodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())

    def write_events(self, *usages, extra_events=()):
        path = self.base / 'events.jsonl'
        turn_events = [{'type': 'turn.completed', 'usage': usage} for usage in usages]
        path.write_text(''.join(json.dumps(event) + '\n' for event in (*extra_events, *turn_events)))
        return path

    def run_worker(self, *usages, config=None, exit_code=0, worker_exit=None, extra_events=(),
                   worker_stderr='', stall=False, task='Test task', context=()):
        events = self.write_events(*usages, extra_events=extra_events)
        worker_exit = exit_code if worker_exit is None else worker_exit
        fake_codex = self.base / 'codex'
        fake_codex.write_text(
            f'#!{sys.executable}\n'
            'import signal, sys\n'
            'from pathlib import Path\n'
            + (f'signal.signal(signal.SIGTERM, lambda *_: sys.exit({worker_exit}))\n' if stall else '')
            + f'Path({str(self.base / "worker-prompt")!r}).write_text(sys.stdin.read())\n'
            "Path(sys.argv[sys.argv.index('-o') + 1]).write_text('Deliverable')\n"
            f'print(Path({str(events)!r}).read_text(), end="", flush=True)\n'
            f'print({worker_stderr!r}, end="", file=sys.stderr, flush=True)\n'
            + ('signal.pause()\n' if stall else '')
            + f'sys.exit({worker_exit})\n'
        )
        fake_codex.chmod(0o700)
        home = self.base / 'home'
        product = home / 'products' / 'demo'
        product.mkdir(parents=True, exist_ok=True)
        prompt = product / 'TASK.md'
        prompt.write_text(task)
        config_dir = self.base / 'config'
        config_dir.mkdir(exist_ok=True)
        # These tests exercise usage reporting through a fake Codex.
        settings = {**(config or {}), 'worker_sandbox': False, 'worker_network': 'host'}
        (config_dir / 'config.json').write_text(json.dumps(settings))
        env = dict(self.env, KOROLEV_HOME=str(home), KOROLEV_CONFIG_DIR=str(config_dir),
                   CODEX_HOME=str(self.base / 'user-codex-home'),
                   KOROLEV_CODEX_HOME=str(self.base / 'codex-home'), PATH=str(self.base))
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'run_codex.py'),
                                 '--role', 'analyst', '--prompt-only', '--product', 'demo', '--cwd', str(self.base),
                                 '--prompt', str(prompt), '--out', 'HANDOFF.md',
                                 *(['--context', *context] if context else [])],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, exit_code, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(json.loads((product / 'USAGE.jsonl').read_text().splitlines()[-1]), record)
        return record, result.stderr

    def test_usage_from_one_turn(self):
        usage = {'input_tokens': 12, 'cached_input_tokens': 10, 'output_tokens': 2}
        self.assertEqual(run_codex.usage_from(self.write_events(usage)), usage)

    def test_usage_sums_two_turns(self):
        events = self.write_events({'input_tokens': 12, 'output_tokens': 2},
                                   {'input_tokens': 20, 'output_tokens': 3})
        self.assertEqual(run_codex.usage_from(events), {'input_tokens': 32, 'output_tokens': 5})

    def test_missing_usage_is_null(self):
        record, stderr = self.run_worker(None)
        self.assertEqual(record['worker_exit'], 0)
        self.assertIsNone(record['usage'])
        self.assertIsNone(record['uncached_input_tokens'])
        self.assertIsNone(record['reviewed_sha'])
        self.assertEqual(record.get('commands_run'), 0)
        self.assertEqual(record['network'], 'host')
        self.assertIsNone(record['egress_log'])
        self.assertIn('Host abstract sockets and local ports are reachable', record['sandbox_warnings'][0])
        self.assertNotIn('input_tokens_per_turn', record)
        self.assertNotIn('warning', record)
        self.assertEqual(stderr, '')

    def assert_limit_diagnostic(self, record, stderr, hint):
        self.assertIs(record['limit_diagnostic'], True)
        self.assertNotIn('limit_hit', record)
        self.assertNotIn('retry_after', record)
        self.assertIn('WARNING: Usage-limit diagnostic', stderr)
        self.assertNotIn(hint, stderr)

    def test_timeout_with_429_keeps_exit_124(self):
        hint = 'HTTP 429 while retrying'
        config = {'roles': {'analyst': {'timeout_minutes': 0.03}}}
        terminal = {'type': 'error', 'message': 'Usage limit reached'}
        record, stderr = self.run_worker(config=config, exit_code=124, worker_exit=19,
                                         extra_events=[terminal], worker_stderr=hint, stall=True)
        self.assertEqual((record['exit'], record['worker_exit']), (124, 19))
        self.assertIs(record['timed_out'], True)
        self.assert_limit_diagnostic(record, stderr, hint)

    def test_completed_turn_supersedes_limit_error(self):
        hint = 'Usage limit reached'
        for exit_code, error in ((0, {'code': 429}), (1, {'message': hint})):
            with self.subTest(exit_code=exit_code):
                record, stderr = self.run_worker(None, exit_code=exit_code,
                                                 extra_events=[{'type': 'error', **error}])
                self.assertEqual((record['exit'], record['worker_exit']), (exit_code, exit_code))
                self.assert_limit_diagnostic(record, stderr, hint)

    def test_raw_stderr_quota_keeps_worker_failure(self):
        hint = 'Credit limit reached. Resets at 2030-01-02T17:00:00Z'
        record, stderr = self.run_worker(exit_code=1, worker_stderr=hint)
        self.assertEqual((record['exit'], record['worker_exit']), (1, 1))
        self.assert_limit_diagnostic(record, stderr, hint)

    def test_terminal_limit_overrides_worker_failure_and_records_its_retry(self):
        earlier = {'type': 'error', 'message': 'Rate limit reached. Retry after 2030-01-01T17:00:00Z'}
        for terminal in (
            {'type': 'turn.failed', 'error': {'message': 'Usage limit reached. Reset at 2030-01-02T17:00:00Z'}},
            {'type': 'error', 'code': 'usage_limit_reached', 'resets_at': '2030-01-02T17:00:00Z'},
        ):
            with self.subTest(event_type=terminal['type']):
                record, stderr = self.run_worker(exit_code=75, worker_exit=1, extra_events=[earlier, terminal])
                self.assertEqual((record['exit'], record['worker_exit']), (75, 1))
                self.assertIs(record['limit_hit'], True)
                self.assertEqual(record['retry_after'], '2030-01-02T17:00:00Z')
                self.assertNotIn('limit_diagnostic', record)
                self.assertEqual(stderr, '')

    def test_non_limit_terminal_failure_does_not_take_earlier_retry(self):
        hint = 'Usage limit reached. Reset at 2030-01-02T17:00:00Z'
        record, stderr = self.run_worker(exit_code=1, extra_events=[
            {'type': 'error', 'message': hint},
            {'type': 'turn.failed', 'error': {'message': 'Disconnected'}},
        ])
        self.assert_limit_diagnostic(record, stderr, hint)

    def test_task_text_and_generic_429_are_diagnostic_only(self):
        hint = 'Usage limit reached'
        for event in (
            {'type': 'item.completed', 'item': {'type': 'error', 'message': hint}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': hint}},
            {'type': 'error', 'message': 'Task failed', 'item': {'text': hint}},
            {'type': 'error', 'message': 'unexpected status 429 Too Many Requests'},
            {'type': 'error', 'code': 'invalid_config', 'message': 'Failed to parse rate limit headers'},
        ):
            with self.subTest(event=event):
                record, stderr = self.run_worker(exit_code=1, extra_events=[event])
                self.assert_limit_diagnostic(record, stderr, event.get('message', hint))

    def test_worker_success_keeps_exit_0_even_with_terminal_limit(self):
        hint = 'Usage limit reached'
        record, stderr = self.run_worker(extra_events=[{'type': 'error', 'message': hint}])
        self.assertEqual((record['exit'], record['worker_exit']), (0, 0))
        self.assert_limit_diagnostic(record, stderr, hint)

    def test_worker_header_sanitizes_display_name(self):
        product = self.base / 'home/products/demo'
        product.mkdir(parents=True)
        raw = "**host:dir(v1)/repo?x=fixture** 1.[fe80::1%eth0]:demo.git?token=fixture"
        safe = "**host:dir(v1)/repo** 1.[fe80::1%eth0]:demo.git"
        (product / 'INVENTORY.md').write_text(raw)
        for display, expected in (
            (raw, safe),
            ('(https://user:123)-tok@host/x.git', 'demo'),
            ('Demo _https://user:fixture@host/repo?token=fixture#private_', 'Demo _https://host/repo_'),
            ('https://user:fixture@[broken', 'demo'),
        ):
            with self.subTest(expected=expected):
                (product / 'product.json').write_text(json.dumps({'display': display}))
                self.run_worker(task=raw, context=('INVENTORY.md',))
                prompt = (self.base / 'worker-prompt').read_text()
                self.assertIn(f'for the project "{expected}"', prompt)
                self.assertIn(f'----- TASK -----\n{safe}', prompt)
                bundle = max((product / '.runs').glob('*-ctx'), key=lambda path: path.stat().st_mtime_ns)
                self.assertEqual((bundle / 'INVENTORY.md').read_text(), safe)
                for secret in ('user:fixture', 'user:123', '-tok@', 'token=fixture', '#private', 'x=fixture'):
                    self.assertNotIn(secret, prompt)

    def test_uncached_input_sums_turns_and_subtracts_cached_tokens(self):
        record, _ = self.run_worker({'input_tokens': 12, 'cached_input_tokens': 10},
                                    {'input_tokens': 20, 'cached_input_tokens': 3})
        self.assertEqual(record.get('uncached_input_tokens'), 19)

    def test_missing_cached_tokens_count_as_zero(self):
        record, _ = self.run_worker({'input_tokens': 12})
        self.assertEqual(record.get('uncached_input_tokens'), 12)

    def test_warns_when_total_exceeds_default_and_records_average(self):
        record, stderr = self.run_worker({'input_tokens': 750_001, 'cached_input_tokens': 750_001},
                                         {'input_tokens': 750_000, 'cached_input_tokens': 750_000})
        self.assertEqual(record.get('input_tokens_per_turn'), 750_000.5)
        warning = record.get('warning', '')
        self.assertTrue(warning.startswith('large context: '), warning)
        self.assertEqual(stderr.splitlines(), [f'WARNING: {warning}'])

    def test_no_warning_at_or_below_default(self):
        for tokens in (1_499_999, 1_500_000):
            with self.subTest(tokens=tokens):
                record, stderr = self.run_worker({'input_tokens': tokens, 'cached_input_tokens': tokens})
                self.assertNotIn('warning', record)
                self.assertEqual(stderr, '')
                self.assertEqual(record.get('input_tokens_per_turn'), tokens)

    def test_configured_threshold_warns_without_changing_failure_status(self):
        record, stderr = self.run_worker({'input_tokens': 101},
                                         config={'context_warn_tokens': 100}, exit_code=7)
        warning = record.get('warning', '')
        self.assertTrue(warning.startswith('large context: '), warning)
        self.assertEqual(stderr.splitlines(), [f'WARNING: {warning}'])
        self.assertEqual(record['exit'], 7)

    def test_default_uncached_threshold_warns_only_above_400000(self):
        for uncached in (400_000, 400_001):
            with self.subTest(uncached=uncached):
                record, stderr = self.run_worker({'input_tokens': uncached + 100, 'cached_input_tokens': 100})
                self.assertNotIn('warning', record)
                if uncached == 400_000:
                    self.assertNotIn('uncached_warning', record)
                    self.assertEqual(stderr, '')
                else:
                    warning = record.get('uncached_warning', '')
                    self.assertIn('400,001 uncached input tokens', warning)
                    self.assertEqual(stderr.splitlines(), [f'WARNING: {warning}'])
                    self.assertEqual(record['exit'], 0)

    def test_configured_uncached_threshold_can_warn_alongside_context_without_changing_exit(self):
        record, stderr = self.run_worker({'input_tokens': 101, 'cached_input_tokens': 50},
                                         config={'context_warn_tokens': 100, 'uncached_warn_tokens': 50},
                                         exit_code=7)
        self.assertIn('51 uncached input tokens', record.get('uncached_warning', ''))
        self.assertEqual(stderr.splitlines(), [
            f"WARNING: {record['warning']}",
            f"WARNING: {record['uncached_warning']}",
        ])
        self.assertEqual(record['exit'], 7)

    def test_counts_completed_commands_and_tools_without_turn_usage(self):
        record, _ = self.run_worker(extra_events=[
            {'type': 'item.started', 'item': {'type': 'command_execution'}},
            {'type': 'item.completed', 'item': {'type': 'command_execution'}},
            {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'status': 'failed'}},
            {'type': 'item.completed', 'item': {'type': 'dynamic_tool_call'}},
            {'type': 'item.completed', 'item': {'type': 'collab_tool_call'}},
            {'type': 'item.completed', 'item': {'type': 'web_search'}},
            {'type': 'item.completed', 'item': {'type': 'image_generation'}},
            {'type': 'item.completed', 'item': {'type': 'image_view'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message'}},
            {'type': 'item.completed', 'item': {'type': 'file_change'}},
            {'type': 'item.completed', 'item': {'type': 'future_event'}},
            {'type': 'item.completed', 'item': {}},
            {'type': 'item.completed', 'item': None},
            {'type': 'item.completed'},
            {'item': {'type': 'command_execution'}},
            None,
        ])
        self.assertEqual(record.get('commands_run'), 7)


class ContextBundleTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.product = self.base / 'product'
        self.config = self.base / 'config'
        self.config.mkdir()
        self.enterContext(patch.dict(run_codex.load_config.__globals__, CONFIG_DIR=self.config))
        self.runs = self.product / '.runs'
        self.runs.mkdir(parents=True)
        self.task = self.product / 'TASK.md'
        self.task.write_text('# Test task\n')

    def bundle(self, *paths, prompt=None, skill_fd=None):
        with ExitStack() as stack:
            product_fd = stack.enter_context(run_codex.open_directory(self.product))
            runs_fd = stack.enter_context(run_codex.private_runs_dir(product_fd))
            if skill_fd is None:
                skill_fd = stack.enter_context(run_codex.open_directory(run_codex.SKILL))
            bundle, task, included = run_codex.build_context_bundle(
                self.product, product_fd, skill_fd, runs_fd, 'stamp', prompt or self.task, paths)
            return self.runs / bundle.name, task, included

    def test_bundle_copies_only_named_files_and_projects_state_for_every_request(self):
        notes = self.product / 'notes'
        notes.mkdir()
        (notes / 'With spaces.md').write_bytes(b'\x00fixture\xff')
        plan = self.product / 'PLAN.md'
        plan.write_text('Plan')
        (self.product / 'INBOX.jsonl').touch()
        (self.product / 'approvals.sqlite').touch()
        secret = 'ghp_' + 'a' * 36
        sha = 'a' * 40
        email = 'maintainer' + '@' + 'private.tld'
        private_home = '/' + 'home' + '/account'
        key_marker = '-----BEGIN ' + 'PRIVATE KEY-----'
        state = self.product / 'STATE.md'
        state.write_text(f'# Product state {secret} {email} {private_home}/path INTERNAL\n'
                         'Narrative with private details\n\n## Session handoff\n\n'
                         '- UTC: 2026-11-26T10:24:39Z\n'
                         f'- Clone: HEAD {sha}, branch {email}, working tree clean.\n'
                         '- Pending lessons: 5\n\n### Recent runs\n'
                         f'- builder | exit 0 | commit {sha} | uncached input tokens 1234\n'
                         '\n### Pending owner questions (DECISIONS.md)\n- Pending owner question\n'
                         '\n### Recent inbox messages\n- Message 1: private Telegram preview\n'
                         '\n### Coordinator notes\n> A private sentence in the handoff note.\n'
                         f'> ## Quoted heading\n> {key_marker}\n> fixture-key-body\n'
                         f'\n## {private_home}/heading\n')
        self.task.write_text('# Test task\nContext: "notes/With spaces.md", PLAN.md, STATE.md\n')
        bundle, task, paths = self.bundle(str(plan))
        self.assertRegex(bundle.name, r'^stamp-.+-ctx$')
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
        self.assertEqual(paths, [Path('PLAN.md'), Path('notes/With spaces.md'), Path('STATE-brief.md')])
        self.assertEqual(task, self.task.read_text())
        self.assertEqual({path.relative_to(bundle) for path in bundle.rglob('*') if path.is_file()},
                         {Path('TASK.md'), Path('STATE-brief.md'), *paths})
        self.assertEqual((bundle / 'TASK.md').read_bytes(), self.task.read_bytes())
        self.assertEqual((bundle / paths[0]).read_bytes(), plan.read_bytes())
        self.assertEqual((bundle / paths[1]).read_bytes(), b'\x00fixture\xff')
        brief = (bundle / 'STATE-brief.md').read_text()
        self.assertEqual(brief.splitlines(), [
            '# Product state', '## Session handoff', '- UTC: 2026-11-26T10:24:39Z',
            f'- Clone: HEAD {sha}, branch named, working tree clean.', '- Pending lessons: 5', '### Recent runs',
            f'- builder | exit 0 | commit {sha}',
        ])
        (self.config / 'privacy.json').write_text('{"deny": ["BUILDER", "internal"]}')
        redacted, _, _ = self.bundle()
        self.assertEqual((redacted / 'STATE-brief.md').read_text(), brief.replace('builder', '[REDACTED]'))
        (self.config / 'privacy.json').unlink()
        self.task.write_text('# Test task\n')
        for requests, expected in (((), []), ((str(state),), [Path('STATE-brief.md')])):
            with self.subTest(requests=requests):
                bundle, _, paths = self.bundle(*requests)
                self.assertEqual(paths, expected)
                self.assertEqual({path.name for path in bundle.iterdir()}, {'TASK.md', 'STATE-brief.md'})
                self.assertEqual((bundle / 'STATE-brief.md').read_text(), brief)
        with self.assertRaisesRegex(ValueError, 'prompt name is reserved'):
            self.bundle(prompt=state)
        state.unlink()
        with self.assertRaises(FileNotFoundError):
            self.bundle('STATE.md')

    def test_credential_names_are_refused_in_context(self):
        (self.product / 'nested').mkdir()
        for name in ('.env', 'nested/development.PEM', 'server.example.com.key', 'app.db', 'ｃredentials-app.json',
                     'server.KEY', 'ＮＯＴＥＳ.md', '．private/anything.md', 'ＩＮＢＯＸ.jsonl', 'uſage.jsonl'):
            (self.product / name).parent.mkdir(exist_ok=True)
            (self.product / name).touch()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'private product data'):
                self.bundle(name)
        (self.product / '.env.example').touch()
        self.assertEqual(self.bundle('.env.example')[2], [Path('.env.example')])

    def test_normalized_bundle_collisions_and_state_aliases(self):
        for name in ('taſk.md', 'State-Brief.md', 'ﬂags.md', 'FLAGS.md', 'ＳＴＡＴＥ.md'):
            (self.product / name).touch()
        for names in (('taſk.md',), ('State-Brief.md',), ('ﬂags.md', 'FLAGS.md')):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, 'conflicts'):
                self.bundle(*names)
        with self.assertRaisesRegex(ValueError, 'only the product STATE.md'):
            self.bundle('ＳＴＡＴＥ.md')
        with self.assertRaisesRegex(ValueError, 'prompt name is reserved'):
            self.bundle(prompt=self.product / 'State-Brief.md')

    def test_inventory_sources_are_sanitized_when_generated_and_bundled(self):
        urls = ('https://user:tok@host/x.git?token=fixture#private', 'ssh://user@host?key=fixture#private',
                'user!fixture@host:x.git?key=fixture#private', 'https://host/plain.git')
        public_urls = ('https://host/x.git', 'ssh://host', 'host:x.git', 'https://host/plain.git')
        adjacent = f'[{urls[0]}]({urls[1]})[{urls[2]}]({urls[0]})'
        safe_adjacent = f'[{public_urls[0]}]({public_urls[1]})[{public_urls[2]}]({public_urls[0]})'
        examples = (
            (f'_{urls[0]}_ **{urls[2]}**.', f'_{public_urls[0]}_ **{public_urls[2]}**.'),
            (f'1.{urls[0]} 2{urls[0]} +{urls[0]} -{urls[0]} .{urls[0]}',
             f'1.{public_urls[0]} 2{public_urls[0]} +{public_urls[0]} -{public_urls[0]} .{public_urls[0]}'),
            (f'git+{urls[0]}', f'git+{public_urls[0]}'),
            ('[repo](user@host:dir(v1)/x.git?token=fixture#private)', '[repo](host:dir(v1)/x.git)'),
            ('host:x(v1).git?token=fixture#private', 'host:x(v1).git'),
            ('**host:dir(v1)/repo?x=fixture**', '**host:dir(v1)/repo**'),
            ("'host:x.git?token=fixture'", "'host:x.git'"),
            ('~~host:x.git?token=fixture~~', '~~host:x.git~~'),
            ('_[fe80::1%eth0]:demo.git?token=fixture_', '_[fe80::1%eth0]:demo.git_'),
            ('1.[fe80::1%eth0]:demo.git?token=fixture', '1.[fe80::1%eth0]:demo.git'),
            ('user@[fe80::1%eth0]:x.git?token=fixture#private', '[fe80::1%eth0]:x.git'),
            ('/tmp/plain.git host:plain.git', '/tmp/plain.git host:plain.git'),
            (adjacent, safe_adjacent),
        )
        raw = '\n'.join((*urls, *(example for example, _ in examples))) + '\n'
        expected = '\n'.join((*public_urls, *(public for _, public in examples))) + '\n'
        with patch.object(onboard, 'run', return_value=''), patch.object(onboard, 'display_name', return_value='Demo'):
            for source, public in zip(urls, public_urls):
                with self.subTest(source=source):
                    result = onboard.inventory(self.base, 'demo', source, 'fixture')
                    self.assertIn(f'Source: `{public}`', result)
                    if source != public:
                        self.assertNotIn(source, result)
            with patch.object(onboard, 'run', side_effect=lambda cmd, **_: raw if '%h %ad %s' in cmd[-2] else ''):
                self.assertIn(expected, onboard.inventory(self.base, 'demo', urls[0], 'fixture'))
        heading = self.base / 'README.md'
        heading.write_text('# https://user:' + 'x' * 120 + '@host/x.git\n')
        self.assertEqual(onboard.first_heading(heading), 'https://host/x.git')
        legacy = self.product / 'INVENTORY.md'
        legacy.write_text(raw)
        bundle, _, _ = self.bundle('INVENTORY.md')
        self.assertEqual((bundle / legacy.name).read_text(), expected)
        self.assertEqual(self.bundle(prompt=legacy)[1], expected)
        self.task.write_text(raw)
        self.assertEqual(self.bundle()[1], expected)

    def test_ambiguous_inventory_line_reports_location_without_contents(self):
        legacy = self.product / 'nested' / 'INVENTORY.md'
        legacy.parent.mkdir()
        for ambiguous in ('https://user:tok@[broken', 'https://host/x;https://user:tok@second/y',
                          'https://user"@host/x', '(host:repo)?key=fixture',
                          '(https://user:123):pass@host/x.git', '(https://user:123)-tok@host/x.git',
                          '[https://user:123]:pass@host/x.git', '[https://user:123](:pass@host/x.git)',
                          '"https://user:123":pass@host/x.git',
                          'user:tok@[fe80::1%eth0:repo.git?key=fixture'):
            legacy.write_text(f'# Inventory\n\n{ambiguous}\n')
            for via_prompt in (False, True):
                with self.subTest(ambiguous=ambiguous, via_prompt=via_prompt):
                    with self.assertRaisesRegex(ValueError, r'nested/INVENTORY.md at line 3') as error:
                        self.bundle(prompt=legacy) if via_prompt else self.bundle('nested/INVENTORY.md')
                    self.assertNotIn('user:tok', str(error.exception))
                    self.assertNotIn('user:123', str(error.exception))
                    self.assertNotIn('key=fixture', str(error.exception))
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_legacy_product_metadata_is_reserved_in_prompts_and_context(self):
        for name in ('product.json', 'ＰＲＯＤＵＣＴ.json'):
            metadata = self.product / name
            metadata.write_text('{"source": "https://user:tok@host/path"}')
            for via_prompt in (False, True):
                with self.subTest(name=name, via_prompt=via_prompt), self.assertRaisesRegex(
                        ValueError, 'private product data'):
                    self.bundle(prompt=metadata) if via_prompt else self.bundle(name)
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_state_inode_cannot_be_used_through_another_name(self):
        (self.product / 'STATE.md').write_text('# Private heading')
        original = run_codex.open_context_file

        def alias(root, path):
            return original(root, Path('STATE.md') if path == Path('alias.md') else path)

        with patch.object(run_codex, 'open_context_file', alias):
            with self.assertRaisesRegex(ValueError, 'STATE.md may only be included as a brief'):
                self.bundle('alias.md')
            with self.assertRaisesRegex(ValueError, 'STATE.md may only be included as a brief'):
                self.bundle(prompt=self.product / 'alias.md')

    def test_external_prompts_require_trusted_templates_and_refuse_descendant_symlinks(self):
        checkout = self.base / 'checkout'
        checkout.mkdir()
        private = self.base / 'private.md'
        private.write_text('Private fixture')
        link = checkout / 'shortcut.md'
        link.symlink_to(private)
        installed = self.base / 'installed'
        installed.symlink_to(SCRIPTS.parent, target_is_directory=True)

        def build(prompt):
            return self.bundle(prompt=prompt)

        with self.assertRaisesRegex(ValueError, 'inside the installed skill templates'):
            build(link)
        template = SCRIPTS.parent / 'templates/prompts/analysis.md'
        for path in (template, installed / 'templates/prompts/analysis.md'):
            self.assertEqual(build(path)[1], template.read_text())
        skill = self.base / 'skill'
        templates = skill / 'templates'
        templates.mkdir(parents=True)
        (templates / 'linked.md').symlink_to(private)
        (templates / 'linked-dir').symlink_to(self.base, target_is_directory=True)
        (templates / 'linked-skill').symlink_to(skill, target_is_directory=True)
        (templates / 'good.md').touch()
        skill_alias = self.base / 'skill-alias'
        skill_alias.symlink_to(skill, target_is_directory=True)
        with patch.object(run_codex, 'SKILL', skill):
            self.assertEqual(build(skill_alias / 'templates/good.md')[1], '')
            for name in ('linked.md', 'linked-dir/private.md', 'linked-skill/templates/good.md'):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'cannot open trusted prompt'):
                    build(templates / name)

    def test_external_prompt_read_survives_installation_directory_swap(self):
        skill = self.base / 'canonical' / 'skill'
        templates = skill / 'templates'
        templates.mkdir(parents=True)
        prompt = templates / 'review.md'
        prompt.write_text('Approved task')
        replacement = self.base / 'replacement' / 'skill' / 'templates'
        replacement.mkdir(parents=True)
        (replacement / prompt.name).write_text('Private replacement')
        original = run_codex.open_directory

        @contextmanager
        def swapped(path, **kwargs):
            if path == Path('templates'):
                skill.parent.rename(self.base / 'previous')
                skill.parent.symlink_to(replacement.parent.parent, target_is_directory=True)
            with original(path, **kwargs) as fd:
                yield fd

        with patch.object(run_codex, 'SKILL', skill), patch.object(run_codex, 'open_directory', swapped):
            self.assertEqual(self.bundle(prompt=prompt)[1], 'Approved task')

    def test_external_prompt_refuses_ancestor_redirect_before_selection(self):
        skill = self.base / 'installation' / 'skill'
        replacement = self.base / 'replacement' / 'skill'
        for installation in (skill, replacement):
            (installation / 'templates').mkdir(parents=True)
            (installation / 'templates/review.md').write_text(str(installation))
        with run_codex.open_directory(skill) as skill_fd:
            skill.parent.rename(self.base / 'previous')
            skill.parent.symlink_to(replacement.parent, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'inside the installed skill templates'):
                self.bundle(prompt=skill / 'templates/review.md', skill_fd=skill_fd)
            self.assertEqual(self.bundle(prompt=self.base / 'previous/skill/templates/review.md',
                                         skill_fd=skill_fd)[1], str(skill))

    def test_prompt_limit_checks_the_read_as_well_as_file_size(self):
        self.task.write_text('x' * 9)
        with patch.object(run_codex, 'CONTEXT_LIMIT', 8), self.assertRaisesRegex(ValueError, 'exceeds'):
            self.bundle()
        with self.task.open('rb') as stream, patch.object(run_codex, 'CONTEXT_LIMIT', 8):
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                run_codex.bounded_read(stream, SimpleNamespace(st_size=0))
            self.assertEqual(stream.tell(), 9)

    def test_runs_directory_permissions_and_identity(self):
        self.runs.chmod(0o755)
        with run_codex.open_directory(self.product) as product_fd:
            with run_codex.private_runs_dir(product_fd):
                pass
        self.assertEqual(self.runs.stat().st_mode & 0o777, 0o700)
        with patch.object(run_codex.os, 'getuid', return_value=os.getuid() + 1):
            with run_codex.open_directory(self.product) as product_fd, self.assertRaisesRegex(
                    ValueError, 'owned by the launcher user'), run_codex.private_runs_dir(product_fd):
                pass
        self.runs.rmdir()
        self.runs.symlink_to(self.base, target_is_directory=True)
        with run_codex.open_directory(self.product) as product_fd, self.assertRaisesRegex(
                ValueError, 'without symlinks'), run_codex.private_runs_dir(product_fd):
            pass

    def test_run_storage_remains_bound_to_open_directory(self):
        old = self.runs.with_name('previous-runs')
        with run_codex.open_directory(self.product) as product_fd, run_codex.private_runs_dir(product_fd) as runs_fd, \
                run_codex.open_directory(run_codex.SKILL) as skill_fd:
            self.runs.rename(old)
            self.runs.mkdir()
            bundle, _, _ = run_codex.build_context_bundle(self.product, product_fd, skill_fd, runs_fd, 'stamp', self.task, [])
            self.assertTrue((old / bundle.name / 'TASK.md').is_file())
            self.assertEqual(list(self.runs.iterdir()), [])

    def test_deliverable_publication_refuses_swapped_parent_and_leaf_links(self):
        reports = self.base / 'reports'
        reports.mkdir()
        private = self.base / 'private'
        private.mkdir()
        out = reports / 'result.md'
        target = private / out.name
        target.write_text('private')
        outbox = self.base / 'outbox'
        outbox.mkdir()
        (outbox / 'deliverable.md').write_text('report')
        original = reports.with_name('old-reports')
        receipt = {}
        with run_codex.open_directory(reports) as out_fd, run_codex.open_directory(outbox) as outbox_fd:
            reports.rename(original)
            reports.symlink_to(private, target_is_directory=True)
            with self.assertRaises(OSError):
                run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
            self.assertFalse((original / out.name).exists())
            self.assertEqual(target.read_text(), 'private')
            reports.unlink()
            original.rename(reports)
            out.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'cannot be a symlink'):
                run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
            self.assertTrue(out.is_symlink())
            out.unlink()
            self.assertEqual(receipt, {})
            run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
            self.assertEqual(receipt, dict(out_bytes=6, out_sha256=hashlib.sha256(b'report').hexdigest()))
        self.assertEqual(out.read_text(), 'report')
        self.assertEqual((outbox / 'deliverable.md').read_text(), 'report')
        self.assertEqual(target.read_text(), 'private')

    def test_deliverable_publication_copies_open_inode_after_source_name_swap(self):
        outbox = self.base / 'outbox'
        outbox.mkdir()
        leaf = outbox / 'deliverable.md'
        leaf.write_text('pinned report')
        private = self.base / 'private'
        private.write_text('private bytes')
        out = self.base / 'result.md'
        out.write_text('previous report')
        original = run_codex.open_context_file

        @contextmanager
        def swap_after_open(*args):
            with original(*args) as (stream, info):
                leaf.unlink()
                leaf.symlink_to(private)
                yield stream, info

        with run_codex.open_directory(self.base) as out_fd, run_codex.open_directory(outbox) as outbox_fd:
            with patch.object(run_codex, 'open_context_file', swap_after_open):
                receipt = {}
                run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
                self.assertEqual(receipt['out_bytes'], len('pinned report'))
        self.assertEqual(out.read_text(), 'pinned report')
        self.assertEqual(leaf.readlink(), private)
        self.assertEqual(private.read_text(), 'private bytes')

    def test_deliverable_publication_across_filesystems(self):
        other = Path('/dev/shm')
        if not os.access(other, os.W_OK) or other.stat().st_dev == self.base.stat().st_dev:
            self.skipTest('no writable second filesystem')
        outbox = Path(self.enterContext(tempfile.TemporaryDirectory(dir=other)))
        (outbox / 'deliverable.md').write_bytes(b'cross-device report')
        out = self.base / 'result.md'
        out.write_bytes(b'previous report')
        with run_codex.open_directory(self.base) as out_fd, run_codex.open_directory(outbox) as outbox_fd:
            receipt = {}
            run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
            self.assertEqual(receipt['out_bytes'], len(b'cross-device report'))
        self.assertEqual(out.read_bytes(), b'cross-device report')

    def test_report_limit_boundary_sparse_and_growth(self):
        outbox = self.base / 'outbox'
        outbox.mkdir()
        leaf = outbox / 'deliverable.md'
        out = self.base / 'report.md'
        data = b'r' * run_codex.REPORT_LIMIT
        leaf.write_bytes(data)
        receipt = {}
        with run_codex.open_directory(self.base) as out_fd, run_codex.open_directory(outbox) as outbox_fd:
            run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
            self.assertEqual(receipt, dict(out_bytes=len(data), out_sha256=hashlib.sha256(data).hexdigest()))
            with leaf.open('wb') as stream:
                stream.truncate(len(data) + 1)
            with self.assertRaisesRegex(ValueError, 'logical byte limit'):
                run_codex.move_deliverable(outbox_fd, out, out_fd, {})
            self.assertEqual(out.read_bytes(), data)
            leaf.write_bytes(b's' * (run_codex.REPORT_CHUNK + 1))
            original = run_codex.report_chunks

            def grow_during_copy(*args):
                for chunk in original(*args):
                    yield chunk
                    with leaf.open('ab') as stream:
                        stream.write(b'growth')

            with patch.object(run_codex, 'report_chunks', grow_during_copy):
                with self.assertRaisesRegex(ValueError, 'grew or shrank'):
                    run_codex.move_deliverable(outbox_fd, out, out_fd, {})
            self.assertEqual(out.read_bytes(), data)
            self.assertFalse(list(self.base.glob('.report-*')))

    def test_report_refuses_staged_file_and_directory_substitutions(self):
        outbox = self.base / 'outbox'
        outbox.mkdir()
        (outbox / 'deliverable.md').write_text('new report')
        out = self.base / 'result.md'
        out.write_text('previous report')
        check_leaf = run_codex.check_output_leaf
        for swap_directory in (False, True):
            with self.subTest(swap_directory=swap_directory):
                moved = self.base / 'moved-stage'

                def substitute(*args):
                    check_leaf(*args)
                    staging = next(self.base.glob('.report-*'))
                    if swap_directory:
                        staging.rename(moved)
                        staging.mkdir()
                    else:
                        (staging / 'report').rename(moved)
                    (staging / 'report').write_text('replacement')

                with run_codex.open_directory(self.base) as out_fd, run_codex.open_directory(outbox) as outbox_fd, \
                        patch.object(run_codex, 'check_output_leaf', substitute):
                    receipt = {}
                    with self.assertRaisesRegex(OSError, 'changed'):
                        run_codex.move_deliverable(outbox_fd, out, out_fd, receipt)
                    self.assertEqual(receipt, {})
                self.assertEqual(out.read_text(), 'previous report')
                staging = next(self.base.glob('.report-*'))
                self.assertEqual((staging / 'report').read_text(), 'replacement')
                shutil.rmtree(staging)
                if swap_directory:
                    self.assertEqual(list(moved.iterdir()), [])
                    moved.rmdir()
                else:
                    self.assertEqual(moved.read_text(), 'new report')
                    moved.unlink()

    def test_bundle_refuses_private_paths_traversal_aliases_and_invalid_files(self):
        outside = self.base / 'outside.md'
        outside.touch()
        private = self.product / 'INBOX.jsonl'
        private.touch()
        (self.product / 'alias.md').symlink_to(private)
        os.link(private, self.product / 'hardlink.md')
        (self.product / 'outside').symlink_to(self.base, target_is_directory=True)
        (self.product / 'dir').mkdir()
        os.mkfifo(self.product / 'pipe')
        with (self.product / 'big.md').open('wb') as stream:
            stream.truncate(run_codex.CONTEXT_LIMIT + 1)
        for name in ('INBOX.jsonl', 'SENT.jsonl', 'approvals.sqlite', 'store.sqlite-wal', 'backups/a',
                     'release-pending.json', '.runs/other-ctx/TASK.md', 'reviews/a', 'USAGE.jsonl',
                     'task0/snapshot/a', 'journal.json', '../outside.md', str(outside), 'alias.md',
                     'hardlink.md', 'outside/outside.md', 'dir', 'pipe', 'big.md', 'unknown.md'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'context'):
                self.bundle(name)
        self.assertEqual(list(self.runs.iterdir()), [])


class TaskWorkerTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())
        self.home = self.base / 'korolev'
        self.config = self.base / 'config'
        self.config.mkdir()
        # Use a fake Codex with real Git. The sandbox mounts are tested separately.
        (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
        self.env.update(HOME=str(self.base), KOROLEV_HOME=str(self.home),
                        KOROLEV_CONFIG_DIR=str(self.config), CODEX_HOME=str(self.base / 'user-codex-home'),
                        KOROLEV_CODEX_HOME=str(self.base / 'codex-home'),
                        PATH=os.pathsep.join([str(self.base), os.environ['PATH']]))
        self.product = self.home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        self.owner = self.base / 'owner'
        self.make_repo(self.owner)
        self.head = self.git('rev-parse', 'HEAD').strip()
        (self.home / 'projects.json').write_text(json.dumps({'demo': {
            'folder': str(self.owner), 'git_common_dir': str(self.owner / '.git'), 'branch': 'main'}}))
        self.prompt = self.product / 'TASK.md'
        self.prompt.write_text('# Test task')

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '--no-optional-locks', *args], cwd=cwd or self.owner,
                              env=self.env, check=True, capture_output=True, text=True, timeout=10).stdout

    def make_repo(self, path):
        self.git('init', '-q', '-b', 'main', path, cwd=self.base)
        (path / 'tracked').write_text('tracked\n')
        self.git('add', 'tracked', cwd=path)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'base', cwd=path)

    def new_task(self, slug='19-task'):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'tasks.py'), 'new', '--product', 'demo',
                                 '--slug', slug], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(json.loads(result.stdout)['path'])

    def worker(self, body, *args, role='builder', product='demo', exit_code=0, sandboxed=False, setup='', owner=None,
               projected_owner=False):
        fake = self.base / 'codex'
        fake.write_text(f'#!{sys.executable}\n'
                        'import subprocess, sys\n'
                        'from pathlib import Path\n'
                        'sys.stdin.read()\n'
                        'cwd = Path(sys.argv[sys.argv.index("-C") + 1])\n'
                        + body + '\n'
                        'Path(sys.argv[sys.argv.index("-o") + 1]).write_text(str(cwd))\n')
        fake.chmod(0o700)
        launcher = f'import runpy, sys; sys.path.insert(0, {str(SCRIPTS)!r}); import sandbox; ' + EXECUTABLE_FIXTURE
        if sandboxed:
            (self.config / 'config.json').write_text('{"worker_network": "host"}')
            fake_bwrap = self.base / 'bwrap'
            bind_flag = '--bind-fd' if role == 'builder' or '--write' in args else '--ro-bind-fd'
            fake_bwrap.write_text(f'#!{sys.executable}\n'
                                  'import os, sys\n'
                                  'from pathlib import Path\n'
                                  'args = sys.argv[1:]\n'
                                  'cmd = args[args.index("--") + 1:]\n'
                                  'if cmd[4:5] == ["sandbox-selftest"]:\n'
                                  '    print("sandbox checks passed")\n'
                                  'else:\n'
                                  f'    owner = {str(owner or self.owner)!r}\n'
                                  f'    if {projected_owner!r}:\n'
                                  '        assert any(flag == "--ro-bind" and dest == owner\n'
                                  '                   for flag, src, dest in zip(args, args[1:], args[2:]))\n'
                                  '    else:\n'
                                  '        assert ("--tmpfs", owner) in zip(args, args[1:])\n'
                                  '    cwd = cmd[cmd.index("-C") + 1]\n'
                                  f'    assert any(flag == {bind_flag!r} and Path(cwd).is_relative_to(dest)\n'
                                  '               for flag, src, dest in zip(args, args[1:], args[2:]))\n'
                                  '    for flag, src, dest in zip(args, args[1:], args[2:]):\n'
                                  '        if flag in ("--ro-bind-fd", "--bind-fd"):\n'
                                  '            projected = Path(os.readlink("/proc/self/fd/" + src))\n'
                                  '            if not projected.parent.name.startswith("gitless-"):\n'
                                  '                assert os.path.samestat(os.fstat(int(src)), os.stat(dest))\n'
                                  '            os.close(int(src))\n'
                                  '    os.execv(cmd[0], cmd)\n')
            fake_bwrap.chmod(0o700)
            launcher += f'sandbox.bwrap_path = lambda: {str(fake_bwrap)!r}; '
        launcher += '\n' + setup + f'\nrunpy.run_path({str(SCRIPTS / "run_codex.py")!r}, run_name="__main__")'
        product_folder = self.home / 'products' / product
        prompt = product_folder / self.prompt.name
        if prompt != self.prompt:
            prompt.write_bytes(self.prompt.read_bytes())
        result = subprocess.run([sys.executable, '-I', '-c', launcher,
                                 '--role', role, '--product', product, '--prompt', str(prompt),
                                 '--out', 'HANDOFF.md', *args], env=self.env, capture_output=True,
                                text=True, timeout=10)
        if result.returncode != exit_code:
            events = '\n'.join(path.read_text(errors='replace')
                               for path in (product_folder / '.runs').glob('*.events.jsonl'))
            self.fail(f'expected worker exit {exit_code}, got {result.returncode}\n{result.stderr}{events}')
        return result

    def assert_owner_unchanged(self):
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_busy_write_lock_returns_75_without_limit_record(self):
        self.new_task()
        lock = self.home / 'locks' / 'demo-builder-write.lock'
        lock.parent.mkdir(exist_ok=True)
        with lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            result = self.worker('raise AssertionError("worker started")', '--task', '19-task', exit_code=75)
        self.assertIn("lock 'demo-builder-write' is busy", result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertFalse((self.product / 'USAGE.jsonl').exists())

    def test_product_identity_refuses_alternate_spellings_and_directory_aliases(self):
        (self.product.parent / 'alias').symlink_to(self.product, target_is_directory=True)
        outside = self.base / 'outside-product'
        outside.mkdir()
        (self.product.parent / 'outside').symlink_to(outside, target_is_directory=True)
        for name in ('./demo', 'other/../demo', 'Demo', 'demo.one', 'demo_one', 'ｄemo', 'alias', 'outside'):
            with self.subTest(name=name):
                result = self.worker('raise AssertionError("worker started")', '--product', name, exit_code=2)
                self.assertIn('product', result.stderr)
        self.assertFalse((self.product / '.runs').exists())

    def test_output_refuses_absolute_path_outside_product_before_writing(self):
        self.new_task()
        report = self.base / 'outside-product' / 'OPINION-x.md'
        marker = self.base / 'worker-started'
        result = self.worker(f'Path({str(marker)!r}).touch()', '--task', '19-task', '--out', str(report), exit_code=78)
        self.assertIn('inside the product folder', result.stderr)
        for path in (marker, report.parent, self.product / '.runs', self.home / 'locks'):
            self.assertFalse(path.exists(), path)

    def test_output_refuses_reports_inside_runs(self):
        self.new_task()
        marker = self.base / 'worker-started'
        result = self.worker(f'Path({str(marker)!r}).touch()', '--task', '19-task', '--out', '.runs/x.md',
                             exit_code=78)
        self.assertIn('outside .runs', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.product / '.runs').exists())

    def test_output_publishes_product_relative_report(self):
        task = self.new_task()
        self.worker('', '--task', '19-task', '--out', 'OPINION-x.md')
        self.assertEqual((self.product / 'OPINION-x.md').read_text(), str(task))

    def test_task_launch_ignores_empty_git_directory_above_checkout(self):
        (self.base / '.git').mkdir()
        task = self.new_task()
        self.worker('', '--task', '19-task', '--write')
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), str(task))

    def test_output_rejects_checkout_owner_and_separate_git_paths_before_mkdir(self):
        task = self.new_task()
        for root in (task, self.owner):
            with self.subTest(root=root):
                result = self.worker('raise AssertionError("worker started")', '--task', '19-task',
                                     '--out', str(root / 'reports' / 'result.md'), exit_code=78)
                self.assertIn('inside the product folder', result.stderr)
                self.assertFalse((root / 'reports').exists())

        common = self.product / 'separate.git'
        self.git('init', '--separate-git-dir', str(common))
        (self.product / 'redirect').symlink_to(common, target_is_directory=True)
        result = self.worker('raise AssertionError("worker started")', '--role', 'analyst',
                             '--out', 'redirect/reports/result.md', exit_code=77)
        self.assertIn('Git metadata', result.stderr)
        self.assertFalse((common / 'reports').exists())

    def test_nested_review_protects_outer_checkout_and_selects_inner_checkout(self):
        (self.home / 'projects.json').unlink()
        outer = self.owner.rename(self.product / 'owner')
        inner = outer / 'inner'
        self.make_repo(inner)
        (inner / 'tracked').write_text('inner repository\n')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qam', 'inner', cwd=inner)
        inner_head = self.git('rev-parse', 'HEAD', cwd=inner).strip()
        self.assertNotEqual(inner_head, self.head)
        report = outer / 'outer-sibling' / 'report.md'
        marker = self.base / 'worker-started'
        result = self.worker(f'Path({str(marker)!r}).touch()', '--cwd', str(inner), '--out', str(report),
                             role='astra', sandboxed=True, owner=inner, exit_code=77)
        self.assertIn('outside the checkout', result.stderr)
        self.assertFalse(report.parent.exists())
        self.assertFalse((self.product / '.runs').exists())
        self.assertFalse(marker.exists())

        outer.rename(self.owner)
        inner = self.owner / 'inner'
        result = self.worker('assert (cwd / "tracked").read_text() == "inner repository\\n"',
                             '--cwd', str(inner), role='astra', sandboxed=True, owner=inner)
        self.assertEqual(json.loads(result.stdout)['reviewed_sha'], inner_head)

    def test_output_rejects_routes_through_symlinked_git_object_storage(self):
        (self.home / 'projects.json').unlink()
        objects = self.owner / '.git' / 'objects'
        external = self.product / 'external-objects'
        objects.rename(external)
        objects.symlink_to(external, target_is_directory=True)
        self.assertEqual(self.git('cat-file', '-t', self.head).strip(), 'commit')
        alias = self.base / 'objects-alias'
        alias.symlink_to(objects, target_is_directory=True)
        marker = self.base / 'worker-started'
        for route in (objects, alias, external):
            with self.subTest(route=route):
                parent = route / 'new-reports'
                result = self.worker(f'Path({str(marker)!r}).touch()', '--cwd', str(self.owner),
                                     '--out', str(parent / 'result.md'), role='astra', exit_code=77)
                self.assertIn('Git metadata', result.stderr)
                self.assertFalse(parent.exists())
                self.assertFalse((self.product / '.runs').exists())
                self.assertFalse(marker.exists())

    def test_output_rejects_checkout_under_a_symlinked_korolev_home_ancestor(self):
        (self.home / 'projects.json').unlink()
        checkout = self.product / 'checkout'
        self.make_repo(checkout)
        alias = self.base / 'alias'
        alias.symlink_to(self.home.parent, target_is_directory=True)
        aliased_home = alias / self.home.name
        aliased_checkout = aliased_home / checkout.relative_to(self.home)
        requested_out = aliased_checkout / 'reports/result.md'
        with patch.dict(self.env, KOROLEV_HOME=str(aliased_home)):
            result = self.worker('raise AssertionError("worker started")', '--cwd', str(aliased_checkout),
                                 '--out', str(requested_out), role='astra', exit_code=77)
        self.assertIn('outside the checkout', result.stderr)
        self.assertFalse(requested_out.parent.exists())
        self.assertFalse((self.product / '.runs').exists())

        # Exercise lexical checkout roots directly, since launch resolves its input paths.
        checkouts = {checkout: (aliased_checkout, aliased_checkout / '.git')}
        roots = run_codex.protected_output_dirs(requested_out.resolve(), requested_out, checkouts, None, None, None)
        self.assertEqual(roots, {checkout, checkout / '.git'})

    def test_legacy_sha_review_accepts_an_ancestor_alias(self):
        (self.home / 'projects.json').unlink()
        checkout = self.home / 'work/demo/repo'
        checkout.parent.mkdir(parents=True)
        self.make_repo(checkout)
        sha = self.git('rev-parse', 'HEAD', cwd=checkout).strip()
        alias = self.base / 'alias'
        alias.symlink_to(self.home.parent)
        with patch.dict(self.env, KOROLEV_HOME=str(alias / self.home.name)):
            result = self.worker('assert (cwd / "tracked").is_file()', '--sha', sha,
                                 role='astra', sandboxed=True, owner=checkout)
        self.assertEqual(json.loads(result.stdout)['reviewed_sha'], sha)

    def test_unregistered_review_keeps_checkout_outside_symlink_targets_parent(self):
        (self.home / 'projects.json').unlink()
        vendor = self.owner / 'vendor'
        self.make_repo(vendor)
        repo = self.home / 'work/demo/repo'
        repo.parent.mkdir(parents=True)
        repo.symlink_to(vendor, target_is_directory=True)

        # Check while the review checkout still exists, before launcher cleanup.
        self.worker(f'assert not list(Path({str(self.owner)!r}).glob("review-*"))',
                    role='astra', sandboxed=True, owner=vendor)

    def test_checkout_and_state_case_aliases_refuse_before_mkdir(self):
        (self.home / 'projects.json').unlink()
        owner = self.owner.rename(self.product / 'owner')
        output_alias = owner.with_name(owner.name.upper())
        setup = f"""
import os
from pathlib import Path
parent = Path({str(owner.parent)!r}).stat()
owner = Path({str(owner)!r})
real_open, real_mkdir = os.open, os.mkdir
def casefold_open(path, flags, *args, dir_fd=None, **kwargs):
    if path == {output_alias.name!r} and dir_fd is not None and os.path.samestat(parent, os.fstat(dir_fd)):
        path = owner.name
    return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
def forbid_mkdir(path, *args, **kwargs):
    assert Path(path).name not in ('new-report', '.runs'), path
    return real_mkdir(path, *args, **kwargs)
os.open, os.mkdir = casefold_open, forbid_mkdir
"""
        report = output_alias / 'new-report/result.md'
        result = self.worker('', '--cwd', str(owner), '--out', str(report), role='astra', setup=setup, exit_code=77)
        self.assertIn('outside the checkout', result.stderr)
        self.assertFalse((owner / 'new-report').exists())
        self.assertFalse((self.product / '.runs').exists())
        # Give one already-opened state ancestor the identity of the checkout.
        owner.rename(self.owner)
        setup += f"""
owner = Path({str(self.owner)!r})
def state_alias(path, flags, *args, dir_fd=None, **kwargs):
    if path == 'products' and dir_fd is not None and os.fstat(dir_fd).st_ino == Path({str(self.home)!r}).stat().st_ino:
        return real_open(owner, flags, *args, **kwargs)
    return casefold_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
os.open = state_alias
"""
        report = self.product / 'new-report/result.md'
        result = self.worker('', '--cwd', str(self.owner), '--out', str(report), role='astra', setup=setup, exit_code=77)
        self.assertIn('outside the checkout', result.stderr)
        self.assertFalse(report.parent.exists())
        self.assertFalse((self.product / '.runs').exists())

    def test_output_rejects_resolved_installation_in_both_modes(self):
        self.new_task()
        installed = self.product / 'installation' / 'skill'
        shutil.copytree(SCRIPTS, installed / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        module = installed / 'scripts' / 'common.py'
        original = module.read_bytes()
        alias = self.product / 'alias'
        alias.symlink_to(installed, target_is_directory=True)
        marker = self.base / 'worker-started'
        codex = self.base / 'codex'
        codex.write_text(f'#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 99\n')
        codex.chmod(0o700)
        for sandboxed in (False, True):
            (self.config / 'config.json').write_text(json.dumps({'worker_sandbox': sandboxed}))
            for output in (module, Path('alias/scripts/common.py'), Path('alias/new/report.md')):
                with self.subTest(sandboxed=sandboxed, output=output):
                    result = subprocess.run([sys.executable, '-I', str(alias / 'scripts' / 'run_codex.py'),
                                             '--role', 'builder', '--product', 'demo', '--task', '19-task',
                                             '--prompt', str(self.prompt), '--out', str(output)],
                                            env=self.env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 77, result.stderr)
                    self.assertIn('outside the Korolev installation', result.stderr)
                    self.assertEqual(module.read_bytes(), original)
                    self.assertFalse((installed / 'new').exists())
                    self.assertFalse(marker.exists())
                    self.assertFalse((self.product / '.runs').exists())

    def test_output_rejects_installation_case_alias_by_identity_in_both_modes(self):
        self.new_task()
        installed = self.product / 'installation' / 'skill'
        shutil.copytree(SCRIPTS, installed / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        module = installed / 'scripts' / 'common.py'
        original = module.read_bytes()
        alias = installed.with_name('SKILL')
        marker = self.base / 'worker-started'
        codex = self.base / 'codex'
        codex.write_text(f'#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 99\n')
        codex.chmod(0o700)
        # Model case-folded descriptor lookup while canonicalization retains the requested spelling.
        source = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(installed / 'scripts')!r})
import run_codex
parent = Path({str(installed.parent)!r}).stat()
real_open = os.open
def casefold_open(path, flags, *args, dir_fd=None, **kwargs):
    if path == 'SKILL' and dir_fd is not None and os.path.samestat(parent, os.fstat(dir_fd)):
        path = 'skill'
    return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
assert Path({str(alias)!r}).resolve() == Path({str(alias)!r})
os.open = casefold_open
sys.exit(run_codex.main())
"""
        for sandboxed in (False, True):
            (self.config / 'config.json').write_text(json.dumps({'worker_sandbox': sandboxed}))
            for output in (alias / 'scripts/common.py', alias / 'new/report.md'):
                with self.subTest(sandboxed=sandboxed, output=output):
                    result = subprocess.run([sys.executable, '-I', '-c', source,
                                             '--role', 'builder', '--product', 'demo', '--task', '19-task',
                                             '--prompt', str(self.prompt), '--out', str(output)],
                                            env=self.env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 77, result.stderr)
                    self.assertIn('outside the Korolev installation', result.stderr)
                    self.assertEqual(module.read_bytes(), original)
                    self.assertFalse((installed / 'new').exists())
                    self.assertFalse(marker.exists())
                    self.assertFalse((self.product / '.runs').exists())

    def test_registered_writes_refuse_default_owner_and_other_directories(self):
        body = "(cwd / 'unexpected').touch()"
        (self.owner / 'nested').mkdir()
        attempts = (('builder', ()), ('analyst', ('--write', '--cwd', str(self.owner))),
                    ('builder', ('--cwd', str(self.owner / 'nested'))),
                    ('builder', ('--cwd', str(self.base))))
        for role, options in attempts:
            with self.subTest(role=role, options=options):
                result = self.worker(body, *options, role=role, exit_code=77)
                self.assertIn('tasks.py new', result.stderr)
        self.assertFalse((self.product / 'HANDOFF.md').exists())
        self.assertFalse((self.base / 'unexpected').exists())
        self.assert_owner_unchanged()

    def test_task_validation_review_cleanup_and_staging_use_one_pinned_git(self):
        task = self.new_task()
        def registered_paths():
            return [line for line in self.git('worktree', 'list', '--porcelain').splitlines()
                    if line.startswith('worktree ')]

        bin_dir = self.new_task('20-sibling') / 'bin'
        before = registered_paths()
        bin_dir.mkdir()
        marker = self.base / 'fake-git-ran'
        fake_git = bin_dir / 'git'
        fake_git.write_text(f'#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 95\n')
        fake_git.chmod(0o700)
        setup = '''
import common
trusted = common.trusted_git_executable
selected = []
def pin_once():
    assert not selected, 'Git was resolved more than once'
    executable = trusted()
    selected.append(executable)
    return executable
common.trusted_git_executable = sandbox.trusted_git_executable = pin_once
'''
        with patch.dict(self.env, PATH=f'{bin_dir}:{self.env["PATH"]}', GIT_CONFIG_COUNT='malformed'):
            review = self.worker('assert (cwd / "tracked").is_file()', '--task', '19-task', '--sha', self.head,
                                 role='analyst', sandboxed=True, setup=setup)
            (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
            build = self.worker('(cwd / "tracked").write_text("updated")', '--task', '19-task', setup=setup)
        self.assertEqual(json.loads(review.stdout)['reviewed_sha'], self.head)
        self.assertIsNotNone(json.loads(build.stdout)['commit'])
        self.assertEqual(registered_paths(), before)
        self.assertEqual(self.git('show', 'HEAD:tracked', cwd=task), 'updated')
        self.assertFalse(marker.exists())

    def test_worker_refuses_without_trusted_git_before_starting(self):
        self.new_task()
        setup = f'from pathlib import Path\nimport common\ncommon.GIT_DIRS = (Path({str(self.base / "missing")!r}),)\n'
        result = self.worker('raise AssertionError("worker started")', '--task', '19-task',
                             setup=setup, exit_code=78)
        self.assertIn('no trusted Git executable', result.stderr)
        self.assertFalse((self.product / '.runs').exists())

    def test_write_clone_cannot_contain_resolved_installation(self):
        (self.home / 'projects.json').unlink()
        checkout = self.home / 'work' / 'demo' / 'repo'
        checkout.parent.mkdir(parents=True)
        self.make_repo(checkout)
        installed = checkout / 'skill'
        shutil.copytree(SCRIPTS, installed / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        link = self.base / 'installed-korolev'
        link.symlink_to(installed, target_is_directory=True)
        result = subprocess.run([sys.executable, '-I', str(link / 'scripts' / 'run_codex.py'),
                                 '--role', 'builder', '--product', 'demo', '--prompt', str(self.prompt),
                                 '--out', 'HANDOFF.md'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('overlaps the Korolev installation', result.stderr)
        self.assertFalse((self.product / '.runs').exists())

    def test_registered_review_uses_detached_commit_and_discards_untracked_owner_and_task_files(self):
        result = self.worker('', '--task', '19-missing', role='analyst', exit_code=1)
        self.assertIn('is not recorded', result.stderr)
        task = self.new_task()
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                 'commit', '--allow-empty', '-qm', 'task-only commit', cwd=task)
        self.git('config', 'core.autocrlf', 'true')
        for source in (self.owner, task):
            (source / 'untracked').write_text('Private fixture')
            (source / 'tracked').write_text('Dirty tracked file\n')
        git = shutil.which('git', path=self.env['PATH'])
        body = f'''
assert not (cwd / 'untracked').exists()
assert (cwd / 'tracked').read_bytes() == b'tracked\\n'
assert subprocess.check_output([{git!r}, 'rev-parse', 'HEAD'], cwd=cwd, text=True).strip() == {self.head!r}
assert subprocess.check_output([{git!r}, 'show', 'HEAD:tracked'], cwd=cwd) == b'tracked\\n'
assert subprocess.run([{git!r}, 'symbolic-ref', '-q', 'HEAD'], cwd=cwd).returncode == 1
'''
        for options in ((), ('--task', '19-task')):
            with self.subTest(options=options):
                before = self.git('worktree', 'list', '--porcelain')
                result = self.worker(body, *options, role='analyst', sandboxed=True)
                record = json.loads(result.stdout)
                self.assertIsNone(record['commit'])
                self.assertEqual(record['reviewed_sha'], self.head)
                self.assertEqual(json.loads((self.product / 'USAGE.jsonl').read_text().splitlines()[-1]), record)
                checkout = Path((self.product / 'HANDOFF.md').read_text())
                self.assertTrue(checkout.is_relative_to(self.home / 'work'))
                self.assertFalse(checkout.parent.exists())
                self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_registered_review_refuses_sandbox_off(self):
        result = self.worker('raise AssertionError("must not start")', role='analyst', exit_code=78)
        self.assertIn('require the worker sandbox to hide the owner folder', result.stderr)
        self.assertFalse((self.product / 'HANDOFF.md').exists())

    def test_legacy_review_detects_subdirectories_and_linked_worktrees(self):
        (self.home / 'projects.json').unlink()
        legacy = self.home / 'work/demo/repo'
        legacy.parent.mkdir(parents=True)
        self.git('worktree', 'add', '--detach', legacy)
        nested = self.owner / 'deep' / 'nested'
        nested.mkdir(parents=True)
        (nested / 'file').write_text('tracked subdirectory')
        self.git('add', 'deep')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'nested')
        linked = self.base / 'linked'
        self.git('worktree', 'add', '--detach', linked)
        self.head = self.git('rev-parse', 'HEAD').strip()
        alias = self.base / 'alias'
        alias.symlink_to(nested)
        for source in (alias, linked / 'deep' / 'nested'):
            with self.subTest(source=source):
                before = self.git('worktree', 'list', '--porcelain')
                (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
                off = self.worker('', '--cwd', str(source), role='astra', exit_code=78)
                self.assertIn('require the worker sandbox', off.stderr)
                result = self.worker('assert (cwd / "file").read_text() == "tracked subdirectory"',
                                     '--cwd', str(source), role='astra', sandboxed=True,
                                     owner=self.owner if source == alias else linked)
                self.assertEqual(json.loads(result.stdout)['reviewed_sha'], self.head)
                self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_damaged_bare_repo_refuses_prompt_admission_and_report_output(self):
        (self.home / 'projects.json').unlink()
        bare = self.product / 'damaged.git'
        self.git('clone', '-q', '--bare', self.owner, bare)
        (bare / 'HEAD').unlink()
        marker = self.base / 'worker-started'
        for source, destination, exit_code in ((bare, self.product, 78), (self.base, bare, 77)):
            with self.subTest(source=source):
                report = destination / 'new-reports' / 'result.md'
                result = self.worker(f'Path({str(marker)!r}).touch()', '--cwd', str(source), '--prompt-only',
                                     '--out', str(report), role='astra', exit_code=exit_code)
                self.assertIn('cannot identify the review repository', result.stderr)
                self.assertFalse(report.parent.exists())
                self.assertFalse((self.product / '.runs').exists())
                self.assertFalse(marker.exists())

        report = self.product / 'new-reports' / 'result.md'
        result = self.worker('', '--cwd', str(self.base), '--prompt-only', '--out', str(report), role='astra')
        self.assertTrue(report.is_file())
        self.assertIsNone(json.loads(result.stdout)['reviewed_sha'])

    def test_prompt_worker_actual_directory_has_git_free_ancestry(self):
        (self.home / 'projects.json').unlink()
        root = self.home / 'work/demo'
        git = shutil.which('git', path=self.env['PATH'])
        config = self.config / 'config.json'
        data = json.loads(config.read_text())
        config.write_text(json.dumps(data | {'worker_env_allow': ['GIT_DIR', 'GIT_WORK_TREE']}))
        body = f'''
import os
assert not {{"GIT_DIR", "GIT_WORK_TREE"}} & os.environ.keys()
env = os.environ.copy()
env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
probe = subprocess.run([{git!r}, "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
                       env=env, capture_output=True, text=True)
assert probe.returncode == 128 and "not a git repository" in probe.stderr, probe
'''
        for missing in (True, False):
            with self.subTest(missing_work_root=missing):
                if not missing:
                    root.parent.mkdir()
                    self.make_repo(root)
                    report = root / 'new-report/result.md'
                    result = self.worker('', '--cwd', str(self.base), '--prompt-only', '--out', str(report),
                                         role='astra', exit_code=78)
                    self.assertIn('inside the product folder', result.stderr)
                    self.assertFalse(report.parent.exists())
                with patch.dict(self.env, TMPDIR=str(self.owner), GIT_DIR=str(self.owner / '.git'),
                                GIT_WORK_TREE=str(self.owner)):
                    result = self.worker(body, '--cwd', str(self.base), '--prompt-only', role='astra')
                record = json.loads(result.stdout)
                self.assertIsNone(record['reviewed_sha'])
                workspace = Path((self.product / 'HANDOFF.md').read_text())
                self.assertFalse(workspace.is_relative_to(self.owner))
                self.assertFalse(workspace.is_relative_to(root))
                self.assertFalse(workspace.exists())
                self.assertFalse(list(self.owner.glob('prompt-*')))
                self.assertFalse(list(root.glob('prompt-*')))

    def test_bare_review_and_output_protection_with_packed_head_symlink(self):
        (self.home / 'projects.json').unlink()
        for name in ('ordinary.git', 'packed.git'):
            with self.subTest(name=name):
                bare = self.base / name
                self.git('clone', '-q', '--bare', self.owner, bare)
                if name == 'packed.git':
                    self.git('pack-refs', '--all', '--prune', cwd=bare)
                    (bare / 'HEAD').unlink()
                    (bare / 'HEAD').symlink_to('refs/heads/main')
                    self.assertFalse((bare / 'HEAD').exists())
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=bare).strip(), self.head)
                denied = self.worker('', '--cwd', str(bare), '--prompt-only', role='analyst',
                                     sandboxed=True, exit_code=78)
                self.assertIn('source without a Git repository', denied.stderr)
                if name == 'packed.git':
                    result = self.worker('', '--cwd', str(bare), role='analyst', sandboxed=True, exit_code=78)
                    self.assertIn('Git metadata must be a regular file: HEAD', result.stderr)
                else:
                    result = self.worker('assert (cwd / "tracked").read_text() == "tracked\\n"',
                                         '--cwd', str(bare), role='analyst', sandboxed=True, owner=bare,
                                         projected_owner=True)
                    self.assertEqual(json.loads(result.stdout)['reviewed_sha'], self.head)

                reports = bare.rename(self.product / name) / 'new-reports'
                result = self.worker('', '--cwd', str(self.base), '--prompt-only',
                                     '--out', str(reports / 'result.md'), role='analyst', exit_code=77)
                self.assertIn('outside the checkout, owner folder and Git metadata', result.stderr)
                self.assertFalse(reports.exists())

    def test_gitless_review_requests_and_empty_or_writable_sha_fail(self):
        (self.home / 'projects.json').unlink()
        for role, options, code, diagnostic in (
            ('astra', (), 78, 'use --prompt-only'),
            ('analyst', (), 78, 'use --prompt-only'),
            ('analyst', ('--prompt-only', '--sha', self.head), 2, '--prompt-only cannot be used'),
            ('builder', ('--prompt-only',), 2, '--prompt-only cannot be used'),
            ('analyst', ('--sha', ''), 2, '--sha must not be empty'),
            ('builder', ('--write', '--sha', self.head), 2, '--sha is only for read-only roles'),
        ):
            with self.subTest(role=role):
                result = self.worker('raise AssertionError("worker started")', '--cwd', str(self.base),
                                     *options, role=role, exit_code=code)
                self.assertIn(diagnostic, result.stderr)
        legacy = self.home / 'work/demo/repo'
        legacy.parent.mkdir(parents=True)
        self.git('worktree', 'add', '--detach', legacy)
        broken = self.base / 'broken'
        (broken / '.git' / 'objects').mkdir(parents=True)
        (broken / '.git' / 'HEAD').write_text('invalid\n')
        for role in ('analyst', 'astra'):
            with self.subTest(role=role):
                (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
                result = self.worker('assert not list(cwd.iterdir())', '--cwd', str(self.base),
                                     '--prompt-only', role=role)
                self.assertIsNone(json.loads(result.stdout)['reviewed_sha'])
                result = self.worker('', '--cwd', str(legacy), '--prompt-only', role=role,
                                     sandboxed=True, exit_code=78)
                self.assertIn('source without a Git repository', result.stderr)
                for flags in ((), ('--prompt-only',)):
                    result = self.worker('', '--cwd', str(broken), *flags, role=role, exit_code=78)
                    self.assertIn('cannot identify the review repository', result.stderr)

    def test_prompt_only_skips_missing_repo_slot_below_git_metadata(self):
        (self.home / 'projects.json').unlink()
        home = self.owner / '.git' / 'korolev'
        self.home.rename(home)
        self.home = home
        self.product = home / 'products/demo'
        self.prompt = self.product / self.prompt.name
        self.env['KOROLEV_HOME'] = str(home)
        legacy = home / 'work/demo/repo'
        legacy.parent.mkdir(parents=True)
        output = self.product / 'new-reports/result.md'
        options = ('--cwd', str(self.base), '--prompt-only', '--out', str(output))

        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                if dangling:
                    legacy.symlink_to(self.base / 'absent')
                else:
                    legacy.touch()
                try:
                    result = self.worker('raise AssertionError("worker started")', *options, role='astra', exit_code=78)
                finally:
                    legacy.unlink()
                self.assertIn('cannot identify the review repository', result.stderr)
                self.assertFalse(output.parent.exists())
                self.assertFalse((self.product / '.runs').exists())

        result = self.worker('raise AssertionError("worker started")', *options, role='astra', exit_code=77)
        self.assertIn('outside the checkout', result.stderr)
        self.assertFalse(output.parent.exists())
        self.assertFalse((self.product / '.runs').exists())

    def test_review_discovery_ignores_inherited_git_environment(self):
        (self.home / 'projects.json').unlink()
        source = self.base / 'non-repository'
        source.mkdir()
        broken = self.product / 'broken'
        broken.mkdir()
        (broken / '.git').symlink_to('missing-metadata')
        nested = self.owner / 'nested'
        nested.mkdir()
        (nested / 'file').write_text('tracked subdirectory')
        self.git('add', 'nested')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'nested')
        self.head = self.git('rev-parse', 'HEAD').strip()
        linked = self.base / 'linked'
        self.git('worktree', 'add', '--detach', linked)
        bare, packed = self.base / 'ordinary.git', self.base / 'packed.git'
        for repo in (bare, packed):
            self.git('clone', '-q', '--bare', self.owner, repo)
        self.git('pack-refs', '--all', '--prune', cwd=packed)
        (packed / 'HEAD').unlink()
        (packed / 'HEAD').symlink_to('refs/heads/main')
        self.assertFalse((packed / 'HEAD').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=packed).strip(), self.head)
        metadata = self.owner / '.git'
        inherited = dict(GIT_DIR=str(metadata), GIT_WORK_TREE=str(self.owner),
                         GIT_COMMON_DIR=str(metadata), GIT_INDEX_FILE=str(metadata / 'index'))
        trace = self.base / 'git-called'
        setup = f'''
from pathlib import Path
import os, subprocess
inherited = {inherited!r}
assert all(os.environ[key] == value for key, value in inherited.items())
run = subprocess.run
def verify_git_environment(cmd, **kwargs):
    if Path(cmd[0]).name == 'git':
        env = kwargs['env']
        present = inherited.keys() & env.keys()
        if 'GIT_INDEX_FILE' in present and 'read-tree' in cmd:
            index = Path(env['GIT_INDEX_FILE'])
            assert index.is_absolute() and index.parent.parent.name == 'worktrees' and index.name == 'index'
            present.remove('GIT_INDEX_FILE')
        assert not present
        if '--resolve-git-dir' in cmd:
            Path({str(trace)!r}).write_text(str(cmd[-1]))
    return run(cmd, **kwargs)
subprocess.run = verify_git_environment
'''
        with patch.dict(self.env, inherited):
            self.assertEqual(self.git('rev-parse', '--git-dir', cwd=source).strip(), str(metadata))
            reports = self.product / 'new-reports'
            result = self.worker('', '--cwd', str(broken), '--prompt-only',
                                 '--out', str(reports / 'result.md'), role='astra', exit_code=78, setup=setup)
            self.assertIn('cannot identify the review repository', result.stderr)
            self.assertEqual(trace.read_text(), str(broken / '.git'))
            self.assertFalse(reports.exists())
            self.assertFalse((self.product / '.runs').exists())

            result = self.worker('', '--cwd', str(source), '--prompt-only',
                                 '--out', str(broken / '.git' / 'reports' / 'result.md'),
                                 role='astra', exit_code=77, setup=setup)
            self.assertIn('cannot identify the review repository', result.stderr)
            self.assertFalse((broken / 'missing-metadata').exists())
            self.assertFalse((self.product / '.runs').exists())

            result = self.worker('', '--cwd', str(source),
                                 role='astra', exit_code=78, setup=setup)
            self.assertIn('use --prompt-only', result.stderr)
            self.assertFalse((self.product / 'HANDOFF.md').exists())
            result = self.worker(f'import os\nassert not {set(inherited)!r}.intersection(os.environ)\n'
                                 'assert not list(cwd.iterdir())',
                                 '--cwd', str(source), '--prompt-only', role='astra', setup=setup)
            self.assertIsNone(json.loads(result.stdout)['reviewed_sha'])

            for checkout, marker in ((nested, metadata), (linked, linked / '.git'), (bare, bare), (packed, packed)):
                with self.subTest(checkout=checkout):
                    trace.unlink(missing_ok=True)
                    denied = self.worker('raise AssertionError("worker started")', '--cwd', str(checkout),
                                         '--prompt-only', role='astra', sandboxed=True, exit_code=78, setup=setup)
                    self.assertIn('source without a Git repository', denied.stderr)
                    self.assertEqual(trace.read_text(), str(marker))

            for checkout, owner, body in (
                (nested, self.owner, 'assert (cwd / "file").read_text() == "tracked subdirectory"'),
                (linked, linked, 'assert (cwd / "nested/file").read_text() == "tracked subdirectory"'),
                (bare, bare, 'assert (cwd / "tracked").read_text() == "tracked\\n"'),
            ):
                with self.subTest(checkout=checkout):
                    result = self.worker(body, '--cwd', str(checkout), role='astra', sandboxed=True,
                                         owner=owner, projected_owner=checkout == bare, setup=setup)
                    self.assertEqual(json.loads(result.stdout)['reviewed_sha'], self.head)

            rejected = self.worker('raise AssertionError("worker started")', '--cwd', str(packed), role='astra',
                                   sandboxed=True, exit_code=78, setup=setup)
            self.assertIn('Git metadata must be a regular file: HEAD', rejected.stderr)
            for repo in (bare, packed):
                with self.subTest(repo=repo):
                    reports = repo.rename(self.product / repo.name) / 'new-reports'
                    refused = self.worker('raise AssertionError("worker started")', '--cwd', str(source),
                                          '--prompt-only', '--out', str(reports / 'result.md'),
                                          role='astra', exit_code=77, setup=setup)
                    self.assertIn('outside the checkout, owner folder and Git metadata', refused.stderr)
                    self.assertFalse(reports.exists())

    def test_review_refuses_a_symlink_in_the_requested_working_subdirectory(self):
        nested = self.owner / 'nested'
        target = self.owner / 'actual'
        target.mkdir()
        (target / 'tracked').write_text('alternate files')
        nested.symlink_to(target.name, target_is_directory=True)
        signature = ('-c', 'user.name=Test', '-c', 'user.email=test@example.test')
        self.git('add', 'nested', 'actual')
        self.git(*signature, 'commit', '-qm', 'historical symlink')
        sha = self.git('rev-parse', 'HEAD').strip()
        nested.unlink()
        nested.mkdir()
        (nested / 'tracked').write_text('requested files')
        self.git('add', 'nested')
        self.git(*signature, 'commit', '-qm', 'working directory')
        before = self.git('worktree', 'list', '--porcelain')
        self.worker('raise AssertionError("worker started")', '--cwd', str(nested), '--sha', sha,
                    role='astra', sandboxed=True, exit_code=78)
        self.assertFalse((self.product / 'USAGE.jsonl').exists())
        self.assertFalse(list((self.home / 'work/demo').glob('review-*')))
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_publication_attests_bytes_in_both_launch_modes(self):
        for sandboxed in (True, False):
            with self.subTest(sandboxed=sandboxed):
                if not sandboxed:
                    (self.home / 'projects.json').unlink()
                    (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
                args = ('--sha', self.head) if sandboxed else ('--prompt-only', '--cwd', str(self.base))
                result = self.worker('', *args, role='analyst', sandboxed=sandboxed)
                report = (self.product / 'HANDOFF.md').read_bytes()
                record = json.loads(result.stdout)
                self.assertEqual((record['out_bytes'], record['out_sha256']),
                                 (len(report), hashlib.sha256(report).hexdigest()))

    def test_sandbox_off_refuses_publication_with_a_detached_worker_descendant(self):
        (self.home / 'projects.json').unlink()
        report = self.product / 'HANDOFF.md'
        report.write_text('previous report')
        release = self.base / 'release-descendant'
        wait = f'import time; from pathlib import Path\nfor _ in range(400):\n' \
               f'    if Path({str(release)!r}).exists(): break\n    time.sleep(0.01)'
        body = f'subprocess.Popen([sys.executable, "-c", {wait!r}], start_new_session=True)'
        try:
            result = self.worker(body, '--prompt-only', '--cwd', str(self.base), role='analyst', exit_code=77)
        finally:
            release.touch()
        self.assertIn('worker descendants are still running', result.stderr)
        self.assertEqual(report.read_text(), 'previous report')
        self.assertFalse(list(self.product.glob('.report-*')))

    def test_review_signals_during_copy_and_event_parsing_record_publication(self):
        report = self.product / 'HANDOFF.md'
        ledger = self.product / 'USAGE.jsonl'
        for phase, signum in (('copy', signal.SIGTERM), ('copy', signal.SIGHUP), ('events', signal.SIGHUP)):
            with self.subTest(phase=phase, signal=signum):
                report.write_text('previous report')
                if ledger.exists():
                    ledger.unlink()
                setup = f'''
import os, signal, run_codex
runpy.run_path = lambda *_args, **_kwargs: sys.exit(run_codex.main())
original = run_codex.report_chunks
def copy(*args):
    yield next(original(*args))
    os.kill(os.getpid(), {int(signum)})
def events(*_args):
    os.kill(os.getpid(), {int(signal.SIGTERM if phase == 'copy' else signum)})
run_codex.read_event_stats = events
if {phase!r} == 'copy':
    run_codex.report_chunks = copy
'''
                code = 128 + signum
                body = ('Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes('
                        f'b"r" * {run_codex.REPORT_CHUNK + 1}); sys.exit(0)')
                result = self.worker(body, '--sha', self.head, role='analyst', sandboxed=True,
                                     exit_code=code, setup=setup)
                record = json.loads(result.stdout)
                self.assertEqual([json.loads(line) for line in ledger.read_text().splitlines()], [record])
                self.assertEqual((record['exit'], record['worker_exit']), (code, 0))
                if phase == 'copy':
                    self.assertEqual(report.read_text(), 'previous report')
                    self.assertEqual((record['out_bytes'], record['out_sha256']), (0, None))
                    self.assertFalse(list(self.product.glob('.report-*')))
                else:
                    data = report.read_bytes()
                    self.assertNotEqual(data, b'previous report')
                    self.assertEqual((record['out_bytes'], record['out_sha256']),
                                     (len(data), hashlib.sha256(data).hexdigest()))

    setup_budget = '''
import run_codex
def launch_with_short_timeout(*_args, **_kwargs):
    run_codex.REVIEW_SETUP_SECONDS = 1
    for signum in (run_codex.signal.SIGTERM, run_codex.signal.SIGHUP):
        run_codex.signal.signal(signum, run_codex.exit_on_signal)
    sys.exit(run_codex.main())
runpy.run_path = launch_with_short_timeout
'''

    def test_review_setup_deadline_and_term_stop_stalled_git_before_launch(self):
        before = self.git('worktree', 'list', '--porcelain')
        for signum in (None, signal.SIGTERM):
            with self.subTest(signum=signum):
                started = self.base / 'git-started'
                stalled = (
                    f'from pathlib import Path; import os, time; Path({str(started)!r}).touch(); '
                    + (f'os.kill(os.getppid(), {int(signum)}); ' if signum else '') + 'time.sleep(60)'
                )
                setup = self.setup_budget + f'''
import subprocess
git = subprocess.run
def stall_checkout(cmd, **kwargs):
    if 'read-tree' in cmd:
        assert 0 < kwargs['timeout'] < 1
        return git([sys.executable, '-I', '-c', {stalled!r}], **kwargs)
    return git(cmd, **kwargs)
subprocess.run = stall_checkout
'''
                begin = time.monotonic()
                result = self.worker('', role='astra', sandboxed=True, setup=setup,
                                     exit_code=128 + signum if signum else 78)
                self.assertLess(time.monotonic() - begin, 4)
                self.assertTrue(started.is_file(), result.stderr)
                started.unlink()
                self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
                self.assertFalse(list((self.home / 'work/demo').glob('review-*')))
                self.assertFalse(list((self.product / '.runs').glob('*.events.jsonl')))

    def test_review_projections_share_the_setup_deadline(self):
        before = self.git('worktree', 'list', '--porcelain')
        for attempt, operation, signum in ((1, 'config', None), (3, 'fsck', None), (2, 'config', signal.SIGTERM)):
            with self.subTest(attempt=attempt, operation=operation, signum=signum):
                ready = self.base / 'projection-started'
                stalled = (
                    f'from pathlib import Path; import os, time; Path({str(ready)!r}).touch(); '
                    + (f'os.kill(os.getppid(), {int(signum)}); ' if signum else '') + 'time.sleep(60)'
                )
                setup = self.setup_budget + f'''
import subprocess
git = subprocess.run
budgets = []
def stall_projection(cmd, **kwargs):
    if {operation!r} in cmd and ('--no-includes' in cmd or '--connectivity-only' in cmd):
        budgets.append(kwargs['timeout'])
        assert 0 < budgets[-1] < 1
        if len(budgets) == {attempt}:
            assert all(first - second >= 0.15 for first, second in zip(budgets, budgets[1:]))
            return git([sys.executable, '-I', '-c', {stalled!r}], **kwargs)
        run_codex.time.sleep(0.2)
        if {attempt} == 3 and len(budgets) == 2:
            return subprocess.CompletedProcess(cmd, 1)
    return git(cmd, **kwargs)
subprocess.run = stall_projection
'''
                result = self.worker('raise AssertionError("worker started")', role='astra', sandboxed=True,
                                     setup=setup, exit_code=128 + signum if signum else 78)
                self.assertTrue(ready.is_file(), result.stderr)
                ready.unlink()
                self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
                self.assertFalse(list((self.home / 'work/demo').glob('review-*')))
                self.assertFalse(list((self.product / '.runs').glob('*.events.jsonl')))

    def test_review_projection_deadline_expires_during_packed_ref_and_pack_copy(self):
        (self.owner / 'blob').write_bytes(random.Random(0).randbytes(131072))
        self.git('add', 'blob')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'pack fixture')
        self.git('repack', '-ad')
        self.git('pack-refs', '--all')
        before = self.git('worktree', 'list', '--porcelain')
        for phase, suffix in ((1, 'packed-refs'), (2, '.pack')):
            with self.subTest(phase=phase, suffix=suffix):
                reached = self.base / f'copy-{phase}'
                setup = f'''
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
source, mounts = sandbox.git_source, sandbox.git_mounts
projections = 0
monotonic = sandbox.time.monotonic
expired = False
def count(*args, **kwargs):
    global projections
    projections += 1
    return mounts(*args, **kwargs)
@contextmanager
def expire_during_read(parent, name, *, directory=False):
    with source(parent, name, directory=directory) as stream:
        if stream is None or directory or projections != {phase} or not name.endswith({suffix!r}):
            yield stream
            return
        reads = 0
        def read(method, size):
            global expired
            nonlocal reads
            data = method(size)
            reads += 1
            if reads == 2 and data:
                Path({str(reached)!r}).touch()
                expired = True
            return data
        yield SimpleNamespace(read=lambda size: read(stream.read, size),
                              readline=lambda size: read(stream.readline, size))
sandbox.git_mounts, sandbox.git_source = count, expire_during_read
sandbox.time = SimpleNamespace(monotonic=lambda: monotonic() + (60 if expired else 0))
'''
                result = self.worker('raise AssertionError("worker started")', role='astra', sandboxed=True,
                                     setup=setup, exit_code=78)
                self.assertTrue(reached.is_file(), result.stderr)
                self.assertIn('review Git deadline expired', result.stderr)
                self.assertFalse(list((self.product / '.runs').glob('*.events.jsonl')))
                self.assertFalse(list((self.home / 'work/demo').glob('review-*')))
                self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_review_budget_checked_after_final_projection(self):
        before = self.git('worktree', 'list', '--porcelain')
        setup = self.setup_budget + '''
wrap = sandbox.wrap
def expire_after_projection(cmd, **kwargs):
    argv = wrap(cmd, **kwargs)
    if cmd[0] == 'codex':
        run_codex.time.sleep(run_codex.review_remaining(kwargs['git_deadline']) + 0.01)
    return argv
sandbox.wrap = expire_after_projection
'''
        result = self.worker('raise AssertionError("worker started")', role='astra', sandboxed=True,
                             setup=setup, exit_code=78)
        self.assertIn('review Git deadline expired', result.stderr)
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
        self.assertFalse(list((self.home / 'work/demo').glob('review-*')))
        self.assertFalse((self.product / 'USAGE.jsonl').exists())

    def test_worker_head_verification_refuses_to_start_codex_on_mismatch(self):
        setup = '''
from pathlib import Path
wrap = sandbox.wrap
def replace_head(cmd, **kwargs):
    argv = wrap(cmd, **kwargs)
    if cmd[0] == 'codex':
        pointer = (kwargs['clone'] / '.git').read_text().removeprefix('gitdir: ').strip()
        (Path(pointer) / 'HEAD').write_text('0' * 40 + '\\n')
    return argv
sandbox.wrap = replace_head
'''
        result = self.worker(f"Path({str(self.base / 'started')!r}).touch()", '--sha', self.head,
                             role='analyst', sandboxed=True, exit_code=78, setup=setup)
        record = json.loads(result.stdout)
        self.assertIn('HEAD does not match the pinned commit', Path(record['events']).read_text())
        self.assertFalse((self.base / 'started').exists())

    def test_unknown_context_from_cli_or_task_fails_before_launch(self):
        body = 'raise AssertionError("the worker must not start")'
        result = self.worker(body, '--context', 'missing.md', role='analyst', exit_code=1)
        self.assertIn("Cannot build worker context: cannot include context file 'missing.md'", result.stderr)
        self.prompt.write_text('# Test task\nContext: unknown.md\n')
        result = self.worker(body, role='analyst', exit_code=1)
        self.assertIn("Cannot build worker context: cannot include context file 'unknown.md'", result.stderr)
        self.assertFalse((self.product / 'HANDOFF.md').exists())

    def test_registered_task_commits_there_and_allows_owner_code_with_emails_and_paths(self):
        task = self.new_task()
        (self.config / 'privacy.json').write_text('{"deny": ["internal-name"]}')
        content = 'Example internal-name maintainer' + '@' + 'private.tld /' + 'home/account/project\n'
        result = self.worker(f"(cwd / '[id].js').write_text({content!r})", '--task', '19-task')
        record = json.loads(result.stdout)
        self.assertEqual(record['commit'], self.git('rev-parse', 'HEAD', cwd=task).strip())
        self.assertNotEqual(record['commit'], self.head)
        self.assertEqual(self.git('show', 'HEAD:[id].js', cwd=task), content)
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), str(task))
        self.assert_owner_unchanged()

    def test_registered_task_deletion_only_commits_using_explicit_worktree_cwd(self):
        task = self.new_task()
        result = self.worker("(cwd / 'tracked').unlink()", '--cwd', str(task))
        self.assertIsNotNone(json.loads(result.stdout)['commit'])
        self.assertEqual(self.git('diff-tree', '--no-commit-id', '--name-status', 'HEAD', cwd=task), 'D\ttracked\n')
        self.assert_owner_unchanged()

    def test_registered_protected_paths_fail_before_staging(self):
        for slug, path in (('19-knowledge', '.korolev/PLAN.md'), ('19-environment', 'app/.env')):
            with self.subTest(path=path):
                task = self.new_task(slug)
                result = self.worker(f'p = cwd / {path!r}\np.parent.mkdir()\np.write_text("blocked test")',
                                     '--task', slug, exit_code=4)
                self.assertIn(path, result.stderr)
                self.assertNotIn('blocked test', result.stderr)
                self.assertIsNone(json.loads(result.stdout)['commit'])
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
                self.assertEqual(self.git('diff', '--cached', '--name-only', cwd=task), '')
                self.assertTrue((task / path).exists())

    def test_registered_secret_in_added_line_is_redacted_and_unstaged(self):
        task = self.new_task()
        fake_token = '123456789:' + 'AA' + 'z' * 32
        body = f"(cwd / ':details [1].js').write_text('++ b/header ' + {fake_token!r} + '\\n')\n"
        result = self.worker(body, '--task', '19-task', exit_code=4)
        self.assertIn('telegram bot token', result.stderr)
        self.assertIn(':details [1].js', result.stderr)
        self.assertIn('123…zz', result.stderr)
        self.assertNotIn(fake_token, result.stderr)
        self.assertIsNone(json.loads(result.stdout)['commit'])
        self.assertEqual(self.git('diff', '--cached', '--name-only', cwd=task), '')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
        self.assertTrue((task / ':details [1].js').exists())

    def test_staged_rename_checks_both_paths_and_commits_regular_rename(self):
        task = self.new_task()
        move = "subprocess.run(['git', 'mv', 'tracked', 'renamed'], cwd=cwd, check=True)"
        result = self.worker(move, '--task', '19-task')
        self.assertIsNotNone(json.loads(result.stdout)['commit'])
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD', cwd=task), 'renamed\n')
        self.git('mv', 'renamed', '.env', cwd=task)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture', cwd=task)
        result = self.worker("subprocess.run(['git', 'mv', '.env', 'renamed'], cwd=cwd, check=True)",
                             '--task', '19-task', exit_code=4)
        self.assertIn('.env', result.stderr)

    def test_launchers_ignore_worker_selected_git_hooks_in_legacy_and_task_repos(self):
        task = self.new_task()
        legacy = self.home / 'work' / 'legacy' / 'repo'
        (self.home / 'products' / 'legacy').mkdir()
        self.make_repo(legacy)
        body = '''
hooks = cwd / '.husky'
hooks.mkdir()
hook = hooks / 'pre-commit'
hook.write_text('#!/bin/sh\\ntouch hook-ran\\nexit 1\\n')
hook.chmod(0o700)
subprocess.run(['git', 'config', 'core.hooksPath', str(hooks)], cwd=cwd, check=True)
for name in ('[id].js', '*.txt', ':literal', 'multiple\\nlines'):
    (cwd / name).touch()
'''
        for repo, product, options in ((task, 'demo', ('--task', '19-task', '--write')),
                                       (legacy, 'legacy', ('--write',))):
            with self.subTest(product=product):
                result = self.worker(body, *options, role='analyst', product=product)
                self.assertIsNotNone(json.loads(result.stdout)['commit'], result.stderr)
                self.assertFalse((repo / 'hook-ran').exists())
                files = self.git('ls-tree', '--name-only', '-z', 'HEAD', cwd=repo).split('\0')
                self.assertEqual(set(files[:-1]), {'.husky', 'tracked', '[id].js', '*.txt', ':literal', 'multiple\nlines'})
        self.assert_owner_unchanged()

    def test_staging_ignores_repository_filter_selected_by_worker_attributes(self):
        task = self.new_task()
        marker = self.base / 'filter-ran'
        driver = task / 'filter-driver'
        self.git('config', 'filter.fixture.clean', shlex.quote(str(driver)))
        self.git('config', 'filter.fixture.required', 'true')
        script = '#!/bin/sh\n: > ' + shlex.quote(str(marker)) + '\nprintf filtered\n'
        body = f'''
Path({str(driver)!r}).write_text({script!r})
Path({str(driver)!r}).chmod(0o700)
(cwd / '.gitattributes').write_text('tracked filter=fixture\\n*.dat filter=fixture\\n')
(cwd / 'tracked').write_text('raw tracked\\n')
(cwd / 'new.dat').write_text('raw new\\n')
'''
        result = self.worker(body, '--task', '19-task')
        self.assertIsNotNone(json.loads(result.stdout)['commit'], result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.git('show', 'HEAD:tracked', cwd=task), 'raw tracked\n')
        self.assertEqual(self.git('show', 'HEAD:new.dat', cwd=task), 'raw new\n')

    def test_worker_created_gitfile_refuses_staging_without_recording_a_gitlink(self):
        task = self.new_task()
        index = self.git('ls-files', '--stage', cwd=task)
        pointer = f'gitdir: {self.owner / ".git"}\n'
        body = f"""
embedded = cwd / 'sub'
embedded.mkdir()
(embedded / '.git').write_text({pointer!r})
(embedded / 'worker-file').touch()
"""
        result = self.worker(body, '--task', '19-task', exit_code=4)
        self.assertIn('embedded Git repository is unsupported: sub/.git', result.stderr)
        self.assertIsNone(json.loads(result.stdout)['commit'])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
        self.assertEqual(self.git('ls-files', '--stage', cwd=task), index)
        self.assertNotIn('160000', self.git('ls-tree', 'HEAD', cwd=task))
        self.assert_owner_unchanged()

    def test_failed_pack_index_link_cleans_up_and_allows_retry(self):
        task = self.new_task()
        common = self.owner / '.git'
        executable = Path(shutil.which('git')).resolve()
        staging = run_codex.staging_checkout(task, task, executable)
        index = staging[3] / 'index'
        before = index.read_bytes()
        data = 'packed fixture\n' * 8192
        (task / 'large').write_text(data)
        launcher_git, link = run_codex.launcher_git, os.link
        packs = set()

        def pack_large_blob(folder, *args, **kwargs):
            return launcher_git(folder, '-c', 'core.bigFileThreshold=64k', *args, **kwargs)

        def fail_pack_index(source, destination, **kwargs):
            if source.name.startswith('import-'):
                packs.add(source.suffix)
                if source.suffix == '.idx':
                    self.assertTrue(destination.with_suffix('.keep').is_file())
                    raise OSError('fixture refused the index link')
            return link(source, destination, **kwargs)

        with (run_codex.sandbox.git_root(task) as fd,
              patch.object(run_codex, 'launcher_git', side_effect=pack_large_blob)):
            with patch.object(run_codex.os, 'link', side_effect=fail_pack_index):
                with self.assertRaisesRegex(OSError, 'fixture refused the index link'):
                    run_codex.commit_changes(staging, task, '# Pack fixture', 'test', checkout_fd=fd,
                                             expected_common=common)
            self.assertEqual(packs, {'.pack', '.idx'})
            self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
            self.assertEqual(index.read_bytes(), before)
            pack_dir = common / 'objects' / 'pack'
            self.assertEqual(list(pack_dir.iterdir()), [])
            commit = run_codex.commit_changes(staging, task, '# Pack fixture', 'test', checkout_fd=fd,
                                              expected_common=common)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), commit)
        self.assertEqual(self.git('show', f'{commit}:large', cwd=task), data)
        self.assertTrue(list((common / 'objects' / 'pack').glob('pack-*.idx')))
        self.git('fsck', '--connectivity-only', cwd=task)

    def test_worker_checkout_replacement_after_exit_refuses_in_both_modes(self):
        common = self.owner / '.git'
        for sandboxed in (False, True):
            with self.subTest(sandboxed=sandboxed):
                slug = f'19-checkout-swap-{int(sandboxed)}'
                task = self.new_task(slug)
                index = common / 'worktrees' / task.name / 'index'
                before = index.read_bytes()
                original = task.with_name(f'{task.name}-original')
                setup = f"""
import subprocess
from pathlib import Path
communicate = subprocess.Popen.communicate
def replace_checkout(proc, *args, **kwargs):
    result = communicate(proc, *args, **kwargs)
    if '--json' in proc.args and '-o' in proc.args:
        assert proc.returncode == 0
        checkout = Path({str(task)!r})
        original = checkout.rename({str(original)!r})
        checkout.mkdir()
        (checkout / '.git').write_bytes((original / '.git').read_bytes())
        (checkout / 'tracked').write_text('replacement content')
    return result
subprocess.Popen.communicate = replace_checkout
"""
                result = self.worker("(cwd / 'tracked').write_text('worker content')", '--task', slug,
                                     sandboxed=sandboxed, setup=setup, exit_code=4)
                self.assertIn('directory changed during launch', result.stderr)
                self.assertIsNone(json.loads(result.stdout)['commit'])
                self.assertEqual((original / 'tracked').read_text(), 'worker content')
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
                self.assertEqual(index.read_bytes(), before)

    def test_task_backlink_is_checked_at_admission_and_after_worker_exit(self):
        task = self.new_task()
        private = Path(self.git('rev-parse', '--absolute-git-dir', cwd=task).strip())
        backlink, index = private / 'gitdir', private / 'index'
        relative = os.path.relpath(task / '.git', private) + '\n'
        dangling = 'absent/../' + relative
        backlink.write_text(dangling)
        for role, code in (('builder', 77), ('analyst', 1)):
            result = self.worker('raise AssertionError("worker started")', '--task', '19-task',
                                 role=role, exit_code=code)
            self.assertIn('a registered task worktree is required', result.stderr)
        self.assertFalse((self.product / '.runs').exists())

        backlink.write_text(relative)
        before = index.read_bytes()
        setup = f"""
import subprocess
from pathlib import Path
communicate = subprocess.Popen.communicate
def change_backlink(proc, *args, **kwargs):
    result = communicate(proc, *args, **kwargs)
    if '--json' in proc.args and '-o' in proc.args:
        assert proc.returncode == 0
        Path({str(backlink)!r}).write_text({dangling!r})
    return result
subprocess.Popen.communicate = change_backlink
"""
        result = self.worker("(cwd / 'tracked').write_text('changed')", '--task', '19-task',
                             exit_code=4, setup=setup)
        self.assertIn('backlink does not name the pinned checkout', result.stderr)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
        backlink.write_text(relative)
        result = self.worker('', '--task', '19-task')
        self.assertIsNotNone(json.loads(result.stdout)['commit'])

    def test_publication_refuses_a_same_oid_head_retarget_after_capture(self):
        task = self.new_task()
        common = self.owner / '.git'
        staging = run_codex.staging_checkout(task, task, Path(shutil.which('git')).resolve())
        index = staging[3] / 'index'
        before = index.read_bytes()
        self.git('branch', 'other', cwd=task)
        refs = self.git('show-ref', cwd=task)
        (task / 'tracked').write_text('changed')
        stage = run_codex.stage_changes

        def retarget(*args, **kwargs):
            commit = stage(*args, **kwargs)
            self.git('symbolic-ref', 'HEAD', 'refs/heads/other', cwd=task)
            return commit

        with (run_codex.sandbox.git_root(task) as fd,
              patch.object(run_codex, 'stage_changes', side_effect=retarget)):
            with self.assertRaisesRegex(run_codex.VerificationError, 'checkout HEAD or ref changed'):
                run_codex.commit_changes(staging, task, '# Retarget', 'test', checkout_fd=fd, expected_common=common)
        self.assertEqual(self.git('symbolic-ref', 'HEAD', cwd=task).strip(), 'refs/heads/other')
        self.assertEqual(self.git('show-ref', cwd=task), refs)
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((index.parent / 'HEAD.lock').exists())

    def test_publication_fences_detached_and_unborn_head_through_rollback_and_success(self):
        executable = Path(shutil.which('git')).resolve()
        prepare, replace = run_codex.prepared_publication, Path.replace
        for form in ('detached', 'unborn'):
            with self.subTest(form=form):
                repo = self.base / form
                if form == 'detached':
                    self.make_repo(repo)
                    self.git('checkout', '--detach', cwd=repo)
                else:
                    self.git('init', '-q', '-b', 'main', repo, cwd=self.base)
                (repo / 'new').write_text('candidate')
                staging = run_codex.staging_checkout(repo, repo, executable)
                metadata = repo / '.git'
                head, index = metadata / 'HEAD', metadata / 'index'
                original_head = head.read_bytes()
                original_index = index.read_bytes() if index.exists() else None
                original_refs = self.git('for-each-ref', cwd=repo)
                attempted = []

                def verify_fenced(path, target):
                    if target.name == 'index':
                        retarget = subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/other'],
                                                  cwd=repo, env=self.env, capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(retarget.returncode, 0)
                        self.assertIn('HEAD.lock', retarget.stderr)
                        self.assertTrue((metadata / 'index.lock').exists())
                        if form == 'unborn':
                            self.assertTrue((metadata / 'refs/heads/main.lock').exists())
                        attempted.append(path.name)
                    return replace(path, target)

                @contextmanager
                def fail_commit(*args):
                    def refuse():
                        self.assertTrue(index.exists())
                        raise OSError('fixture refused ref commit')

                    with prepare(*args):
                        yield refuse

                with run_codex.sandbox.git_root(repo) as fd, patch.object(Path, 'replace', verify_fenced):
                    with patch.object(run_codex, 'prepared_publication', fail_commit):
                        with self.assertRaisesRegex(OSError, 'fixture refused ref commit'):
                            run_codex.commit_changes(staging, repo, '# Rollback', 'test', checkout_fd=fd)
                    self.assertEqual(head.read_bytes(), original_head)
                    self.assertEqual(self.git('for-each-ref', cwd=repo), original_refs)
                    self.assertEqual(index.read_bytes() if index.exists() else None, original_index)
                    commit = run_codex.commit_changes(staging, repo, '# Publish', 'test', checkout_fd=fd)
                self.assertEqual(attempted, ['prepared', 'original', 'prepared'] if original_index else
                                 ['prepared', 'prepared'])
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=repo).strip(), commit)
                self.assertEqual(self.git('status', '--porcelain', cwd=repo), '')
                self.assertFalse(list(metadata.rglob('*.lock')))

    def test_publication_refuses_unsupported_backend_and_indirect_head(self):
        task = self.new_task()
        executable = Path(shutil.which('git')).resolve()
        staging = run_codex.staging_checkout(task, task, executable)
        common, private = staging[2:]
        (task / 'tracked').write_text('changed')
        index = private / 'index'
        before = index.read_bytes()
        config = str(common / 'config')
        self.git('config', '--file', config, 'extensions.refStorage', 'unknown')
        with run_codex.sandbox.git_root(task) as fd:
            with self.assertRaisesRegex(run_codex.VerificationError, 'unsupported Git ref backend'):
                run_codex.commit_changes(staging, task, '# Backend', 'test', checkout_fd=fd, expected_common=common)
            self.git('config', '--file', config, '--unset', 'extensions.refStorage', cwd=self.base)
            self.git('symbolic-ref', 'refs/heads/alias', 'refs/heads/main', cwd=task)
            self.git('symbolic-ref', 'HEAD', 'refs/heads/alias', cwd=task)
            refs = self.git('show-ref', '--head', cwd=task)
            with self.assertRaisesRegex(run_codex.VerificationError, 'unsupported checkout HEAD'):
                run_codex.commit_changes(staging, task, '# Indirect', 'test', checkout_fd=fd, expected_common=common)
        self.assertEqual(self.git('show-ref', '--head', cwd=task), refs)
        self.assertEqual(self.git('symbolic-ref', '--no-recurse', 'HEAD', cwd=task).strip(), 'refs/heads/alias')
        self.assertEqual(index.read_bytes(), before)

    def test_tracked_hardlink_to_root_gitfile_refuses_staging(self):
        task = self.new_task()
        alias = task / 'tracked'
        alias.unlink()
        os.link(task / '.git', alias)
        self.git('add', 'tracked', cwd=task)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                 'commit', '-qm', 'tracked alias', cwd=task)
        head = self.git('rev-parse', 'HEAD', cwd=task)
        index = Path(self.git('rev-parse', '--git-path', 'index', cwd=task).strip())
        before, contents = index.read_bytes(), alias.read_bytes()
        result = self.worker("(cwd / 'new').touch()", '--task', '19-task', exit_code=4)
        self.assertIn('multiply linked root Git metadata', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task), head)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), contents)
        self.assertTrue(alias.samefile(task / '.git'))

    def test_staged_modes_refuse_new_and_changed_gitlinks_and_keep_existing_ones(self):
        executable = Path(shutil.which('git')).resolve()
        for scenario in ('unborn', 'new', 'changed', 'unchanged'):
            with self.subTest(scenario=scenario):
                repo = self.base / scenario
                if scenario == 'unborn':
                    self.git('init', '-q', '-b', 'main', repo, cwd=self.base)
                else:
                    self.make_repo(repo)
                oid = self.head if scenario == 'unborn' else self.git('rev-parse', 'HEAD', cwd=repo).strip()
                module = repo / 'module'
                module.mkdir()
                if scenario in ('changed', 'unchanged'):
                    self.git('update-index', '--add', '--cacheinfo', f'160000,{oid},module', cwd=repo)
                    self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                             'commit', '-qm', 'existing gitlink', cwd=repo)
                (repo / 'unrelated').touch()
                if scenario != 'unchanged':
                    wanted = self.git('rev-parse', 'HEAD', cwd=repo).strip() if scenario == 'changed' else oid
                    self.git('update-index', '--add', '--cacheinfo', f'160000,{wanted},module', cwd=repo)
                head = (repo / '.git' / 'HEAD').read_bytes()
                git = lambda *args, **kwargs: run_codex.launcher_git(repo, *args, executable=executable, **kwargs)
                if scenario == 'unchanged':
                    commit = run_codex.stage_changes(git, repo, '# Modes', 'test', verify=True)
                    self.assertEqual(self.git('ls-tree', commit, 'module', cwd=repo), f'160000 commit {oid}\tmodule\n')
                else:
                    ref = self.git('show-ref', '--head', cwd=repo) if scenario != 'unborn' else None
                    with self.assertRaisesRegex(run_codex.VerificationError, 'new or changed staged Git links'):
                        run_codex.stage_changes(git, repo, '# Modes', 'test', verify=True)
                    self.assertEqual((repo / '.git' / 'HEAD').read_bytes(), head)
                    if ref is not None:
                        self.assertEqual(self.git('show-ref', '--head', cwd=repo), ref)
                    self.assertIn('160000', self.git('ls-files', '--stage', 'module', cwd=repo))

    def test_ignored_sparse_file_is_frozen_without_expanding_holes(self):
        task = self.new_task()
        common = self.owner / '.git'
        executable = Path(shutil.which('git')).resolve()
        staging = run_codex.staging_checkout(task, task, executable)
        (task / '.gitignore').write_text('sparse\n')
        size = 4 * 1024 * 1024
        with (task / 'sparse').open('wb') as data:
            data.write(b'head')
            data.seek(size - 4)
            data.write(b'tail')
        stage = run_codex.stage_changes

        def inspect_export(git, checkout, *args, **kwargs):
            sparse = checkout / 'sparse'
            info = sparse.stat()
            self.assertEqual(info.st_size, size)
            self.assertLess(info.st_blocks * 512, size // 2)
            with sparse.open('rb') as file:
                self.assertEqual(file.read(4), b'head')
                self.assertLess(os.lseek(file.fileno(), 0, os.SEEK_HOLE), size // 2)
                file.seek(size // 2)
                self.assertEqual(file.read(4), b'\0' * 4)
                file.seek(-4, os.SEEK_END)
                self.assertEqual(file.read(), b'tail')
            return stage(git, checkout, *args, **kwargs)

        with (run_codex.sandbox.git_root(task) as fd,
              patch.object(run_codex, 'stage_changes', side_effect=inspect_export) as checked):
            commit = run_codex.commit_changes(staging, task, '# Sparse', 'test', checkout_fd=fd, expected_common=common)
        checked.assert_called_once()
        self.assertEqual(self.git('ls-tree', '--name-only', commit, cwd=task), '.gitignore\ntracked\n')

    def test_cumulative_checkout_copy_budget_refuses_without_publication(self):
        task = self.new_task()
        common = self.owner / '.git'
        index = common / 'worktrees' / task.name / 'index'
        before = index.read_bytes()
        body = """
(cwd / '.gitignore').write_text('ignored-*\\n')
for i in range(3):
    (cwd / f'ignored-{i}').write_bytes(b'x' * 5120)
"""
        result = self.worker(body, '--task', '19-task', exit_code=4,
                             setup='sandbox.CHECKOUT_COPY_BUDGET = 12 * 1024')
        self.assertIn('checkout export exceeds its cumulative byte budget', result.stderr)
        self.assertIsNone(json.loads(result.stdout)['commit'])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((index.parent / 'index.lock').exists())
        self.assertFalse(list((common / 'objects').glob('korolev-stage-*')))
        self.assertFalse(list((common / 'objects' / 'pack').glob('*')))

    def test_staging_cumulative_entry_and_metadata_limits_leave_repository_intact(self):
        task = self.new_task()
        common = self.owner / '.git'
        index = common / 'worktrees' / task.name / 'index'
        before = index.read_bytes()
        for name in ('a', 'b'):
            child = task / name
            child.mkdir()
            (child / 'empty').touch()
            (child / 'link').symlink_to('x' * 200)
        names = ('a', 'b', 'tracked', '.git', 'empty', 'link', 'empty', 'link')
        metadata = len(names) * run_codex.sandbox.CHECKOUT_ENTRY_CHARGE + sum(map(len, names)) + 200
        for limit, value, error in (('CHECKOUT_ENTRY_LIMIT', 7, 'entry limit'),
                                    ('CHECKOUT_METADATA_BUDGET', metadata, 'metadata budget')):
            with self.subTest(limit=limit):
                result = self.worker('', '--task', '19-task', exit_code=4, setup=f'sandbox.{limit} = {value}')
                self.assertIn(error, result.stderr)
                self.assertIsNone(json.loads(result.stdout)['commit'])
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
                self.assertEqual(index.read_bytes(), before)
                self.assertFalse(list(index.parent.glob('*.lock')))
                self.assertFalse(list(index.parent.glob('korolev-index-*')))
                self.assertFalse(list((common / 'objects').glob('korolev-stage-*')))

    def test_kept_publication_pack_survives_repack_until_ref_and_index_outcome(self):
        common = self.owner / '.git'
        executable = Path(shutil.which('git')).resolve()
        objects = common / 'objects'
        pack_dir = objects / 'pack'
        launcher_git, replace = run_codex.launcher_git, Path.replace
        for outcome in ('success', 'ref-locked', 'index-refused'):
            with self.subTest(outcome=outcome):
                task = self.new_task(f'19-kept-{outcome}')
                staging = run_codex.staging_checkout(task, task, executable)
                index = staging[3] / 'index'
                before = index.read_bytes()
                ref = self.git('symbolic-ref', 'HEAD', cwd=task).strip()
                ref_lock = common / f'{ref}.lock'
                borrowed, loose = task / 'borrowed', task / 'loose'
                borrowed.write_text(f'borrowed {outcome}\n')
                loose.write_text(f'loose {outcome}\n')
                borrowed_oid = self.git('hash-object', '-w', borrowed, cwd=task).strip()
                loose_oid = self.git('hash-object', '-w', loose, cwd=task).strip()
                packed = launcher_git(task, 'pack-objects', str(pack_dir / 'pack'), executable=executable,
                                      input=f'{borrowed_oid}\n'.encode('ascii'))
                self.assertEqual(packed.returncode, 0, packed.stderr)
                donor = pack_dir / f'pack-{packed.stdout.strip().decode()}.pack'
                self.git('prune-packed', cwd=task)
                self.assertFalse((objects / borrowed_oid[:2] / borrowed_oid[2:]).exists())
                self.assertTrue((objects / loose_oid[:2] / loose_oid[2:]).is_file())
                kept = []
                connected = False
                def concurrent_maintenance(folder, *args, **kwargs):
                    nonlocal connected
                    result = launcher_git(folder, *args, **kwargs)
                    if 'rev-list' in args and '--missing=error' in args:
                        connected = result.returncode == 0
                    return result

                prepare = run_codex.prepared_publication

                def repack_before_prepare(*args):
                    self.assertTrue(connected)
                    keep, = pack_dir.glob('*.keep')
                    kept.append(keep)
                    listing = self.git('verify-pack', '-v', keep.with_suffix('.idx'), cwd=task)
                    for oid in (borrowed_oid, loose_oid):
                        self.assertIn(f'{oid} blob ', listing)
                    self.git('repack', '-ad', cwd=task)
                    self.assertFalse(donor.exists())
                    self.assertTrue(keep.is_file())
                    self.assertTrue(keep.with_suffix('.pack').is_file())
                    if outcome == 'ref-locked':
                        ref_lock.touch()
                    return prepare(*args)

                def publish_index(path, target):
                    if target == index:
                        self.assertTrue(kept[0].is_file())
                        retarget = subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'],
                                                  cwd=task, env=self.env, capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(retarget.returncode, 0)
                        self.assertIn('HEAD.lock', retarget.stderr)
                        self.assertTrue(ref_lock.exists())
                        if outcome == 'index-refused':
                            raise OSError('fixture refused index replacement')
                    return replace(path, target)

                with (run_codex.sandbox.git_root(task) as fd,
                      patch.object(run_codex, 'launcher_git', side_effect=concurrent_maintenance),
                      patch.object(run_codex, 'prepared_publication', side_effect=repack_before_prepare),
                      patch.object(Path, 'replace', publish_index)):
                    try:
                        if outcome == 'success':
                            commit = run_codex.commit_changes(staging, task, '# Repack', 'test', checkout_fd=fd,
                                                              expected_common=common)
                        else:
                            error, message = ((OSError, 'fixture refused index replacement')
                                              if outcome == 'index-refused' else
                                              (run_codex.VerificationError, 'cannot publish the staging commit'))
                            with self.assertRaisesRegex(error, message):
                                run_codex.commit_changes(staging, task, '# Repack', 'test', checkout_fd=fd,
                                                         expected_common=common)
                    finally:
                        ref_lock.unlink(missing_ok=True)
                self.assertEqual(len(kept), 1)
                self.assertFalse(list(pack_dir.glob('*.keep')))
                if outcome == 'success':
                    self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), commit)
                    self.assertEqual(self.git('show', f'{commit}:borrowed', cwd=task), borrowed.read_text())
                    self.assertEqual(self.git('show', f'{commit}:loose', cwd=task), loose.read_text())
                    self.assertEqual(self.git('status', '--porcelain', cwd=task), '')
                    self.git('fsck', '--connectivity-only', cwd=task)
                else:
                    self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
                    self.assertEqual(index.read_bytes(), before)

    def test_pack_import_preserves_preexisting_destination_entries(self):
        task = self.new_task()
        common = self.owner / '.git'
        staging = run_codex.staging_checkout(task, task, Path(shutil.which('git')).resolve())
        (task / 'tracked').write_text('candidate content')
        index = staging[3] / 'index'
        before = index.read_bytes()
        link = os.link
        for suffix in ('.keep', '.idx'):
            with self.subTest(suffix=suffix):
                foreign = []

                def collide(source, destination, **kwargs):
                    if destination.suffix == suffix:
                        destination.write_text('owned by another attempt\n')
                        foreign.append(destination)
                    return link(source, destination, **kwargs)

                with (run_codex.sandbox.git_root(task) as fd,
                      patch.object(run_codex.os, 'link', side_effect=collide)):
                    with self.assertRaises(FileExistsError):
                        run_codex.commit_changes(staging, task, '# Collision', 'test', checkout_fd=fd,
                                                 expected_common=common)
                entry, = foreign
                self.assertEqual(entry.read_text(), 'owned by another attempt\n')
                self.assertEqual(list(entry.parent.iterdir()), [entry])
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
                self.assertEqual(index.read_bytes(), before)
                entry.unlink()

    def test_isolated_staging_preserves_paths_from_a_split_index(self):
        task = self.new_task()
        self.git('mv', 'tracked', 'renamed', cwd=task)
        self.git('update-index', '--split-index', cwd=task)
        self.assertTrue(self.git('rev-parse', '--shared-index-path', cwd=task).strip())
        result = self.worker("(cwd / 'new').write_text('new')", '--task', '19-task')
        self.assertIsNotNone(json.loads(result.stdout)['commit'], result.stderr)
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD', cwd=task), 'new\nrenamed\n')
        self.assertEqual(self.git('status', '--porcelain', cwd=task), '')

    def test_legacy_write_from_nested_cwd_stages_paths_from_checkout_root(self):
        legacy = self.home / 'work' / 'legacy' / 'repo'
        (self.home / 'products' / 'legacy').mkdir()
        self.make_repo(legacy)
        nested = legacy / 'nested'
        nested.mkdir()
        result = self.worker("(cwd / '[id].js').touch()\n(cwd.parent / 'tracked').unlink()",
                             '--cwd', str(nested), '--write', product='legacy', role='analyst')
        self.assertIsNotNone(json.loads(result.stdout)['commit'], result.stderr)
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'HEAD', cwd=legacy), 'nested/[id].js\n')
