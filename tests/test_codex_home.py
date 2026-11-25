import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class CodexHomeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.user_home = self.base / 'user'
        self.real_home = self.user_home / '.codex'
        self.real_home.mkdir(parents=True)
        self.auth = self.real_home / 'auth.json'
        self.auth.write_text('{}\n')
        (self.real_home / 'config.toml').write_text('model_reasoning_summary = "auto"\n')
        self.isolated_home = self.user_home / '.local' / 'share' / 'korolev' / 'codex-home'
        self.config_dir = self.user_home / '.config' / 'korolev'
        self.config_dir.mkdir(parents=True)
        (self.config_dir / 'config.json').write_text('{"worker_sandbox": false}\n')
        self.korolev_home = self.base / 'korolev'
        self.product = self.korolev_home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        self.prompt = self.product / 'TASK.md'
        self.prompt.write_text('Test task')

        bin_dir = self.base / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'git').symlink_to(shutil.which('git'))
        fake_codex = bin_dir / 'codex'
        fake_codex.write_text(
            f'#!{sys.executable}\n'
            'import os, sys\n'
            'from pathlib import Path\n'
            'if sys.argv[1:] == ["--version"]:\n'
            '    print("fake codex")\n'
            '    sys.exit(0)\n'
            'sys.stdin.read()\n'
            'out = Path(sys.argv[sys.argv.index("-o") + 1])\n'
            'out.write_text(os.environ.get("CODEX_HOME", "<unset>"))\n'
        )
        fake_codex.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.user_home), PATH=str(bin_dir),
                        KOROLEV_HOME=str(self.korolev_home), KOROLEV_CONFIG_DIR=str(self.config_dir),
                        KOROLEV_BOT_STATE=str(self.base / 'bot'))
        for key in ('CODEX_HOME', 'KOROLEV_CODEX_HOME'):
            self.env.pop(key, None)

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, '-I', str(SCRIPTS / script), *args], cwd=self.base,
                              env=self.env, capture_output=True, text=True, timeout=10)

    def run_worker(self):
        return self.run_script('run_codex.py', '--role', 'analyst', '--product', 'demo',
                               '--cwd', str(self.base), '--prompt', str(self.prompt), '--out', 'HANDOFF.md')

    def assert_worker_home(self, expected, mode):
        result = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), str(expected))
        record = json.loads(result.stdout)
        self.assertEqual(record.get('codex_home'), mode)
        self.assertEqual(json.loads((self.product / 'USAGE.jsonl').read_text().splitlines()[-1]), record)

    def disable_isolation(self):
        (self.config_dir / 'config.json').write_text('{"worker_sandbox": false, "isolate_codex_home": false}\n')

    def test_worker_is_isolated_by_default(self):
        self.assert_worker_home(self.isolated_home, 'isolated')
        self.assertEqual(stat.S_IMODE(self.isolated_home.stat().st_mode), 0o700)
        self.assertEqual((self.isolated_home / 'config.toml').read_text(), 'model_reasoning_summary = "none"\n')
        self.assertEqual((self.isolated_home / 'auth.json').readlink(), self.auth)

    def test_config_switch_preserves_the_inherited_codex_home(self):
        self.disable_isolation()
        for user_home in (None, self.real_home):
            with self.subTest(user_home=user_home):
                if user_home is not None:
                    self.env['CODEX_HOME'] = str(user_home)
                self.assert_worker_home(user_home or '<unset>', 'user')
        self.assertFalse(self.isolated_home.exists())

    def test_overrides_link_to_the_users_auth_without_changing_the_real_home(self):
        isolated = self.base / 'isolated'
        real_home = self.base / 'custom-codex'
        self.real_home.rename(real_home)
        self.env.update(CODEX_HOME=str(real_home), KOROLEV_CODEX_HOME=str(isolated))
        before = {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in real_home.iterdir()}
        self.assert_worker_home(isolated, 'isolated')
        self.assertEqual((isolated / 'auth.json').readlink(), real_home / 'auth.json')
        after = {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in real_home.iterdir()}
        self.assertEqual(after, before)

    def test_worker_rejects_an_isolated_home_inside_the_real_home(self):
        self.env['KOROLEV_CODEX_HOME'] = str(self.real_home / 'isolated')
        result = self.run_worker()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.real_home / 'isolated').exists())

    def test_doctor_passes_when_auth_symlink_is_valid(self):
        result = self.run_script('doctor.py', '--offline')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS codex home isolation: on', result.stdout)
        self.assertIn('PASS isolated codex auth: valid symlink', result.stdout)

    def test_doctor_fails_when_auth_target_is_missing_with_isolation_on(self):
        self.auth.unlink()
        result = self.run_script('doctor.py', '--offline')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('PASS codex home isolation: on', result.stdout)
        self.assertIn('FAIL isolated codex auth:', result.stdout)

    def test_doctor_reports_missing_auth_without_failing_with_isolation_off(self):
        self.auth.unlink()
        self.disable_isolation()
        result = self.run_script('doctor.py', '--offline')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS codex home isolation: off', result.stdout)
        self.assertIn('WARN isolated codex auth:', result.stdout)
        self.assertFalse(self.isolated_home.exists())
