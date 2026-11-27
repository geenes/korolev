"""Collect D13 review facts from Git objects using the trusted installed copy."""
import ast
import json
import os
import re
import subprocess
import sys
import sysconfig
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from importlib.machinery import PathFinder
from pathlib import Path

from common import trusted_git_executable


MANIFEST = 'product.json'
BYTE_LIMIT = 64 * 1024 * 1024
BLOB_LIMIT = 512 * 1024
OBJECT_LIMIT = 100_000
COMMIT_LIMIT = 4_096
SEARCH_ROOTS = ('', 'skill/scripts/', 'tests/')
STDLIB = tuple(filter(None, (sysconfig.get_path('stdlib'), sysconfig.get_config_var('DESTSHARED'))))
STDLIB_NAMES = sys.stdlib_module_names
STARTUP_MODULES = STDLIB_NAMES | {'sitecustomize', 'usercustomize'}
INSTALLED = Path(__file__).resolve().parents[2]
# Local targets of reviewed shell, service and dynamic Python entry points.
EXECUTION = {
    'install.sh': ('skill/scripts/doctor.py', 'skill/systemd/korolev-resume.timer'),
    '.github/workflows/ci.yml': ('tests/run_without_bwrap.py',),
    'skill/systemd/korolev-resume.timer': ('skill/systemd/korolev-resume.service',),
    'skill/systemd/korolev-resume.service': ('skill/scripts/resume_watch.py',),
    **{f'skill/scripts/{name}.py': tuple(f'skill/scripts/{target}.py' for target in targets.split()) for name, targets in {
        'ci_wait': 'testcount', 'lesson': 'notify', 'opinions': 'run_codex',
        'resume_watch': 'inbox procs heartbeat handoff notify run_codex testcount',
        'sandbox': 'worker_exec',
    }.items()},
    'tests/run_without_bwrap.py': ('tests/git_fixture.py',),
    'skill/scripts/release.py': ('skill/scripts/ci_wait.py', 'skill/scripts/notify.py'),
}
DECLARATIVE = (ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.Assign,
               ast.Name, ast.Store, ast.Constant, ast.Expr, ast.Pass)

OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
# Reviewed execution footprint, including indirect imports, shell/systemd
# targets, the sandbox's embedded worker and the release preflight runner.
# Re-audit this inventory when changing how a trusted entry point loads code.
TRUSTED_PATHS = (
    MANIFEST, 'install.sh', 'skill/SKILL.md', 'skill/hooks/pre-push',
    '.github/workflows/**', 'skill/scripts/adapters/**', 'skill/systemd/**',
    'skill/templates/**', 'tests/**',
    *(f'skill/scripts/{name}.py' for name in '''
        approvals baseline branch_archive branches candidate_check chat ci_wait cli common doctor
        egress_proxy gate handoff heartbeat hosted_release i18n inbox knowledge landing lesson
        live_update notify onboard opinions policy presentation privacy_scan procs publish_git
        receiver release release_ci resume_watch review_receipts run_codex safety_review sandbox
        setup_telegram task0 task0_adopt task0_unborn tasks testcount trace verify worker_exec
    '''.split()),
)
GIT_ENV = {
    'PATH': os.defpath, 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_GRAFT_FILE': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
    'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_LAZY_FETCH': '1',
}
DIFF = ('diff-tree', '-r', '--no-commit-id', '--name-only', '-z', '--no-renames',
        '--ignore-submodules=none', '--no-ext-diff', '--no-textconv', '--no-relative')


class ReviewError(ValueError):
    """The inputs or Git evidence could not establish safety coverage."""


@dataclass(frozen=True)
class ReviewRange:
    base: str
    candidate: str
    base_manifest_digest: str
    candidate_manifest_digest: str
    trusted_dependencies: tuple[str, ...]
    effective_paths: tuple[str, ...]
    touching_commits: tuple[str, ...]


@dataclass(frozen=True)
class CoverageFacts:
    descends_from_touch: bool
    in_candidate_ancestry: bool
    sensitive_paths_unchanged: bool


