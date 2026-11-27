"""Check every newly reachable commit, path, blob, tree and annotated tag."""
import gzip
import hashlib
import json
import subprocess
import sys

if __package__:
    from .test_skill import SCRIPTS, Sandbox
else:
    from test_skill import SCRIPTS, Sandbox


class PrivacyScanTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        (self.repo / 'sample').write_text('baseline\n')
        self.commit('initial')
        self.old = self.git('rev-parse', 'HEAD')
        self.fixture_secret = 'sk-' + 'temporaryFixture' * 2

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, title):
        self.git('add', '--all')
        self.git('commit', '-qm', title)

    def scan(self, new='HEAD', old=None):
        return json.loads(self.py(f"""
import json
from privacy_scan import scan_new_objects
print(json.dumps(scan_new_objects({str(self.repo)!r}, {self.old if old is None else old!r}, {new!r})))
"""))

    def test_transient_text_blob_is_detected_and_samples_are_redacted(self):
        path = self.repo / 'temporary'
        path.write_text('header\n' + self.fixture_secret)
        self.commit('add fixture')
        added = self.git('rev-parse', 'HEAD')
        path.unlink()
        self.commit('remove fixture')
        commits, findings = self.scan()
        self.assertEqual(len(commits), 2)
        self.assertTrue(any(where.startswith(f'commit {added[:12]} path ') and kind == 'openai key'
                            for where, kind, _ in findings))
        self.assertNotIn(self.fixture_secret, json.dumps(findings))

    def test_tag_only_change_is_scanned(self):
        self.git('tag', '-a', 'v0.3.1', '-m', 'tag message ' + self.fixture_secret)
        commits, findings = self.scan('refs/tags/v0.3.1')
        self.assertEqual(commits, [])
        self.assertTrue(any(where.startswith('tag ') and kind == 'openai key' for where, kind, _ in findings))

    def test_cli_range_includes_only_annotated_tags_pointing_into_the_range(self):
        self.git('tag', '-a', 'published', '-m', self.fixture_secret)
        (self.repo / 'sample').write_text('clean change\n')
        self.commit('Clean commit')
        self.git('tag', '-a', 'outgoing', '-m', self.fixture_secret)
        tag = self.git('rev-parse', 'outgoing')[:12]
        result = self.run_script('privacy_scan.py', '--repo', str(self.repo), '--range', f'{self.old}..HEAD',
                                 '--json', check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['commits'], 1)
        self.assertEqual([(hit['where'], hit['kind']) for hit in report['findings']], [(f'tag {tag}', 'openai key')])
        self.assertNotIn(self.fixture_secret, result.stdout)
        self.assertEqual(self.scan(), [[self.git('rev-parse', 'HEAD')], []])

    def test_cli_range_checks_annotations_on_outgoing_blobs_and_trees_only(self):
        self.git('tag', '-a', 'old-blob', f'{self.old}:sample', '-m', self.fixture_secret)
        self.git('tag', '-a', 'old-tree', f'{self.old}^{{tree}}', '-m', self.fixture_secret)
        foreign = self.repo / 'unrelated'
        foreign.write_text('unrelated content\n')
        self.git('tag', '-a', 'unrelated', self.git('hash-object', '-w', 'unrelated'), '-m', self.fixture_secret)
        foreign.unlink()
        (self.repo / 'sample').write_text('clean change\n')
        self.commit('Clean commit')
        for name, target in (('out-blob', 'HEAD:sample'), ('out-tree', 'HEAD^{tree}')):
            self.git('tag', '-a', name, target, '-m', self.fixture_secret)
        expected = {f'tag {self.git("rev-parse", name)[:12]}' for name in ('out-blob', 'out-tree')}
        result = self.run_script('privacy_scan.py', '--repo', str(self.repo), '--range', f'{self.old}..HEAD',
                                 '--json', check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['commits'], 1)
        self.assertCountEqual([(row['where'], row['kind']) for row in report['findings']],
                              [(where, 'openai key') for where in expected])
        self.assertNotIn(self.fixture_secret, result.stdout)
        self.assertEqual(self.scan(), [[self.git('rev-parse', 'HEAD')], []])

    def test_object_scan_ignores_ambient_git_for_every_subprocess(self):
        (self.repo / 'transient').write_text(self.fixture_secret)
        self.commit('Add fixture')
        fake_git = self.base / 'git'
        marker = self.base / 'ambient-git-ran'
        fake_git.write_text('#!/bin/sh\n: > "$GIT_FIXTURE_MARKER"\nexit 95\n')
        fake_git.chmod(0o700)
        self.env.update(PATH=str(self.base), GIT_FIXTURE_MARKER=str(marker))
        self.env['GIT_DIR'] = str(self.base / 'wrong.git')
        result = self.run_script('privacy_scan.py', '--repo', str(self.repo), '--range', f'{self.old}..HEAD',
                                 '--json', check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['commits'], 1)
        self.assertEqual([row['kind'] for row in report['findings']], ['openai key'])
        self.assertFalse(marker.exists())

    def test_paths_and_commit_messages_even_when_blob_already_public(self):
        path = self.repo / 'home' / 'fixtureuser'
        path.mkdir(parents=True)
        (path / 'copied').write_bytes((self.repo / 'sample').read_bytes())
        self.commit('message ' + self.fixture_secret)
        _, findings = self.scan()
        self.assertTrue(any(' path sha256:' in where and kind == 'home path' for where, kind, _ in findings))
        self.assertTrue(any(where.startswith('commit ') and kind == 'openai key' for where, kind, _ in findings))
        self.assertNotIn('fixtureuser', json.dumps(findings))

    def test_empty_destination_includes_root_history(self):
        self.git('tag', '-a', 'v0.3.1', '-m', 'tag message ' + self.fixture_secret)
        commits, findings = self.scan('refs/tags/v0.3.1', old='')
        self.assertEqual(commits, [self.old])
        self.assertTrue(any(kind == 'openai key' for _, kind, _ in findings))
        commits, findings = self.scan()
        self.assertEqual((commits, findings), ([], []))

    def test_added_then_deleted_utf16_and_utf32_secrets(self):
        encodings = ('utf-16', 'utf-16-be', 'utf-32', 'utf-32-le')
        for encoding in encodings:
            (self.repo / encoding).write_bytes(f'fixture={self.fixture_secret}\n'.encode(encoding))
        self.commit('Add encoded fixtures')
        for encoding in encodings:
            (self.repo / encoding).unlink()
        self.commit('Remove encoded fixtures')
        _, findings = self.scan(old='')
        self.assertEqual(sum(kind == 'openai key' for _, kind, _ in findings), len(encodings))
        self.assertNotIn(self.fixture_secret, json.dumps(findings))

    def test_refuses_cumulative_budget(self):
        for index in range(2):
            (self.repo / f'sample-{index}').write_bytes(bytes([index]) * 2000)
        self.commit('Add test blobs')
        result = self.py(f"""
import privacy_scan
privacy_scan.TOTAL_LIMIT = 4000
try:
    privacy_scan.scan_new_objects({str(self.repo)!r}, {self.old!r}, 'HEAD')
except privacy_scan.ScanBudgetError as error:
    print(error)
""")
        self.assertIn('500 MiB total budget', result)

    def test_refuses_opaque_content(self):
        fixtures = {
            'bad-utf8': b'complete text before an invalid byte\n' + b'\xff',
            'bad-utf16': 'looks valid\n'.encode('utf-16') + b'\x10',
            'unknown-binary': b'opaque\x00\x01',
            'archive': gzip.compress(self.fixture_secret.encode()),
            'armor': b'-----' + b'BEGIN PGP MESSAGE-----\r\n\r\nYWJj\r\n-----END PGP MESSAGE-----\r\n',
            'multipart': b'-----' + b'BEGIN PGP MESSAGE, PART 1/2-----\nYWJj\n',
            'patch': b'diff --git a/demo b/demo\nGIT ' + b'binary patch\nliteral 3\nKcmYbMO#vqW0000f\n',
        }
        for name, payload in fixtures.items():
            (self.repo / name).write_bytes(payload)
        self.commit('Add opaque fixtures')
        commit = self.git('rev-parse', 'HEAD')[:12]
        _, findings = self.scan()
        self.assertEqual(len(findings), len(fixtures))
        for name in fixtures:
            path_hash = hashlib.sha256(name.encode()).hexdigest()[:12]
            self.assertTrue(any(where.startswith(f'commit {commit} path sha256:{path_hash} blob ')
                                and kind == 'opaque content' for where, kind, _ in findings), findings)
        self.assertNotIn(self.fixture_secret, json.dumps(findings))

    def test_text_cli_scans_exact_utf8_with_shared_rules(self):
        config = self.base / 'config'
        config.mkdir()
        (config / 'privacy.json').write_text(json.dumps({'deny': ['unpublished fixture']}))
        for payload, expected in ((b'  Clean UTF-8\r\n', []),
                                  (b'unpublished ' + b'fixture\n', ['private term']),
                                  (self.fixture_secret.encode(), ['openai key']),
                                  (b'-----' + b'BEGIN AGE ENCRYPTED FILE-----\nYWJj\n', ['opaque content']),
                                  (b'-----' + b'BEGIN PGP MESSAGE, PART 2-----\nYWJj\n', ['opaque content']),
                                  (b'-----' + b'BEGIN PGP MESSAGE, PART 2/3-----\nYWJj\n', ['opaque content']),
                                  (b'valid\n\xff', ['opaque content'])):
            with self.subTest(expected=expected):
                result = subprocess.run([sys.executable, str(SCRIPTS / 'privacy_scan.py'), '--text', '--json'],
                                        input=payload, env=self.env, capture_output=True)
                self.assertEqual(result.returncode, int(bool(expected)))
                self.assertEqual([f['kind'] for f in json.loads(result.stdout)['findings']], expected)
                self.assertNotIn(self.fixture_secret.encode(), result.stdout)
