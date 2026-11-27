import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from skill.scripts import release
from common import is_task_worktree

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())
        self.home = self.base / 'korolev'
        self.product_dir = self.home / 'products' / 'demo'
        self.product_dir.mkdir(parents=True)
        self.tasks_json = self.product_dir / 'tasks.json'
        self.owner = self.base / 'Owner Folder'
        self.env.update(HOME=str(self.base), KOROLEV_HOME=str(self.home))
        self.git('init', '-q', '-b', 'main', self.owner, cwd=self.base)
        self.git('config', 'user.name', 'Task test')
        self.git('config', 'user.email', 'test@example.test')
        (self.owner / 'tracked').write_text('base\n')
        self.git('add', 'tracked')
        self.git('commit', '-qm', 'base')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.registry = self.home / 'projects.json'
        self.registry.write_text(json.dumps({'demo': {'folder': str(self.owner),
                                                      'git_common_dir': str(self.owner / '.git'),
                                                      'branch': 'main'}}))

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '--no-optional-locks', *args], cwd=cwd or self.owner,
                              env=self.env, check=True, capture_output=True, text=True, timeout=10).stdout

    def tasks(self, command, *args, ok=True):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'tasks.py'), command,
                                 '--product', 'demo', *args], env=self.env, capture_output=True,
                                text=True, timeout=10)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def test_new_uses_owner_head_and_preserves_checkout_and_index(self):
        hooks = self.owner / '.git' / 'hooks'
        hook = hooks / 'post-checkout'
        hook.write_text('#!/bin/sh\ntouch checkout-hook-ran\nexit 1\n')
        hook.chmod(0o700)
        (self.owner / 'tracked').write_text('staged owner work')
        self.git('add', 'tracked')
        (self.owner / 'tracked').write_text('unstaged owner work')
        (self.owner / 'owner-only').write_text('untracked owner work')
        before_files = sorted(path.name for path in self.owner.iterdir() if path.name != '.git')
        before_status = self.git('status', '--porcelain')
        before_index = (self.owner / '.git' / 'index').read_bytes()

        record = self.tasks('new', '--slug', '19-task')
        task = self.home / 'tasks' / 'demo' / '19-task'
        self.assertEqual(record, dict(slug='19-task', path=str(task), branch='korolev/19-task',
                                      base_sha=self.head, created=record['created']))
        self.assertEqual(stat.S_IMODE(task.parent.stat().st_mode), 0o700)
        self.assertEqual(self.git('branch', '--show-current', cwd=task).strip(), record['branch'])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=task).strip(), self.head)
        self.assertEqual((task / 'tracked').read_text(), 'base\n')
        self.assertFalse((task / 'checkout-hook-ran').exists())
        self.assertEqual(json.loads(self.tasks_json.read_text()), {'19-task': record})
        self.assertEqual(self.tasks('list'), [dict(record, exists=True, head=self.head, dirty_count=0)])
        (task / 'dirty').touch()
        self.assertEqual(self.tasks('list')[0]['dirty_count'], 1)
        self.assertIn(str(task), self.tasks('new', '--slug', '19-task', ok=False))
        self.assertEqual(sorted(path.name for path in self.owner.iterdir() if path.name != '.git'), before_files)
        self.assertEqual(self.git('status', '--porcelain'), before_status)
        self.assertEqual((self.owner / '.git' / 'index').read_bytes(), before_index)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual(self.git('branch', '--show-current').strip(), 'main')

    def test_explicit_base_and_existing_branch_or_directory(self):
        self.git('tag', 'task-base')
        self.git('commit', '--allow-empty', '-qm', 'new owner HEAD')
        self.assertEqual(self.tasks('new', '--slug', '19-base', '--base', 'task-base')['base_sha'], self.head)
        self.git('branch', 'korolev/19-branch')
        for slug in ('19-branch', '19-directory'):
            path = self.home / 'tasks' / 'demo' / slug
            if slug.endswith('directory'):
                path.mkdir()
            self.assertIn(str(path), self.tasks('new', '--slug', slug, ok=False))

    def test_remove_requires_clean_worktree_and_merge_into_recorded_branch(self):
        record = self.tasks('new', '--slug', '19-task')
        task = Path(record['path'])
        (task / 'change').touch()
        options = ('--slug', record['slug'], '--delete-branch')
        self.assertIn('dirty', self.tasks('remove', *options, ok=False))
        self.git('add', 'change', cwd=task)
        self.git('commit', '-qm', 'task change', cwd=task)
        self.assertIn('not merged', self.tasks('remove', *options, ok=False))
        self.assertTrue(task.is_dir())
        self.assertEqual(json.loads(self.tasks_json.read_text()), {record['slug']: record})
        self.git('branch', 'old-owner-head')
        self.git('merge', '--ff-only', record['branch'])
        self.git('switch', '-q', 'old-owner-head')
        self.git('branch', '--set-upstream-to=old-owner-head', record['branch'])
        self.tasks('remove', *options)
        self.assertFalse(task.exists())
        self.assertEqual(self.git('branch', '--list', record['branch']), '')
        self.assertEqual(self.tasks('list'), [])

    def test_remove_can_keep_unmerged_branch(self):
        record = self.tasks('new', '--slug', '19-keep')
        task = Path(record['path'])
        self.git('commit', '--allow-empty', '-qm', 'task only', cwd=task)
        self.tasks('remove', '--slug', record['slug'])
        self.assertFalse(task.exists())
        self.assertIn(record['branch'], self.git('branch', '--list', record['branch']))

    def test_rejects_invalid_slug_and_unregistered_project(self):
        self.assertIn('invalid task slug', self.tasks('new', '--slug', '../task', ok=False))
        self.registry.unlink()
        self.assertIn('not registered', self.tasks('list', ok=False))

    def test_remote_credential_diagnostic_only_names_remote(self):
        for key, url in (('remote.origin.url', 'https://fake-user:fake-pass@example.test/project'),
                         ('remote.backup.pushurl', 'http://fake-access-token@example.test/project')):
            with self.subTest(key=key):
                self.git('config', key, url)
                error = self.tasks('new', '--slug', '19-task', ok=False)
                self.assertIn(key.split('.')[1], error)
                self.assertNotIn(url, error)
                self.assertNotIn('fake-', error)
                self.assertFalse(self.tasks_json.exists())
                self.git('config', '--unset', key)

    def test_worktree_guard_requires_record_and_matching_common_dir(self):
        task = Path(self.tasks('new', '--slug', '19-task')['path'])
        other = self.base / 'other'
        self.git('worktree', 'add', '--detach', other, 'HEAD')
        with patch('common.HOME', self.home), patch.dict(os.environ, GIT_CONFIG_COUNT='malformed',
                                                        GIT_CONFIG_PARAMETERS='malformed'):
            with patch.dict(os.environ, self.env, clear=True):
                self.assertTrue(is_task_worktree('demo', task))
                self.assertFalse(is_task_worktree('demo', self.owner))
                self.assertFalse(is_task_worktree('demo', other))
                self.assertFalse(is_task_worktree('legacy', task))
                (task / '.git').write_text(f'gitdir: {self.owner / ".git"}\n')
                self.assertFalse(is_task_worktree('demo', task))
                (task / '.git').unlink()
                self.git('init', '-q', task)
                self.assertFalse(is_task_worktree('demo', task))
            self.assertEqual(os.environ['GIT_CONFIG_COUNT'], 'malformed')
            self.assertEqual(os.environ['GIT_CONFIG_PARAMETERS'], 'malformed')

    def test_release_rollback_guard_resolves_registered_owner_folder(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.owner, target_is_directory=True)
        with patch('common.HOME', self.home):
            with self.assertRaisesRegex(ValueError, 'rollback refused in registered owner folder'):
                release.guard_rollback(alias)
            release.guard_rollback(self.base / 'legacy')