class GitReads:
    """One cumulative budget for a complete object inspection."""

    def __init__(self, repo):
        self.repo = repo
        self.executable = trusted_git_executable()
        self.bytes, self.objects, self.commits = BYTE_LIMIT, OBJECT_LIMIT, COMMIT_LIMIT

    def charge(self, field, count):
        remaining = getattr(self, field)
        if not 0 <= count <= remaining:
            raise ReviewError(f'pending: Git {field} budget exhausted')
        setattr(self, field, remaining - count)

    @contextmanager
    def output(self, *args, batch=False):
        command = [self.executable, '-c', 'core.commitGraph=false', '-c', 'core.hooksPath=/dev/null',
                   '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=/dev/null', *args]
        try:
            with subprocess.Popen(command, cwd=self.repo, env=GIT_ENV, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  stdin=subprocess.PIPE if batch else subprocess.DEVNULL) as process:
                try:
                    yield process
                    if batch:
                        process.stdin.close()
                    if process.wait():
                        raise ReviewError(f'pending: cannot verify Git objects ({args[0]})')
                finally:
                    if process.poll() is None:
                        process.kill()
        except OSError as error:
            raise ReviewError('pending: cannot read Git objects') from error

    def records(self, *args, delimiter=b'\n'):
        with self.output(*args) as process:
            tail = b''
            while chunk := process.stdout.read1(min(8192, self.bytes + 1)):
                self.charge('bytes', len(chunk))
                *records, tail = (tail + chunk).split(delimiter)
                if any(len(record) > 8192 for record in (*records, tail)):
                    raise ReviewError('pending: oversized Git record')
                yield from records
            if tail:
                raise ReviewError('pending: incomplete Git record')

    def batch(self, process, oid, *, limit):
        process.stdin.write(oid + b'\n')
        process.stdin.flush()
        header = process.stdout.readline(min(256, self.bytes + 1))
        self.charge('bytes', len(header))
        fields = header.split()
        if not header.endswith(b'\n') or len(fields) != 3 or fields[0] != oid or not fields[2].isdigit():
            raise ReviewError('pending: unverifiable Git object')
        _, kind, raw_size = fields
        size = int(raw_size)
        if process.args[-1] == '--batch-check':
            return kind, size
        if size > limit:
            raise ReviewError('pending: Git blob exceeds the per-object budget')
        self.charge('bytes', size + 1)
        body = process.stdout.read(size)
        if len(body) != size or process.stdout.read(1) != b'\n':
            raise ReviewError('pending: incomplete Git object')
        return kind, body


def _commit(git, sha):
    if (not isinstance(sha, str) or not OID.fullmatch(sha) or
            tuple(git.records('rev-parse', '--verify', '--end-of-options', f'{sha}^{{commit}}')) != (sha.encode(),)):
        raise ReviewError('expected a full commit object ID')


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate JSON key')
    return result


def _valid_path(path):
    return (isinstance(path, str) and PATH.fullmatch(path) is not None
            and not set(path.split('/')) & {'.', '..', '.git'})


def _manifest_paths(blob):
    """Read v1 rules: literal paths and recursive directory rules ending in /**."""
    try:
        section = json.loads(blob, object_pairs_hook=_unique_object)['sensitive_paths']
        version, paths = section['version'], section['paths']
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ReviewError('invalid or missing sensitive_paths manifest') from error
    if type(version) is not int or version != 1 or not isinstance(paths, list):
        raise ReviewError('unsupported sensitive_paths manifest schema')
    if any(not isinstance(path, str) or not _valid_path(path.removesuffix('/**')) for path in paths):
        raise ReviewError('invalid sensitive path')
    if MANIFEST not in paths or len(set(paths)) != len(paths):
        raise ReviewError('sensitive paths must be unique and include product.json')
    return set(paths)


def _matches(path, rules):
    for rule in rules:
        root = rule.removesuffix('/**')
        if path == rule or (root != rule and (path == root or path.startswith(root + '/'))):
            return True
    return False


def _require_coverage(paths, trusted_dependencies):
    for path in trusted_dependencies:
        if not _valid_path(path):
            raise ReviewError('unverifiable trusted dependency path')
    for path in (*TRUSTED_PATHS, *trusted_dependencies):
        if not _matches(path, paths):
            raise ReviewError(f'trusted path coverage is missing for {path!r}')


