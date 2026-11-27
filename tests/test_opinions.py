import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__:
    from .git_fixture import git_fixture
    from .test_sandbox import EXECUTABLE_FIXTURE
else:
    from git_fixture import git_fixture
    from test_sandbox import EXECUTABLE_FIXTURE



SKILL = Path(__file__).resolve().parents[1] / 'skill'
PLAN_FILES = {'PLAN.md': 'Proposed plan', 'ANALYSIS.md': 'Repository analysis', 'INVENTORY.md': 'File inventory'}


class OpinionsTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())
        home = self.base / 'korolev'
        self.product = home / 'products' / 'demo'
        self.product.mkdir(parents=True)
        self.repo = home / 'work' / 'demo' / 'repo'
        self.repo.mkdir(parents=True)
        config = self.base / 'config'
        config.mkdir()
        (config / 'config.json').write_text('{"worker_network": "host"}')
        self.env.update(HOME=str(self.base), KOROLEV_HOME=str(home),
                        KOROLEV_CONFIG_DIR=str(config), CODEX_HOME=str(self.base / 'user-codex-home'),
                        KOROLEV_CODEX_HOME=str(self.base / 'codex-home'),
                        PATH=os.pathsep.join([str(self.base), os.environ['PATH']]))
        self.git('init', '-q')
        (self.repo / 'tracked').write_text('Base revision')
        self.git('add', 'tracked')
        self.git('commit', '-qm', 'base')
        for name, content in PLAN_FILES.items():
            (self.product / name).write_text(content)
        self.installed = self.base / 'installed-skill'
        self.installed.symlink_to(SKILL, target_is_directory=True)
        self.bwrap = self.base / 'bwrap'
        self.bwrap.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import os, sys
            cmd = sys.argv[sys.argv.index('--') + 1:]
            if cmd[4:5] == ['sandbox-selftest']:
                print('sandbox checks passed')
            else:
                os.execv(cmd[0], cmd)
        '''))
        self.bwrap.chmod(0o700)
        self.ready = self.base / 'ready'
        self.ready.mkdir()
        fake = self.base / 'codex'
        fake.write_text(f'#!{sys.executable}\n'
                        'import json, subprocess, sys, time\n'
                        'from pathlib import Path\n'
                        'prompt = sys.stdin.read()\n'
                        'bundle = Path(next(line.removeprefix("Run context bundle: ") for line in prompt.splitlines()\n'
                        '                   if line.startswith("Run context bundle: ")))\n'
                        'cwd = Path(sys.argv[sys.argv.index("-C") + 1])\n'
                        'if (bundle / "code-review.md").exists():\n'
                        '    assert (bundle / "packages/demo/TASK.md").read_text() == "Check pinned revision"\n'
                        '    assert "Commit under review:" in prompt\n'
                        f'ready = Path({str(self.ready)!r})\n'
                        '(ready / cwd.name).touch()\n'
                        'deadline = time.monotonic() + 5\n'
                        'while len(list(ready.iterdir())) < 2:\n'
                        '    assert time.monotonic() < deadline, "reviews did not overlap"\n'
                        '    time.sleep(0.01)\n'
                        f'head = subprocess.check_output([{shutil.which("git")!r}, "rev-parse", "HEAD"],\n'
                        '                               cwd=cwd, text=True).strip()\n'
                        f'files = {{name: (bundle / name).read_text() for name in {list(PLAN_FILES)!r}}}\n'
                        'Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({"head": head, "files": files}))\n')
        fake.chmod(0o700)

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args],
                              cwd=self.repo, env=self.env, check=True, capture_output=True, text=True,
                              timeout=10).stdout.strip()

    def opinions(self, *args, before_spawn='', before_verify='', before_publish='', prompt='plan-review.md'):
        launcher = (f'import runpy, sys; sys.path.insert(0, {str(SKILL / "scripts")!r}); import sandbox; '
                    + EXECUTABLE_FIXTURE + f'sandbox.bwrap_path = lambda: {str(self.bwrap)!r}; '
                    + f'runpy.run_path({str(SKILL / "scripts/run_codex.py")!r}, run_name="__main__")')
        driver = f'''
import subprocess, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(SKILL / "scripts")!r})
import opinions, sandbox
{EXECUTABLE_FIXTURE}
def spawn(cmd, **kwargs):
    {before_spawn or 'pass'}
    return subprocess.Popen([*cmd[:2], '-c', {launcher!r}, *cmd[3:]], **kwargs)
