import json
import os
import signal
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from skill.scripts import procs
from common import git_common_dir

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())
        self.home = self.base / 'home'
        self.config = self.base / 'config'
        self.config.mkdir()
        self.env.update(KOROLEV_HOME=str(self.home), KOROLEV_CONFIG_DIR=str(self.config),
                        HOME=str(self.base))

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', '--no-optional-locks', *map(str, args)], cwd=cwd or self.base,
                                env=self.env, check=True, capture_output=True, text=True, timeout=10)
        return result.stdout

    def make_repo(self, name='source'):
        path = self.base / name
        self.git('init', '-q', '-b', 'main', path)
        (path / 'tracked').write_text('tracked\n')
        self.git('add', 'tracked', cwd=path)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                 'commit', '-qm', 'initial', cwd=path)
        return path

    def make_bare(self):
        path = self.base / 'Example.git'
        self.git('clone', '-q', '--bare', self.make_repo(), path)
        return path

    def clone(self, source, name):
        dest = self.base / name
        self.git('clone', '-q', source, dest)
        return dest

    def attach(self, source, *args, check=True):
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'onboard.py'), 'attach',
                                 str(source), *args], env=self.env, capture_output=True, text=True, timeout=10)
        if not check:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def registry(self):
        return json.loads((self.home / 'projects.json').read_text())

    def test_attach_records_git_metadata_without_writing_to_checkout(self):
        bare = self.make_bare()
        folder = self.clone(bare, 'Owner Repo')
        nested = folder / 'nested'
        nested.mkdir()
        (nested / 'untracked').write_text('owner data\n')
        before_files = sorted(folder.rglob('*'))
        before_status = self.git('status', '--porcelain', cwd=folder)
        before_index = (folder / '.git/index').stat().st_mtime_ns
        summary = self.attach(nested)
        entry = self.registry()['owner-repo']
        for key, expected in dict(name='owner-repo', folder=str(folder), git_common_dir=str(folder / '.git'),
                                  branch='main', upstream='origin/main', default_branch='main',
                                  remotes={'origin': str(bare)}).items():
            self.assertEqual(entry[key], expected, key)
        self.assertTrue(entry['attached'])
        self.assertEqual(summary['attach_status'], 'attached')
        self.assertEqual(summary['folder'], str(folder))
        product = json.loads((Path(summary['product_dir']) / 'product.json').read_text())
        self.assertEqual(product['source'], str(folder))
        self.assertEqual(product['folder'], str(folder))
        self.assertEqual(sorted(folder.rglob('*')), before_files)
        self.assertEqual(self.git('status', '--porcelain', cwd=folder), before_status)
        self.assertEqual((folder / '.git/index').stat().st_mtime_ns, before_index)

    def test_symlink_resumes_and_refreshes_metadata_under_the_registered_name(self):
        bare = self.make_bare()
        folder = self.clone(bare, 'owner')
        link = self.base / 'alias'
        link.symlink_to(folder, target_is_directory=True)
        first = self.attach(link, '--name', 'demo')
        self.assertEqual(first['folder'], str(folder))
        self.git('switch', '-qc', 'topic', cwd=folder)
        self.git('remote', 'add', 'secondary', 'file:///tmp/example.git', cwd=folder)
        resumed = self.attach(folder)
        self.assertEqual(resumed['attach_status'], 'resumed')
        self.assertEqual(resumed['attached'], first['attached'])
        self.assertEqual(resumed['branch'], 'topic')
        self.assertEqual(resumed['upstream'], '')
        self.assertIn('secondary', resumed['remotes'])
        self.assertEqual(list(self.registry()), ['demo'])
        self.assertIn('already attached', self.attach(link, '--name', 'different', check=False).stderr)

    def test_existing_product_preserves_source_and_owner_state(self):
        folder = self.make_repo('demo')
        product = self.home / 'products' / 'demo'
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'onboard.py'), 'init', str(folder)],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = product / 'product.json'
        previous = json.loads(meta.read_text())
        meta.write_text(json.dumps(dict(previous, display='My Demo')))
        (product / 'STATE.md').write_text('owner state\n')
        summary = self.attach(folder)
        self.assertEqual(summary['display'], 'My Demo')
        self.assertEqual(json.loads(meta.read_text())['source'], str(folder))
        self.assertEqual(json.loads(meta.read_text())['folder'], str(folder))
        self.assertEqual((product / 'STATE.md').read_text(), 'owner state\n')
        self.assertEqual(self.attach(folder)['attach_status'], 'resumed')

    def test_legacy_init_and_attach_updates_store_sanitized_source(self):
        folder = self.make_repo('demo')
        raw = 'https://user:fixture@example.test/demo.git?key=fixture#fragment'
        expected = 'https://example.test/demo.git'
        launcher = (
            f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import onboard; '
            'onboard.clone = lambda source, dest: dest.mkdir(parents=True) or "synthetic clone"; onboard.main()'
        )
        result = subprocess.run([sys.executable, '-I', '-c', launcher, 'init', raw],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = self.home / 'products/demo/product.json'
        self.assertEqual(json.loads(meta.read_text())['source'], expected)
        old = json.loads(meta.read_text())
        old['source'] = raw
        meta.write_text(json.dumps(old))
        legacy = self.home / 'work/demo/repo'
        legacy.rmdir()
        self.git('worktree', 'add', '--detach', legacy, cwd=folder)
        self.attach(folder, '--name', 'demo')
        self.assertEqual(json.loads(meta.read_text())['source'], expected)

    def test_explicit_product_ids_are_validated_before_source_access(self):
        for name in ('', 'Mixed', 'has.dot', 'has_underscore', '../demo'):
            for command in ('init', 'attach'):
                with self.subTest(command=command, name=name):
                    result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'onboard.py'),
                                             command, 'file:///missing/fixture.git', '--name', name],
                                            env=self.env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('invalid product name', result.stderr)
        self.assertFalse(self.home.exists())

    def test_zone_qualified_scp_source_is_sanitized_or_refused_before_clone(self):
        raw = 'fixture@[fe80::1%eth0]:demo.git?token=fixture#private'
        launcher = (
            f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import onboard\n'
            'def unexpected(*args, **kwargs): raise AssertionError("clone or fetch called")\n'
            'onboard.clone = onboard.subprocess.run = unexpected; onboard.main()'
        )
        dest = self.base / 'destination'
        for command, options in (('init', []), ('attach', ['--dest', str(dest), '--yes'])):
            for source, diagnostic in ((raw, 'zone-qualified SCP repository URLs'),
                                       (raw.replace('fe80', 'invalid'), 'ambiguous repository URL')):
                with self.subTest(command=command, diagnostic=diagnostic):
                    result = subprocess.run([sys.executable, '-I', '-c', launcher, command, source, *options],
                                            env=self.env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertNotIn('fixture', result.stderr)
                    self.assertFalse(self.home.exists())
                    self.assertFalse(dest.exists())

    def test_derived_product_id_collisions_refuse_clone_or_fetch_without_writes(self):
        origin = self.make_repo('Example.Name')
        second = self.make_repo('example_name')
        script = [sys.executable, '-I', str(SCRIPTS / 'onboard.py')]
        first = subprocess.run([*script, 'init', str(origin)], env=self.env,
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['name'], 'example-name')
        previous = {path: path.stat().st_mtime_ns for path in self.home.rglob('*')}
        no_git = dict(self.env, PATH=str(self.base))
        for command, source, options in (('init', str(second), []),
                                         ('attach', second.as_uri(), ['--yes'])):
            with self.subTest(command=command):
                result = subprocess.run([*script, command, source, *options], env=no_git,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("product ID 'example-name' is already in use", result.stderr)
        self.assertEqual({path: path.stat().st_mtime_ns for path in self.home.rglob('*')}, previous)
        registry = self.home / 'projects.json'
        meta = self.home / 'products/example-name/product.json'
        snapshots = registry.read_bytes(), meta.read_bytes()
        result = self.attach(second, check=False)
        self.assertIn('unrelated legacy state', result.stderr)
        self.assertEqual((registry.read_bytes(), meta.read_bytes()), snapshots)
        # Even matching recorded paths need matching Git history.
        (second / 'tracked').write_text('different repository history\n')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit',
                 '--amend', '-qam', 'different root', cwd=second)
        clone = self.home / 'work/example-name/repo'
        self.git('remote', 'set-url', 'origin', str(second), cwd=clone)
        meta.write_text(json.dumps(dict(json.loads(meta.read_text()), source=str(second))))
        snapshots = registry.read_bytes(), meta.read_bytes()
        self.assertIn('unrelated legacy state', self.attach(second, check=False).stderr)
        self.assertEqual((registry.read_bytes(), meta.read_bytes()), snapshots)

    def test_local_attach_checks_final_slots_before_updating_state(self):
        owner = self.make_repo('owner')
        self.attach(owner, '--name', 'demo')
        registry = self.home / 'projects.json'
        meta = self.home / 'products/demo/product.json'
        snapshots = registry.read_bytes(), meta.read_bytes()
        for slot in ('work/demo', 'tasks/demo', 'work/demo/repo'):
            with self.subTest(slot=slot):
                path = self.home / slot
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(self.base / 'missing')
                self.attach(owner, check=False)
                self.assertTrue(path.is_symlink())
                self.assertEqual((registry.read_bytes(), meta.read_bytes()), snapshots)
                path.unlink()
                if slot == 'work/demo/repo':
                    path.parent.rmdir()

    def test_attach_publishes_around_predictable_temp_symlinks(self):
        owner = self.make_repo('demo')
        initialized = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'onboard.py'), 'init', str(owner)],
                                     env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        sentinel = self.base / 'sentinel'
        sentinel.write_text('untouched')
        metadata = self.home / 'products/demo/product.json'
        registry = self.home / 'projects.json'
        temporary = [path.with_suffix('.json.tmp') for path in (metadata, registry)]
        for path in temporary:
            path.symlink_to(sentinel)
        summary = self.attach(owner)
        self.assertEqual(summary['attach_status'], 'attached')
        self.assertEqual(json.loads(metadata.read_text())['folder'], str(owner))
        self.assertEqual(self.registry()['demo']['folder'], str(owner))
        for path in temporary:
            self.assertEqual(path.readlink(), sentinel)
        self.assertEqual(sentinel.read_text(), 'untouched')

    def test_interrupted_first_attach_recovers_only_matching_attachment(self):
        owner = self.make_repo('demo')
        launcher = f'''
import os, signal, sys
sys.path.insert(0, {str(SCRIPTS)!r})
import common, onboard
publish = common.publish_metadata
def interrupt_registry(path, *args):
    if path.name == 'projects.json':
        os.kill(os.getpid(), signal.SIGTERM)
    publish(path, *args)
common.publish_metadata = interrupt_registry
onboard.main()
'''
        result = subprocess.run([sys.executable, '-I', '-c', launcher, 'attach', str(owner)],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        metadata = self.home / 'products/demo/product.json'
        registry = self.home / 'projects.json'
        receipt = metadata.read_bytes()
        self.assertFalse(registry.exists())
        unrelated = self.make_repo('other')
        self.assertIn('unrelated attachment state', self.attach(unrelated, '--name', 'demo', check=False).stderr)
        common = owner / '.git'
        preserved = owner / '.git-original'
        common.rename(preserved)
        (unrelated / '.git').rename(common)
        self.assertIn('unrelated attachment state', self.attach(owner, check=False).stderr)
        common.rename(unrelated / '.git')
        preserved.rename(common)
        metadata.write_text(json.dumps(dict(json.loads(receipt), name='other')))
        self.assertIn('unrelated attachment state', self.attach(owner, check=False).stderr)
        self.assertFalse(registry.exists())
        metadata.write_bytes(receipt)
        self.assertEqual(self.attach(owner)['attach_status'], 'attached')
        self.assertEqual(metadata.read_bytes(), receipt)
        self.assertEqual(self.registry()['demo']['git_common_dir'], str(common))
        self.assertEqual(self.attach(owner)['attach_status'], 'resumed')

    def test_local_attach_refuses_unverified_metadata_and_file_slots(self):
        self.attach(self.make_repo('other'))
        owner = self.make_repo('demo')
        product = self.home / 'products/demo'
        product.mkdir()
        meta = product / 'product.json'
        meta.write_text('{"display": "Existing state"}')
        registry = self.home / 'projects.json'
        before = registry.read_bytes(), meta.read_bytes()
        self.assertIn('no verified legacy repository', self.attach(owner, check=False).stderr)
        slot = self.home / 'tasks/demo'
        slot.parent.mkdir()
        slot.write_text('file data')
        self.assertIn('invalid state slot', self.attach(owner, check=False).stderr)
        self.assertEqual((registry.read_bytes(), meta.read_bytes()), before)
        self.assertEqual(slot.read_text(), 'file data')

    def test_url_attach_rejects_registry_and_dangling_destination_collisions(self):
        bare = self.make_bare()
        dest = self.base / 'new-owner'
        self.home.mkdir()
        registry = self.home / 'projects.json'
        for collision in ('projects.json', 'products/example', 'work/example', 'tasks/example'):
            with self.subTest(collision=collision):
                path = self.home / collision
                path.parent.mkdir(exist_ok=True)
                if path == registry:
                    registry.write_text('{"example": {}}')
                else:
                    path.symlink_to(self.base / 'missing')
                result = self.attach(bare.as_uri(), '--yes', '--dest', str(dest), check=False)
                self.assertIn("product ID 'example' is already in use", result.stderr)
                self.assertFalse(dest.exists())
                self.assertFalse((self.home / 'projects.json.lock').exists())
                path.unlink()

    def test_rejects_linked_worktree_with_same_common_dir(self):
        folder = self.make_repo()
        linked = self.base / 'linked'
        self.git('worktree', 'add', '-qb', 'linked', linked, cwd=folder)
        self.attach(folder)
        result = self.attach(linked, check=False)
        self.assertIn('same Git common directory', result.stderr)
        self.assertEqual(list(self.registry()), ['source'])

    def test_same_remote_warns_and_strips_credentials(self):
        bare = self.make_bare()
        first = self.clone(bare, 'first')
        second = self.clone(bare, 'second')
        for username, folder in (('one', first), ('two', second)):
            self.git('remote', 'set-url', 'origin',
                     f'https://{username}:fakepassword@example.test/repo.git?token=faketoken#private',
                     cwd=folder)
        self.attach(first)
        summary = self.attach(second)
        self.assertEqual(summary['remotes'], {'origin': 'https://example.test/repo.git'})
        self.assertIn('WARN', summary['warnings'][0])
        self.assertIn('first', summary['warnings'][0])
        self.assertEqual(self.registry()['second']['warnings'], summary['warnings'])
        self.assertEqual(len(self.registry()), 2)
        self.assertNotIn('fake', (self.home / 'projects.json').read_text())

    def test_url_clone_requires_yes_and_an_empty_destination(self):
        bare = self.make_bare()
        url = bare.as_uri()
        root = self.base / 'configured-projects'
        (self.config / 'config.json').write_text(json.dumps({'projects_root': str(root)}))
        result = self.attach(url, check=False)
        self.assertIn(str(root / 'Example'), result.stderr)
        self.assertIn('--yes', result.stderr)
        self.assertFalse(root.exists())
        self.assertFalse((self.home / 'projects.json').exists())

        dest = self.base / 'explicit destination'
        dest.mkdir()
        occupied = dest / 'occupied'
        occupied.write_text('keep\n')
        result = self.attach(url, '--dest', str(dest), '--yes', check=False)
        self.assertIn('not empty', result.stderr)
        self.assertEqual(list(dest.iterdir()), [occupied])

        occupied.unlink()
        summary = self.attach(url, '--dest', str(dest), '--yes')
        self.assertTrue((dest / '.git').is_dir())
        self.assertEqual(self.registry()['example']['folder'], str(dest))
        product = json.loads((Path(summary['product_dir']) / 'product.json').read_text())
        self.assertEqual(product['source'], url)

    def test_detached_head_has_no_branch_or_upstream(self):
        folder = self.clone(self.make_bare(), 'owner')
        self.git('switch', '--detach', '-q', cwd=folder)
        self.attach(folder)
        entry = self.registry()['owner']
        self.assertEqual(entry['branch'], '')
        self.assertEqual(entry['upstream'], '')
        self.assertEqual(entry['default_branch'], 'main')

    def test_concurrent_attaches_preserve_both_registry_entries(self):
        folders = [self.make_repo('first'), self.make_repo('second')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.attach, folders))
        self.assertEqual({result['name'] for result in results}, {'first', 'second'})
        self.assertEqual(set(self.registry()), {'first', 'second'})

    def test_linked_worktree_cannot_acquire_the_owners_repository_lock(self):
        folder = self.make_repo()
        linked = self.base / 'linked'
        self.git('worktree', 'add', '-qb', 'linked', linked, cwd=folder)
        self.enterContext(patch.object(procs, 'HOME', self.home))
        with patch.dict(os.environ, GIT_CONFIG_COUNT='malformed', GIT_CONFIG_PARAMETERS='malformed'):
            with patch.dict(os.environ, self.env, clear=True):
                folder_common = git_common_dir(folder)
                linked_common = git_common_dir(linked)
            self.assertEqual(os.environ['GIT_CONFIG_COUNT'], 'malformed')
            self.assertEqual(os.environ['GIT_CONFIG_PARAMETERS'], 'malformed')
        with procs.repo_lock(folder_common, session='controller-1', label='attach'):
            with self.assertRaises(procs.LockBusyError) as caught:
                with procs.repo_lock(linked_common):
                    self.fail('linked worktree acquired a held repository lock')
            self.assertEqual(caught.exception.holder, {
                'pid': os.getpid(), 'start_time': procs._start_time(os.getpid()),
                'session': 'controller-1', 'label': 'attach',
            })
        with procs.repo_lock(linked_common):
            self.assertTrue(procs.repo_status(folder_common)['busy'])

    def child_in(self, folder):
        child = self.enterContext(subprocess.Popen(
            [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
            cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True))
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline(), 'ready\n')
        return child.pid

    def test_other_controllers_lists_worktrees_processes_and_hints(self):
        folder = self.make_repo()
        linked = folder / '.kilo/worktrees/x'
        linked.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-qb', 'kilo-work', linked, cwd=folder)
        (linked / 'untracked').write_text('dirty\n')
        (folder / 'AGENTS.md').write_text('ordinary instruction\n' + '\n'.join([
            '  Use supervision for dispatch  ', ' Controller ' + 'a' * 200,
            'hold the controller_lock', 'before dispatch', 'supervision hint', 'sixth lock',
        ]))
        pids = {self.child_in(folder), self.child_in(linked)}
        status = self.git('status', '--porcelain', cwd=linked)
        index = self.git('rev-parse', '--path-format=absolute', '--git-path', 'index', cwd=linked).strip()
        before_index = Path(index).stat().st_mtime_ns
        found = self.attach(folder)['other_controllers']
        self.assertEqual(found['worktrees'], [{
            'path': str(linked), 'head': self.git('rev-parse', 'HEAD', cwd=linked).strip(),
            'branch': 'kilo-work', 'dirty_count': 1, 'other_tool': True,
        }])
        children = [record for record in found['processes'] if record['pid'] in pids]
        self.assertEqual({record['pid'] for record in children}, pids)
        for record in children:
            self.assertTrue(record['command'])
            self.assertGreater(record['start_time'], 0)
        self.assertEqual(len(found['supervision_hints']), 5)
        self.assertEqual(found['supervision_hints'][0], 'Use supervision for dispatch')
        self.assertEqual(len(found['supervision_hints'][1]), 160)
        self.assertEqual(found['supervision_hints'][2], 'hold the controller_lock')
        self.assertEqual(Path(index).stat().st_mtime_ns, before_index)
        self.assertEqual(self.git('status', '--porcelain', cwd=linked), status)

    def python(self, code):
        return subprocess.run([sys.executable, '-I', '-c',
                               f'import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n{code}'],
                              env=self.env, capture_output=True, text=True, check=True, timeout=10).stdout

    def test_project_folder_falls_back_to_legacy_clone(self):
        legacy = self.home / 'work' / 'legacy' / 'repo'
        legacy.mkdir(parents=True)
        folder = self.make_repo()
        self.attach(folder)
        output = self.python("from common import project_folder\n"
                             "print(project_folder('legacy'))\nprint(project_folder('source'))")
        self.assertEqual(output.splitlines(), [str(legacy), str(folder)])

    def test_doctor_warns_about_missing_folder_changed_common_dir_and_shared_remote(self):
        bare = self.make_bare()
        first = self.clone(bare, 'first')
        second = self.clone(bare, 'second')
        self.attach(first)
        self.attach(second)
        first.rename(self.base / 'moved')
        self.git('init', '-q', '--separate-git-dir', self.base / 'different-git-dir', cwd=second)
        before = self.registry()
        output = self.python('from doctor import check_registry\ncheck_registry()')
        self.assertIn('WARN project first: attached folder is missing', output)
        self.assertIn('WARN project second: Git common directory changed', output)
        self.assertIn('WARN projects first and second: share remote', output)
        self.assertEqual(self.registry(), before)
