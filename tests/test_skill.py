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


class RoleTests(Sandbox):
    def test_config_overrides_roles(self):
        config = self.base / 'config'
        config.mkdir()
        (config / 'config.json').write_text(json.dumps({'roles': {'builder': {'effort': 'xhigh'}}}))
        out = self.py("from common import roles\nr = roles()\nprint(r['builder']['effort'], r['astra']['model'])")
        self.assertEqual(out, 'xhigh gpt-6.2-astra')


if __name__ == '__main__':
    unittest.main()
