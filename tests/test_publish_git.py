"""Privileged Git transport tests with fake identity and temporary local remotes."""
import os
import hashlib
import subprocess
import sys
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__:
    from .git_fixture import git_fixture
    from .test_approvals import provision_receiver
    from .test_notify import FakeTelegram
    from .test_skill import SCRIPTS
else:
    from git_fixture import git_fixture
    from test_approvals import provision_receiver
    from test_notify import FakeTelegram
    from test_skill import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
import common
import approvals
import notify
import publish_git

GITHUB = 'https://github.com/example/fixture.git'
OTHER = 'https://git.example.test/example/fixture.git'
SSH = 'git@git.example.test:example/fixture.git'
TARGET = 'refs/heads/main'


class PublishGitTests(unittest.TestCase):
    def setUp(self):
        self.root, self.env = self.enterContext(git_fixture())
        self.repo, self.remote = self.root / 'source', self.root / 'destination.git'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('init', '--bare', '-q', str(self.remote))
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        self.old = self.commit('Initial')
        self.git('push', '-q', str(self.remote), 'HEAD:' + TARGET)
        self.sha = self.commit('Update')
        self.git('remote', 'add', 'origin', GITHUB)
        self.destination = {'url': GITHUB, 'repository_id': 'R_fixture', 'branch': TARGET}
        self.receiver = provision_receiver(self.root)
        self.enterContext(patch.object(approvals, 'receiver_state', return_value=self.receiver))
        self.observed = ('R_fixture', 'PRIVATE')
        self.identity_reads = []
        self.writes = []
        self.github_transport = SimpleNamespace(push_by_repository_id=partial(self.receive, require_private=False),
                                               push_private_by_repository_id=partial(self.receive, require_private=True))
        git = publish_git.isolated_git

        def offline(repo, *args, protocol='none', **kwargs):
            if args[0] == 'push':
                position = args.index('--') + 1
                self.assertIn(args[position], (GITHUB, OTHER, SSH, str(self.remote)))
                self.writes.append((args[position], args[position + 1:]))
                args = (*args[:position], str(self.remote), *args[position + 1:])
                protocol = 'file'
            return git(repo, *args, protocol=protocol, **kwargs)

        transport = patch.object(publish_git, 'isolated_git', side_effect=offline)
        transport.start()
        self.addCleanup(transport.stop)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, title):
        (self.repo / 'content').write_text(title)
        self.git('add', '.')
        self.git('commit', '-qm', title)
        return self.git('rev-parse', 'HEAD')

    def identity(self, slug):
        self.identity_reads.append(slug)
        return self.observed

    def receive(self, transport, args, *, protocol, lock_fd, repository_id, require_private):
        actual_id, visibility = self.observed
        if actual_id != repository_id:
            raise publish_git.PublishError('provider refused a different repository ID')
        if require_private and visibility != 'PRIVATE':
            raise publish_git.PublishError('provider refused non-private visibility')
        publish_git.isolated_git(transport, *args, protocol=protocol, lock_fd=lock_fd)

    def authorize(self, updates, *, action='target', consume=True):
        payload = b'Fixture release notes.'
        details = {'action_set': {publish_git.REF_ACTIONS: [
            [operation, ref, old or 'absent', new or 'absent'] for operation, ref, old, new in updates]}}
        if action == 'release':
            details |= {'action_set_sha256': hashlib.sha256(approvals.canonical(details['action_set'])).hexdigest(),
                        'notes_sha256': hashlib.sha256(payload).hexdigest()}
        decision_id = approvals.create(action, 'fixture', self.destination, self.sha, **details)
        decision = approvals.get(decision_id)
        text = approvals.request_text(decision)
        notes = None
        if action == 'release':
            telegram = FakeTelegram()
            with patch.object(notify, 'review_urlopen', telegram), patch.object(notify, 'append_line'):
                notes = notify.send_review_payload(telegram.env, 'fixture', 'notes.md', payload,
                                                   proposal=(decision_id, decision['nonce']))
            approvals.update_delivery(decision_id, notes_delivery=notes)
        chat = {'id': 321, 'type': 'private'}
        original = {'chat': chat, 'from': {'id': 123, 'is_bot': True}, 'message_id': 100, 'text': text}
        delivery = {'chat_id': 321, 'bot_id': 123, 'message_id': 100,
                    'sha256': hashlib.sha256(text.encode()).hexdigest()}
        approvals.bind_message(decision_id, delivery, notes_delivery=notes)
        approvals.issue_receipt({'chat': chat, 'from': {'id': 321, 'is_bot': False}, 'message_id': 101,
                                 'text': f'approve {decision_id} {self.sha}', 'reply_to_message': original}, bot_id=123)
        if consume:
            return approvals.claim(decision_id, self.sha, action=action, project='fixture', destination=self.destination)
        return approvals.get(decision_id)

    def operations(self, action, updates):
        return [(publish_git._ref_operation(action, *update), *update) for update in updates]

    def dispatch(self, decision_id, execution_id, updates, *, action='target', before_write=lambda: None):
        publish_git.verified_push(self.repo, updates, action=action, decision_id=decision_id,
                                  execution_id=execution_id, identity=self.identity, before_write=before_write,
                                  github_transport=self.github_transport, allow_file=True)

    def push(self, *updates, action='target', approved=None, before_write=lambda: None):
        authorized = self.operations(action, updates if approved is None else approved)
        record = self.authorize(authorized, action=action)
        self.dispatch(record['decision_id'], record['claimed'], self.operations(action, updates),
                      action=action, before_write=before_write)

    def refs(self):
        return self.git('--git-dir=' + str(self.remote), 'for-each-ref', '--format=%(refname) %(objectname)')

    def assert_refused(self, message, *updates, **kwargs):
        refs, writes = self.refs(), list(self.writes)
        with self.assertRaisesRegex(publish_git.PublishError, message):
            self.push(*updates, **kwargs)
        self.assertEqual(self.writes, writes)
        self.assertEqual(self.refs(), refs)

    def test_transport_pins_helpers_and_drops_ambient_git_and_loader_settings(self):
        from subprocess import CompletedProcess

        for protocol in ('ssh', 'https'):
            selected = []

            def trusted(name, directories=None):
                selected.append(name)
                return Path('/verified/bin') / name

            with (patch.dict(os.environ, {'PATH': '/shadow', 'LD_PRELOAD': '/shadow.so',
                                          'GIT_EXEC_PATH': '/shadow', 'GIT_CONFIG_COUNT': '1'}, clear=True),
                  patch.object(publish_git, 'trusted_system_executable', side_effect=trusted),
                  patch.object(publish_git.subprocess, 'run', return_value=CompletedProcess([], 0, b'')) as run):
                publish_git._git(self.repo, 'ls-remote', protocol=protocol)
            argv, env = run.call_args.args[0], run.call_args.kwargs['env']
            self.assertEqual(env['PATH'], '/usr/bin:/bin')
            self.assertNotIn('LD_PRELOAD', env)
            self.assertNotIn('GIT_CONFIG_COUNT', env)
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertEqual(env['GIT_EXEC_PATH'], '/usr/lib/git-core')
            if protocol == 'ssh':
                self.assertEqual(selected, ['sh', 'ssh'])
                self.assertIn('core.sshCommand=/verified/bin/ssh -F /dev/null -o BatchMode=yes', argv)
            else:
                self.assertEqual(selected, ['sh', 'git-remote-https', 'gh'])
                self.assertIn('credential.https://github.com.helper=!/verified/bin/gh auth git-credential', argv)

    def test_effective_rewrite_is_pinned_as_literal(self):
        self.git('remote', 'set-url', 'origin', 'alias:fixture.git')
        self.git('config', 'url.https://github.com/example/.pushInsteadOf', 'alias:')
        # Git would apply this rule to a second resolution in the source repo.
        self.git('config', f'url.{OTHER}.insteadOf', GITHUB)
        self.assertEqual(publish_git.resolve_push_url(self.repo), GITHUB)
        self.push((TARGET, self.old, self.sha))
        self.assertEqual(self.writes, [(GITHUB, (f'{self.sha}:{TARGET}',))])
        self.assertEqual(self.identity_reads, ['example/fixture'])

    def test_selected_git_is_reused_for_setup_config_object_reads_and_transport(self):
        selected = self.root / 'selected-git'
        selected.symlink_to(common.trusted_git_executable())
        token = common.SYSTEM_GIT.set(selected)
        try:
            with patch.object(common, 'trusted_system_executable',
                              side_effect=AssertionError('unexpected Git lookup')):
                self.push((TARGET, self.old, self.sha))
        finally:
            common.SYSTEM_GIT.reset(token)
        self.assertIn(f'{TARGET} {self.sha}', self.refs())
        self.assertEqual(len(self.writes), 1)

    def test_explicit_pushurl_uses_insteadof_and_multiple_urls_are_refused(self):
        self.git('config', 'remote.origin.pushurl', 'alias:fixture.git')
        self.git('config', 'url.https://github.com/example/.insteadOf', 'alias:')
        self.assertEqual(publish_git.resolve_push_url(self.repo), GITHUB)
        self.git('config', '--add', 'remote.origin.pushurl', OTHER)
        self.assert_refused('exactly one unambiguous', (TARGET, self.old, self.sha))
        self.assertEqual(self.identity_reads, [])

    def test_unknown_and_unsupported_endpoints_are_refused(self):
        self.git('remote', 'remove', 'origin')
        self.assert_refused('could not resolve', (TARGET, self.old, self.sha))
        self.git('remote', 'add', 'origin', 'ext::fixture')
        self.destination['url'] = 'ext::fixture'
        self.assert_refused('unsupported Git endpoint', (TARGET, self.old, self.sha))
        self.assertEqual(self.identity_reads, [])

    def test_identity_is_fresh_on_each_push_and_delete(self):
        task = 'refs/heads/korolev/25-fixture'
        self.push((task, '', self.sha), action='backup')
        self.observed = ('R_replacement', 'PRIVATE')
        self.assert_refused('repository identity changed', (task, self.sha, ''), action='backup')
        self.assert_refused('repository identity changed', (TARGET, self.old, self.sha))
        self.observed = ('R_fixture', 'PUBLIC')
        self.assert_refused('not allowed', (task, self.sha, ''), action='backup')
        self.assert_refused('not allowed', ('refs/heads/korolev/26-other', '', self.sha), action='backup')
        self.assertEqual(self.identity_reads, ['example/fixture'] * 5)

    def test_missing_github_transport_capability_leaves_writes_pending(self):
        del self.github_transport.push_private_by_repository_id
        task = 'refs/heads/korolev/25-fixture'
        self.assert_refused('lacks required repository-ID or visibility authority', (task, '', self.sha), action='backup')
        self.github_transport = None
        self.assert_refused('lacks required repository-ID or visibility authority', (TARGET, self.old, self.sha))

    def test_provider_fences_changes_after_last_local_sample(self):
        task = 'refs/heads/korolev/25-fixture'
        for update, action, changed, refusal in (
                ((TARGET, self.old, self.sha), 'target', ('R_replacement', 'PRIVATE'), 'different repository ID'),
                ((task, '', self.sha), 'backup', ('R_fixture', 'PUBLIC'), 'non-private visibility')):
            self.observed = ('R_fixture', 'PRIVATE')
            self.assert_refused(refusal, update, action=action, before_write=lambda: setattr(self, 'observed', changed))
        self.assertEqual(self.identity_reads, ['example/fixture'] * 2)

    def test_unsigned_forged_wrong_attempt_and_wrong_operation_never_dispatch(self):
        updates = [('CAS update target', TARGET, self.old, self.sha)]
        approved = self.authorize(updates, consume=False)
        active = self.authorize(updates)
        wrong = self.authorize(updates)
        wrong_operation = [(publish_git.RELEASE_TARGET, TARGET, self.old, self.sha)]
        wrongly_labeled = self.authorize(wrong_operation)
        decision, attempt = active['decision_id'], active['claimed']
        for record, execution, action, requested, error in (
                (active, None, 'target', updates, 'execution ID required'),
                (approved, attempt, 'target', updates, 'approval is approved'),
                (active, wrong['claimed'], 'target', updates, 'requested execution ID'),
                (active, attempt, 'backup', updates, 'different action'),
                (active, attempt, 'target', wrong_operation, 'outside the exact approved action set'),
                (wrongly_labeled, wrongly_labeled['claimed'], 'target', wrong_operation,
                 'outside the exact approved action set'),
                (active, attempt, 'target', [('CAS update target', 'refs/heads/other', self.old, self.sha)],
                 'outside the exact approved action set'),
                (active, attempt, 'target', [('CAS update target', TARGET, None, self.sha)],
                 'outside the exact approved action set')):
            with self.subTest(error=error), self.assertRaisesRegex(publish_git.PublishError, error):
                self.dispatch(record['decision_id'], execution, requested, action=action)
        with approvals.transaction() as db:
            forged = approvals.canonical(active['receipt'] | {'mac': '0' * 64}).decode()
            db.execute('UPDATE approvals SET receipt = ? WHERE decision_id = ?', (forged, decision))
            db.execute('UPDATE requests SET receipt = ? WHERE nonce = ?', (forged, active['nonce']))
        with self.assertRaisesRegex(publish_git.PublishError, 'receipt does not match'):
            self.dispatch(decision, attempt, updates)
        self.assertEqual(self.identity_reads, [])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.refs(), f'{TARGET} {self.old}')

    def test_every_transport_rechecks_revocation_at_dispatch(self):
        target = ('CAS update target', TARGET, self.old, self.sha)
        backup = ('CAS create backup ref', 'refs/heads/korolev/25-fixture', '', self.sha)
        for url, action, update in ((GITHUB, 'target', target), (GITHUB, 'backup', backup),
                                    (OTHER, 'target', target), (SSH, 'target', target),
                                    (str(self.remote), 'target', target)):
            with self.subTest(url=url, action=action):
                self.git('remote', 'set-url', 'origin', url)
                self.destination['url'] = url
                record = self.authorize([update], action=action)
                decision, attempt = record['decision_id'], record['claimed']
                before_write = lambda: approvals.mark(decision, 'stale')
                with self.assertRaisesRegex(publish_git.PublishError, 'nonce is revoked'):
                    self.dispatch(decision, attempt, [update], action=action, before_write=before_write)
                with self.assertRaisesRegex(publish_git.PublishError, 'nonce is revoked'):
                    self.dispatch(decision, attempt, [update], action=action)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.refs(), f'{TARGET} {self.old}')

    def test_delete_resolves_the_endpoint_again(self):
        task = 'refs/heads/korolev/25-fixture'
        self.push((task, '', self.sha), action='backup')
        self.git('remote', 'set-url', 'origin', OTHER)
        self.assert_refused('effective Git push URL changed', (task, self.sha, ''), action='backup')

    def test_non_github_only_allows_supported_exact_actions(self):
        self.git('remote', 'set-url', 'origin', OTHER)
        self.destination = {'url': OTHER, 'branch': TARGET}
        target = (TARGET, self.old, self.sha)
        self.assert_refused('exact approved action set', target, approved=[(TARGET, '', self.sha)])
        self.assert_refused('not allowed', ('refs/heads/korolev/25-fixture', '', self.sha), action='backup')
        ci = f'refs/heads/korolev/release-ci/{self.sha}'
        self.assert_refused('not allowed', (ci, '', self.sha), action='release')
        self.push(target)
        self.assertEqual(self.writes, [(OTHER, (f'{self.sha}:{TARGET}',))])
        self.assertEqual(self.identity_reads, [])

    def test_public_release_allows_only_its_enumerated_refs(self):
        self.observed = ('R_fixture', 'PUBLIC')
        ci = f'refs/heads/korolev/release-ci/{self.sha}'
        head = f'refs/heads/korolev/release-pr/{self.sha}'
        self.git('tag', '-am', 'Fixture release', 'v0.0.1')
        tag = self.git('rev-parse', 'refs/tags/v0.0.1')
        approved = [(ci, '', self.sha), (ci, self.sha, ''), (head, '', self.sha),
                    (TARGET, self.old, self.sha), ('refs/tags/v0.0.1', '', tag)]
        self.assert_refused('exact approved action set', approved[0], action='release', approved=approved[1:])
        self.assert_refused('exact approved action set', approved[2], action='release', approved=approved[:2])
        for name in ('25-fixture', '00-baseline', 'archive/completed'):
            ref = 'refs/heads/korolev/' + name
            self.assert_refused('not allowed', (ref, '', self.sha), action='release')
        for updates in ([approved[0]], [approved[2]], approved[3:], [approved[1]]):
            self.push(*updates, action='release', approved=approved)
        self.assertEqual({refspec.split(':', 1)[1] for _, refspecs in self.writes for refspec in refspecs},
                         {ci, head, TARGET, 'refs/tags/v0.0.1'})
        self.assertEqual(set(self.refs().splitlines()),
                         {f'{head} {self.sha}', f'{TARGET} {self.sha}', f'refs/tags/v0.0.1 {tag}'})

    def test_pre_push_hook_and_lfs_filter_leave_publication_pending(self):
        hooks = self.root / 'custom-hooks'
        hooks.mkdir()
        marker = self.root / 'ran'
        hook = hooks / 'pre-push'
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o755)
        self.git('config', 'core.hooksPath', str(hooks))
        self.assert_refused('pending: repository has a pre-push hook', (TARGET, self.old, self.sha))
        hook.unlink()
        self.git('config', 'filter.lfs.process', f'touch "{marker}"')
        self.assert_refused('pending: repository has a Git LFS filter', (TARGET, self.old, self.sha))
        self.assertFalse(marker.exists())

    def test_removed_lfs_pointer_stays_pending_without_running_other_filters(self):
        (self.repo / 'large.bin').write_text('version https://git-lfs.github.com/spec/v1\n'
                                             'oid sha256:' + '1' * 64 + '\nsize 10\n')
        self.commit('Pointer')
        (self.repo / 'large.bin').unlink()
        (self.repo / '.gitattributes').write_text('content filter=fixture\n')
        self.sha = self.commit('Remove pointer')
        marker = self.root / 'ran'
        self.git('config', 'filter.fixture.clean', f'touch "{marker}"; cat')
        self.git('config', 'filter.fixture.smudge', f'touch "{marker}"; cat')
        self.assert_refused('pending: outgoing history contains a Git LFS pointer', (TARGET, self.old, self.sha))
        self.push((TARGET, self.old, self.git('rev-parse', self.sha + '~2')))
        self.assertFalse(marker.exists())

    def test_outgoing_cumulative_budgets_refuse_before_any_write(self):
        before_write = []
        for index in range(16):
            (self.repo / f'blob-{index}').write_bytes(str(index).encode() + b'x' * 900)
        many = self.commit('Many individually small blobs')
        for field, limit, name in (('BYTE_LIMIT', 6000, 'bytes'), ('OBJECT_LIMIT', 9, 'objects'),
                                   ('COMMIT_LIMIT', 1, 'commits')):
            with self.subTest(field=field), patch('safety_review.' + field, limit):
                with publish_git.isolated_repository(self.repo) as transport:
                    publish_git.check_outgoing(transport, [(TARGET, self.old, self.sha)])
                self.assert_refused(f'pending: Git {name} budget exhausted', (TARGET, self.old, many),
                                    before_write=lambda: before_write.append(True))
        self.assertEqual(before_write, [])
