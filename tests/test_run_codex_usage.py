import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skill.scripts import run_codex


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class RunCodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write_events(self, *usages, extra_events=()):
        path = self.base / 'events.jsonl'
        turn_events = [{'type': 'turn.completed', 'usage': usage} for usage in usages]
        path.write_text(''.join(json.dumps(event) + '\n' for event in (*extra_events, *turn_events)))
        return path

    def run_worker(self, *usages, config=None, exit_code=0, extra_events=()):
        events = self.write_events(*usages, extra_events=extra_events)
        fake_codex = self.base / 'codex'
        fake_codex.write_text(
            f'#!{sys.executable}\n'
            'import sys\n'
            'from pathlib import Path\n'
            'sys.stdin.read()\n'
            "Path(sys.argv[sys.argv.index('-o') + 1]).write_text('Deliverable')\n"
            f'print(Path({str(events)!r}).read_text(), end="")\n'
            f'sys.exit({exit_code})\n'
        )
        fake_codex.chmod(0o700)
        home = self.base / 'home'
        product = home / 'products' / 'demo'
        product.mkdir(parents=True, exist_ok=True)
        prompt = product / 'TASK.md'
        prompt.write_text('Test task')
        config_dir = self.base / 'config'
        if config is not None:
            config_dir.mkdir()
            (config_dir / 'config.json').write_text(json.dumps(config))
        env = dict(os.environ, KOROLEV_HOME=str(home), KOROLEV_CONFIG_DIR=str(config_dir),
                   KOROLEV_CODEX_HOME=str(self.base / 'codex-home'), PATH=str(self.base))
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'run_codex.py'),
                                 '--role', 'analyst', '--product', 'demo', '--cwd', str(self.base),
                                 '--prompt', str(prompt), '--out', 'HANDOFF.md'],
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
        self.assertIsNone(record['usage'])
        self.assertIsNone(record['uncached_input_tokens'])
        self.assertEqual(record.get('commands_run'), 0)
        self.assertNotIn('input_tokens_per_turn', record)
        self.assertNotIn('warning', record)
        self.assertEqual(stderr, '')

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
