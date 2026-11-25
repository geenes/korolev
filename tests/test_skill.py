"""Offline tests for the Korolev skill scripts. Run: python3 -m unittest discover -s tests"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skill' / 'scripts'


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.env = dict(os.environ, KOROLEV_HOME=str(base / 'home'), KOROLEV_CONFIG_DIR=str(base / 'config'),
                        KOROLEV_BOT_STATE=str(base / 'bot'))
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, name, *args, check=True):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / name), *args], env=self.env,
                                capture_output=True, text=True)
        if check and result.returncode != 0:
            self.fail(f'{name} failed: {result.stderr}')
        return result

    def py(self, code):
        result = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n{code}'],
                                env=self.env, capture_output=True, text=True)
        if result.returncode != 0:
            self.fail(result.stderr)
        return result.stdout.strip()


class OnboardTests(Sandbox):
    def make_origin(self):
        origin = self.base / 'origin'
        origin.mkdir()
        for cmd in (['git', 'init', '-q', '-b', 'main'],):
            subprocess.run(cmd, cwd=origin, check=True)
        (origin / 'README.md').write_text('# Demo\n')
        (origin / 'docs').mkdir()
        (origin / 'docs' / 'SPEC.md').write_text('# Spec\n')
        (origin / 'sub').mkdir()
        (origin / 'sub' / 'AGENTS.md').write_text('# Rules\n')
        (origin / 'tests').mkdir()
        (origin / 'tests' / 'test_x.py').write_text('def test_x():\n    assert True\n')
        (origin / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
        subprocess.run(['git', 'add', '.'], cwd=origin, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], cwd=origin, check=True)
        return origin

    def test_init_local_path_leaves_original_untouched_and_writes_inventory(self):
        origin = self.make_origin()
        before = subprocess.run(['git', 'status', '--porcelain'], cwd=origin, capture_output=True, text=True).stdout
        out = json.loads(self.run_script('onboard.py', 'init', str(origin), '--name', 'demo').stdout)
        self.assertIn('original untouched', out['clone_status'])
        inv = Path(out['inventory']).read_text()
        for needle in ('docs/SPEC.md', 'sub/AGENTS.md', 'pytest', 'test-like files'):
            self.assertIn(needle, inv)
        self.assertEqual(before, subprocess.run(['git', 'status', '--porcelain'], cwd=origin,
                                                capture_output=True, text=True).stdout)
        for name in ('STATE.md', 'DECISIONS.md', 'PLAN.md', 'QUESTIONS.md', 'product.json'):
            self.assertTrue((Path(out['product_dir']) / name).exists(), name)

    def test_reinit_keeps_owner_files(self):
        origin = self.make_origin()
        out = json.loads(self.run_script('onboard.py', 'init', str(origin), '--name', 'demo').stdout)
        decisions = Path(out['product_dir']) / 'DECISIONS.md'
        decisions.write_text('owner decision\n')
        again = json.loads(self.run_script('onboard.py', 'init', str(origin), '--name', 'demo').stdout)
        self.assertEqual(again['clone_status'], 'existing clone, fetched')
        self.assertEqual(decisions.read_text(), 'owner decision\n')


class TelegramTests(Sandbox):
    def test_chunks_split_long_text_and_keep_short_text(self):
        out = self.py("from notify import chunks\n"
                      "print(len(chunks('a'*10)), len(chunks('\\n\\n'.join(['b'*2000]*3))), max(map(len, chunks('c'*9000))))")
        self.assertEqual(out.split(), ['1', '3', '3800'])

    def test_reply_routing_and_prefix(self):
        bot = self.base / 'bot'
        bot.mkdir()
        (bot / 'SENT.jsonl').write_text(json.dumps({'message_id': 7, 'project': 'alpha'}) + '\n')
        products = self.base / 'home' / 'products' / 'beta'
        products.mkdir(parents=True)
        (products / 'product.json').write_text(json.dumps({'display': 'Beta Shop'}))
        out = self.py("from inbox import project_for\n"
                      "print(project_for(7, 'x'), project_for(None, 'Beta Shop: ship it'), project_for(None, 'hello'))")
        self.assertEqual(out, 'alpha beta None')

    def test_second_reader_is_refused_by_lock(self):
        bot = self.base / 'bot'
        bot.mkdir()
        with (bot / 'inbox.lock').open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script('inbox.py', check=False)
        self.assertEqual(result.returncode, 75)

    def test_notify_without_config_fails_cleanly(self):
        result = self.run_script('notify.py', '--project', 'x', 'hi', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not configured', result.stderr)


class LessonTests(Sandbox):
    def test_add_list_resolve_lifecycle(self):
        lid = self.run_script('lesson.py', 'add', '--title', 'T', '--detail', 'D', '--proposal', 'P').stdout.strip()
        self.assertRegex(lid, r'^L-\d{8}-1$')
        self.assertIn('pending', self.run_script('lesson.py', 'list').stdout)
        self.run_script('lesson.py', 'resolve', lid, 'approved')
        self.assertIn('approved', self.run_script('lesson.py', 'list', '--status', 'approved').stdout)
        direct = self.run_script('lesson.py', 'add', '--title', 'X', '--detail', 'D', '--proposal', 'P',
                                 '--status', 'applied').stdout.strip()
        self.assertTrue(direct.endswith('-2'))
        self.assertIn(direct, self.run_script('lesson.py', 'list', '--status', 'applied').stdout)

    def test_propose_without_telegram_fails_cleanly_and_keeps_pending(self):
        self.run_script('lesson.py', 'add', '--title', 'T', '--detail', 'D', '--proposal', 'P')
        result = self.run_script('lesson.py', 'propose', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pending', self.run_script('lesson.py', 'list').stdout)

    def test_inventory_lists_agent_tooling(self):
        origin = self.base / 'eco'
        (origin / '.claude' / 'skills' / 'x').mkdir(parents=True)
        (origin / '.claude' / 'skills' / 'x' / 'SKILL.md').write_text('---\nname: x\n---\n')
        (origin / '.mcp.json').write_text('{}')
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=origin, check=True)
        subprocess.run(['git', 'add', '.'], cwd=origin, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'i'], cwd=origin, check=True)
        out = json.loads(self.run_script('onboard.py', 'init', str(origin), '--name', 'eco').stdout)
        inv = Path(out['inventory']).read_text()
        self.assertIn('Claude skills', inv)
        self.assertIn('MCP servers', inv)


class ReleaseTests(Sandbox):
    def test_version_bump(self):
        out = self.py("from release import bump\nprint(bump('0.1.9','patch'), bump('0.1.9','minor'), bump('1.2.3','major'))")
        self.assertEqual(out, '0.1.10 0.2.0 2.0.0')


class PrivacyTests(Sandbox):
    def repo_with(self, content, email='12345+someone@users.noreply.github.com', message='change'):
        repo = self.base / 'r'
        repo.mkdir(exist_ok=True)
        if not (repo / '.git').exists():
            subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=repo, check=True)
            (repo / 'a.txt').write_text('base\n')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', '-c', 'user.name=x', '-c', f'user.email={email}', 'commit', '-qm', 'base'], cwd=repo, check=True)
        (repo / 'b.txt').write_text(content)
        subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=x', '-c', f'user.email={email}', 'commit', '-qm', message], cwd=repo, check=True)
        return repo

    def scan(self, repo):
        return self.run_script('privacy_scan.py', '--repo', str(repo), '--range', 'HEAD~1..HEAD', '--json', check=False)

    def test_clean_change_passes(self):
        result = self.scan(self.repo_with('hello world\n'))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_blocks_personal_email_path_secret_and_private_term(self):
        config = self.base / 'config'
        config.mkdir(exist_ok=True)
        (config / 'privacy.json').write_text(json.dumps({'deny': ['projectx']}))
        fake_email = 'jane.doe' + '@' + 'gmail' + '.com'  # built from parts so this file passes the scan itself
        fake_home = '/' + 'home' + '/alice/work/file'
        cases = ['contact me at ' + fake_email + '\n', 'see ' + fake_home + '\n',
                 'token = "' + 'ghp_' + 'A' * 36 + '"\n', 'notes about ProjectX launch\n']
        for content in cases:
            with self.subTest(content=content[:20]):
                self.tmp.cleanup(); self.setUp(); config = self.base / 'config'; config.mkdir()
                (config / 'privacy.json').write_text(json.dumps({'deny': ['projectx']}))
                result = self.scan(self.repo_with(content))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn(fake_email, result.stdout)  # findings are redacted

    def test_blocks_personal_commit_email(self):
        result = self.scan(self.repo_with('fine\n', email='real.person' + '@' + 'gmail' + '.com'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('author', result.stdout)

    def test_publish_is_maintainer_only(self):
        result = self.run_script('release.py', 'publish', '--repo', str(self.base), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('maintainer-only', result.stderr)

    def test_publish_refuses_without_approval(self):
        self.env['KOROLEV_TEST_SKIP_MAINTAINER'] = '1'
        result = self.run_script('release.py', 'publish', '--repo', str(self.base), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no release proposal', result.stderr)
        state = self.base / 'home' / 'products' / 'korolev'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'release-pending.json').write_text(json.dumps({'status': 'waiting', 'message_id': 1}))
        result = self.run_script('release.py', 'publish', '--repo', str(self.base), check=False)
        self.assertIn('not approved', result.stderr)


class RoleTests(Sandbox):
    def test_config_overrides_roles(self):
        config = self.base / 'config'
        config.mkdir()
        (config / 'config.json').write_text(json.dumps({'roles': {'builder': {'effort': 'xhigh'}}}))
        out = self.py("from common import roles\nr = roles()\nprint(r['builder']['effort'], r['astra']['model'])")
        self.assertEqual(out, 'xhigh gpt-6.2-astra')


class ReleaseTitleTests(Sandbox):
    lesson_args = ('--title', 'Long worker chats waste tokens', '--detail', 'Long sessions cost more.',
                   '--proposal', 'Flag costly runs.')
    change_title = 'Warning when a worker run gets too expensive'

    def write_legacy_lesson(self):
        lesson_id = 'L-20261125-1'
        log = self.base / 'home' / 'lessons' / 'lessons.jsonl'
        log.parent.mkdir(parents=True)
        events = [
            {'event': 'add', 'id': lesson_id, 'kind': 'cost', 'title': self.lesson_args[1],
             'detail': self.lesson_args[3], 'proposal': self.lesson_args[5]},
            {'event': 'status', 'id': lesson_id, 'status': 'applied'},
        ]
        log.write_text(''.join(json.dumps(event) + '\n' for event in events))
        return lesson_id, log

    def draft_notes(self):
        repo = self.base / 'draft-repo'
        subprocess.run(['git', 'clone', '-q', '--local', '--no-hardlinks', '--no-tags', '--no-checkout',
                        str(ROOT), str(repo)], check=True)
        result = self.run_script('release.py', 'notes', '--repo', str(repo))
        return result.stdout, (repo / 'RELEASE-NOTES.draft.md').read_text()

    def test_add_requires_change_title(self):
        result = self.run_script('lesson.py', 'add', *self.lesson_args, check=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('required: --change', result.stderr)

    def test_retitle_appends_event_and_preserves_status(self):
        lesson_id, log = self.write_legacy_lesson()
        before = log.read_text()
        self.assertIn(self.lesson_args[1], self.run_script('lesson.py', 'list').stdout)
        self.run_script('lesson.py', 'retitle', lesson_id, '--change', self.change_title)
        after = log.read_text()
        self.assertTrue(after.startswith(before))
        event = json.loads(after[len(before):])
        self.assertEqual((event['event'], event['id'], event['change']), ('retitle', lesson_id, self.change_title))
        listing = self.run_script('lesson.py', 'list', '--status', 'applied').stdout
        self.assertIn(self.change_title, listing)
        self.assertNotIn(self.lesson_args[1], listing)
        _, draft = self.draft_notes()
        self.assertIn(f'**{self.change_title}** ({lesson_id})', draft)

    def test_draft_uses_change_title_and_keeps_commits_last(self):
        lesson_id = self.run_script('lesson.py', 'add', *self.lesson_args, '--change', self.change_title,
                                    '--status', 'applied').stdout.strip()
        _, draft = self.draft_notes()
        self.assertIn(f'- **{self.change_title}** ({lesson_id})\n  - Why: {self.lesson_args[3]}', draft)
        self.assertNotIn('What changed:', draft)
        self.assertEqual([line for line in draft.splitlines() if line.startswith('### ')],
                         ['### Fixed', '### Commits'])

    def test_draft_warns_when_falling_back_to_legacy_title(self):
        lesson_id, _ = self.write_legacy_lesson()
        output, draft = self.draft_notes()
        self.assertIn(f'- **{self.lesson_args[1]}** ({lesson_id})\n'
                      f'  - Why: {self.lesson_args[3]}\n  - What changed: {self.lesson_args[5]}', draft)
        self.assertIn('Warning: no change title', output)
        self.assertIn(f'lesson.py retitle {lesson_id}', output)


if __name__ == '__main__':
    unittest.main()
