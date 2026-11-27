#!/usr/bin/env python3
"""Inventory a registered Git checkout before Task 0 prepares any changes."""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import fcntl
from functools import lru_cache
from hashlib import new as digest
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import launcher_git, product_dir, read_projects, valid_product
from onboard import public_url
import privacy_scan
from sandbox import credential_name


PATH_LIMIT = 2000
IGNORED_NAME_LIMIT = 50
COMMIT_LIMIT = 50
CLONE_DEPTH = 4
CLONE_SECONDS = 60
ROOT_SECONDS = 5
LARGE = 5_000_000
CHANGED = ('staged', 'unstaged', 'untracked')
CATEGORIES = (*CHANGED, 'ignored')
CLASSES = ('in remote', 'superseded', 'commit', 'archive', 'never commit', 'owner decides')
RUNTIME_DIRS = {'node_modules', 'build', 'dist', 'out', 'target', 'coverage', 'logs', 'dump', 'dumps',
                '.next', '.nuxt', '.turbo', '__pycache__'}
DATA_NAME = re.compile(r'\.sqlite|\.db$|(?:^|[._-])dumps?(?:[._-]|$)|\.(?:dmp|log)(?:[._-]|$)'
                       r'|\.sql\.(?:gz|xz|bz2|zst)$')
LFS_POINTER = re.compile(rb'version https://git-lfs\.github\.com/spec/v1\n'
                         rb'(?:ext-[^\n]+\n)*oid sha256:[0-9a-f]{64}\nsize [0-9]+\n?')
GIT_ENV = {'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_ALLOW_PROTOCOL': '', 'GIT_CONFIG_PARAMETERS': '', 'GIT_CONFIG_COUNT': '0',
           'GIT_EXTERNAL_DIFF': '', 'GIT_TRACE': '0', 'GIT_TRACE2': '0',
           'GIT_TRACE2_EVENT': '0', 'GIT_TRACE2_PERF': '0', 'GIT_TRACE_PACKET': '0',
           'GIT_TRACE_PACK_ACCESS': '0', 'GIT_TRACE_PERFORMANCE': '0', 'GIT_TRACE_SETUP': '0',
           'GIT_TRACE_SHALLOW': '0', 'GIT_TRACE_CURL': '0', 'GIT_TRACE_FSMONITOR': '0',
           'LC_ALL': 'C'}


class ScanError(ValueError):
    pass


class PrivacyError(ScanError):
    pass