def _changed(git, paths, *revisions):
    changed = False
    with closing(git.records(*DIFF, *revisions, delimiter=b'\0')) as entries:
        for entry in entries:
            git.charge('objects', 1)
            changed |= _matches(os.fsdecode(entry), paths)
    return changed


def _tree(git, sha):
    tree = {}
    with closing(git.records('ls-tree', '-r', '-t', '-z', sha, delimiter=b'\0')) as entries:
        for entry in entries:
            git.charge('objects', 1)
            metadata, path = entry.split(b'\t', 1)
            mode, _, oid = metadata.split()
            tree[os.fsdecode(path)] = mode, oid
    return tree


def _local_modules(tree, module, pending):
    parts = module.split('.')
    found = False
    for root in SEARCH_ROOTS:
        stem = root + '/'.join(parts)
        for index in range(1, len(parts)):
            parent = root + '/'.join(parts[:index])
            if parent + '.py' in tree or parent in tree and tree[parent][0] != b'040000':
                raise ReviewError(f'pending: ambiguous package import {module!r} at {parent!r}')
            if parent + '/__init__.py' in tree:
                pending.append(parent + '/__init__.py')
        for path in (stem + '.py', stem + '/__init__.py'):
            if path in tree:
                pending.append(path)
                found = True
    return found


def _stdlib_module(module):
    if module == 'os.path' or module in sys.builtin_module_names:
        return True
    if module.split('.')[0] not in STDLIB_NAMES:
        return False
    search = STDLIB
    for part in module.split('.'):
        spec = PathFinder.find_spec(part, search)
        if spec is None:
            return False
        search = spec.submodule_search_locations or ()
    return True


