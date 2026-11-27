"""Exercise the release commands against a bare remote and offline transports."""
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .test_notify import FakeTelegram
    from .test_skill import SCRIPTS, Sandbox
else:
    from test_notify import FakeTelegram
    from test_skill import SCRIPTS, Sandbox

UPSTREAM = 'https://github.com/geenes/korolev.git'
OLD_TAG = 'v0.3.7'
TAG = 'v0.3.8'
BRANCH = 'refs/heads/main'
TAG_REF = 'refs/tags/' + TAG
RELEASE_URL = 'https://github.com/geenes/korolev/releases/tag/' + TAG
PROPOSAL_DATE = 1700000000


def fake_gh(args):
    state = Path.home() / 'github'
    hosted = state / 'release.json'
    with (state / 'calls').open('a') as stream:
        stream.write(json.dumps(args) + '\n')
    if args == ['api', '--hostname', 'github.com', 'user', '--jq', '.login']:
        print('geenes')
    elif args == ['api', '--hostname', 'github.com', 'repos/geenes/korolev']:
        print(json.dumps({'full_name': 'geenes/korolev', 'node_id': 'R_fixture', 'visibility': 'public'}))
    elif args[:4] == ['api', '--hostname', 'github.com', 'repos/geenes/korolev/releases?per_page=100']:
        if '--slurp' in args:
            print('unknown flag: --slurp', file=sys.stderr)
            return 1
        assert args[4:] == ['--paginate']
        first_page = state / 'first-page.json'
        if first_page.exists():
            print(first_page.read_text(), end='')
        print(json.dumps([json.loads(hosted.read_text())] if hosted.exists() else []))
    elif args[0] == 'api' and args[1].startswith('repos/geenes/korolev/actions/runs?head_sha='):
        sha = args[1].split('=')[1]
        (state / 'ci-sha').write_text(sha)
        print(json.dumps({'workflow_runs': [{'id': 42, 'head_sha': sha, 'head_branch': 'main'}]}))
    elif args[:3] == ['run', 'watch', '42']:
        return int((state / 'fail-ci').exists())
    elif args[:3] == ['run', 'view', '42']:
        conclusion = 'failure' if (state / 'fail-ci').exists() else 'success'
        print(json.dumps({'headSha': (state / 'ci-sha').read_text(), 'status': 'completed',
                          'conclusion': conclusion, 'jobs': [{'conclusion': conclusion}]}))
    elif args[:2] == ['release', 'create']:
        assert args == ['release', 'create', TAG, '--title', 'Korolev ' + TAG, '--notes-file', '-',
                        '--verify-tag', '--repo', 'github.com/geenes/korolev']
        remote = Path.home() / 'remote.git'
        sha = subprocess.check_output(['git', f'--git-dir={remote}', 'rev-parse', TAG_REF + '^{commit}'], text=True)
        assert sha.strip() == (state / 'ci-sha').read_text()
        with hosted.open('x') as stream:
            json.dump({'tag_name': TAG, 'name': 'Korolev ' + TAG, 'draft': False,
                       'body': sys.stdin.buffer.read().decode(), 'html_url': RELEASE_URL}, stream)
        if (state / 'lose-create-response').exists():
            return 1
        print(RELEASE_URL)
    else:
        raise AssertionError(f'unexpected gh command {args}')
    return 0