class Repo:
    def __init__(self, path, deadline=None):
        self.path = path
        self.deadline = deadline

    def git(self, command, *args, missing=(), input=None, config=(), env=None):
        timeout = None if self.deadline is None else self.deadline - time.monotonic()
        if timeout is not None and timeout <= 0:
            raise subprocess.TimeoutExpired('Git', 0)
        result = launcher_git(self.path, '--no-optional-locks', '--no-replace-objects',
                              '-c', 'core.untrackedCache=false', *config, command, *args,
                              input=input, timeout=timeout, env_overrides=GIT_ENV | (env or {}))
        if result.returncode in missing:
            return None
        if result.returncode:
            raise ScanError(f'cannot read Git metadata with {command}')
        return result.stdout

    def text(self, *args, **kwargs):
        result = self.git(*args, **kwargs)
        return None if result is None else os.fsdecode(result.removesuffix(b'\n'))

    def root(self):
        return Path(self.text('rev-parse', '--show-toplevel')).resolve()

    def common_dir(self):
        return Path(self.text('rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()

    def head(self):
        return self.text('rev-parse', '--verify', '--quiet', 'HEAD', missing=(1,))

    def branch(self):
        ref = self.text('symbolic-ref', '--quiet', 'HEAD', missing=(1,))
        return ref.removeprefix('refs/heads/') if ref else None

    def upstream(self):
        return self.text('rev-parse', '--symbolic-full-name', '@{upstream}', missing=(128,))

    def refs(self, *prefixes):
        result = self.text('for-each-ref', '--format=%(objectname)%09%(refname)%09%(symref)', *prefixes)
        return {ref: {'sha': sha, 'symbolic': target or None}
                for sha, ref, target in (line.split('\t') for line in result.split('\n') if line)}

    def ancestor(self, older, newer):
        return self.git('merge-base', '--is-ancestor', older, newer, missing=(1,)) is not None

    def remotes(self):
        result = {}
        for name in self.text('remote').splitlines():
            fetch = self.text('remote', 'get-url', '--all', '--', name, missing=(2,)) or ''
            push = self.text('remote', 'get-url', '--push', '--all', '--', name, missing=(2,)) or ''
            result[name] = dict(fetch=fetch.splitlines(), urls=list(dict.fromkeys((fetch + '\n' + push).splitlines())))
        return result

    def status(self, *, ignored=False):
        # Git status can invoke a configured clean/process filter for a modified tracked file.
        keys = self.git('config', '--null', '--name-only', '--get-regexp',
                        r'^filter\..*\.(clean|process|required)$', missing=(1,)) or b''
        config = []
        for key in nul_fields(keys):
            config.extend(('-c', key + ('=false' if key.endswith('.required') else '=')))
        return self.git('status', '--porcelain=v2', '-z', '--untracked-files=all',
                        '--ignore-submodules=dirty', '--find-renames=100%',
                        *(['--ignored=traditional'] if ignored else []), config=config)


def nul_fields(raw):
    return os.fsdecode(raw).removesuffix('\0').split('\0') if raw else []


def parse_status(raw):
    entries = iter(nul_fields(raw))
    for entry in entries:
        kind = entry[0]
        if kind in '?!':
            yield dict(path=entry[2:], xy=kind)
            continue
        if kind == 'u':
            _, xy, sub, m1, m2, m3, mw, h1, h2, h3, path = entry.split(' ', 10)
            yield dict(path=path, xy=xy, submodule=sub, work_mode=mw,
                       conflict=[(m1, h1), (m2, h2), (m3, h3)])
            continue
        fields = entry.split(' ', 8 if kind == '1' else 9)
        _, xy, sub, mh, mi, mw, hh, hi = fields[:8]
        record = dict(path=fields[-1], xy=xy, submodule=sub, work_mode=mw,
                      head_entry=object_entry(mh, hh), index_entry=object_entry(mi, hi))
        if kind == '2':
            record.update(source=next(entries), similarity=fields[8])
        yield record


def object_entry(mode, oid):
    return {'mode': mode, 'blob': oid} if mode != '000000' else None


def read_index(repo):
    raw = repo.git('ls-files', '-s', '-z')
    entries = []
    for field in nul_fields(raw):
        metadata, path = field.split('\t', 1)
        mode, oid, stage = metadata.split(' ')
        entries.append(dict(path=path, mode=mode, blob=oid, stage=int(stage)))
    return {'digest': digest('sha256', raw).hexdigest(), 'entries': entries}


def sensitive_name(path):
    parts = path.lower().split('/')
    if any(credential_name(part) for part in parts):
        return 'credential or data name'
    if DATA_NAME.search(parts[-1]):
        return 'data store, dump or log'


def protected(path):
    if reason := sensitive_name(path):
        return reason
    if RUNTIME_DIRS.intersection(path.lower().split('/')):
        return 'runtime or build output'
    return None


def file_types(sample, size):
    return [label for label, present in (
        ('large', size > LARGE), ('binary', b'\0' in sample),
        ('lfs pointer', size <= 1024 and LFS_POINTER.fullmatch(sample)),
    ) if present]


@contextmanager
def path_parent(root, path):
    # A tracked directory can have been replaced by a symlink since the status read.
    # Open each component without following links before reading any working file.
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ScanError('invalid path in Git metadata')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, parts[-1]
    finally:
        os.close(directory)


def fingerprint(repo, path, algorithm, submodules, name_only):
    if path in submodules and submodules[path]['populated']:
        submodule = submodules[path]
        return {'mode': '160000', 'blob': submodule['head'], 'dirty': submodule['dirty'],
                'populated': submodule['populated']}
    try:
        with path_parent(repo.path, path.rstrip('/')) as (directory, name):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                target = os.fsencode(os.readlink(name, dir_fd=directory))
                oid = digest(algorithm, b'blob %d\0' % len(target) + target).hexdigest()
                return {'mode': '120000', 'blob': oid, 'types': ['symlink']}
            if not stat.S_ISREG(info.st_mode):
                return {'mode': None, 'blob': None, 'types': ['non-regular']}
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(name, flags, dir_fd=directory), 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ScanError('a working file changed type during hashing')
                checksum = digest(algorithm, b'blob %d\0' % before.st_size)
                sample = b''
                while block := stream.read(128 * 1024):
                    checksum.update(block)
                    if not sample and not name_only:
                        sample = block[:8000]
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ScanError('a working file changed during hashing; rerun scan')
            result = {'mode': '100755' if before.st_mode & stat.S_IXUSR else '100644',
                      'blob': checksum.hexdigest()}
            if not name_only:
                result['types'] = file_types(sample, before.st_size)
            return result
    except (FileNotFoundError, NotADirectoryError):
        return None


def read_submodules(repo, entries, status):
    pointers = {entry['path']: entry['blob'] for entry in entries
                if entry['mode'] == '160000' and entry['stage'] == 0}
    for record in status:
        if (entry := record.get('head_entry')) and entry['mode'] == '160000':
            pointers.setdefault(record.get('source', record['path']), entry['blob'])
    result = {}
    for path, pointer in sorted(pointers.items()):
        sub = Repo(repo.path / path)
        record = dict(path=path, pointer=pointer, head=None, dirty=None, populated=False)
        result[path] = record
        # Query the immediate checkout for dirt only. Its own submodules are excluded
        # from dirty traversal, and no submodule paths or file hashes enter the scan.
        try:
            with path_parent(repo.path, path) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or sub.path.resolve() != sub.path:
                continue
            if not (sub.path / '.git').exists() or sub.root() != sub.path:
                continue
            record.update(populated=True, head=sub.head(), dirty=bool(sub.status()))
        except (OSError, ScanError):
            pass
    return result


def snapshot(repo, *, ignored=False, nested=()):
    index = read_index(repo)
    nested_entries = {path + '/' for path in nested}
    status = [record for record in parse_status(repo.status(ignored=ignored))
              if record['xy'] not in '?!' or record['path'] not in nested_entries]
    submodules = read_submodules(repo, index['entries'], status)
    for record in status:
        sub = submodules.get(record['path'])
        if sub and sub['dirty'] and record['xy'] not in '?!' and record['xy'][1] == '.':
            record['xy'] = record['xy'][0] + 'M'
    seen = {record['path'] for record in status}
    status.extend(dict(path=path, xy='.M', submodule='S...')
                  for path, sub in submodules.items() if path not in seen and sub['dirty'])
    algorithm = repo.text('rev-parse', '--show-object-format')
    paths = {path for record in status if record['xy'] != '!'
             for path in (record['path'], record.get('source')) if path}
    sensitive_destinations = {r['path'] for r in status if r.get('source') and protected(r['source'])}
    hashes = {path: fingerprint(repo, path, algorithm, submodules,
                                bool(protected(path)) or path in sensitive_destinations) for path in sorted(paths)}
    upstream = repo.upstream()
    pin = dict(head=repo.head(), branch=repo.branch(), upstream=upstream, index=index,
               upstream_sha=repo.text('rev-parse', '--verify', upstream) if upstream else None,
               object_format=algorithm)
    return pin, status, hashes, submodules


def item(kind, name, classification='owner decides', suggestion=None, evidence=None, hint=None):
    return {'kind': kind, 'name': name, 'class': classification, 'suggestion': suggestion,
            'evidence': evidence or {}, 'hint_overlap': hint}


def safe_names(names):
    findings = []
    for name in names:
        privacy_scan.check_text(name or '', 'baseline metadata', (), (), findings)
    secret_kinds = {name for name, _ in privacy_scan.SECRETS}
    if any(kind in secret_kinds for _, kind, _ in findings):
        raise PrivacyError('privacy scan blocked a secret-shaped name in baseline metadata')


class Evidence:
    def __init__(self, repo, refs):
        self.repo = repo
        self.tracking = refs
        self.remote_refs = {ref: info['sha'] for ref, info in refs.items() if not info['symbolic']}
        self.patch_table = None
        # Cache only metadata, patch fingerprints and added lines held during the scan.
        self.tree = lru_cache(maxsize=8192)(self.tree)
        self.types = lru_cache(maxsize=8192)(self.types)
        self.commit_patch = lru_cache(maxsize=512)(self.commit_patch)
        self.containing = lru_cache(maxsize=8192)(self.containing)
        self.ancestor = lru_cache(maxsize=128)(repo.ancestor)

    def tree(self, sha, path):
        if not sha:
            return None
        entry = self.repo.text('ls-tree', '-z', sha, '--', path)
        if not entry:
            return None
        metadata, found = entry.removesuffix('\0').split('\t', 1)
        mode, _, oid = metadata.split(' ')
        return object_entry(mode, oid) if found == path else None

    def types(self, mode, oid):
        if mode == '160000':
            return ['submodule']
        if mode == '120000':
            return ['symlink']
        size = int(self.repo.text('cat-file', '-s', oid))
        if size > LARGE:
            return ['large']
        raw = self.repo.git('cat-file', 'blob', oid)
        return file_types(raw[:8000], size)

    def containing(self, sha):
        if not sha or not self.remote_refs:
            return {}
        refs = self.repo.text('for-each-ref', '--contains=' + sha, '--format=%(refname)%09%(objectname)',
                              'refs/remotes').split('\n')
        return {ref: tip for ref, _, tip in (line.partition('\t') for line in refs if line)
                if self.remote_refs.get(ref) == tip}

    def commit_patch(self, sha):
        raw = self.repo.git('diff-tree', '--root', '--no-commit-id', '--no-renames',
                            '--no-abbrev', '-r', '--raw', '-z', sha)
        fields = iter(nul_fields(raw))
        changes = []
        for change, path in zip(fields, fields):
            old_mode, mode, old_oid, oid, _ = change[1:].split(' ')
            if protected(path):
                return None
            changes.append((path, ((old_mode, old_oid), (mode, oid))))
        if not changes or any(self.types(mode, oid) for _, entries in changes for mode, oid in entries
                              if mode != '000000'):
            return None
        patch = self.repo.git('diff-tree', '--root', '--no-commit-id', '--no-renames',
                              '--no-ext-diff', '--no-textconv', '--no-color', '--no-indent-heuristic',
                              '--diff-algorithm=myers', '--src-prefix=a/', '--dst-prefix=b/',
                              '--unified=3', '-r', '-p', sha)
        if b'\nBinary files ' in patch:
            return None
        stable = self.repo.git('patch-id', '--stable', input=patch).split()
        if not stable:
            return None
        added = set()
        file_header = b''
        for line in patch.split(b'\n'):
            if line.startswith(b'diff --git '):
                file_header = line
            elif line.startswith(b'+') and not line.startswith(b'+++ '):
                added.add((file_header, line[1:]))
        return stable[0].decode('ascii'), added, {path for path, _ in changes}

    def remote_patches(self, wanted):
        # Walk the pinned tracking tips, not live refs, and filter by candidate paths.
        # A stable patch-id match has the same diff paths.
        tips = sorted(set(self.remote_refs.values()))
        if not tips or not wanted:
            return {}
        paths = sorted(wanted)
        commits = {}
        for offset in range(0, len(paths), 128):
            history = self.repo.text('rev-list', '--no-merges', '--full-history', '--stdin',
                                     '--', *paths[offset:offset + 128], input=('\n'.join(tips) + '\n').encode('ascii'))
            commits.update(dict.fromkeys(history.split()))
        return {sha: patch for sha in commits if (patch := self.commit_patch(sha)) is not None}

    def prepare_commits(self, commits):
        wanted = set()
        if self.remote_refs:
            for sha in commits:
                if not self.containing(sha) and (patch := self.commit_patch(sha)) is not None:
                    wanted.update(patch[2])
        self.patch_table = self.remote_patches(wanted)

    def commit(self, sha, suggestion, *, match_patch=True):
        if refs := self.containing(sha):
            return item('commit', sha, 'in remote', evidence={'sha': sha, 'containing_refs': refs})
        proof = {'sha': sha}
        hint = None
        if match_patch and self.patch_table and (patch := self.commit_patch(sha)) is not None:
            patch_id, added, paths = patch
            proof['patch_id'] = patch_id
            for other, (other_id, other_added, other_paths) in self.patch_table.items():
                if patch_id == other_id:
                    proof.update(matching_commit=other, matching_refs=self.containing(other))
                    return item('commit', sha, 'superseded', evidence=proof,
                                hint=len(added & other_added) / len(added) if added else None)
                if added and paths.intersection(other_paths):
                    hint = max(hint or 0, len(added & other_added) / len(added))
        return item('commit', sha, suggestion=suggestion, evidence=proof, hint=hint)

    def file(self, kind, record, value, pin, working):
        path = record['path']
        source = record.get('source')
        exclude = protected(path) or (source and protected(source))
        if exclude:
            proof = {'reason': exclude}
            if source:
                proof['source'] = source
            return item(kind, path, 'never commit', evidence=proof)
        upstream = self.tracking.get(pin['upstream'], {}).get('sha')
        head = pin['head']
        conflict = record.get('conflict')
        # Only the staged side of porcelain's rename record has a source path.
        renamed = source if kind == 'staged' and record['xy'][0] == 'R' else None
        proof = dict(status=record['xy'], value=value, working=working, base=head, upstream=upstream)
        if source:
            proof.update(source=source, similarity=record['similarity'])
        if conflict:
            proof['conflict_stages'] = conflict
        types = set(working.get('types', []) if working else [])
        for entry in (record.get('head_entry'), record.get('index_entry')):
            if entry:
                types.update(self.types(entry['mode'], entry['blob']))
        if record.get('submodule', '').startswith('S'):
            types.add('submodule')
        proof['types'] = sorted(types)
        suggestion = 'commit' if upstream and not conflict else None
        if not upstream or conflict or (value and (value.get('dirty') or value['blob'] is None)):
            return item(kind, path, suggestion=suggestion, evidence=proof)

        comparable = object_entry(value['mode'], value['blob']) if value else None
        remote = self.tree(upstream, path)
        proof['upstream_entry'] = remote
        if comparable == remote and (not renamed or self.tree(upstream, renamed) is None):
            return item(kind, path, 'in remote', evidence=proof)
        # A historical match must represent a later change at this exact path,
        # rather than a coincidental blob elsewhere or an earlier version.
        if (head and head != upstream and value and self.ancestor(head, upstream)
                and comparable != self.tree(head, path)):
            history = self.repo.text('rev-list', '--ancestry-path', '--full-history',
                                     f'{head}..{upstream}', '--', path)
            for sha in history.split('\n'):
                if sha and comparable == self.tree(sha, path) and (
                        not renamed or self.tree(sha, renamed) is None):
                    proof['matching_commit'] = sha
                    return item(kind, path, 'superseded', evidence=proof)
        return item(kind, path, suggestion=suggestion, evidence=proof)


def directory_group(path):
    return path.partition('/')[0] if '/' in path else '(root)'


def path_digest(status, hashes, submodules):
    data = json.dumps([status, hashes, submodules], sort_keys=True).encode('ascii')
    return digest('sha256', data).hexdigest()


def location(repo, name, evidence, nested=()):
    pin, status, hashes, submodules = snapshot(repo, ignored=True, nested=nested)
    records = {kind: [] for kind in CHANGED}
    changes = {kind: [] for kind in CHANGED}
    groups = defaultdict(Counter)
    counts = Counter({kind: 0 for kind in CATEGORIES})
    class_counts = Counter()
    ignored_sensitive = []
    ignored_sensitive_count = 0
    changed_status = []
    for record in status:
        path, xy = record['path'], record['xy']
        if xy == '!':
            counts['ignored'] += 1
            groups[directory_group(path)]['ignored'] += 1
            class_counts['never commit'] += 1
            if sensitive_name(path):
                ignored_sensitive_count += 1
                if len(ignored_sensitive) < IGNORED_NAME_LIMIT:
                    ignored_sensitive.append(path)
            continue
        changed_status.append(record)
        if xy == '?':
            changes['untracked'].append(record)
        else:
            if xy[0] != '.':
                changes['staged'].append(record)
            if xy[1] != '.':
                changes['unstaged'].append(record)

    safe_names([pin['branch'], pin['upstream'], str(repo.path), *hashes, *submodules, *groups,
                *ignored_sensitive, *(entry['path'] for entry in pin['index']['entries'])])
    ordered_paths = dict.fromkeys(path for batch in changes.values()
                                  for record in sorted(batch, key=lambda record: record['path'])
                                  for path in (record['path'], record.get('source')) if path)
    detailed = set(list(ordered_paths)[:PATH_LIMIT])
    notes = []
    if len(ordered_paths) > PATH_LIMIT:
        notes.append(f'Path detail capped at {PATH_LIMIT} of {len(ordered_paths)} paths. '
                     'Directory totals and the snapshot digest cover the full set.')
    if any(not sub['populated'] for sub in submodules.values()):
        notes.append('Submodule dirt is unavailable for unpopulated or unreadable checkouts.')
    for kind, batch in changes.items():
        for record in batch:
            path = record['path']
            working = hashes.get(path)
            value = record.get('index_entry') if kind == 'staged' else working
            change = evidence.file(kind, record, value, pin, working)
            counts[kind] += 1
            classification = change['class']
            class_counts[classification] += 1
            group = groups[directory_group(path)]
            group[kind] += 1
            group[classification] += 1
            if change['suggestion']:
                group['suggest ' + change['suggestion']] += 1
            if path in detailed:
                records[kind].append(change)
    for path in submodules:
        groups[directory_group(path)]['submodules'] += 1
    pin.update(path_digest=path_digest(changed_status, hashes, submodules),
               paths={path: value for path, value in hashes.items() if path in detailed})
    return dict(name=name, path=str(repo.path), snapshot=pin, **records, counts=dict(counts),
                classes=dict(class_counts), groups={name: dict(group) for name, group in sorted(groups.items())},
                submodules=list(submodules.values()), ignored_sensitive=ignored_sensitive,
                ignored_sensitive_count=ignored_sensitive_count, coverage=notes)


def worktrees(repo):
    raw = repo.git('worktree', 'list', '--porcelain', '-z')
    records = []
    for block in os.fsdecode(raw).split('\0\0'):
        fields = dict(line.partition(' ')[::2] for line in block.split('\0') if line)
        if not fields:
            continue
        path = Path(fields['worktree']).resolve()
        if path != repo.path:
            tool = next((p[1:] for p in path.parts if p in ('.kilo', '.codex', '.claude')), None)
            records.append(dict(path=str(path), head=fields.get('HEAD'),
                                branch=fields.get('branch', '').removeprefix('refs/heads/') or None,
                                other_tool=tool is not None, tool=tool,
                                bare='bare' in fields, prunable='prunable' in fields))
    return records


def remote_names(refs, remotes):
    # Remote names may themselves contain a slash, so match the longest first.
    names = sorted(remotes, key=len, reverse=True)
    result = defaultdict(dict)
    for ref, info in refs.items():
        if info['symbolic']:
            continue
        tail = ref.removeprefix('refs/remotes/')
        remote = next((name for name in names if tail.startswith(name + '/')), tail.partition('/')[0])
        result[remote][tail.removeprefix(remote + '/')] = ref
    return result


def branch_items(repo, local, refs, remotes, evidence, detached_heads, current):
    by_remote = remote_names(refs, remotes)
    groups = []
    commits = set()
    remote_tips = sorted(set(evidence.remote_refs.values()))

    def unique_history(head):
        return repo.text('rev-list', head, *(['--not', *remote_tips] if remote_tips else [])).split()

    for ref, info in local.items():
        name = ref.removeprefix('refs/heads/')
        head = info['sha']
        matches = [branches[name] for branches in by_remote.values() if name in branches]
        if not matches:
            unique = unique_history(head) or [head]
            groups.append(dict(name=name, head=head, relation='local-only', commits=unique))
            commits.update(unique)
            continue
        for upstream_ref in matches:
            upstream = refs[upstream_ref]['sha']
            behind, ahead = map(int, repo.text('rev-list', '--left-right', '--count',
                                                f'{upstream}...{head}').split())
            if not ahead:
                continue
            unique = repo.text('rev-list', f'{upstream}..{head}').split()
            groups.append(dict(name=name, head=head, relation='diverged' if behind else 'ahead',
                               upstream_ref=upstream_ref, upstream=upstream, ahead=ahead, behind=behind,
                               commits=unique))
            commits.update(unique)
    detached = {head: unique_history(head) or [head] for head in detached_heads}
    for history in detached.values():
        commits.update(history)
    evidence.prepare_commits(sorted(commits))
    for group in groups:
        suggestion = ('archive' if group['relation'] == 'local-only' else
                      'commit' if group['name'] == current else None)
        group['commits'] = [evidence.commit(sha, suggestion) for sha in group['commits']]
        if any(change['class'] == 'owner decides' for change in group['commits']):
            classification = 'owner decides'
        elif any(change['class'] == 'superseded' for change in group['commits']):
            classification = 'superseded'
        else:
            classification = 'in remote'
        proof = {key: value for key, value in group.items() if key not in ('name', 'commits')}
        group.update(item('branch', group['name'], classification,
                          suggestion if classification == 'owner decides' else None, proof))
    detached = {head: [evidence.commit(sha, 'archive') for sha in history]
                for head, history in detached.items()}
    return groups, detached


def remote_branch_items(repo, refs, remotes):
    defaults = {}
    unmerged = []
    notes = []
    by_remote = remote_names(refs, remotes)
    for remote in sorted(set(remotes) | set(by_remote)):
        branches = by_remote[remote]
        alias = f'refs/remotes/{remote}/HEAD'
        selected = refs.get(alias, {}).get('symbolic')
        method = 'remote HEAD'
        if selected not in branches.values():
            method = next((name for name in ('main', 'master') if name in branches), None)
            selected = branches.get(method)
        defaults[remote] = dict(ref=selected, sha=refs[selected]['sha'] if selected else None, source=method)
        if not selected:
            notes.append(f'No default tracking branch for {remote}. Unmerged branches could not be determined.')
            continue
        for ref in branches.values():
            sha = refs[ref]['sha']
            if ref != selected and not repo.ancestor(sha, refs[selected]['sha']):
                unmerged.append(item('remote branch', ref, 'in remote', evidence={
                    'sha': sha, 'ref': ref, 'default_ref': selected, 'default_sha': refs[selected]['sha'],
                    'merged': False,
                }))
    return defaults, unmerged, notes


def stash_items(rows, evidence):
    stashes = []
    for row in rows.split('\n'):
        if not row:
            continue
        name, sha, parents = row.split('\t')
        parents = parents.split()
        base, index, untracked = (parents + [None] * 3)[:3]
        refs = evidence.containing(sha)
        stashes.append(item('stash', name, 'in remote' if refs else 'owner decides',
                            None if refs else 'archive',
                            dict(sha=sha, base=base, index=index, untracked=untracked,
                                 has_index=index is not None, has_untracked=untracked is not None,
                                 containing_refs=refs)))
    return stashes


def stash_rows(repo):
    return repo.text('stash', 'list', '--format=%gd%x09%H%x09%P')


SCP_REMOTE = re.compile(r'(?:(?:[^/@:]+)@)?([^/@:]+):(.*)')


def remote_identity(url, folder):
    # Compare endpoints without userinfo, query strings, fragments or credentials.
    clean = public_url(url)
    if '://' in clean:
        parts = urlsplit(clean)
        if parts.scheme.lower() != 'file' or parts.netloc.lower() not in ('', 'localhost'):
            return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/').removesuffix('.git').rstrip('/'))
        clean = unquote(parts.path)
    elif match := SCP_REMOTE.fullmatch(clean):
        host, path = match.groups()
        return ('ssh', host.lower(), path.rstrip('/').removesuffix('.git').rstrip('/'))
    return ('file', (folder / clean).resolve())


INDEX_STAT = re.compile(rb'  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n  dev: (\d+)\s+ino: (\d+)\n'
                        rb'  uid: (\d+)\s+gid: (\d+)\n  size: (\d+)\s+flags: ([0-9a-f]+)\n')


def clone_dirt(repo, head):
    # Porcelain status may hash a stat-dirty tracked file. Use cached index stat
    # data, lstat, and the untracked names instead. No checkout payload is opened.
    index = read_index(repo)
    entries = {entry['path']: entry for entry in index['entries'] if entry['stage'] == 0}
    staged = set(nul_fields(repo.git('diff-index', '--cached', '--name-only', '-z', '--no-renames',
                                     '--ignore-submodules=none', head, '--'))) if head else set(entries)
    dirty = staged | {entry['path'] for entry in index['entries'] if entry['stage'] != 0}
    raw = repo.git('ls-files', '--debug', '-z')
    offset = 0
    while offset < len(raw):
        end = raw.index(b'\0', offset)
        path = os.fsdecode(raw[offset:end])
        match = INDEX_STAT.match(raw, end + 1)
        if not match:
            raise ScanError('cannot parse clone index metadata')
        offset = match.end()
        entry = entries.get(path)
        if not entry or int(match[10], 16) & 0x40008000:
            continue
        if time.monotonic() >= repo.deadline:
            raise subprocess.TimeoutExpired('clone index metadata', 0)
        try:
            with path_parent(repo.path, path) as (directory, name):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            dirty.add(path)
            continue
        mode = entry['mode']
        if mode == '160000':
            if not stat.S_ISDIR(info.st_mode):
                dirty.add(path)
            continue
        if mode == '120000':
            matches_mode = stat.S_ISLNK(info.st_mode)
        else:
            matches_mode = stat.S_ISREG(info.st_mode) and bool(info.st_mode & stat.S_IXUSR) == (mode == '100755')
        ctime = divmod(info.st_ctime_ns, 10**9)
        mtime = divmod(info.st_mtime_ns, 10**9)
        observed = (*ctime, *mtime, info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_size)
        if not matches_mode or tuple(n & 0xffffffff for n in observed) != tuple(map(int, match.groups()[:9])):
            dirty.add(path)
    untracked = nul_fields(repo.git('ls-files', '--others', '--exclude-standard', '-z'))
    return len(dirty), len(dirty.union(untracked))


def clone_candidates(roots, max_depth, deadline, excluded, notes):
    queue = [(root, 0) for root in reversed(roots)]
    visited = set()
    skipped = 0
    try:
        while queue:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('clone search', 0)
            parent, depth = queue.pop()
            if (parent, depth) in visited or parent in excluded or parent.is_symlink():
                continue
            visited.add((parent, depth))
            try:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired('clone search', 0)
                        if entry.name == '.git':
                            if entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False):
                                yield parent
                        elif (max_depth is None or depth < max_depth) and entry.is_dir(follow_symlinks=False):
                            queue.append((Path(entry.path), depth + 1))
            except OSError:
                skipped += 1
    finally:
        if skipped:
            notes.append(f'Clone search skipped {skipped} unreadable or missing directories.')


CLONE_LIMITS = ('Dirty counts use index and filesystem metadata and can include stat-only changes. '
                'Submodule dirt and changes invisible to cached stat data are unverified.')


def project_history(repo, head):
    tips = repo.text('rev-list', '--no-walk', '--all', *([head] if head else [])).split()
    object_format = repo.text('rev-parse', '--show-object-format')
    objects = repo.text('rev-parse', '--path-format=absolute', '--git-path', 'objects')
    # The candidate may be missing the project tips while still sharing their ancestors.
    # Make the project objects readable for this command without creating an alternates file.
    alternate = '"' + objects.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if existing := os.environ.get('GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        alternate = existing + os.pathsep + alternate
    return object_format, ''.join(f'^{tip}\n' for tip in tips).encode('ascii'), {
        'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternate,
    }


def repository_item(candidate, urls, head, match, history):
    branch = candidate.branch()
    object_format, exclusions, alternates = history
    if candidate.text('rev-parse', '--show-object-format') != object_format:
        exclusions, alternates = b'', {}
    revisions = ['--branches', *([head] if head and not branch else []), '--stdin']
    unique = candidate.text('rev-list', f'--max-count={COMMIT_LIMIT}', *revisions,
                            input=exclusions, env=alternates).split()
    unique_count = len(unique)
    if unique_count == COMMIT_LIMIT:
        unique_count = int(candidate.text('rev-list', '--count', *revisions, input=exclusions, env=alternates))
    refs = candidate.refs('refs/remotes')
    remote_branches = {name for branches in remote_names(refs, urls).values() for name in branches}
    local_only = {ref.removeprefix('refs/heads/'): value['sha']
                  for ref, value in candidate.refs('refs/heads').items()
                  if ref.removeprefix('refs/heads/') not in remote_branches}
    tracked, dirt = clone_dirt(candidate, head)
    path = str(candidate.path)
    safe_names([path, branch, *local_only])
    proof = dict(path=path, head=head, branch=branch, dirty_count=dirt, dirty_tracked_count=tracked,
                 local_only_branch_count=len(local_only), local_only_branches=local_only,
                 unique_commits=unique, unique_commit_count=unique_count)
    if match:
        proof['match'] = match
    classification = 'owner decides' if unique or dirt else 'in remote'
    return item('clone' if match else 'nested repository', path, classification,
                'archive' if unique or tracked else 'in remote', proof)


def project_repositories(repo, remotes, excluded, scanned, args):
    roots = [] if args.no_clone_search else args.search_root or [Path('/tmp'), Path('/var'), Path.home()]
    roots = [root.expanduser().absolute() for root in roots]
    extras = [extra.expanduser() for extra in args.extra]
    notes = []
    if args.no_clone_search:
        notes.append('Automatic clone search disabled. Explicit paths are still checked.')
    else:
        notes.append(f'Automatic clone search checks .git directories and files through depth {args.search_depth} '
                     'from each root.')
    identities = {remote_identity(url, repo.path) for remote in remotes.values() for url in remote['urls']}
    deadline = time.monotonic() + CLONE_SECONDS
    project = Repo(repo.path, deadline)
    head = project.head()
    history = project_history(project, head)
    project_roots = set()
    root_timeouts = 0
    if roots or extras:
        try:
            probe = Repo(repo.path, min(deadline, time.monotonic() + ROOT_SECONDS))
            project_roots = set(probe.text('rev-list', '--max-parents=0', '--branches', '--remotes',
                                           *([head] if head else [])).split())
        except (ScanError, subprocess.TimeoutExpired):
            notes.append('Project root commits unavailable. Clone matching uses remote URLs and local paths.')

    clones = {}
    nested = {}
    waiting = defaultdict(list)
    seen = set(excluded)
    skipped = 0
    common_dir = project.common_dir()

    def inspect(path):
        path = path.resolve()
        if path in seen:
            return None
        seen.add(path)
        marker = path / '.git'
        if marker.is_symlink() or not marker.exists():
            return None
        candidate = Repo(path, deadline)
        if candidate.root() != path:
            return None
        metadata = candidate.common_dir()
        scanned.update((path, metadata))
        if metadata != common_dir:
            return candidate

    def record_clone(candidate, urls, head, match):
        nonlocal skipped
        discovered = [(candidate, urls, head, match)]
        while discovered:
            checkout, checkout_remotes, tip, rule = discovered.pop()
            if checkout.path in clones:
                continue
            try:
                clones[checkout.path] = repository_item(checkout, checkout_remotes, tip, rule, history)
            except PrivacyError:
                raise
            except (OSError, RuntimeError, ValueError):
                skipped += 1
                continue
            discovered.extend((*entry, 'local-path') for entry in waiting.pop(checkout.path, ()))

    try:
        for path in clone_candidates([repo.path], None, deadline, set(), notes):
            try:
                if candidate := inspect(path):
                    nested[candidate.path] = repository_item(candidate, candidate.remotes(), candidate.head(),
                                                             None, history)
            except PrivacyError:
                raise
            except (OSError, RuntimeError, ValueError):
                skipped += 1

        def external_candidates():
            yield from extras
            yield from clone_candidates(roots, args.search_depth, deadline, excluded, notes)

        for path in external_candidates():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('clone search', 0)
            try:
                if path.resolve().is_relative_to(repo.path) or not (candidate := inspect(path)):
                    continue
                urls = candidate.remotes()
                head = candidate.head()
                endpoints = {remote_identity(url, candidate.path) for remote in urls.values()
                             for url in remote['urls']}
                paths = {endpoint[1] for endpoint in endpoints if endpoint[0] == 'file'}
                if endpoints & identities:
                    record_clone(candidate, urls, head, 'url')
                elif any(target.is_relative_to(repo.path) or target in clones for target in paths):
                    record_clone(candidate, urls, head, 'local-path')
                else:
                    for target in paths - {candidate.path}:
                        waiting[target].append((candidate, urls, head))
                    if head and project_roots:
                        probe = Repo(candidate.path, min(deadline, time.monotonic() + ROOT_SECONDS))
                        try:
                            roots_at_head = probe.text('rev-list', '--max-parents=0', 'HEAD').split()
                        except subprocess.TimeoutExpired:
                            root_timeouts += 1
                            continue
                        if project_roots.intersection(roots_at_head):
                            record_clone(candidate, urls, head, 'root-commit')
            except PrivacyError:
                raise
            except (OSError, RuntimeError, ValueError):
                skipped += 1
    except subprocess.TimeoutExpired:
        notes.append(f'Clone search stopped at its {CLONE_SECONDS}-second deadline; coverage is incomplete.')
    if skipped:
        notes.append(f'Clone search skipped {skipped} unreadable or invalid checkouts.')
    if root_timeouts:
        notes.append(f'Root commit checks exceeded {ROOT_SECONDS} seconds for {root_timeouts} checkouts.')
    if clones or nested:
        notes.append(CLONE_LIMITS)
    return [clones[path] for path in sorted(clones)], [nested[path] for path in sorted(nested)], notes


def live_heads(repo, remotes):
    heads = {}
    notes = []
    for name, remote in remotes.items():
        urls = remote['fetch']
        if not urls:
            continue
        url = public_url(urls[0])
        if '::' in url and '://' not in url:
            notes.append(f'Live heads unavailable for {name}: custom transport.')
            continue
        if '://' not in url and not SCP_REMOTE.fullmatch(url):
            url = str((repo.path / url).resolve())
        # Using a URL outside the checkout prevents repository URL rewrites and
        # credential helpers from interacting with this explicitly requested query.
        transport = Repo(Path('/'), time.monotonic() + CLONE_SECONDS)
        try:
            raw = transport.text('ls-remote', '--heads', '--', url,
                                 env={'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                                      'GIT_ALLOW_PROTOCOL': 'file:http:https:ssh:git',
                                      'GIT_ASKPASS': '/bin/false', 'SSH_ASKPASS': '/bin/false'},
                                 config=('-c', 'credential.helper='))
        except (OSError, ScanError, subprocess.TimeoutExpired):
            notes.append(f'Live heads unavailable for {name}: Git query failed or timed out.')
            continue
        heads[name] = {ref: sha for sha, ref in (line.split('\t') for line in raw.split('\n') if line)}
    return heads, notes


def target_reasons(pin, remotes, refs):
    reasons = []
    if not pin['head']:
        reasons.append('The repository is unborn.')
    if not pin['branch']:
        reasons.append('HEAD is detached.')
    if not remotes:
        reasons.append('No Git remote is configured.')
    if pin['branch'] and pin['upstream'] not in refs:
        reasons.append('Several remotes are configured and the current branch has no remote-tracking upstream.'
                       if len(remotes) > 1 else 'The current branch has no remote-tracking upstream.')
    return reasons


def label(text):
    value = privacy_scan.HOME_PATH.sub('~', text)
    value = privacy_scan.EMAIL.sub('[email redacted]', value)
    value = ''.join(char if char.isprintable() else ascii(char)[1:-1] for char in value)
    # Render names as literal code spans, including names containing Markdown.
    fence = '`' * (max((len(run) for run in re.findall(r'`+', value)), default=0) + 1)
    return f'{fence} {value} {fence}'


def recommendation(change):
    title = change['class']
    suggestion = change['suggestion']
    return f'{title} (suggest {suggestion})' if suggestion and suggestion != title else title


def named_counts(counts):
    return ', '.join(f'{count} {name}' for name, count in counts.items() if count) or 'clean'


def report_list(lines, entries, *, indent=''):
    limit = 20 if len(entries) > 30 else len(entries)
    for block in entries[:limit]:
        lines.extend(block)
    if limit < len(entries):
        lines.append(f'{indent}- … and {len(entries) - limit} more (see baseline.json)')


def render_location(record, title):
    lines = ['', '## ' + title, '']
    pin = record.get('snapshot')
    if not pin:
        lines.append(f"- Branch {label(record['branch'] or 'detached')} at {label(record['head'] or 'unborn')}.")
        lines.extend('- ' + note for note in record['coverage'])
        return lines
    lines.append(f"- Branch {label(pin['branch'] or 'detached')} at {label(pin['head'] or 'unborn')}.")
    lines.append('- ' + named_counts(record['counts']))
    lines.extend('- ' + note for note in record['coverage'])
    report_list(lines, [[f"- Detached commit {label(change['name'])} · {recommendation(change)}"]
                        for change in record.get('detached_commits', [])])
    paths_by_group = defaultdict(list)
    for kind in CHANGED:
        for change in record[kind]:
            paths_by_group[directory_group(change['name'])].append((kind, change))
    submodules_by_group = defaultdict(list)
    for sub in record['submodules']:
        submodules_by_group[directory_group(sub['path'])].append(sub)
    groups = []
    for name, counts in record['groups'].items():
        group = ['', '### ' + label(name), '', '- ' + named_counts(counts)]
        details = []
        for kind, change in paths_by_group[name]:
            name_text = label(change['name'])
            proof = change['evidence']
            if proof.get('source') and (change['class'] == 'never commit' or
                                        proof.get('similarity', '').startswith('R')):
                name_text = f"{label(proof['source'])} → {name_text}"
            types = proof.get('types', [])
            suffix = ' · ' + ', '.join(types) if types else ''
            details.append([f'- {name_text} · {kind} · {recommendation(change)}{suffix}'])
        for sub in submodules_by_group[name]:
            state = ('dirty' if sub['dirty'] else 'clean') if sub['populated'] else 'dirt unavailable'
            details.append([f"- {label(sub['path'])} · submodule pointer {label(sub['pointer'])} · {state}"])
        report_list(group, details)
        groups.append(group)
    report_list(lines, groups)
    if count := record['ignored_sensitive_count']:
        names = record['ignored_sensitive']
        lines.extend(['', '### Sensitive ignored files', '',
                      f'- {count} sensitive ignored file(s) · {len(names)} name(s) recorded'])
        report_list(lines, [['- ' + label(path)] for path in names])
    return lines


def render_repository(change):
    proof = change['evidence']
    match = f" · matched by {proof['match']}" if 'match' in proof else ''
    lines = [f"- {label(change['name'])} · {label(proof['branch'] or 'detached')} at "
             f"{label(proof['head'] or 'unborn')}{match} · {proof['dirty_count']} potentially dirty path(s) · "
             f"{proof['unique_commit_count']} local-only commit(s) · {recommendation(change)}"]
    report_list(lines, [['  - ' + label(sha)] for sha in proof['unique_commits']], indent='  ')
    return lines


def render(manifest):
    pin = manifest['snapshot']
    lines = ['# Code baseline', '', f"- Project {label(manifest['product'])}",
             f"- Target upstream {label(pin['upstream'] or 'unavailable')}"]
    lines.extend('- ' + reason for reason in manifest['target_reasons'])
    lines.extend(render_location(manifest['owner'], 'Owner folder'))
    worktrees = []
    for record in manifest['worktrees']:
        tool = f" ({record['tool']})" if record['other_tool'] else ''
        worktrees.append(render_location(record, 'Worktree ' + label(record['path']) + tool))
    report_list(lines, worktrees)
    if manifest['branches']:
        lines.extend(['', '## Local branches', ''])
        branches = []
        for branch in manifest['branches']:
            relationship = branch['relation']
            if 'ahead' in branch:
                relationship += (f" with {branch['ahead']} ahead and {branch['behind']} behind "
                                 f"{label(branch['upstream_ref'])}")
            block = [f"- {label(branch['name'])} · {relationship} · {len(branch['commits'])} "
                     f"commit(s) · {recommendation(branch)}"]
            report_list(block, [[f"  - {label(change['name'])} · {recommendation(change)}"]
                                for change in branch['commits']], indent='  ')
            branches.append(block)
        report_list(lines, branches)
    lines.extend(['', '## Remote-tracking branches', ''])
    tracking = []
    for remote, default in manifest['defaults'].items():
        tracking.append([f"- {label(remote)} default {label(default['ref'] or 'unavailable')}"
                         + (f" via {default['source']}" if default['source'] else '')])
    for change in manifest['remote_branches']:
        tracking.append([f"- {label(change['name'])} · unmerged · {recommendation(change)}"])
    report_list(lines, tracking)
    if manifest['stashes']:
        lines.extend(['', '## Stashes', ''])
        stashes = []
        for change in manifest['stashes']:
            proof = change['evidence']
            parts = ', '.join(part for part in ('index', 'untracked') if proof['has_' + part]) or 'working changes'
            stashes.append([f"- {label(change['name'])} · {parts} · base {label(proof['base'] or 'unavailable')}"
                            f" · {recommendation(change)}"])
        report_list(lines, stashes)
    if manifest['nested_repositories']:
        lines.extend(['', '## Nested repositories inside the folder', ''])
        report_list(lines, [render_repository(change) for change in manifest['nested_repositories']])
    lines.extend(['', '## Other clones', ''])
    report_list(lines, [render_repository(change) for change in manifest['clones']])
    lines.extend(['', '## Coverage', ''])
    lines.extend('- ' + note for note in manifest['coverage'])
    return '\n'.join(lines) + '\n'


def check_snapshot(repo, previous, label, *, nested=(), legacy=False):
    current, status, hashes, submodules = snapshot(repo, nested=nested, ignored=legacy)
    drift = [f'{label} {field}' for field in ('head', 'branch', 'upstream', 'upstream_sha', 'object_format')
             if current[field] != previous[field]]
    if current['index']['digest'] != previous['index']['digest']:
        drift.append(f'{label} index')
    detail = previous['paths']
    changed_paths = [f'{label} path {path}' for path, value in detail.items()
                     if path not in hashes or value != hashes[path]]
    if len(detail) < PATH_LIMIT:
        changed_paths.extend(f'{label} new path {path}' for path in sorted(hashes.keys() - detail.keys())[:PATH_LIMIT])
    if path_digest(status, hashes, submodules) != previous['path_digest']:
        drift.extend(changed_paths or [f'{label} working-tree paths or status'])
    return drift


def drifted(repo, manifest, tracking=None):
    if tracking is None:
        tracking = repo.refs('refs/remotes')
    pin = manifest['snapshot']
    drift = check_snapshot(repo, pin, 'owner', nested=manifest['owner'].get('nested_paths', ()),
                           legacy='ignored' in manifest['owner'])
    pinned_refs = pin['remote_tracking_refs']
    drift.extend(f'remote-tracking ref {ref}' for ref in sorted(pinned_refs.keys() | tracking.keys())
                 if pinned_refs.get(ref) != tracking.get(ref))
    if repo.refs('refs/heads', 'refs/stash') != pin['local_refs']:
        drift.append('local branch or stash refs')
    if stash_rows(repo) != pin['stash_refs']:
        drift.append('stash entries')
    current = {worktree['path']: worktree for worktree in worktrees(repo)}
    pinned = {worktree['path']: worktree for worktree in manifest['worktrees']}
    for path in sorted(current.keys() | pinned.keys()):
        if path not in current or path not in pinned:
            drift.append(f'worktree registration {path}')
            continue
        checkout = pinned[path]
        if checkout.get('snapshot'):
            try:
                drift.extend(check_snapshot(Repo(Path(path)), checkout['snapshot'], f'worktree {path}',
                                            legacy='ignored' in checkout))
            except (OSError, ScanError):
                drift.append(f'worktree unavailable {path}')
        elif any(current[path][key] != checkout[key] for key in ('head', 'branch', 'bare', 'prunable')):
            drift.append(f'worktree metadata {path}')
    safe_names(drift)
    return drift


def write_outputs(repo, directory, manifest, report):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = {'baseline_json': directory / 'baseline.json', 'baseline_md': directory / 'BASELINE.md'}
    # Prepare both artifacts privately before replacing either destination.
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        staging = Path(temp)
        for key, data in (('baseline_json', json.dumps(manifest, indent=2) + '\n'), ('baseline_md', report)):
            target = staging / paths[key].name
            with target.open('w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
        lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if drifted(repo, manifest):
                raise ScanError('the checkout changed during the baseline scan; rerun scan')
            for path in reversed(paths.values()):
                (staging / path.name).replace(path)
        finally:
            os.close(lock)
    return {key: str(path) for key, path in paths.items()}


def scan(repo, directory, args):
    tracking = repo.refs('refs/remotes')
    local = repo.refs('refs/heads', 'refs/stash')
    remotes = repo.remotes()
    safe_names([args.product, *tracking, *(ref['symbolic'] for ref in tracking.values()), *local, *remotes])
    coverage = [
        'Remote evidence uses local remote-tracking refs, which may be stale.',
        'Rename detection pairs exact matches. Edited renames may appear as a deletion and an addition.',
        'Submodule dirt uses the immediate checkout. The scan does not inspect nested submodule contents.',
        'Special file detection covers changed paths. Binary detection samples the first 8,000 bytes of '
        'working files and Git blobs up to 5 MB.',
        'Patch IDs and added-line hints cover ordinary non-merge commit patches. Hints never determine a class.',
        'Ignored paths are counted by directory, with names recorded for sensitive files.',
        'Verify pins non-ignored changes. Clone and nested repository summaries are point-in-time observations.',
    ]
    linked = worktrees(repo)
    excluded = {repo.path, *(Path(worktree['path']) for worktree in linked)}
    scanned = excluded | {repo.common_dir()}
    clones, nested, notes = project_repositories(repo, remotes, excluded, scanned, args)
    coverage.extend(notes)
    if any(directory.is_relative_to(path) for path in scanned):
        raise ScanError('baseline output must be outside scanned repositories and Git metadata')

    evidence = Evidence(repo, tracking)
    nested_paths = [Path(change['name']).relative_to(repo.path).as_posix() for change in nested]
    owner = location(repo, 'owner', evidence, nested_paths)
    owner['nested_paths'] = nested_paths
    pin = owner['snapshot']
    pin.update(remote_tracking_refs=tracking, local_refs=local, stash_refs=stash_rows(repo))
    reasons = target_reasons(pin, remotes, tracking)
    for worktree in linked:
        path = Path(worktree['path'])
        safe_names([str(path), worktree['branch']])
        try:
            checkout = Repo(path)
            if worktree['bare'] or checkout.root() != path or checkout.common_dir() != repo.common_dir():
                raise ScanError('checkout not available')
            record = location(checkout, 'worktree', Evidence(checkout, tracking))
            worktree.update(record)
            worktree.update({key: record['snapshot'][key] for key in ('head', 'branch')})
        except PrivacyError:
            raise
        except (OSError, ScanError):
            worktree['coverage'] = ['Worktree status and path details unavailable.']
    locations = [owner, *linked]
    detached_heads = {section['snapshot']['head'] for section in locations if section.get('snapshot')
                      and not section['snapshot']['branch'] and section['snapshot']['head']}
    branches, detached = branch_items(repo, {ref: value for ref, value in local.items()
                                             if ref.startswith('refs/heads/')},
                                      tracking, remotes, evidence, detached_heads, pin['branch'])
    for section in locations:
        if section.get('snapshot') and not section['snapshot']['branch']:
            section['detached_commits'] = detached.get(section['snapshot']['head'], [])
    defaults, unmerged, notes = remote_branch_items(repo, tracking, remotes)
    coverage.extend(notes)
    stashes = stash_items(pin['stash_refs'], evidence)
    if args.ls_remote:
        pin['live_remote_heads'], notes = live_heads(repo, remotes)
        safe_names([ref for refs in pin['live_remote_heads'].values() for ref in refs])
        coverage.extend(notes)
    else:
        coverage.append('Live remote heads were not queried.')
    head = evidence.commit(pin['head'], 'commit', match_patch=False) if pin['head'] else None
    if head:
        head = next((commit for group in [*(branch['commits'] for branch in branches), *detached.values()]
                     for commit in group if commit['name'] == pin['head']), head)
    result = dict(version=1, product=args.product, snapshot=pin, owner=owner, worktrees=linked,
                  head_commit=head, branches=branches, defaults=defaults, remote_branches=unmerged,
                  stashes=stashes, clones=clones, nested_repositories=nested, coverage=coverage, needs_target=bool(reasons),
                  target_reasons=reasons)
    report = render(result)
    safe_names([report])
    counts = Counter({name: 0 for name in CLASSES})
    for record in locations:
        counts.update(record.get('classes', {}))
    items = [*branches, *unmerged, *stashes, *clones, *nested]
    # Count each commit once even if multiple branch groups reference it.
    unique = {entry['name']: entry for branch in branches for entry in branch['commits']}
    unique.update((entry['name'], entry) for history in detached.values() for entry in history)
    if head:
        unique[head['name']] = head
    counts.update(change['class'] for change in [*items, *unique.values()])
    return dict(counts=dict(counts), needs_target=bool(reasons), **write_outputs(repo, directory, result, report))


def registered_repo(product):
    entry = read_projects().get(product) if valid_product(product) else None
    if not entry:
        raise ScanError('product is not registered; run onboard.py attach first')
    repo = Repo(Path(entry['folder']).resolve())
    if repo.root() != repo.path or repo.common_dir() != Path(entry['git_common_dir']).resolve():
        raise ScanError('the folder no longer matches the registered Git checkout')
    return repo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    scanning = commands.add_parser('scan')
    verifying = commands.add_parser('verify')
    for command in (scanning, verifying):
        command.add_argument('--product', required=True)
    scanning.add_argument('--extra', type=Path, action='append', default=[])
    scanning.add_argument('--search-root', type=Path, action='append', default=[])
    scanning.add_argument('--search-depth', type=int, default=CLONE_DEPTH,
                          help='maximum checkout depth below each search root (default: %(default)s)')
    scanning.add_argument('--no-clone-search', action='store_true')
    scanning.add_argument('--ls-remote', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'scan' and args.search_depth < 0:
        scanning.error('--search-depth must be non-negative')
    try:
        repo = registered_repo(args.product)
        directory = product_dir(args.product).resolve()
        if args.command == 'scan':
            print(json.dumps(scan(repo, directory, args)))
            return 0
        saved = directory / 'baseline.json'
        if not saved.is_file():
            raise ScanError('no baseline has been recorded for this product')
        manifest = json.loads(saved.read_text(encoding='utf-8'))
        if manifest['product'] != args.product or manifest['version'] != 1:
            raise ScanError('the baseline does not match this product or scanner version')
        drift = (drifted(repo, manifest) if manifest['owner']['path'] == str(repo.path)
                 else ['registered owner folder'])
        print(json.dumps({'drifted': drift}))
        return 3 if drift else 0
    except ScanError as error:
        print(f'baseline: {error}', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        # Raw errors can contain paths, config values, or Git transport arguments.
        print('baseline: unable to read the checkout or baseline artifacts', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
