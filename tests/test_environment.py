"""Exercise Git-sensitive fixtures through filtered and direct unittest runs."""
import shlex
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def hostile_git_settings():
    with git_fixture() as (base, env):
        checkout = base / 'checkout'
        checkout.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=checkout, env=env, check=True)
        (checkout / 'tracked').write_text('sentinel index entry\n')
        subprocess.run(['git', 'add', 'tracked'], cwd=checkout, env=env, check=True)
        index = checkout / '.git' / 'index'
        original = index.read_bytes()

        hooks = base / 'hooks'
        hooks.mkdir()
        signer = base / 'signer'
        markers = (base / 'hook-called', base / 'signer-called')
        for program, marker in zip((hooks / 'pre-commit', signer), markers):
            program.write_text(f'#!/bin/sh\n: > {shlex.quote(str(marker))}\n')
            program.chmod(0o700)
        config = base / 'gitconfig'
        config.write_text(f'[core]\n hooksPath = "{hooks}"\n'
                          f'[gpg]\n program = "{signer}"\n[commit]\n gpgSign = true\n')
        env.update(GIT_INDEX_FILE=str(index), GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM='0',
                   GIT_CONFIG_SYSTEM=str(config), GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.hooksPath',
                   GIT_CONFIG_VALUE_0=str(hooks))

        stray = base / 'stray'
        (stray / '.git').mkdir(parents=True)
        try:
            yield env, (stray, checkout)
        finally:
            if index.read_bytes() != original or any(marker.exists() for marker in markers):
                raise AssertionError('fixture Git changed the sentinel index or invoked the sentinel hook or signer')


class GitFixtureTests(unittest.TestCase):
    def test_scoped_git_fixtures_ignore_host_settings_and_temp_ancestry(self):
        # Each subprocess resolves TMPDIR independently of tempfile's parent-process cache.
        commands = (
            ('discover', '-s', 'tests', '-p', 'test_attach.py', '-k', 'test_attach_records_git_metadata'),
            ('tests.test_attach.AttachTests.test_linked_worktree_cannot_acquire_the_owners_repository_lock',),
            ('tests.test_tasks.TasksTests.test_worktree_guard_requires_record_and_matching_common_dir',),
            ('discover', '-s', 'tests', '-p', 'test_skill.py', '-k', 'test_inventory_lists_agent_tooling'),
            ('tests.test_run_codex_usage.RunCodexUsageTests.test_missing_usage_is_null',),
        )
        with hostile_git_settings() as (env, temp_parents):
            for directory in temp_parents:
                for options in commands:
                    with self.subTest(temp_parent=directory.name, options=options):
                        result = subprocess.run([sys.executable, '-m', 'unittest', *options], cwd=ROOT,
                                                env=dict(env, TMPDIR=str(directory)), capture_output=True,
                                                text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('Ran 1 test', result.stderr)