opinions.subprocess = SimpleNamespace(Popen=spawn, PIPE=subprocess.PIPE)
verify_report = opinions.verify_report
def verify(files, round_fd, out, record):
    {before_verify or 'pass'}
    return verify_report(files, round_fd, out, record)
opinions.verify_report = verify
publish_manifest = opinions.publish_manifest
def publish(round_dir, *args):
    {before_publish or 'pass'}
    return publish_manifest(round_dir, *args)
opinions.publish_manifest = publish
sys.exit(opinions.main())
'''
        return subprocess.run([sys.executable, '-I', '-c', driver, '--product', 'demo',
                               '--prompt', str(self.installed / 'templates/prompts' / prompt), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def assert_reviews(self, sha, result, tag=''):
        manifest = json.loads(self.assert_manifest(sha, result, tag).read_text())
        records = [json.loads(line) for line in (self.product / 'USAGE.jsonl').read_text().splitlines()]
        self.assertCountEqual([(row['role'], row['reviewed_sha']) for row in records],
                              [('astra', sha), ('sol56', sha)])
        for row in records:
            self.assertEqual(row['out_sha256'], manifest['report_sha256'][row['role']])
            self.assertEqual(row['out_bytes'], Path(row['out']).stat().st_size)
        self.assertEqual(len(list(self.ready.iterdir())), 2)
        self.assertFalse(list(self.repo.parent.glob('review-*')))

    def assert_manifest(self, sha, result, tag=''):
        path = Path(result.stdout.splitlines()[-1])
        manifest = json.loads(path.read_text())
        self.assertEqual((manifest['round_id'], manifest['tag'], manifest['reviewed_sha']), (path.parent.name, tag, sha))
        self.assertEqual(set(manifest['reports']), {'astra', 'sol56'})
        self.assertEqual(manifest['report_sha256'], {
            role: hashlib.sha256(Path(report).read_bytes()).hexdigest()
            for role, report in manifest['reports'].items()
        })
        for role, report in manifest['reports'].items():
            self.assertEqual(Path(report), path.parent / f'OPINION-{role}.md')
            opinion = json.loads(Path(report).read_text())
            self.assertEqual(opinion, {'head': sha, 'files': PLAN_FILES})
        return path

    def test_documented_plan_review_bundles_all_declared_files_and_requires_them(self):
        inventory = self.product / 'INVENTORY.md'
        inventory.unlink()
        missing = self.opinions()
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn("cannot include context file 'INVENTORY.md'", missing.stderr)
        self.assertFalse(list(self.product.glob('review-round-*/OPINION-*.md')))
        self.assertFalse(list(self.product.glob('review-round-*/manifest.json')))
        self.assertFalse(list((self.product / '.runs').glob('*.events.jsonl')))
        inventory.write_text(PLAN_FILES[inventory.name])
        result = self.opinions()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_reviews(self.git('rev-parse', 'HEAD'), result)

    def test_sha_pins_both_opinions_to_the_requested_commit(self):
        sha = self.git('rev-parse', 'HEAD')
        (self.repo / 'tracked').write_text('Newer revision')
        self.git('commit', '-qam', 'newer')
        result = self.opinions('--sha', sha)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_reviews(sha, result)

    def test_round_resolves_head_before_either_launcher_starts(self):
        old = self.git('rev-parse', 'HEAD')
        newer = self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                         'commit-tree', 'HEAD^{tree}', '-p', old, '-m', 'newer')
        move_head = (f'subprocess.run([{shutil.which("git")!r}, "update-ref", "HEAD", {newer!r}], '
                     f'cwd={str(self.repo)!r}, check=True)')
        result = self.opinions(before_spawn=move_head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), newer)
        self.assert_reviews(old, result)

    def test_round_fails_when_a_launcher_is_killed_or_reports_a_different_sha(self):
        cases = (
            (f'import os, signal; os.kill(os.getpid(), {signal.SIGKILL})', 128 + signal.SIGKILL, 'exit 137'),
            ('print(\'{"role": "astra", "reviewed_sha": "other"}\')', 1, 'mismatched role, reviewed_sha'),
        )
        for code, exit_code, diagnostic in cases:
            with self.subTest(code=code):
                result = self.opinions('--roles', 'astra', before_spawn=(
                    f'return subprocess.Popen([sys.executable, "-I", "-c", {code!r}], **kwargs)'))
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertFalse(list(self.product.glob('review-round-*/manifest.json')))

    def test_same_tag_rounds_overlap_at_different_commits(self):
        old = self.git('rev-parse', 'HEAD')
        (self.repo / 'tracked').write_text('New revision')
        self.git('commit', '-qam', 'newer')
        new = self.git('rev-parse', 'HEAD')
        fake = self.base / 'codex'
        fake.write_text(fake.read_text().replace('len(list(ready.iterdir())) < 2', 'len(list(ready.iterdir())) < 4'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            rounds = [(sha, pool.submit(self.opinions, '--tag', 'same', '--sha', sha)) for sha in (old, new)]
        paths = []
        for sha, future in rounds:
            result = future.result()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            paths.append(self.assert_manifest(sha, result, 'same'))
        self.assertNotEqual(*paths)
        self.assertEqual(len(list(self.ready.iterdir())), 4)

    def test_failed_child_prevents_publication_even_when_other_child_succeeds(self):
        fake = self.base / 'codex'
        fake.write_text(fake.read_text().replace('len(list(ready.iterdir())) < 2', 'len(list(ready.iterdir())) < 1'))
        result = self.opinions(before_spawn='''
    if cmd[cmd.index('--role') + 1] == 'sol56':
        return subprocess.Popen([sys.executable, '-I', '-c', 'raise SystemExit(17)'], **kwargs)''')
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(len(list(self.product.glob('review-round-*/OPINION-astra.md'))), 1)
        self.assertFalse(list(self.product.glob('review-round-*/manifest.json')))

    def test_round_refuses_directory_and_report_swaps_after_verification(self):
        for swap in (
            '''
    round_dir.rename(round_dir.with_suffix('.old'))
    round_dir.mkdir()
    (round_dir / 'OPINION-astra.md').write_text('replacement report')