def _imports(tree, path, nodes, pending):
    package = path.split('/')[:-1]
    for node in nodes:
        if isinstance(node, ast.Import):
            modules = [(alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > len(package):
                raise ReviewError(f'pending: unresolved relative import in {path!r}')
            prefix = package[:len(package) + 1 - node.level] if node.level else []
            modules = [('.'.join(prefix + [node.module] if node.module else prefix), node.names)]
        else:
            continue
        for module, names in modules:
            local = _local_modules(tree, module, pending)
            package_module = any(root + module.replace('.', '/') + '/__init__.py' in tree for root in SEARCH_ROOTS)
            children = ([_local_modules(tree, f'{module}.{alias.name}', pending) for alias in names]
                        if package_module or not local else [])
            if not (local or _stdlib_module(module) or children and all(children)):
                raise ReviewError(f'pending: unresolved import {module!r} in {path!r}')


def _execution(tree, read, reviews):
    opaque = ('skill/systemd/**', '.github/workflows/**', 'skill/scripts/adapters/**', 'skill/hooks/pre-push')
    pending = [path for path, (mode, _) in tree.items() if mode != b'040000' and (
        path in EXECUTION or _matches(path, opaque) or path.endswith('.py') and
        path.startswith(('skill/scripts/', 'tests/')))]
    seen = set()
    for path, (mode, _) in tree.items():
        if path in ('skill', 'skill/scripts', 'tests', '.github') and mode != b'040000':
            raise ReviewError(f'pending: unverifiable execution search path {path!r}')
        for root in SEARCH_ROOTS:
            if path.startswith(root):
                module = path[len(root):].split('/')[0].split('.')[0]
                if module in STARTUP_MODULES and (path.endswith(('.py', '.so', '.pyc')) or mode == b'120000'):
                    raise ReviewError(f'pending: standard-library import shadow at {path!r}')
        if mode != b'040000' and path.endswith(('.so', '.pyc', '.pyd')):
            raise ReviewError(f'pending: opaque module on execution search path {path!r}')
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        body = read(tree, path)
        key = path, sha256(body).hexdigest()
        reviewed = key in reviews
        if reviewed:
            pending.extend(reviews[key])
        elif (installed := INSTALLED / path).is_file():
            with installed.open('rb') as source:
                reviewed = source.read(len(body) + 1) == body
            if reviewed:
                pending.extend(EXECUTION.get(path, ()))
        if not path.endswith('.py'):
            if not reviewed:
                raise ReviewError(f'pending: unreviewed execution source {path!r}')
            continue
        try:
            nodes = list(ast.walk(ast.parse(body)))
        except (SyntaxError, ValueError, RecursionError) as error:
            raise ReviewError(f'pending: cannot inspect Python execution in {path!r}') from error
        if not reviewed and any(not isinstance(node, DECLARATIVE) for node in nodes):
            raise ReviewError(f'pending: unresolved execution edge in {path!r}')
        _imports(tree, path, nodes, pending)
    return seen


def review_range(repo, base, candidate, *, bootstrap_manifest=None, trusted_dependencies=(), trusted_execution=None):
    """Collect frozen facts. Extra execution reviews map (path, SHA-256) to audited local targets."""
    git = GitReads(repo)
    if tuple(git.records('rev-parse', '--is-shallow-repository')) != (b'false',):
        raise ReviewError('complete Git history is required')
    _commit(git, base)
    _commit(git, candidate)
    reviews = trusted_execution or {}
    dependencies = tuple(trusted_dependencies)
    touched, discovered = [], set()
    with git.output('cat-file', '--batch', batch=True) as blobs:
        def read(tree, path):
            if path not in tree or tree[path][0] not in (b'100644', b'100755') or not _valid_path(path):
                raise ReviewError(f'pending: unresolved execution or manifest path {path!r}')
            git.charge('objects', 1)
            kind, body = git.batch(blobs, tree[path][1], limit=BLOB_LIMIT)
            if kind != b'blob':
                raise ReviewError(f'pending: expected a regular Git blob for {path!r}')
            return body

        base_tree, tip_tree = _tree(git, base), _tree(git, candidate)
        base_blob = read(base_tree, MANIFEST) if MANIFEST in base_tree else None
        if bootstrap_manifest is not None:
            if base_blob is not None or not isinstance(bootstrap_manifest, bytes):
                raise ReviewError('bootstrap requires pinned bytes and a pre-manifest base')
            git.charge('bytes', len(bootstrap_manifest))
            base_blob = bootstrap_manifest
        candidate_blob = read(tip_tree, MANIFEST) if MANIFEST in tip_tree else None
        base_paths, tip_paths = _manifest_paths(base_blob), _manifest_paths(candidate_blob)
        _require_coverage(base_paths, dependencies)
        _require_coverage(tip_paths, dependencies)
        if 'skill/scripts/doctor.py' not in tip_tree:
            raise ReviewError('pending: missing mandatory skill/scripts/doctor.py')
        base_exec, tip_exec = (_execution(tree, read, reviews) for tree in (base_tree, tip_tree))
        _require_coverage(base_paths, base_exec)
        _require_coverage(tip_paths, tip_exec)
        discovered.update(base_exec | tip_exec)
        history = []
        with closing(git.records('rev-list', '--topo-order', '--reverse', '--parents',
                                 f'--max-count={git.commits + 1}', f'{base}..{candidate}')) as rows:
            for row in rows:
                git.charge('commits', 1)
                sha, *parents = row.decode('ascii').split()
                history.append((sha, parents or ['--root']))
                discovered.update(_execution(_tree(git, sha), read, reviews) if sha != candidate else tip_exec)
    paths = tuple(sorted(base_paths | tip_paths | discovered))
    for sha, parents in history:
        if any([_changed(git, paths, parent, sha) for parent in parents]):
            touched.append(sha)
    return ReviewRange(base, candidate, sha256(base_blob).hexdigest(), sha256(candidate_blob).hexdigest(),
                       tuple(sorted(set(dependencies))), paths, tuple(touched))


def covering_facts(repo, review, touch, covering_sha):
    """Facts for a review SHA. Receipt and frozen-range validation belong to the gate."""
    if touch not in review.touching_commits:
        raise ReviewError('commit is outside the touching range')
    git = GitReads(repo)
    _commit(git, covering_sha)
    def contains(tip, commit):
        found = False
        with closing(git.records('rev-list', f'--max-count={git.commits + 1}', tip)) as rows:
            for sha in rows:
                git.charge('commits', 1)
                found |= sha == commit.encode()
        return found

    return CoverageFacts(contains(covering_sha, touch), contains(review.candidate, covering_sha),
                         not _changed(git, review.effective_paths, covering_sha, review.candidate))