def fixture_command(command):
    import common
    import inbox
    import notify
    import publish_git
    import release

    root = Path.home()
    original_select = common.trusted_system_executable

    def select(name, *args, **kwargs):
        return root / 'gh' if name == 'gh' else original_select(name, *args, **kwargs)

    original_git = release.isolated_git

    def git(repo, *args, protocol='none', **kwargs):
        if protocol != 'none':
            assert args[0] in ('ls-remote', 'push') and UPSTREAM in args and protocol == 'https'
            args = tuple(str(root / 'remote.git') if arg == UPSTREAM else arg for arg in args)
            protocol = 'file'
        if args[0] == 'push':
            with (root / 'github' / 'pushes').open('a') as stream:
                stream.write(json.dumps(args) + '\n')
        return original_git(repo, *args, protocol=protocol, **kwargs)

    date = {} if (root / 'missing-proposal-date').exists() else {'date': PROPOSAL_DATE}
    notes, request = FakeTelegram(), FakeTelegram(approval=True, message_override=date)

    def review(request_obj, *, timeout):
        if request_obj.full_url.endswith('/sendMessage') and b'Notes+SHA-256' in request_obj.data:
            response = request(request_obj, timeout=timeout)
            (root / 'proposal').write_text(request.text)
            return response
        response = notes(request_obj, timeout=timeout)
        (root / 'reviewed-notes').write_bytes(notes.payload)
        return response

    def notice(url, data, *, timeout):
        (root / 'notification').write_bytes(data)
        return io.BytesIO(b'{"ok":true,"result":{"message_id":102}}')

    def telegram(token, method, params, **kwargs):
        assert token == FakeTelegram.env['KOROLEV_BOT_TOKEN']
        if method == 'getUpdates':
            return {'result': [json.loads((root / 'update').read_text())]}
        assert method in ('getWebhookInfo', 'sendMessage')
        return {'ok': True, 'result': {}}

    original_run = subprocess.run

    def run(args, *pos, **kwargs):
        for script in ('ci_wait', 'notify'):
            local = str(SCRIPTS / (script + '.py'))
            if local in args:
                args = [sys.executable, __file__, script, *args[args.index(local) + 1:]]
                break
        return original_run(args, *pos, **kwargs)

    with (patch.object(release, 'trusted_system_executable', select),
          patch.object(common, 'trusted_system_executable', select),
          patch.object(publish_git, 'trusted_system_executable', select),
          patch.object(release, 'isolated_git', git),
          patch.object(notify, 'review_urlopen', review),
          patch.object(notify.urllib.request, 'urlopen', notice),
          patch.object(inbox, 'call', telegram),
          patch.object(subprocess, 'run', run)):
        if command == 'ci_wait':
            import ci_wait
            return ci_wait.main(sys.argv[2:])
        if command in ('inbox', 'notify'):
            sys.argv = sys.argv[1:]
            return inbox.main() if command == 'inbox' else notify.main()
        release.main(sys.argv[2:])


if __name__ == '__main__':
    if sys.argv[1] == 'gh':
        sys.exit(fake_gh(sys.argv[2:]))
    sys.exit(fixture_command(sys.argv[1]))


class ReleaseTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.env['HOME'] = str(self.base)
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK'):
            self.env.pop(name, None)
        self.repo = self.base / 'source'
        self.repo.mkdir()
        self.remote = self.base / 'remote.git'
        self.host = self.base / 'github'
        self.host.mkdir()
        self.git('init', '--bare', '-q', '-b', 'main', str(self.remote))
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        (self.repo / 'VERSION').write_text('0.3.7\n')
        (self.repo / 'CHANGELOG.md').write_text('# Changelog\n\n## v0.3.7\n\nOld.\n')
        self.commit('Initial')
        self.git('tag', '-a', OLD_TAG, '-m', 'Fixture release')
        self.git('push', '-q', str(self.remote), 'main', OLD_TAG)
        self.git('remote', 'add', 'origin', UPSTREAM)
        self.old = self.git('rev-parse', 'HEAD')
        (self.repo / 'VERSION').write_text('0.3.8\n')
        tests = self.repo / 'tests'
        tests.mkdir()
        self.test_file = tests / 'test_candidate.py'
        self.test_file.write_text('from pathlib import Path\nfrom unittest import TestCase\n'
                                  'class Candidate(TestCase):\n'
                                  '    def test_changelog(self):\n'
                                  '        self.assertIn("## v0.3.8", Path("CHANGELOG.md").read_text())\n')
        self.commit('Fix fixture output')
        self.head = self.git('rev-parse', 'HEAD')
        self.notes = self.base / 'notes.md'
        self.notes.write_text('### Fixed\n\n- **A bug.**\n')
        self.pending = self.base / 'home' / 'products' / 'korolev' / 'release-pending.json'
        self.log = self.base / 'home' / 'lessons' / 'lessons.jsonl'
        self.log.parent.mkdir(parents=True)
        self.log.write_text('{"event":"add","id":"L-fixture"}\n'
                            '{"event":"status","id":"L-fixture","status":"applied"}\n')
        config = self.base / 'config'
        config.mkdir()
        (config / 'maintainer.json').write_text('{}')
        self.telegram_config = config / 'telegram.env'
        self.telegram_config.write_text(''.join(f'{key}={value}\n' for key, value in FakeTelegram.env.items()))
        self.telegram_config.chmod(0o600)
        gh = self.base / 'gh'
        gh.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(__file__)} gh "$@"\n')
        gh.chmod(0o700)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, title):
        self.git('add', '--all')
        self.git('commit', '-qm', title)

    def invoke(self, command, *args, script='release'):
        if script == 'release':
            args = (*args, '--repo', str(self.repo))
        return subprocess.run([sys.executable, __file__, script, command, *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def propose(self):
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def owner_reply(self, answer, target, *, sequence=101, date=PROPOSAL_DATE):
        message = {'message_id': sequence, 'date': date, 'chat': {'id': 321},
                   'from': {'id': 321}, 'text': answer}
        if target is not None:
            message['reply_to_message'] = {'message_id': target}
        (self.base / 'update').write_text(json.dumps({'message': message, 'update_id': sequence}))
        result = self.invoke('--last', '20', script='inbox')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('accepted 1 message(s)', result.stdout)

    def approve(self):
        proposal = self.propose()
        self.owner_reply('a', proposal['message_id'])
        result = self.invoke('approve')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'approved'), result.stderr)
        return proposal

    def record(self):
        return json.loads(self.pending.read_text())

    def remote_refs(self):
        refs = self.git(f'--git-dir={self.remote}', 'for-each-ref', '--format=%(refname) %(objectname)')
        return dict(line.split() for line in refs.splitlines())

    def assert_no_publication(self):
        self.assertEqual(self.remote_refs()[BRANCH], self.old)
        self.assertNotIn(TAG_REF, self.remote_refs())
        self.assertFalse((self.host / 'release.json').exists())
        self.assertFalse((self.host / 'pushes').exists())

    def test_prepares_exact_patch_and_asks_with_full_identifiers(self):
        proposal = self.approve()
        record = self.record()
        self.assertEqual(record['head'], self.head)
        self.assertEqual(record['status'], 'approved')
        self.assertEqual(record['sha'], proposal['sha'])
        self.assertEqual(record['notes_sha256'], hashlib.sha256(self.notes.read_bytes()).hexdigest())
        text = (self.base / 'proposal').read_text()
        for value in (proposal['sha'], proposal['tag_sha'], record['notes_sha256'], 'a to publish or b to decline'):
            self.assertIn(value, text)
        self.assertEqual((self.base / 'reviewed-notes').read_bytes(), self.notes.read_bytes())
        self.assert_no_publication()

        result = self.invoke('publish')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sha'], proposal['sha'])
        self.assertEqual(self.remote_refs()[BRANCH], proposal['sha'])
        self.assertEqual(self.remote_refs()[TAG_REF], proposal['tag_sha'])
        self.assertEqual(self.git(f'--git-dir={self.remote}', 'rev-parse', TAG_REF + '^{commit}'), proposal['sha'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(json.loads((self.host / 'release.json').read_text())['body'], self.notes.read_text())
        self.assertEqual(self.record()['status'], 'published')
        self.assertIn('Published+Korolev+', (self.base / 'notification').read_text())
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1])['status'], 'released')
        push, = (self.host / 'pushes').read_text().splitlines()
        self.assertIn('--atomic', push)
        self.assertIn(f'--force-with-lease={BRANCH}:{self.old}', push)
        self.assertIn(f'--force-with-lease={TAG_REF}:', push)

    def test_unbound_replies_approve_nothing(self):
        proposal = self.propose()
        before = self.pending.read_bytes()
        for sequence, target in enumerate((proposal['message_id'] - 1, None), 101):
            with self.subTest(target=target):
                self.owner_reply('a', target, sequence=sequence)
                result = self.invoke('approve')
                self.assertEqual(result.returncode, 2)
                self.assertIn('no owner reply', result.stdout)
                self.assertEqual(self.pending.read_bytes(), before)

    def test_unclear_reply_records_nothing(self):
        proposal = self.propose()
        before = self.pending.read_bytes()
        self.owner_reply('maybe later', proposal['message_id'])
        result = self.invoke('approve')
        self.assertEqual(result.returncode, 2)
        self.assertIn('unclear reply', result.stdout)
        self.assertEqual(self.pending.read_bytes(), before)

    def test_decision_words_inside_longer_replies_record_nothing(self):
        for sequence, answer in enumerate(('OK, hold off on publishing', 'a few questions first',
                                           'yes but change the notes', 'no, keep the proposal open'), 101):
            with self.subTest(answer=answer):
                proposal = self.propose()
                before = self.pending.read_bytes()
                self.owner_reply(answer, proposal['message_id'], sequence=sequence)
                result = self.invoke('approve')
                self.assertEqual((result.returncode, result.stdout.strip()),
                                 (2, 'unclear reply; ask the owner again'))
                self.assertEqual(self.pending.read_bytes(), before)

    def test_whole_approval_replies_record_approved(self):
        for sequence, answer in enumerate(('✅', 'Yes.', 'a', '  YES!! \n'), 101):
            with self.subTest(answer=answer):
                proposal = self.propose()
                self.owner_reply(answer, proposal['message_id'], sequence=sequence, date=PROPOSAL_DATE + 1)
                result = self.invoke('approve')
                self.assertEqual((result.returncode, result.stdout.strip()), (0, 'approved'))
                self.assertEqual(self.record()['status'], 'approved')

    def test_stale_reply_to_reused_message_id_records_nothing(self):
        self.owner_reply('a', 100, date=PROPOSAL_DATE - 1)
        proposal = self.propose()
        self.assertEqual(proposal['message_id'], 100)
        before = self.pending.read_bytes()
        result = self.invoke('approve')
        self.assertEqual((result.returncode, result.stdout.strip()), (2, 'no owner reply yet to message 100'))
        self.assertEqual(self.pending.read_bytes(), before)
        self.assertEqual(self.record()['date'], PROPOSAL_DATE)

    def test_proposal_without_telegram_date_stops_without_recording(self):
        (self.base / 'missing-proposal-date').touch()
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertEqual(result.returncode, 1)
        self.assertIn('Telegram date', result.stderr)
        self.assertFalse(self.pending.exists())

    def test_proposal_refuses_hosted_release_on_second_page(self):
        (self.host / 'first-page.json').write_text(json.dumps([{'tag_name': OLD_TAG}]))
        (self.host / 'release.json').write_text(json.dumps({'tag_name': TAG}))
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), 'release pending: a hosted Release already exists for this tag')
        self.assertFalse(self.pending.exists())

    def test_whole_rejection_reply_with_period_records_rejected(self):
        proposal = self.propose()
        self.owner_reply('no.', proposal['message_id'])
        result = self.invoke('approve')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'rejected'))
        self.assertEqual(self.record()['status'], 'rejected')

    def test_rejection_records_rejected(self):
        proposal = self.propose()
        self.owner_reply('b', proposal['message_id'])
        result = self.invoke('approve')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'rejected'), result.stderr)
        self.assertEqual(self.record()['status'], 'rejected')
        self.assertIn('not approved', self.invoke('publish').stderr)
        self.assert_no_publication()

    def test_changed_notes_refuse_publish(self):
        self.approve()
        Path(self.record()['notes_file']).write_text('Different notes.\n')
        self.assertIn('release notes changed', self.invoke('publish').stderr)
        self.assert_no_publication()

    def test_changed_commit_refuses_publish(self):
        self.approve()
        (self.repo / 'extra.txt').write_text('Change after approval.\n')
        self.commit('Change after approval')
        self.assertIn('HEAD changed', self.invoke('publish').stderr)
        self.assert_no_publication()

    def test_publish_rescans_outgoing_history_before_pushing(self):
        private = 'fixture-private-marker'
        (self.repo / 'scratch').write_text(private)
        self.commit('Add scratch')
        (self.repo / 'scratch').unlink()
        self.commit('Remove scratch')
        self.approve()
        (self.base / 'config' / 'privacy.json').write_text(json.dumps({'deny': [private]}))
        result = self.invoke('publish')
        self.assertIn('privacy scan blocked', result.stderr)
        self.assertIn('private term', result.stderr)
        self.assertNotIn(private, result.stderr)
        self.assert_no_publication()

    def test_failing_ci_leaves_no_release_and_green_retry_does_not_push_again(self):
        proposal = self.approve()
        failed = self.host / 'fail-ci'
        failed.touch()
        result = self.invoke('publish')
        self.assertIn('CI did not pass for ' + proposal['sha'], result.stderr)
        self.assertEqual(self.remote_refs()[TAG_REF], proposal['tag_sha'])
        self.assertFalse((self.host / 'release.json').exists())
        failed.unlink()
        retry = self.invoke('publish')
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(len((self.host / 'pushes').read_text().splitlines()), 1)

    def test_retry_after_lost_release_response_reuses_the_hosted_release(self):
        self.approve()
        (self.host / 'lose-create-response').touch()
        self.assertIn('GitHub CLI exited', self.invoke('publish').stderr)
        retry = self.invoke('publish')
        self.assertEqual(retry.returncode, 0, retry.stderr)
        calls = [json.loads(line) for line in (self.host / 'calls').read_text().splitlines()]
        self.assertEqual(sum(call[:2] == ['release', 'create'] for call in calls), 1)
        self.assertEqual(self.record()['status'], 'published')

    def test_retry_refuses_a_changed_remote_tag(self):
        self.approve()
        failed = self.host / 'fail-ci'
        failed.touch()
        self.assertIn('CI did not pass', self.invoke('publish').stderr)
        failed.unlink()
        self.git(f'--git-dir={self.remote}', 'update-ref', TAG_REF, f'refs/tags/{OLD_TAG}')
        self.assertIn('destination refs', self.invoke('publish').stderr)
        self.assertFalse((self.host / 'release.json').exists())
        self.assertEqual(len((self.host / 'pushes').read_text().splitlines()), 1)

    def test_expired_and_completed_decisions_are_refused(self):
        self.approve()
        record = self.record()
        self.pending.write_text(json.dumps(record | {'asked': '2000-01-01T00:00:00Z'}))
        self.assertIn('approval expired', self.invoke('publish').stderr)
        self.assert_no_publication()
        self.pending.write_text(json.dumps(record))
        result = self.invoke('publish')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not approved', self.invoke('publish').stderr)

    def test_failing_local_tests_prevent_push(self):
        self.test_file.write_text(self.test_file.read_text().replace('## v0.3.8', 'nothing like this'))
        self.commit('Change a test')
        self.approve()
        self.assertIn('offline tests failed', self.invoke('publish').stderr)
        self.assert_no_publication()

    def test_privacy_hit_in_rendered_notes_stops_all_delivery(self):
        self.notes.write_text('token = "' + 'ghp_' + 'TemporaryFixture' * 3 + '"\n')
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertIn('privacy scan blocked', result.stderr)
        self.assertFalse((self.base / 'proposal').exists())
        self.assertFalse((self.base / 'reviewed-notes').exists())

    def test_proposal_refuses_secret_in_outgoing_history(self):
        fixture = 'ghp_' + 'TemporaryFixture' * 3
        (self.repo / 'scratch').symlink_to(fixture)
        self.commit('Add scratch')
        (self.repo / 'scratch').unlink()
        self.commit('Remove scratch')
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertIn('privacy scan blocked', result.stderr)
        self.assertIn('github token', result.stderr)
        self.assertNotIn(fixture, result.stderr)
        self.assertFalse((self.base / 'proposal').exists())
        self.assertFalse(self.pending.exists())
        self.assert_no_publication()

    def test_full_notes_are_delivered_byte_for_byte_before_the_approval_summary(self):
        notes = ('  ' + 'a' * 5000 + '\r\nTail. ☀\r\n\n').encode()
        self.notes.write_bytes(notes)
        self.propose()
        self.assertEqual((self.base / 'reviewed-notes').read_bytes(), notes)
        self.assertIn(hashlib.sha256(notes).hexdigest(), (self.base / 'proposal').read_text())

    def test_invalid_private_owner_admission_sends_nothing(self):
        self.telegram_config.write_text(self.telegram_config.read_text().replace('OWNER_ID=321', 'OWNER_ID=322'))
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertIn('private owner chat', result.stderr)
        self.assertFalse((self.base / 'reviewed-notes').exists())
        self.assertFalse(self.pending.exists())

    def test_release_checkout_refuses_configured_filters_and_hooks(self):
        self.approve()
        self.git('config', 'filter.fixture.smudge', 'false')
        self.assertIn('requires a configured Git filter', self.invoke('publish').stderr)
        self.git('config', '--unset', 'filter.fixture.smudge')
        (self.repo / '.git' / 'hooks' / 'pre-push').write_text('#!/bin/sh\nexit 99\n')
        self.assertIn('pre-push hook', self.invoke('publish').stderr)
        self.assert_no_publication()

    def test_outgoing_lfs_pointer_refuses_even_after_deletion(self):
        pointer = self.repo / 'fixture.bin'
        pointer.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:' + '1' * 64 + '\nsize 10\n')
        self.commit('Add pointer')
        pointer.unlink()
        self.commit('Remove pointer')
        result = self.invoke('propose', '--notes', str(self.notes))
        self.assertIn('outgoing history contains a Git LFS pointer', result.stderr)
        self.assertFalse(self.pending.exists())
        self.assert_no_publication()

    def test_production_backend_refuses_local_and_file_destinations_without_gh(self):
        for url in (str(self.remote), self.remote.as_uri()):
            with self.subTest(url=url):
                self.git('remote', 'set-url', 'origin', url)
                result = self.run_script('release.py', 'propose', '--repo', str(self.repo), check=False)
                self.assertIn('destination must be the upstream', result.stderr)
        self.assertFalse((self.base / 'proposal').exists())