''',
            '''
    report = round_dir / 'OPINION-astra.md'
    report.rename(report.with_suffix('.old'))
    report.write_text('replacement report')
''',
        ):
            with self.subTest(swap=swap):
                result = self.opinions(before_publish=swap)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Cannot publish review round', result.stderr)
                self.assertNotIn('manifest.json', result.stdout)
                self.assertFalse(list(self.product.glob('review-round-*/manifest.json')))

    def test_round_refuses_regular_file_substitution_before_first_parent_read(self):
        for delta in (0, 1):
            with self.subTest(size_delta=delta):
                result = self.opinions(before_verify=f'''
    if out.name == 'OPINION-astra.md':
        replacement = out.with_suffix('.replacement')
        replacement.write_bytes(b'x' * (out.stat().st_size + {delta}))
        replacement.replace(out)
''')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('mismatched role, reviewed_sha or report', result.stderr)
                self.assertFalse(list(self.product.glob('review-round-*/manifest.json')))

    def test_round_does_not_follow_preexisting_manifest_temp_symlink(self):
        target = self.base / 'private.txt'
        target.write_text('private bytes')
        result = self.opinions(before_spawn=f'''
    temporary = Path(cmd[cmd.index('--out') + 1]).with_name('manifest.json.tmp')
    if not temporary.is_symlink():
        temporary.symlink_to({str(target)!r})
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self.assert_manifest(self.git('rev-parse', 'HEAD'), result)
        self.assertEqual(manifest.with_name('manifest.json.tmp').readlink(), target)
        self.assertEqual(target.read_text(), 'private bytes')

    def test_prompt_only_round_publishes_null_sha(self):
        fake = self.base / 'codex'
        script = fake.read_text()
        begin = script.index('head = subprocess.check_output')
        end = script.index('files = ', begin)
        fake.write_text(script[:begin] + 'assert not list(cwd.iterdir())\nhead = None\n' + script[end:])
        result = self.opinions('--prompt-only', '--cwd', str(self.base))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_reviews(None, result)

    def test_code_review_template_requires_sha_and_task_context(self):
        result = self.opinions(prompt='code-review.md')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('requires --sha', result.stderr)
        result = self.opinions('--sha', 'HEAD', prompt='code-review.md')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('requires TASK.md in the context', result.stderr)
        self.assertFalse(list(self.ready.iterdir()))
        task = self.product / 'packages/demo/TASK.md'
        task.parent.mkdir(parents=True)
        task.write_text('Check pinned revision')
        result = self.opinions('--sha', 'HEAD', '--context', 'packages/demo/TASK.md', *PLAN_FILES,
                               prompt='code-review.md')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_reviews(self.git('rev-parse', 'HEAD'), result)

    def test_opinions_rejects_noncanonical_product_ids_before_spawning(self):
        for name in ('Demo', 'demo.one', 'demo_one', '../demo'):
            with self.subTest(name=name):
                result = self.opinions('--product', name, '--prompt-only',
                                       before_spawn='raise AssertionError("reviewer started")')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid product name', result.stderr)
