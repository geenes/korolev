import ast
import json
import os
import subprocess
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .git_fixture import git_fixture
else:
    from git_fixture import git_fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skill/scripts'
sys.path.insert(0, str(SCRIPTS))
from safety_review import (  # noqa: E402
    MANIFEST, TRUSTED_PATHS, CoverageFacts, ReviewError, _manifest_paths, _matches,
    covering_facts, review_range,
)

DOCTOR = 'skill/scripts/doctor.py'
DEPENDENCY = 'skill/scripts/worker_exec.py'


def manifest(paths=TRUSTED_PATHS):
    return json.dumps({'sensitive_paths': {'version': 1, 'paths': sorted(paths)}}).encode()


class ManifestTests(unittest.TestCase):
    def test_repository_manifest_covers_installed_code_workflows_and_tests(self):
        paths = _manifest_paths((ROOT / MANIFEST).read_bytes())
        for rule in TRUSTED_PATHS:
            with self.subTest(rule=rule):
                self.assertTrue(_matches(rule, paths))
        # Include the installed modules and other shipped files of the trust
        # surface independent of the checker inventory.
        for folder in ('skill', '.github/workflows', 'tests'):
            for path in (ROOT / folder).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:
                    relative = path.relative_to(ROOT).as_posix()
                    self.assertTrue(_matches(relative, paths), relative)
                    self.assertTrue(_matches(relative, TRUSTED_PATHS), relative)
        for path in ('README.md', 'docs/notes.md', 'skill/scripts/future_unlisted.py'):
            self.assertFalse(_matches(path, paths), path)

    def test_repository_local_imports_and_executed_dependencies_are_covered(self):
        paths = _manifest_paths((ROOT / MANIFEST).read_bytes())
        dependencies = set()
        for script in SCRIPTS.glob('*.py'):
            for node in ast.walk(ast.parse(script.read_bytes())):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    stem = module.replace('.', '/')
                    local = {folder / filename for folder in (script.parent, ROOT)
                             for filename in (f'{stem}.py', f'{stem}/__init__.py')}
                    found = {path for path in local if path.is_file()}
                    self.assertTrue(found or module.split('.')[0] in sys.stdlib_module_names, module)
                    dependencies.update(path.relative_to(ROOT).as_posix() for path in found)
        # Audited local execution edges outside the import graph.
        executions = {
            '.github/workflows/ci.yml': ('tests/run_without_bwrap.py',),
            'install.sh': (DOCTOR, 'skill/systemd/korolev-resume.service', 'skill/systemd/korolev-resume.timer'),
            'skill/systemd/korolev-resume.service': ('skill/scripts/resume_watch.py',),
            'skill/scripts/ci_wait.py': ('skill/scripts/testcount.py',),
            'skill/scripts/opinions.py': ('skill/scripts/run_codex.py',),
            'skill/scripts/release.py': ('skill/scripts/ci_wait.py', 'skill/scripts/notify.py'),
            'skill/scripts/sandbox.py': (DEPENDENCY,),
        }
        for entry, targets in executions.items():
            source = (ROOT / entry).read_text()
            for target in targets:
                self.assertIn(Path(target).name, source)
                self.assertTrue((ROOT / target).is_file(), target)
                dependencies.add(target)
        resume = (SCRIPTS / 'resume_watch.py').read_text()
        for name in ('inbox', 'procs'):
            self.assertIn(f'python("{name}")', resume)
            dependencies.add(f'skill/scripts/{name}.py')
        self.assertIn('skill/scripts/common.py', dependencies)
        for path in sorted(dependencies):
            self.assertTrue(_matches(path, TRUSTED_PATHS), path)
            self.assertTrue(_matches(path, paths), path)


