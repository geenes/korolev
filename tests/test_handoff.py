"""Exercise the handoff CLI with a temporary Korolev home and local data."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skill' / 'scripts' / 'handoff.py'


class HandoffTests(unittest.TestCase):
    def setUp(self):
        base, self.env = self.enterContext(git_fixture())
        self.home = base / 'home'
        self.bot = base / 'bot'
        self.env.update(KOROLEV_HOME=str(self.home), KOROLEV_BOT_STATE=str(self.bot),
                        KOROLEV_CONFIG_DIR=str(base / 'config'))
        self.product = self.home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        self.state = self.product / 'STATE.md'
        self.state.write_text('# State\n\n## Next\nKeep this.\n')
        self.notes = base / 'NOTES.md'

    def run_handoff(self, notes):
        self.notes.write_text(notes)
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--product', 'demo',
                                 '--file', str(self.notes)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.state.read_text(), result.stderr

    def write_records(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_adds_section_and_preserves_existing_state(self):
        before = self.state.read_text()
        state, _ = self.run_handoff('Continue from PLAN.md.\n\n### Suggested skills\n- korolev\n')
        self.assertTrue(state.startswith(before))
        self.assertEqual(state.count('## Session handoff'), 1)
        self.assertIn('Continue from PLAN.md.', state)
        self.assertIn('### Suggested skills', state)
        self.assertRegex(state, r'UTC: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')

    def test_replaces_section_without_consuming_neighboring_sections(self):
        self.state.write_text('# State\n\n```markdown\n## Session handoff\nExample only.\n```\n\n'
                              '## Session handoff\n\nOld notes.\n### Old detail\nRemove me.\n\n'
                              '## Next\nKeep this.\n')
        self.run_handoff('## Notes heading\nIntermediate notes.')
        state, _ = self.run_handoff('Replacement notes.')
        self.assertEqual(state.splitlines().count('## Session handoff'), 2)
        self.assertIn('Example only.\n```', state)
        self.assertIn('Replacement notes.', state)
        self.assertNotIn('Old notes.', state)
        self.assertNotIn('Remove me.', state)
        self.assertNotIn('Intermediate notes.', state)
        self.assertTrue(state.endswith('## Next\nKeep this.\n'))

    def test_redacts_sensitive_text_without_echoing_it(self):
        token = 'sk-' + 'proj-' + 'X' * 24
        password = 'Z' * 20
        home = '/' + 'home' + '/' + 'fake-coordinator'
        self.write_records(self.bot / 'INBOX.jsonl', [
            {'project': 'demo', 'message_id': 20, 'text': f'Private inbox text {token}'},
        ])
        state, warnings = self.run_handoff(f'Local checkout {home}/repo, key {token}\npassword = "{password}"')
        for value in (token, password, home):
            self.assertNotIn(value, state)
            self.assertNotIn(value, warnings)
        self.assertIn('[redacted]', state)
        self.assertIn('WARNING', warnings)

    def test_redacts_private_key_block_including_its_body(self):
        key_type = 'PRIVATE' + ' KEY'
        key_body = 'FAKE' + 'KEYMATERIAL' * 4
        state, _ = self.run_handoff(f'-----BEGIN {key_type}-----\n{key_body}\n-----END {key_type}-----\nResume.')
        self.assertNotIn(key_body, state)
        self.assertIn('Resume.', state)

    def test_truncates_long_notes(self):
        state, _ = self.run_handoff('\n'.join(f'Note line {i}' for i in range(100)))
        section = state.split('## Session handoff', 1)[1]
        self.assertLessEqual(len(section.splitlines()), 60)
        self.assertIn('Note line 0', section)
        self.assertNotIn('Note line 99', section)
        self.assertIn('truncated', section.lower())

    def test_missing_usage_and_inbox_are_reported(self):
        state, _ = self.run_handoff('Resume verification.')
        self.assertIn('No usage runs recorded', state)
        self.assertIn('No inbox messages recorded', state)

    def test_summarizes_recent_runs_pending_questions_and_project_inbox(self):
        self.write_records(self.product / 'USAGE.jsonl', [
            {'role': f'worker-{i}', 'exit': i, 'commit': f'abcdef{i}',
             'uncached_input_tokens': 0 if i == 1 else None, 'out': f'private-output-{i}'}
            for i in range(6)
        ])
        self.write_records(self.bot / 'INBOX.jsonl', [
            {'project': 'other' if i == 4 else 'demo', 'message_id': i, 'text': f'Owner message {i}'}
            for i in range(6)
        ])
        self.write_records(self.home / 'lessons' / 'lessons.jsonl', [
            {'event': 'add', 'id': 'L-1'},
            {'event': 'add', 'id': 'L-2'},
            {'event': 'status', 'id': 'L-2', 'status': 'approved'},
        ])
        (self.product / 'DECISIONS.md').write_text(
            '## Pending (asked 2026-11-25T18:00 UTC via Telegram, message 10)\n\n'
            '1. Choose a schedule?\n\n## 2026-11-25\nA recorded decision.\n\n'
            '## Pending\n- Choose a budget?\n'
        )
        state, _ = self.run_handoff('Follow up with the owner.')
        self.assertIn('Pending lessons: 1', state)
        self.assertIn('Choose a schedule?', state)
        self.assertIn('Choose a budget?', state)
        self.assertNotIn('A recorded decision.', state)
        self.assertNotIn('worker-0', state)
        self.assertRegex(state, r'worker-1.*exit 1.*abcdef1.*uncached.*\b0\b')
        self.assertNotIn('private-output', state)
        for i in (2, 3, 5):
            self.assertIn(f'Owner message {i}', state)
        for i in (0, 1, 4):
            self.assertNotIn(f'Owner message {i}', state)

    def test_keeps_numbered_pending_question_prompts_when_options_are_long(self):
        questions = '\n\n'.join(f'{i}. Question {i}?\nWhy it matters.\n\na) First choice.\nb) Second choice.'
                                 for i in range(1, 6))
        (self.product / 'DECISIONS.md').write_text('## Pending\n\n' + questions)
        state, _ = self.run_handoff('Follow up with the owner.')
        for i in range(1, 6):
            self.assertIn(f'Question {i}?', state)
        self.assertNotIn('First choice.', state)

    def test_reports_clone_head_branch_and_working_tree_status(self):
        repo = self.home / 'work' / 'demo' / 'repo'
        repo.parent.mkdir(parents=True)
        git = ['git', '-C', str(repo)]
        subprocess.run(['git', 'init', '-q', '-b', 'handoff-test', str(repo)], env=self.env, check=True)
        subprocess.run([*git, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                        'commit', '--allow-empty', '-qm', 'Fixture'], env=self.env, check=True)
        head = subprocess.check_output([*git, 'rev-parse', 'HEAD'], env=self.env, text=True).strip()
        state, _ = self.run_handoff('Notes')
        self.assertIn(head, state)
        self.assertIn('handoff-test', state)
        self.assertIn('clean', state)
        (repo / 'untracked.txt').write_text('Changed.')
        state, _ = self.run_handoff('Notes')
        self.assertIn('dirty', state)
