import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture


SCRIPT = Path(__file__).resolve().parents[1] / 'skill' / 'scripts' / 'baseline.py'


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.base, self.env = self.enterContext(git_fixture())
        self.home = self.base / 'korolev'
        self.home.mkdir()
        self.output = self.home / 'products' / 'demo'
        self.env.update(KOROLEV_HOME=str(self.home), HOME=str(self.base),
                        GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
                        GIT_AUTHOR_EMAIL='fixture@example.test', GIT_COMMITTER_EMAIL='fixture@example.test')
        self.owner = None

    def git(self, *args, cwd=None, binary=False):
        result = subprocess.run(['git', '--no-optional-locks', *map(str, args)], cwd=cwd or self.owner or self.base,
                                env=self.env, check=True, capture_output=True, timeout=15)
        return result.stdout if binary else os.fsdecode(result.stdout).strip()

    def init(self, name):
        path = self.base / name
        self.git('init', '-qb', 'main', path, cwd=self.base)
        return path

    def write(self, name, content, folder=None):
        path = (folder or self.owner) / name
        path.parent.mkdir(exist_ok=True, parents=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return path

    def commit(self, message, cwd=None):
        self.git('add', '-A', cwd=cwd)
        self.git('commit', '-qm', message, cwd=cwd)
        return self.git('rev-parse', 'HEAD', cwd=cwd)

    def clone(self, remote, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git('clone', '-q', remote, path, cwd=self.base)
        return path

    def register(self):
        common = self.git('rev-parse', '--path-format=absolute', '--git-common-dir')
        (self.home / 'projects.json').write_text(json.dumps({
            'demo': {'folder': str(self.owner), 'git_common_dir': common},
        }))

    def fixture(self):
        seed = self.init('seed')
        for path in ('staged.py', 'unstaged.py', 'both.py', 'deleted.py', 'rename-old.py', 'same.py', 'later.py'):
            self.write(path, 'original\n', seed)
        self.write('.gitignore', 'logs/\nignored.local\n', seed)
        self.base_sha = self.commit('fixture base', seed)
        self.remote = self.base / 'example.git'
        self.git('clone', '-q', '--bare', seed, self.remote, cwd=self.base)
        self.owner = self.clone(self.remote, self.base / 'owner')
        self.register()

    def command(self, operation, *options, exit_code=0):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), operation, '--product', 'demo', *map(str, options)],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        return result

    def scan(self, *options):
        summary = json.loads(self.command('scan', *options).stdout)
        self.assertEqual(Path(summary['baseline_json']), self.output / 'baseline.json')
        self.assertEqual(Path(summary['baseline_md']), self.output / 'BASELINE.md')
        return summary, json.loads((self.output / 'baseline.json').read_text()), (self.output / 'BASELINE.md').read_text()

    def lookup(self, record, kind):
        return {item['name']: item for item in record[kind]}

    def load_branch(self, *branch_names):
        result = {}
        for name in branch_names:
            self.git('switch', '-qc', name, self.base_sha)
            self.write(f'{name}.py', f'work on {name}\n')
            result[name] = self.commit(f'work on {name}')
        self.git('switch', '-q', 'main')
        return result

    def test_path_classes_rename_and_read_only_proof(self):
        self.fixture()
        self.write('staged.py', 'staged edit\n')
        self.write('both.py', 'staged version\n')
        self.git('add', 'staged.py', 'both.py')
        self.write('unstaged.py', 'unstaged edit\n')
        self.write('both.py', 'working version\n')
        (self.owner / 'deleted.py').unlink()
        self.git('mv', 'rename-old.py', 'renamed.py')
        self.write('new source.py', 'untracked edit\n')
        self.write('src/line\nbreak.py', 'newline filename\n')
        self.write('.env', 'private fixture contents\n')
        self.write('db/data.sqlite3', 'test data contents\n')
        self.write('logs/runtime.log', 'private logs\n')
        self.write('node_modules/dep/index.js', 'dependency output\n')
        self.write('build/output.txt', 'build output\n')
        self.write('ignored.local', 'ignored content\n')
        before_status = self.git('status', '--porcelain', binary=True)
        before_listing = sorted(self.owner.rglob('*'))
        refs_dir = self.owner / '.git/refs'
        before_refs = {p.relative_to(refs_dir): (p.read_bytes(), p.stat().st_mtime_ns)
                       for p in refs_dir.rglob('*') if p.is_file()}
        before_index = self.owner / '.git/index'
        index_data = before_index.read_bytes(), before_index.stat().st_mtime_ns
        clone = self.clone(self.owner, self.base / 'tmp/comparison')
        self.write('same.py', 'modified in clone\n', clone)
        self.write('scratch.py', 'untracked in clone\n', clone)

        def clone_state():
            return {path.relative_to(clone): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
                    for path in clone.rglob('*')}

        before_clone = clone_state()
        before_clone_status = self.git('status', '--porcelain', cwd=clone, binary=True)
        summary, manifest, report = self.scan('--search-root', clone.parent)
        owner = manifest['owner']
        self.assertEqual(manifest['head_commit']['class'], 'in remote')
        self.assertEqual(manifest['head_commit']['evidence']['containing_refs']['refs/remotes/origin/main'],
                         self.base_sha)
        staged, unstaged, untracked = [self.lookup(owner, kind) for kind in ('staged', 'unstaged', 'untracked')]
        self.assertEqual(set(staged), {'both.py', 'staged.py', 'renamed.py'})
        self.assertEqual(set(unstaged), {'both.py', 'deleted.py', 'unstaged.py'})
        self.assertEqual(staged['renamed.py']['evidence']['source'], 'rename-old.py')
        self.assertIsNone(unstaged['deleted.py']['evidence']['value'])
        self.assertIn('new source.py', untracked)
        self.assertIn('src/line\nbreak.py', untracked)
        self.assertEqual(staged['both.py']['suggestion'], 'commit')
        self.assertEqual(staged['both.py']['class'], 'owner decides')
        self.assertNotEqual(staged['both.py']['evidence']['value'], unstaged['both.py']['evidence']['value'])
        for name in ('.env', 'db/data.sqlite3', 'node_modules/dep/index.js', 'build/output.txt'):
            self.assertEqual(untracked[name]['class'], 'never commit')
            self.assertEqual(set(untracked[name]['evidence']), {'reason'})
            self.assertIn(name, report)
        self.assertEqual(owner['counts']['ignored'], 2)
        self.assertEqual(owner['groups']['logs'], {'ignored': 1})
        self.assertEqual(owner['ignored_sensitive'], ['logs/runtime.log'])
        self.assertIn('logs/runtime.log', report)
        self.assertNotIn('ignored.local', report)
        for content in ('private fixture contents', 'test data contents', 'untracked edit', 'ignored content'):
            self.assertNotIn(content, report)
            self.assertNotIn(content, json.dumps(manifest))
        self.assertFalse(summary['needs_target'])
        self.assertEqual(summary['counts']['commit'], 0)
        self.assertEqual(summary['counts']['archive'], 0)
        pin = manifest['snapshot']
        self.assertEqual((pin['head'], pin['branch'], pin['upstream'], pin['upstream_sha']),
                         (self.base_sha, 'main', 'refs/remotes/origin/main', self.base_sha))
        self.assertEqual(pin['index']['digest'],
                         hashlib.sha256(self.git('ls-files', '-s', '-z', binary=True)).hexdigest())
        self.assertEqual(pin['paths']['both.py']['blob'], self.git('hash-object', '--no-filters', 'both.py'))
        self.assertEqual(pin['paths']['both.py']['mode'], '100644')
        self.assertFalse('live_remote_heads' in pin)
        for name in ('baseline.json', 'BASELINE.md'):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), 0o600)
        self.assertEqual(sorted(self.owner.rglob('*')), before_listing)
        self.assertEqual(self.git('status', '--porcelain', binary=True), before_status)
        self.assertEqual((before_index.read_bytes(), before_index.stat().st_mtime_ns), index_data)
        self.assertEqual({p.relative_to(refs_dir): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in refs_dir.rglob('*') if p.is_file()}, before_refs)
        inspected, = manifest['clones']
        self.assertEqual(inspected['name'], str(clone))
        self.assertEqual(inspected['evidence']['match'], 'local-path')
        self.assertEqual(inspected['evidence']['dirty_count'], 2)
        self.assertEqual(inspected['suggestion'], 'archive')
        self.assertEqual(clone_state(), before_clone)
        self.assertEqual(self.git('status', '--porcelain', cwd=clone, binary=True), before_clone_status)

    def test_branch_patch_and_overlap_classification(self):
        self.fixture()
        self.git('switch', '-qc', 'patch-only')
        self.write('match.py', 'one line\n')
        patch = self.commit('local patch')
        self.git('switch', '-qc', 'overlap', self.base_sha)
        self.write('match.py', 'one line\na different line\n')
        overlap = self.commit('overlap only')
        self.git('switch', '-qc', 'remote-side', self.base_sha)
        self.write('remote-init.py', 'remote only predecessor\n')
        self.commit('remote predecessor')
        self.write('match.py', 'one line\n')
        equal = self.commit('patch reached the remote independently')
        self.assertNotEqual(patch, equal)
        self.git('update-ref', 'refs/remotes/origin/main', equal)

        self.git('switch', '-q', 'main')
        self.write('diverged.py', 'local main change\n')
        local = self.commit('main diverged')
        self.git('switch', '-qc', 'remote-topic', self.base_sha)
        self.write('only-remote.py', 'unmerged on the remote\n')
        unmerged = self.commit('remote feature')
        self.git('update-ref', 'refs/remotes/origin/remote-topic', unmerged)
        self.git('switch', '-q', 'main')
        self.git('branch', '-D', 'remote-side', 'remote-topic')
        self.git('branch', 'already-reached', equal)

        _, manifest, _ = self.scan('--no-clone-search')
        groups = self.lookup(manifest, 'branches')
        change = groups['patch-only']['commits'][0]
        self.assertEqual(change['class'], 'superseded')
        self.assertEqual(change['evidence']['matching_commit'], equal)
        self.assertEqual(change['evidence']['matching_refs'], {'refs/remotes/origin/main': equal})
        self.assertEqual(groups['already-reached']['class'], 'in remote')
        near = groups['overlap']['commits'][0]
        self.assertEqual(near['name'], overlap)
        self.assertEqual(near['class'], 'owner decides')
        self.assertEqual(near['suggestion'], 'archive')
        self.assertEqual(near['hint_overlap'], 0.5)
        self.assertEqual((groups['main']['relation'], groups['main']['ahead'], groups['main']['behind']),
                         ('diverged', 1, 2))
        self.assertEqual(groups['main']['head'], local)
        self.assertEqual(groups['main']['commits'][0]['suggestion'], 'commit')
        self.assertEqual(manifest['defaults']['origin']['source'], 'remote HEAD')
        self.assertEqual(self.lookup(manifest, 'remote_branches')['refs/remotes/origin/remote-topic']['evidence']['sha'],
                         unmerged)

    def test_path_identity_and_exact_historical_match(self):
        self.fixture()
        self.git('switch', '-qc', 'upstream-simulation')
        self.write('same.py', 'landed\n')
        self.write('later.py', 'first landed version\n')
        reached = self.commit('land both paths')
        self.write('later.py', 'later landed version\n')
        (self.owner / 'deleted.py').unlink()
        tip = self.commit('advance one path and delete another')
        self.git('update-ref', 'refs/remotes/origin/main', tip)
        self.git('switch', '-q', 'main')
        self.write('same.py', 'landed\n')
        self.write('later.py', 'first landed version\n')
        (self.owner / 'deleted.py').unlink()
        self.write('different-path.py', 'first landed version\n')
        self.git('add', 'later.py')
        _, manifest, _ = self.scan('--no-clone-search')
        owner = manifest['owner']
        working = self.lookup(owner, 'unstaged')
        self.assertEqual(working['same.py']['class'], 'in remote')
        self.assertEqual(working['deleted.py']['class'], 'in remote')
        staged = self.lookup(owner, 'staged')['later.py']
        self.assertEqual(staged['class'], 'superseded')
        self.assertEqual(staged['evidence']['matching_commit'], reached)
        self.assertEqual(self.lookup(owner, 'untracked')['different-path.py']['class'], 'owner decides')

    def test_replay_local_branches_clone_and_kilo_worktree(self):
        self.fixture()
        branch_commits = self.load_branch('feature-one', 'feature-two', 'feature-three')
        kilos = self.base / '.kilo/worktrees/x'
        kilos.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-qb', 'tool-changes', kilos)
        self.write('kilo-staged.py', 'kilo work\n', kilos)
        self.git('add', 'kilo-staged.py', cwd=kilos)

        root = self.base / 'tmp'
        refund = self.clone(self.remote, root / 'depth-one/depth-two/refund-clone')
        self.git('switch', '-qc', 'refund-work', cwd=refund)
        self.write('refund.py', 'refund logic\n', refund)
        unique = self.commit('refund work', refund)
        self.write('dirty.py', 'private clone content\n', refund)
        self.write('same.py', 'private clone edit\n', refund)

        deep = self.clone(self.remote, root / 'one/two/three/four/too-deep')
        self.clone(self.init('different-remote'), root / 'unrelated')
        (root / 'loop').symlink_to(root, target_is_directory=True)
        _, manifest, report = self.scan('--search-root', root, '--extra', kilos)
        worktree, = manifest['worktrees']
        self.assertEqual((worktree['path'], worktree['branch'], worktree['head']), (str(kilos), 'tool-changes', self.base_sha))
        self.assertTrue(worktree['other_tool'])
        self.assertEqual(worktree['tool'], 'kilo')
        self.assertEqual(worktree['counts'], {'staged': 1, 'unstaged': 0, 'untracked': 0, 'ignored': 0})
        self.assertEqual(worktree['staged'][0]['name'], 'kilo-staged.py')
        clone, = manifest['clones']
        proof = clone['evidence']
        self.assertEqual((proof['path'], proof['head'], proof['branch'], proof['dirty_count']),
                         (str(refund), unique, 'refund-work', 2))
        self.assertEqual(proof['local_only_branch_count'], 1)
        self.assertEqual(proof['unique_commits'], [unique])
        self.assertEqual(clone['suggestion'], 'archive')
        for name, sha in branch_commits.items():
            self.assertIn(name, report)
            self.assertIn(sha, report)
        for part in ('refund-clone', unique, 'kilo-staged.py', '.kilo/worktrees/x'):
            self.assertIn(part, report)
        self.assertNotIn('private clone', report)
        _, disabled, _ = self.scan('--search-root', root, '--no-clone-search')
        self.assertEqual(disabled['clones'], [])
        _, explicit, _ = self.scan('--search-root', root, '--no-clone-search', '--extra', deep)
        self.assertEqual([record['name'] for record in explicit['clones']], [str(deep)])

    def test_clone_matches_any_fetch_url_after_normalization(self):
        self.fixture()
        clone = self.clone(self.remote, self.base / 'comparison')
        url = 'https://example.test/path/project'
        self.git('remote', 'add', 'https-path', url + '.git/?param=redacted')
        self.git('remote', 'set-url', 'origin', 'https://reader:mock@example.test/unrelated', cwd=clone)
        self.git('remote', 'set-url', '--add', 'origin',
                 'https://writer:mock@example.test/path/project/#fragment', cwd=clone)
        _, manifest, report = self.scan('--no-clone-search', '--extra', clone)
        entry, = manifest['clones']
        self.assertEqual(entry['name'], str(clone))
        self.assertEqual(entry['evidence']['match'], 'url')
        self.assertNotIn('example.test', report)
        self.assertNotIn('writer', json.dumps(manifest))

    def test_local_clone_chain_and_nested_repositories(self):
        self.fixture()
        nested = self.init('owner/evidence/x/worker')
        self.write('nested-source.py', 'independent repository\n', nested)
        nested_head = self.commit('nested repository root', nested)
        self.write('nested-edit.py', 'untracked nested work\n', nested)
        self.write('notes.py', 'owner work alongside the nested repository\n', nested.parent)
        linked = self.owner / 'linked'
        self.git('worktree', 'add', '-qb', 'linked-branch', linked)

        root = self.base / 'search'
        direct = self.clone(self.owner, root / 'project')
        from_nested = self.clone(nested, root / 'direct/worker')
        chained = self.clone(from_nested, root / 'transitive/worker')
        unrelated = self.init('same-name-source')
        self.write('unrelated.py', 'unrelated history\n', unrelated)
        self.commit('unrelated root', unrelated)
        self.clone(unrelated, root / 'unrelated/worker')
        _, manifest, report = self.scan('--search-root', root, '--extra', chained)

        clones = self.lookup(manifest, 'clones')
        self.assertEqual(set(clones), {str(direct), str(from_nested), str(chained)})
        for clone in clones.values():
            self.assertEqual(clone['evidence']['match'], 'local-path')
        self.assertEqual(clones[str(direct)]['class'], 'in remote')
        self.assertEqual(clones[str(from_nested)]['evidence']['unique_commits'], [nested_head])
        self.assertEqual(clones[str(chained)]['suggestion'], 'archive')
        entry, = manifest['nested_repositories']
        proof = entry['evidence']
        self.assertEqual((proof['path'], proof['head'], proof['branch'], proof['dirty_count']),
                         (str(nested), nested_head, 'main', 1))
        self.assertEqual(proof['unique_commit_count'], 1)
        self.assertEqual(entry['suggestion'], 'archive')
        self.assertIn('## Nested repositories inside the folder', report)
        self.assertIn(str(nested), report)
        untracked = self.lookup(manifest['owner'], 'untracked')
        self.assertIn('evidence/x/notes.py', untracked)
        self.assertFalse(any(name.startswith('evidence/x/worker') for name in untracked))
        self.assertFalse(any('nested-edit.py' in artifact for artifact in (report, json.dumps(manifest))))
        self.assertEqual(json.loads(self.command('verify').stdout), {'drifted': []})

    def test_clone_matches_root_commit_from_a_noncurrent_project_branch(self):
        self.fixture()
        self.git('switch', '-q', '--orphan', 'alternate-history')
        self.write('alternate.py', 'different project root\n')
        alternate = self.commit('alternate root')
        self.git('update-ref', 'refs/remotes/origin/alternate-history', alternate)
        root = self.base / 'search'
        root.mkdir()
        clone = root / 'comparison'
        self.git('clone', '-q', '--separate-git-dir', self.base / 'comparison-git', self.owner, clone)
        self.git('remote', 'set-url', 'origin', 'https://example.test/somewhere-else', cwd=clone)
        self.write('above-root.py', 'extra candidate history\n', clone)
        unique = self.commit('candidate descended from the alternate root', clone)
        self.git('switch', '-q', 'main')
        self.git('branch', '-D', 'alternate-history')

        _, manifest, _ = self.scan('--search-root', root)
        entry, = manifest['clones']
        self.assertTrue((clone / '.git').is_file())
        self.assertEqual(entry['name'], str(clone))
        self.assertEqual(entry['evidence']['match'], 'root-commit')
        self.assertEqual(entry['evidence']['unique_commits'], [unique])

    def test_clone_search_depth_is_configurable(self):
        self.fixture()
        root = self.base / 'search'
        depth_four = self.clone(self.owner, root / 'one/two/three/four')
        depth_five = self.clone(self.owner, root / 'one/two/three/four/five')
        for options, paths in (((), [depth_four]), (('--search-depth', 4), [depth_four]),
                               (('--search-depth', 5), [depth_four, depth_five])):
            with self.subTest(options=options):
                _, manifest, _ = self.scan('--search-root', root, *options)
                self.assertEqual([entry['name'] for entry in manifest['clones']], list(map(str, paths)))

    def test_unique_commit_cap_and_comparison_against_project_refs(self):
        self.fixture()
        self.write('shared.py', 'reachable through the later owner tip\n')
        shared = self.commit('shared predecessor absent from owner tracking refs')
        root = self.base / 'search'
        changed = self.clone(self.owner, root / 'changed')
        scratch = self.clone(self.owner, root / 'scratch')
        self.write('scratch.py', 'untracked only\n', scratch)
        self.write('owner-tip.py', 'commit absent from the candidate\n')
        self.commit('advance owner branch')
        for i in range(51):
            self.git('commit', '-q', '--allow-empty', '-m', f'clone commit {i}', cwd=changed)
        _, manifest, report = self.scan('--search-root', root)
        clones = self.lookup(manifest, 'clones')
        changed_record = clones[str(changed)]
        proof = changed_record['evidence']
        self.assertEqual(proof['unique_commit_count'], 51)
        self.assertEqual(len(proof['unique_commits']), 50)
        self.assertNotIn(self.base_sha, proof['unique_commits'])
        self.assertNotIn(shared, proof['unique_commits'])
        self.assertEqual(changed_record['suggestion'], 'archive')
        self.assertIn('… and 30 more (see baseline.json)', report)
        self.assertEqual(clones[str(scratch)]['class'], 'owner decides')
        self.assertEqual(clones[str(scratch)]['suggestion'], 'in remote')

    def test_stash_and_default_fallback(self):
        self.fixture()
        self.git('symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
        self.write('staged.py', 'stashed index\n')
        self.git('add', 'staged.py')
        self.write('unstaged.py', 'stashed working tree\n')
        self.write('scratch.py', 'stashed untracked\n')
        self.git('stash', 'push', '-qu')
        _, manifest, report = self.scan('--no-clone-search')
        stash, = manifest['stashes']
        self.assertEqual(stash['name'], 'stash@{0}')
        self.assertEqual(stash['suggestion'], 'archive')
        proof = stash['evidence']
        self.assertEqual(proof['base'], self.base_sha)
        self.assertEqual(proof['index'], self.git('rev-parse', 'stash^2'))
        self.assertEqual(proof['untracked'], self.git('rev-parse', 'stash^3'))
        self.assertTrue(proof['has_index'])
        self.assertTrue(proof['has_untracked'])
        self.assertEqual(manifest['defaults']['origin']['source'], 'main')
        self.assertIn('index, untracked', report)

    def test_special_paths_and_submodule_pointer(self):
        self.fixture()
        sub_source = self.init('sub-source')
        self.write('tracked.py', 'submodule fixture\n', sub_source)
        pointer = self.commit('submodule base', sub_source)
        checkout = self.owner / 'nested/sub'
        checkout.parent.mkdir()
        metadata = self.owner / '.git/modules/nested/sub'
        metadata.parent.mkdir(parents=True)
        self.git('clone', '-q', '--separate-git-dir', metadata, sub_source, checkout)
        self.write('.gitmodules', f'[submodule "nested/sub"]\n\tpath = nested/sub\n\turl = {sub_source}\n')
        self.git('add', '.gitmodules')
        self.git('update-index', '--add', '--cacheinfo', f'160000,{pointer},nested/sub')
        self.git('commit', '-qm', 'gitlink')
        self.write('nested/sub/untracked.py', 'submodule content must not appear')
        self.write('big.bin', b'\0' + b'x' * 5_000_000)
        self.write('tracked-lfs.txt', 'version https://git-lfs.github.com/spec/v1\noid sha256:' + '0' * 64 + '\nsize 40\n')
        self.git('add', 'tracked-lfs.txt')
        (self.owner / 'symbolic').symlink_to(self.base / 'target-with-private-name')
        _, manifest, report = self.scan('--no-clone-search')
        owner = manifest['owner']
        sub, = owner['submodules']
        self.assertEqual((sub['path'], sub['pointer'], sub['head'], sub['dirty']),
                         ('nested/sub', pointer, pointer, True))
        untracked = self.lookup(owner, 'untracked')
        self.assertEqual(untracked['big.bin']['evidence']['types'], ['binary', 'large'])
        self.assertEqual(untracked['symbolic']['evidence']['types'], ['symlink'])
        self.assertEqual(owner['staged'][0]['evidence']['types'], ['lfs pointer'])
        self.assertIn('nested/sub', self.lookup(owner, 'unstaged'))
        self.assertNotIn('nested/sub/untracked.py', report)
        self.assertNotIn('target-with-private-name', report)

    def test_status_does_not_run_clean_filter(self):
        self.fixture()
        marker = self.base / 'clean-filter-ran'
        self.write('.gitattributes', 'both.py filter=fixture\n')
        self.commit('filter attribute')
        self.git('config', 'filter.fixture.clean', f"printf invoked > '{marker}'")
        self.git('config', 'filter.fixture.required', 'true')
        self.write('both.py', 'working file changed size\n')
        _, manifest, _ = self.scan('--no-clone-search')
        self.assertFalse(marker.exists())
        self.assertIn('both.py', self.lookup(manifest['owner'], 'unstaged'))

    def test_optional_live_heads_query_uses_local_fixture(self):
        self.fixture()
        _, manifest, _ = self.scan('--ls-remote', '--no-clone-search')
        self.assertEqual(manifest['snapshot']['live_remote_heads'], {'origin': {'refs/heads/main': self.base_sha}})

    def test_detached_and_missing_upstream_still_write_outputs(self):
        self.fixture()
        self.git('switch', '-q', '--detach')
        self.write('detached.py', 'detached commit\n')
        detached = self.commit('detached work')
        summary, manifest, report = self.scan('--no-clone-search')
        self.assertTrue(summary['needs_target'])
        self.assertIn('detached', ' '.join(manifest['target_reasons']))
        self.assertEqual(manifest['head_commit']['suggestion'], 'archive')
        self.assertEqual(manifest['head_commit']['name'], detached)
        self.assertIn(detached, report)
        self.git('switch', '-qc', 'no-upstream')
        self.git('remote', 'add', 'backup', self.remote)
        summary, manifest, _ = self.scan('--no-clone-search')
        self.assertTrue(summary['needs_target'])
        self.assertIn('Several remotes', ' '.join(manifest['target_reasons']))

    def test_unborn_and_missing_remote_still_write_outputs(self):
        self.owner = self.init('unborn')
        self.register()
        self.write('first.py', 'unborn work\n')
        summary, manifest, _ = self.scan('--no-clone-search')
        self.assertTrue(summary['needs_target'])
        self.assertIn('unborn', ' '.join(manifest['target_reasons']))
        self.assertIn('No Git remote', ' '.join(manifest['target_reasons']))
        self.assertIn('first.py', self.lookup(manifest['owner'], 'untracked'))
        self.commit('first commit')
        summary, manifest, _ = self.scan('--no-clone-search')
        self.assertTrue(summary['needs_target'])
        self.assertIn('No Git remote', ' '.join(manifest['target_reasons']))
        self.assertIsNotNone(manifest['snapshot']['head'])

    def test_verify_detects_owner_and_tracking_drift(self):
        self.fixture()
        self.write('unstaged.py', 'pinned edit\n')
        self.scan('--no-clone-search')
        self.assertEqual(json.loads(self.command('verify').stdout), {'drifted': []})
        self.write('unstaged.py', 'different working content\n')
        edited = json.loads(self.command('verify', exit_code=3).stdout)['drifted']
        self.assertIn('owner path unstaged.py', edited)
        self.scan('--no-clone-search')
        self.write('new-staged.py', 'staged after pin\n')
        self.git('add', 'new-staged.py')
        staged = json.loads(self.command('verify', exit_code=3).stdout)['drifted']
        self.assertIn('owner index', staged)
        self.assertIn('owner new path new-staged.py', staged)
        self.scan('--no-clone-search')
        new = self.commit('committed after pin')
        committed = json.loads(self.command('verify', exit_code=3).stdout)['drifted']
        self.assertIn('owner head', committed)
        self.scan('--no-clone-search')
        self.git('update-ref', 'refs/remotes/origin/main', new)
        tracking = json.loads(self.command('verify', exit_code=3).stdout)['drifted']
        self.assertIn('remote-tracking ref refs/remotes/origin/main', tracking)

    def test_cap_keeps_directory_counts_and_detects_drift_beyond_detail(self):
        self.fixture()
        for i in range(2002):
            self.write(f'build/{i:04}.out', 'build output\n')
        self.write('z-staged.py', 'staged takes priority over build output\n')
        self.git('add', 'z-staged.py')
        self.write('unstaged.py', 'unstaged also takes priority\n')
        _, manifest, report = self.scan('--no-clone-search')
        owner = manifest['owner']
        self.assertEqual(owner['counts']['untracked'], 2002)
        self.assertEqual(owner['groups']['build']['never commit'], 2002)
        self.assertEqual(len(owner['untracked']), 1998)
        self.assertIn('z-staged.py', self.lookup(owner, 'staged'))
        self.assertIn('unstaged.py', self.lookup(owner, 'unstaged'))
        self.assertEqual(len(manifest['snapshot']['paths']), 2000)
        self.assertIn('Path detail capped at 2000 of 2004 paths', report)
        self.write('build/2001.out', 'modified after pin\n')
        drift = json.loads(self.command('verify', exit_code=3).stdout)['drifted']
        self.assertIn('owner working-tree paths or status', drift)

    def test_ignored_flood_keeps_untracked_detail_and_verify_stable(self):
        self.fixture()
        self.write('.gitignore', 'cache/\n.env\n')
        self.commit('ignore cache and local credentials')
        for i in range(5101):
            self.write(f'cache/entry-{i:04}.out', 'cache content\n')
        for i in range(55):
            self.write(f'cache/secrets/{i:02}.env', 'credential fixture\n')
        for i in range(100):
            self.write(f'notes/note-{i:02}.py', 'untracked source\n')
        self.write('.env', 'local credential fixture\n')
        _, manifest, report = self.scan('--no-clone-search')
        owner = manifest['owner']
        expected = {f'notes/note-{i:02}.py' for i in range(100)}
        self.assertEqual(set(self.lookup(owner, 'untracked')), expected)
        self.assertEqual(set(manifest['snapshot']['paths']), expected)
        self.assertEqual(owner['counts']['ignored'], 5157)
        self.assertEqual(owner['groups']['cache'], {'ignored': 5156})
        self.assertEqual(owner['groups']['(root)'], {'ignored': 1})
        self.assertNotIn('ignored', owner)
        self.assertEqual(owner['ignored_sensitive_count'], 56)
        self.assertEqual(len(owner['ignored_sensitive']), 50)
        self.assertIn('.env', owner['ignored_sensitive'])
        self.assertNotIn('cache/entry-', json.dumps(manifest))
        self.assertNotIn('cache/entry-', report)
        self.assertIn('- 5156 ignored', report)
        self.assertIn('` .env `', report)
        self.assertIn('notes/note-19.py', report)
        self.assertNotIn('notes/note-20.py', report)
        self.assertIn('… and 80 more (see baseline.json)', report)
        self.assertLess(len(report.encode('utf-8')), 60_000)

        self.write('.env', 'ignored contents changed\n')
        self.write('cache/new.env', 'new ignored file\n')
        (self.owner / 'cache/entry-0000.out').unlink()
        self.assertEqual(json.loads(self.command('verify').stdout), {'drifted': []})

    def test_clean_locations_show_clean(self):
        self.fixture()
        checkout = self.base / 'clean-worktree'
        self.git('worktree', 'add', '-qb', 'clean-branch', checkout)
        _, _, report = self.scan('--no-clone-search')
        self.assertEqual(report.splitlines().count('- clean'), 2)
        self.assertNotIn('- 0', report.splitlines())

    def test_secret_shaped_tracked_name_and_branch_block_publication(self):
        self.fixture()
        token_shape = 'gh' + 'p_' + 'f' * 34
        path = self.write(token_shape + '.py', 'fixture\n')
        self.commit('tracked name')
        result = self.command('scan', '--no-clone-search', exit_code=1)
        self.assertIn('privacy scan blocked', result.stderr)
        self.assertFalse(self.output.exists())
        self.assertNotIn(token_shape, result.stderr)
        path.unlink()
        self.commit('remove name')
        self.git('branch', 'branch-' + token_shape)
        result = self.command('scan', '--no-clone-search', exit_code=1)
        self.assertIn('privacy scan blocked', result.stderr)
        self.assertNotIn(token_shape, result.stderr)
        self.assertFalse(self.output.exists())

    def test_unregistered_product_is_rejected(self):
        result = self.command('scan', '--no-clone-search', exit_code=1)
        self.assertIn('product is not registered', result.stderr)
        self.assertFalse(self.output.exists())