class ReviewRangeTests(unittest.TestCase):
    def setUp(self):
        base, self.env = self.enterContext(git_fixture())
        self.repo = base / 'repo'
        self.repo.mkdir()
        self.env.update(PATH=os.defpath, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test')
        self.git('init', '--quiet', '--template=', '--object-format=sha1')
        self.files = {MANIFEST: manifest(), DOCTOR: b'# base doctor\n', DEPENDENCY: b'# base worker\n'}
        self.base = self.commit('base', self.files)

    def git(self, *args, data=None):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env, input=data,
                              capture_output=True, check=True).stdout

    def commit(self, message, files, *parents, modes=None):
        self.git('read-tree', '--empty')
        records = []
        for path, contents in files.items():
            oid = self.git('hash-object', '-w', '--stdin', data=contents).strip()
            mode = (modes or {}).get(path, '100644').encode()
            records.append(mode + b' ' + oid + b'\t' + os.fsencode(path) + b'\0')
        self.git('update-index', '-z', '--index-info', data=b''.join(records))
        tree = self.git('write-tree').decode().strip()
        return self.git('commit-tree', tree, *(arg for parent in parents for arg in ('-p', parent)),
                        '-m', message).decode().strip()

    def test_candidate_must_keep_mandatory_path_and_rule(self):
        for path in (DOCTOR, DEPENDENCY):
            with self.subTest(path=path):
                files = self.files | {MANIFEST: manifest(set(TRUSTED_PATHS) - {path})}
                removal = self.commit('remove sensitive rule', files, self.base)
                with self.assertRaisesRegex(ReviewError, 'coverage is missing.*' + path):
                    review_range(self.repo, self.base, removal)
        deletion = self.commit('delete doctor', {p: b for p, b in self.files.items() if p != DOCTOR}, self.base)
        with self.assertRaisesRegex(ReviewError, 'missing mandatory.*doctor.py'):
            review_range(self.repo, self.base, deletion)

    def test_new_manifest_path_applies_to_earlier_commits(self):
        path = 'checks/helper.py'
        files = self.files | {path: b'helper'}
        earlier = self.commit('new helper', files, self.base)
        tip = self.commit('declare helper', files | {MANIFEST: manifest((*TRUSTED_PATHS, path))}, earlier)
        review = review_range(self.repo, self.base, tip)
        self.assertIn(path, review.effective_paths)
        self.assertEqual(review.touching_commits, (earlier, tip))

    def test_rename_out_and_deletion(self):
        source = 'skill/templates/fixture.md'
        files = self.files | {source: b'fixture'}
        base = self.commit('add rename fixture', files, self.base)
        files['docs/renamed\nfile.py'] = files.pop(source)
        rename = self.commit('rename file out of coverage', files, base)
        del files[DEPENDENCY]
        deletion = self.commit('delete worker', files, rename)
        self.assertEqual(review_range(self.repo, base, deletion).touching_commits, (rename, deletion))

    def test_merge_compares_every_parent_and_walks_side_history(self):
        main_files = self.files | {DOCTOR: b'# changed doctor'}
        main = self.commit('main safety edit', main_files, self.base)
        side = self.commit('side safety edit', self.files | {DEPENDENCY: b'# changed worker'}, self.base)
        merge = self.commit('retain the main tree', main_files, main, side)
        review = review_range(self.repo, self.base, merge)
        self.assertCountEqual(review.touching_commits, (main, side, merge))
        self.assertEqual(review.touching_commits[-1], merge)

    def test_revert_and_docs_only_tip_preserve_earlier_touches(self):
        first = self.commit('safety edit', self.files | {DOCTOR: b'# changed'}, self.base)
        revert = self.commit('revert', self.files, first)
        docs = self.commit('docs', self.files | {'README.md': b'docs'}, revert)
        self.assertEqual(review_range(self.repo, self.base, docs).touching_commits, (first, revert))
        self.assertEqual(review_range(self.repo, revert, docs).touching_commits, ())

    def test_covering_sha_requires_each_ancestry_and_diff_fact(self):
        before = self.commit('docs before safety change', self.files | {'README.md': b'old'}, self.base)
        touch = self.commit('safety edit', self.files | {DOCTOR: b'# changed'}, before)
        reverted = self.commit('revert', self.files, touch)
        later = self.commit('review point', self.files | {'README.md': b'review point'}, reverted)
        candidate = self.commit('docs-only tip', self.files | {'README.md': b'tip'}, later)
        sibling = self.commit('outside candidate ancestry', self.files, reverted)
        review = review_range(self.repo, self.base, candidate)
        for sha, expected in (
            (before, CoverageFacts(False, True, True)),
            (sibling, CoverageFacts(True, False, True)),
            (touch, CoverageFacts(True, True, False)),
            (later, CoverageFacts(True, True, True)),
            (candidate, CoverageFacts(True, True, True)),
        ):
            with self.subTest(sha=sha):
                self.assertEqual(covering_facts(self.repo, review, touch, sha), expected)
        with self.assertRaisesRegex(ReviewError, 'outside the touching range'):
            covering_facts(self.repo, review, before, candidate)

    def test_bootstrap_reports_full_range_and_pinned_manifest_digests(self):
        files = {DOCTOR: b'# old doctor'}
        base = self.commit('pre-manifest release', files)
        early = self.commit('early safety edit', {DOCTOR: b'# new doctor'}, base)
        candidate = self.commit('manifest introduced', self.files, early)
        with self.assertRaisesRegex(ReviewError, 'missing sensitive_paths'):
            review_range(self.repo, base, candidate)
        bootstrap = self.files[MANIFEST] + b'\n'
        review = review_range(self.repo, base, candidate, bootstrap_manifest=bootstrap)
        self.assertEqual((review.base, review.candidate), (base, candidate))
        self.assertEqual(review.base_manifest_digest, sha256(bootstrap).hexdigest())
        self.assertEqual(review.candidate_manifest_digest, sha256(self.files[MANIFEST]).hexdigest())
        self.assertEqual(review.touching_commits, (early, candidate))
        with self.assertRaisesRegex(ReviewError, 'pre-manifest base'):
            review_range(self.repo, self.base, self.base, bootstrap_manifest=bootstrap)

    def test_trusted_manifest_must_cover_audited_and_additional_dependencies(self):
        files = self.files | {MANIFEST: manifest(set(TRUSTED_PATHS) - {DEPENDENCY})}
        incomplete = self.commit('trusted base missing coverage', files)
        with self.assertRaisesRegex(ReviewError, 'worker_exec.py'):
            review_range(self.repo, incomplete, self.base)
        with self.assertRaisesRegex(ReviewError, 'helpers/runner.py'):
            review_range(self.repo, self.base, self.base, trusted_dependencies=('helpers/runner.py',))
        with self.assertRaisesRegex(ReviewError, 'unverifiable trusted dependency'):
            review_range(self.repo, self.base, self.base, trusted_dependencies=('tests/../runner.py',))
        review = review_range(self.repo, self.base, self.base, trusted_dependencies=('tests/run_without_bwrap.py',))
        self.assertEqual(review.trusted_dependencies, ('tests/run_without_bwrap.py',))

    def test_unusable_candidate_manifest_stops_review(self):
        cases = (
            ({DOCTOR: b'# changed'}, None),
            (self.files, {MANIFEST: '120000'}),
            (self.files | {MANIFEST: b'{}'}, None),
            (self.files | {MANIFEST: b'{"sensitive_paths":{"version":1,"version":1,"paths":[]}}'}, None),
            (self.files | {MANIFEST: manifest((*TRUSTED_PATHS, ':(exclude)tests'))}, None),
            (self.files | {MANIFEST: manifest(set(TRUSTED_PATHS) - {MANIFEST})}, None),
        )
        for files, modes in cases:
            with self.subTest(files=files.keys(), modes=modes):
                candidate = self.commit('bad manifest', files, self.base, modes=modes)
                with self.assertRaises(ReviewError):
                    review_range(self.repo, self.base, candidate)

    def test_pinned_objects_ignore_worktree_replacements_and_ambient_git(self):
        checker = 'skill/scripts/safety_review.py'
        candidate_code = b'raise AssertionError("candidate checker executed")\n'
        touch = self.commit('change safety files', self.files | {DOCTOR: b'# changed', checker: candidate_code},
                            self.base)
        clean = self.commit('clean substitute', self.files, self.base)
        self.git('update-ref', f'refs/replace/{touch}', clean)
        self.git('update-ref', 'refs/heads/pinned', clean)
        (self.repo / MANIFEST).write_bytes(b'')
        local_checker = self.repo / checker
        local_checker.parent.mkdir(parents=True)
        local_checker.write_bytes(candidate_code)
        with patch.dict(os.environ, {'GIT_DIR': '/nonexistent', 'GIT_NO_REPLACE_OBJECTS': '0'}):
            reviews = {(checker, sha256(candidate_code).hexdigest()): ()}
            self.assertEqual(review_range(self.repo, self.base, touch, trusted_execution=reviews).touching_commits, (touch,))
        for revision in ('pinned', touch[:12]):
            with self.assertRaisesRegex(ReviewError, 'full commit object ID'):
                review_range(self.repo, self.base, revision)

    def test_unrelated_root_and_shallow_history(self):
        unrelated = self.commit('independent root', self.files)
        self.assertEqual(review_range(self.repo, self.base, unrelated).touching_commits, (unrelated,))
        (self.repo / '.git/shallow').write_text(f'{unrelated}\n')
        with self.assertRaisesRegex(ReviewError, 'complete Git history'):
            review_range(self.repo, self.base, unrelated)

    def test_transient_standard_library_shadow_stays_pending(self):
        source = 'skill/scripts/approvals.py'
        base_files = self.files | {source: b'import sqlite3\n'}
        base = self.commit('import sqlite3', base_files, self.base)
        for shadow in ('sqlite3.py', 'skill/scripts/sqlite3/__init__.py'):
            with self.subTest(shadow=shadow):
                touch = self.commit('shadow sqlite3', base_files | {shadow: b'raise AssertionError()'}, base)
                revert = self.commit('revert shadow', base_files, touch)
                for tip in (touch, revert):
                    with self.assertRaisesRegex(ReviewError, 'pending: standard-library import shadow'):
                        review_range(self.repo, base, tip)

    def test_declared_import_and_reverted_execution_dependency(self):
        helper = 'helper.py'
        contents = self.files | {DOCTOR: b'import helper\n', helper: b'VALUE = 1\n'}
        new = self.commit('add undeclared helper', contents, self.base)
        with self.assertRaisesRegex(ReviewError, 'coverage is missing.*helper.py'):
            review_range(self.repo, self.base, new)
        declared = self.commit('declare helper', contents | {MANIFEST: manifest((*TRUSTED_PATHS, helper))}, new)
        self.assertEqual(review_range(self.repo, self.base, declared).touching_commits, (new, declared))
        changed = self.commit('change helper', contents | {helper: b'VALUE = 2\n'}, new)
        reverted = self.commit('revert helper and its caller', self.files, changed)
        self.assertEqual(review_range(self.repo, self.base, reverted).touching_commits, (new, changed, reverted))
        self.assertIn(helper, review_range(self.repo, self.base, reverted).effective_paths)

    def test_opaque_execution_requires_reviewed_source_and_exact_targets(self):
        source, helper = 'install.sh', 'helpers/runner.py'
        body = b'#!/bin/sh\npython3 helpers/runner.py\n'
        files = self.files | {source: body, MANIFEST: manifest((*TRUSTED_PATHS, helper))}
        missing = self.commit('unresolved execution', files, self.base)
        with self.assertRaisesRegex(ReviewError, 'unreviewed execution source.*install.sh'):
            review_range(self.repo, self.base, missing)
        reviews = {(source, sha256(body).hexdigest()): (helper,)}
        with self.assertRaisesRegex(ReviewError, 'unresolved execution.*helpers/runner.py'):
            review_range(self.repo, self.base, missing, trusted_execution=reviews)
        fixed = self.commit('supply helper', files | {helper: b'VALUE = 1\n'}, self.base)
        self.assertEqual(review_range(self.repo, self.base, fixed, trusted_execution=reviews).touching_commits, (fixed,))
        code = b'__import__("unknown_fixture")\n'
        tip = self.commit('dynamic import', self.files | {DOCTOR: code}, self.base)
        with self.assertRaisesRegex(ReviewError, 'unresolved execution edge.*doctor.py'):
            review_range(self.repo, self.base, tip)

    def test_cumulative_read_budgets(self):
        files = self.files | {DOCTOR: b'# ' + b'x' * 3500}
        parent = self.base
        for count in range(1, 5):
            parent = self.commit(f'touch {count}', files | {'README.md': str(count).encode()}, parent)
            if count == 1:
                first = parent
        for field, value, name in (('BYTE_LIMIT', 12000, 'bytes'), ('OBJECT_LIMIT', 36, 'objects'),
                                   ('COMMIT_LIMIT', 2, 'commits')):
            with self.subTest(field=field), patch('safety_review.' + field, value):
                self.assertEqual(review_range(self.repo, self.base, first).touching_commits, (first,))
                with self.assertRaisesRegex(ReviewError, f'pending: Git {name} budget exhausted'):
                    review_range(self.repo, self.base, parent)
