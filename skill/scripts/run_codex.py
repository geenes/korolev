#!/usr/bin/env python3
"""Run one fresh Codex session for a Korolev role and save its final answer.

  run_codex.py --role analyst --product NAME --prompt FILE --out ANALYSIS.md
  run_codex.py --role builder --product NAME --task 01-x --prompt TASK.md --out packages/01-x/HANDOFF.md

Each run is a new ephemeral session: no history, a small context. Read-only roles
review a detached worktree at --sha (default: the project's HEAD).
--write (or a role with write=true) allows workspace writes
in a registered task worktree or a legacy clone. The final message goes to OUT,
relative to the product folder unless absolute. Events are kept in the product's .runs/ folder and
summarized in USAGE.jsonl. Exit code: Codex's own; 124 on timeout; 75 if the write
lock is busy or a usage limit is hit; 77 if a registered write has no valid task worktree;
78 if the worker sandbox fails preflight; 4 if task changes fail verification.
Run it in the background: the coordinator wakes when it exits.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import ExitStack, contextmanager, suppress
from functools import partial
from itertools import chain
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIG_DIR, DEFAULT_ROLES, HOST_GIT_ENV, SKILL, append_line, codex_home, display_name,  # noqa: E402
                    is_task_worktree, isolate_codex_home, launcher_git, normalized_name, now, product_dir,
                    read_projects, read_tasks, roles, task_lock_name, touch_heartbeat, user_codex_home,
                    trusted_git_executable, valid_product, verify_linked_checkout, work_repo)
from handoff import PRIVATE_KEY_BLOCK, TITLE, sections  # noqa: E402
from onboard import sanitize_inventory  # noqa: E402
from privacy_scan import EMAIL, HOME_PATH, SECRETS, load_config, redact  # noqa: E402
from procs import LockBusyError, _start_time, run_lock  # noqa: E402
from resume_watch import limit_details  # noqa: E402
import egress_proxy  # noqa: E402
import sandbox  # noqa: E402

HEADER = """You are the {role} for the project "{display}" in a Korolev-coordinated team.
Repository (your working directory): {cwd}
Run context bundle: {bundle}
Read STATE-brief.md there for project state. Additional context must be listed in the task or passed with --context.
Follow the repository's agent instructions (AGENTS.md, CLAUDE.md, nested ones too) and use its own agent
tooling when relevant: skills (.codex/skills, .claude/skills, .agents), subagent definitions, commands,
plugins and MCP configs listed in INVENTORY.md when it is included. Never print secrets, never use
credentials you find, never push, deploy or contact external services unless the task says so.
{write_rule}
Your final message is saved as the deliverable: make it complete, structured and bounded as asked.

----- TASK -----
"""

CONTEXT_LIMIT = 2 * 1024 * 1024
REPORT_LIMIT = 2 * 1024 * 1024
REPORT_CHUNK = 65536
PR_SET_CHILD_SUBREAPER = 36
WAIT_ALL_CHILDREN = 0x40000000
REVIEW_SETUP_SECONDS = 30
REVIEW_CLEANUP_SECONDS = 5
STATE_FILE = Path('STATE.md')
STATE_BRIEF = 'STATE-brief.md'
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
OUTPUT_GIT_ERROR = 'worker output and state must be outside the checkout, owner folder and Git metadata'
COMMIT_ID = r'(?:[0-9a-f]{40}|[0-9a-f]{64})'
HANDOFF_STATUS = re.compile(
    r'- UTC: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'
    rf'|- Clone: HEAD {COMMIT_ID}, branch (?:\S+|detached HEAD), working tree (?:clean|dirty)\.'
    r'|- Clone: unverified \(Git snapshot unavailable\)\.'
    r'|- Pending lessons: \d+', re.ASCII,
)
HANDOFF_RUN = re.compile(
    rf'- ([\w.-]+)( \| exit (?:-?\d+|unknown) \| commit (?:{COMMIT_ID}|none))'
    r'(?: \| uncached input tokens -?\d+)?', re.ASCII,
)


def check_context_name(path):
    for part in path.parts:
        name = normalized_name(part)
        if (name.startswith(('inbox', 'sent', 'approvals', 'release', 'journal', 'authorizations'))
                or '.sqlite' in name
                or sandbox.credential_name(name)
                or name in {'.runs', '.private', 'notes.md', 'coordinator-notes.md',
                            'backups', 'reviews', 'usage.jsonl', 'task0', 'task0.json', 'product.json'}):
            raise ValueError(f'private product data cannot be used as context: {path}')


def fd_path(fd):
    return Path(f'/proc/self/fd/{fd}')


@contextmanager
def open_directory(path, *, parent=None, create=False, protected=()):
    # Walk the canonical path without following a replacement symlink.
    with ExitStack() as stack:
        directory = parent
        for part in path.parts:
            try:
                directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                if not create or part == '/':
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=directory)
                directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
            for identity, error in protected:
                if os.path.samestat(os.fstat(directory), identity):
                    raise ValueError(error)
        yield directory


@contextmanager
def open_context_file(root, path):
    # Open each component relative to its already opened parent. Symlinks must
    # not redirect an approved name to private data, even during a copy.
    with ExitStack() as stack:
        directory = os.dup(root)
        stack.callback(os.close, directory)
        for part in path.parts[:-1]:
            directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'context input is not a regular file: {path}')
        if info.st_nlink != 1:
            raise ValueError(f'context input has multiple hard links: {path}')
        with open(f'/proc/self/fd/{fd}', 'rb') as stream:
            yield stream, info


def bounded_read(stream, info):
    if info.st_size > CONTEXT_LIMIT or len(data := stream.read(CONTEXT_LIMIT + 1)) > CONTEXT_LIMIT:
        raise ValueError('file exceeds the 2 MiB limit')
    return data


def refuse_state_alias(pdir, info, previous=None):
    if previous is not None and os.path.samestat(info, previous):
        raise ValueError('STATE.md may only be included as a brief')
    with suppress(FileNotFoundError):
        if os.path.samestat(info, os.stat(STATE_FILE, dir_fd=pdir, follow_symlinks=False)):
            raise ValueError('STATE.md may only be included as a brief')


def state_brief(pdir, *, required=False):
    try:
        with open_context_file(pdir, STATE_FILE) as (stream, info):
            data = bounded_read(stream, info)
    except FileNotFoundError:
        if required:
            raise
        return '', None
    text = data.decode('utf-8', errors='replace')
    approved = ['# Product state']
    for title, _, body, end in sections(text):
        if title != TITLE:
            continue
        approved.append(f'## {TITLE}')
        recent_runs = False
        for line in text[body:end].splitlines():
            if line == '### Recent runs' and not recent_runs:
                recent_runs = True
                approved.append('### Recent runs')
            elif line.startswith('### '):
                break
            elif not recent_runs and HANDOFF_STATUS.fullmatch(line):
                approved.append(re.sub(r'(?<=, branch )\S+(?=, working tree )', 'named', line))
            elif recent_runs and (run := HANDOFF_RUN.fullmatch(line)):
                role = run[1] if run[1] in DEFAULT_ROLES else 'unknown'
                approved.append(f'- {role}{run[2]}')
            elif recent_runs and line == '- No usage runs recorded.':
                approved.append('- No usage runs recorded.')
        break
    text = PRIVATE_KEY_BLOCK.sub('[REDACTED]', '\n'.join(approved))
    for _, pattern in SECRETS:
        text = pattern.sub('[REDACTED]', text)
    text = EMAIL.sub('[REDACTED]', HOME_PATH.sub('[REDACTED]', text))
    deny, _ = load_config()
    if deny:
        text = re.sub('|'.join(map(re.escape, sorted(deny, key=len, reverse=True))), '[REDACTED]', text, flags=re.I)
    return text + '\n', info


def context_path(pdir, name):
    path = Path(name).expanduser()
    if path.is_absolute():
        if not path.is_relative_to(pdir):
            raise ValueError(f'context path is outside the product folder: {name!r}')
        path = path.relative_to(pdir)
    if not path.parts or '..' in path.parts:
        raise ValueError(f'invalid product-relative context path: {name!r}')
    check_context_name(path)
    if normalized_name(path.name) == normalized_name(STATE_FILE.name) and path != STATE_FILE:
        raise ValueError(f'only the product STATE.md can be used as state context: {name!r}')
    return path


def task_context(task):
    for line in task.splitlines():
        if line.startswith('Context:'):
            lexer = shlex.shlex(line.removeprefix('Context:'), posix=True)
            lexer.whitespace += ','
            lexer.whitespace_split = True
            lexer.commenters = ''
            yield from lexer


@contextmanager
def prompt_source(pdir, product_fd, skill_fd, prompt):
    if '..' in prompt.parts:
        raise ValueError('prompt paths cannot contain ..')
    # Resolve ancestors above the product, while keeping product descendants lexical.
    for parent in reversed(prompt.parents):
        if parent.name == pdir.name and parent.parent.resolve() / parent.name == pdir:
            yield product_fd, prompt.relative_to(parent)
            return
    # Installation aliases must identify the directory opened at startup.
    installed = os.fstat(skill_fd)
    for parent in reversed(prompt.parents):
        if parent.name == 'templates' and os.path.samestat(parent.parent.stat(), installed):
            with open_directory(Path('templates'), parent=skill_fd) as templates_fd:
                yield templates_fd, prompt.relative_to(parent)
            return
    raise ValueError('external prompts must be inside the installed skill templates/ directory')


def build_context_bundle(pdir, product_fd, skill_fd, runs_fd, stamp, prompt_name, requested):
    prompt = Path(prompt_name).expanduser().absolute()
    prompt_key = normalized_name(prompt.name)
    if prompt_key in {normalized_name(STATE_FILE.name), normalized_name(STATE_BRIEF)}:
        raise ValueError(f'prompt name is reserved: {prompt.name}')
    try:
        with prompt_source(pdir, product_fd, skill_fd, prompt) as (source_root, source_path):
            check_context_name(source_path)
            with open_context_file(source_root, source_path) as (stream, info):
                refuse_state_alias(product_fd, info)
                task = bounded_read(stream, info).decode('utf-8')
    except (OSError, ValueError) as error:
        raise ValueError(f'cannot open trusted prompt {prompt.name!r}: {error}') from error
    task = sanitize_inventory(task, str(source_path))
    paths = dict.fromkeys(context_path(pdir, name) for name in (*requested, *task_context(task)))
    if code_review_prompt(prompt) and not any(path.name == 'TASK.md' for path in paths):
        raise ValueError('the code-review template requires TASK.md in the context')
    bundle = Path(tempfile.mkdtemp(prefix=f'{stamp}-', suffix='-ctx', dir=fd_path(runs_fd)))
    try:
        (bundle / prompt.name).write_text(task, encoding='utf-8')
        brief, state_info = state_brief(product_fd, required=STATE_FILE in paths)
        (bundle / STATE_BRIEF).write_text(brief, encoding='utf-8')
        spellings = {}
        for path in paths:
            if path == STATE_FILE:
                continue
            if source_root == product_fd and path == source_path == Path(prompt.name):
                continue
            keys = tuple(map(normalized_name, path.parts))
            if keys[0] == prompt_key or normalized_name(STATE_BRIEF) in keys:
                raise ValueError(f'context path conflicts with a bundle file: {path}')
            for end in range(1, len(keys) + 1):
                if spellings.setdefault(keys[:end], path.parts[:end]) != path.parts[:end]:
                    raise ValueError(f'context path conflicts with another bundle path: {path}')
            try:
                with open_context_file(product_fd, path) as (stream, info):
                    refuse_state_alias(product_fd, info, state_info)
                    data = bounded_read(stream, info)
                if keys[-1] == 'inventory.md':
                    data = sanitize_inventory(data.decode('utf-8'), str(path)).encode('utf-8')
            except (OSError, ValueError) as error:
                raise ValueError(f'cannot include context file {str(path)!r}: {error}') from error
            dest = bundle / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    except BaseException:
        shutil.rmtree(bundle)
        raise
    return bundle, task, [Path(STATE_BRIEF) if path == STATE_FILE else path for path in paths]


@contextmanager
def private_runs_dir(product_fd):
    with suppress(FileExistsError):
        os.mkdir('.runs', mode=0o700, dir_fd=product_fd)
    with ExitStack() as stack:
        try:
            fd = os.open('.runs', DIRECTORY_FLAGS, dir_fd=product_fd)
        except OSError as error:
            raise ValueError('the product .runs must be a directory without symlinks') from error
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise ValueError('the product .runs must be owned by the launcher user')
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        yield fd


def review_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('review Git deadline expired')
    return remaining


def bounded_git(folder, *args, executable, deadline, **kwargs):
    try:
        return launcher_git(folder, *args, executable=executable, timeout=review_remaining(deadline), **kwargs)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError('review Git deadline expired') from error


def review_git(git, folder, *args, error, env=None, fds=()):
    result = git(folder, *args, env_overrides=env, pass_fds=fds)
    if result.returncode:
        raise OSError(error)
    return result.stdout.removesuffix(b'\n')


def verified_git_common_dir(git, root, marker, *, source=None, resources=None):
    error = 'cannot identify the review repository'
    if git is None or not root.is_dir() or (source is not None and not source.is_dir()):
        raise OSError(error)

    def location(folder, *args):
        path = Path(os.fsdecode(review_git(git, folder, 'rev-parse', *args, error=error)))
        if not path.is_absolute() or not path.is_dir():
            raise OSError(error)
        return path.resolve(strict=True)

    # Discovery can skip broken metadata and report a different repository.
    expected = location(root, '--resolve-git-dir', marker)
    if (expected != location(root, '--absolute-git-dir')
            or (source is not None and source != root and expected != location(source, '--absolute-git-dir'))):
        raise OSError(error)
    return review_common_dir(git, root, resources=resources)


def resolve_review_sha(source, revision, *, git=None):
    if revision == '':
        raise OSError('--sha must not be empty')
    if git is None:
        executable = trusted_git_executable()
        git = partial(bounded_git, executable=executable, deadline=time.monotonic() + REVIEW_SETUP_SECONDS)
    ref = 'HEAD' if revision is None else revision
    sha = review_git(git, source, 'rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}',
                     error='cannot resolve the review revision to a commit').decode('ascii')
    if re.fullmatch(COMMIT_ID, ref.lower()) and sha != ref.lower():
        raise OSError('the full review commit ID did not resolve to itself')
    return sha


def review_common_dir(git, source, *, resources=None):
    path = Path(os.fsdecode(review_git(git, source, 'rev-parse', '--path-format=absolute', '--git-common-dir',
                                       error='cannot determine the review Git common directory')))
    if not path.is_absolute() or not path.is_dir():
        raise OSError('cannot verify the review Git common directory')
    path = path.resolve(strict=True)
    return path, resources.enter_context(open_directory(path)) if resources is not None else None


def review_task_workspace(product, cwd, project, git):
    gitfile = cwd / '.git'
    if (not gitfile.is_file() or gitfile.is_symlink()
            or cwd.is_relative_to(Path(project['folder']).resolve())
            or not any(cwd == Path(task['path']) for task in read_tasks(product).values())):
        return False
    try:
        common, _ = review_common_dir(git, cwd)
        private = Path(os.fsdecode(review_git(git, cwd, 'rev-parse', '--absolute-git-dir',
                                            error='cannot identify task worktree metadata'))).resolve()
        if common != Path(project['git_common_dir']).resolve() or private.parent != common / 'worktrees':
            return False
        with open_directory(cwd) as fd:
            verify_linked_checkout(fd, private)
        return True
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False


def code_review_prompt(prompt):
    return normalized_name(Path(prompt).name) == 'code-review.md'


@contextmanager
def review_admin(directory, deadline=None):
    # Git inherits the admin lock during registration and removal, including if the launcher is killed.
    fd = os.open('.', DIRECTORY_FLAGS, dir_fd=directory)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None:
                    raise
                time.sleep(min(0.05, review_remaining(deadline)))
        yield fd
    finally:
        os.close(fd)


@contextmanager
def review_admin_view(lock, worktrees_fd):
    # Git appends /worktrees to its common directory, with no separate
    # override. Redirect that lookup before invoking any worktree command.
    with (tempfile.TemporaryDirectory(dir='/tmp', prefix='korolev-review-git-') as directory,
          open_directory(Path(directory)) as view_fd):
        for name in ('config', 'objects', 'refs', 'packed-refs', 'shallow'):
            (fd_path(view_fd) / name).symlink_to(fd_path(lock) / name)
        (fd_path(view_fd) / 'worktrees').symlink_to(fd_path(worktrees_fd))
        yield lock, worktrees_fd, view_fd


def admin_git(git, lock, worktrees_fd, view_fd, *args, error, fds=()):
    return review_git(git, fd_path(lock), '--git-dir=.', *args, error=error,
                      env={'GIT_COMMON_DIR': str(fd_path(view_fd))}, fds=(lock, worktrees_fd, view_fd, *fds))


def review_holder_gone(lease, boot, namespace):
    pid, start, lease_boot, lease_namespace = (
        lease[key] for key in ('pid', 'start_time', 'boot_id', 'pid_namespace'))
    if (type(pid) is not int or pid <= 0 or type(start) is not int or start < 0
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', lease_boot)):
        return False
    if lease_boot != boot:
        return True
    if lease_namespace != namespace:
        return False
    try:
        return _start_time(pid) != start
    except FileNotFoundError:
        with suppress(ProcessLookupError):
            os.kill(pid, 0)
            return False
        return True


def review_file(directory, path):
    with open_context_file(directory, path) as (stream, info):
        return bounded_read(stream, info)


def review_inode(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def checkout_pointer(checkout_fd):
    return review_file(checkout_fd, Path('.git')).removesuffix(b'\n')


def replace_review_pointer(directory, name, value):
    with tempfile.NamedTemporaryFile(dir=fd_path(directory), prefix='pointer-', delete=False) as pointer:
        pointer.write(os.fsencode(value) + b'\n')
    os.replace(Path(pointer.name).name, name, src_dir_fd=directory, dst_dir_fd=directory)


def move_review_entry(source, target, name, target_name):
    # RENAME_NOREPLACE preserves an entry even if it appears during retirement.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(source, os.fsencode(name), target, os.fsencode(target_name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def retire_checkout(area, lease_fd, private_fd, retired, checkout_inode):
    # Give Git a permanently absent target. Remember the transition so an
    # interrupted deletion of a partially removed checkout can be retried.
    replace_review_pointer(private_fd, 'gitdir', retired / '.git')
    if os.path.lexists(fd_path(lease_fd) / area.name):
        move_review_entry(lease_fd, lease_fd, area.name, 'retiring')
    if os.path.lexists(fd_path(lease_fd) / 'retiring'):
        with open_directory(Path('retiring'), parent=lease_fd) as checkout_fd:
            if checkout_inode is not None and review_inode(checkout_fd) != checkout_inode:
                raise OSError('retiring checkout changed; keeping the lease')
            shutil.rmtree('retiring', dir_fd=lease_fd)


def write_review_lease(lease_fd, lease):
    with (fd_path(lease_fd) / 'lease.json').open('x') as stream:
        stream.write(json.dumps(lease) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def retirement_marker(name, lease_fd):
    return '{}.{}.{}.json'.format(name, *review_inode(lease_fd))


def retire_review_area(parent_fd, retirement_fd, name, lease_fd):
    saved_lease = retirement_marker(name, lease_fd)
    if set(os.listdir(lease_fd)) != {'lease.json'}:
        raise OSError('unexpected review area contents; keeping the lease')
    with egress_proxy.blocked_cleanup_signals():
        if parent_fd != retirement_fd:
            move_review_entry(parent_fd, retirement_fd, name, name)
            try:
                verify_directory(fd_path(retirement_fd) / name, lease_fd)
            except OSError:
                with suppress(FileExistsError):
                    move_review_entry(retirement_fd, parent_fd, name, name)
                raise
        move_review_entry(lease_fd, retirement_fd, 'lease.json', saved_lease)
        try:
            os.rmdir(name, dir_fd=retirement_fd)
        except OSError:
            move_review_entry(retirement_fd, lease_fd, saved_lease, 'lease.json')
            raise
        os.unlink(saved_lease, dir_fd=retirement_fd)


def clean_review(git, common, parent_fd, retirement_fd, area, admin, lease_fd, lease):
    checkout = area / area.name
    gitfile = checkout / '.git'
    contents = fd_path(lease_fd)
    lock, worktrees_fd, _ = admin
    registered_path = fd_path(worktrees_fd) / area.name
    retired = Path('/dev/null') / area.name
    remove = partial(admin_git, git, *admin, 'worktree', 'remove', '--force', '--', retired,
                     error='cannot remove the detached review worktree')
    try:
        verify_directory(fd_path(parent_fd) / area.name, lease_fd)
        info = os.fstat(lease_fd)
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                or lease['run_id'] != area.name or lease['common'] != str(common)
                or lease.get('area_inode', review_inode(lease_fd)) != review_inode(lease_fd)
                or lease.get('common_inode', review_inode(lock)) != review_inode(lock)
                or lease.get('worktrees_inode', review_inode(worktrees_fd)) != review_inode(worktrees_fd)):
            raise OSError('cannot verify review lease')
        entries = set(os.listdir(lease_fd))
        if entries - {'lease.json', 'git', area.name, 'retiring'}:
            raise OSError('unexpected review area contents; keeping the lease')
        if os.path.lexists(registered_path):
            with open_directory(Path(area.name), parent=worktrees_fd) as private_fd:
                backlink = review_file(private_fd, Path('gitdir')).removesuffix(b'\n')
                retired_pointer = backlink == os.fsencode(retired / '.git')
                if (not retired_pointer and backlink != os.fsencode(gitfile)
                        or 'retiring' in entries and (not retired_pointer or area.name in entries)):
                    raise OSError('cannot verify review registration')
                if area.name in entries:
                    with open_directory(Path(area.name), parent=lease_fd) as checkout_fd:
                        pointer = checkout_pointer(checkout_fd)
                        if pointer != b'gitdir: ' + os.fsencode(common / 'worktrees' / area.name):
                            if 'pending_gitdir' not in lease or pointer != os.fsencode(lease['pending_gitdir']):
                                raise OSError('cannot verify review checkout')
                            verify_directory(common / 'worktrees', worktrees_fd)
                        if lease.get('checkout_inode', review_inode(checkout_fd)) != review_inode(checkout_fd):
                            raise OSError('review checkout changed; keeping the lease')
                retire_checkout(area, lease_fd, private_fd, retired, lease.get('checkout_inode'))
            try:
                remove()
            except TimeoutError:
                raise
            except OSError as error:
                print(f'WARNING: {area.name}: {error}; attempting review cleanup fallback', file=sys.stderr)
                # Prune is repository-wide, so only use it for a sole registration.
                if registered_path.exists() and set(os.listdir(worktrees_fd)) == {area.name}:
                    with suppress(OSError):
                        admin_git(git, *admin, 'worktree', 'prune', '--expire=never', error='cannot prune Git worktrees')
                if os.path.lexists(registered_path):
                    remove()
        elif 'common_inode' not in lease:
            raise OSError('cannot verify the repository of an older review lease')
        registered = admin_git(git, *admin, 'worktree', 'list', '--porcelain', '-z',
                               error='cannot check review worktree registration')
        if (b'worktree ' + os.fsencode(checkout) in registered.split(b'\0')
                or os.path.lexists(registered_path) or any(os.path.lexists(contents / name)
                                                         for name in (area.name, 'retiring'))):
            raise OSError('review checkout or registration remains; keeping the lease')
        if os.path.lexists(contents / 'git'):
            with open_directory(Path('git'), parent=lease_fd) as metadata:
                if set(os.listdir(metadata)) - {'.git'}:
                    raise OSError('unexpected review metadata contents; keeping the lease')
            shutil.rmtree('git', dir_fd=lease_fd)
        retire_review_area(parent_fd, retirement_fd, area.name, lease_fd)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'WARNING: {area.name}: {error}', file=sys.stderr)


def reclaim_reviews(git, root, root_fd, retirement_fd, common, boot, namespace, admin, deadline):
    for parent_fd in (root_fd, retirement_fd):
        for area in fd_path(parent_fd).glob('review-*'):
            if area.suffix == '.json':
                continue
            review_remaining(deadline)
            try:
                with open_directory(Path(area.name), parent=parent_fd) as directory, review_admin(directory) as lease_fd:
                    marker = None
                    try:
                        lease = json.loads(review_file(lease_fd, Path('lease.json')))
                    except FileNotFoundError:
                        if parent_fd != retirement_fd:
                            continue
                        marker = Path(retirement_marker(area.name, lease_fd))
                        lease = json.loads(review_file(retirement_fd, marker))
                    if isinstance(lease, dict) and lease.get('lock') == 'area' and review_holder_gone(lease, boot, namespace):
                        if marker is not None:
                            if lease.get('common') != str(common) or lease.get('common_inode') != review_inode(admin[0]):
                                continue
                            move_review_entry(retirement_fd, lease_fd, marker, 'lease.json')
                        clean_review(git, common, parent_fd, retirement_fd, root / area.name, admin, lease_fd, lease)
            except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
                continue  # Live, legacy, or unverifiable lease.


def populate_review(git, common_fd, lease_fd, checkout_fd, private_fd, sha):
    # Populate through clean metadata to avoid filters from the source config.
    storage = fd_path(lease_fd)
    object_format = 'sha256' if len(sha) == 64 else 'sha1'
    review_git(git, storage, 'init', '--quiet', '--template=', f'--object-format={object_format}', 'git',
               fds=(lease_fd,), error='cannot initialize review checkout metadata')
    review_git(git, fd_path(checkout_fd), f'--git-dir={storage / "git/.git"}', '--work-tree=.',
               'read-tree', '--reset', '-u', sha, fds=(common_fd, lease_fd, private_fd, checkout_fd),
               env={'GIT_OBJECT_DIRECTORY': str(fd_path(common_fd) / 'objects'),
                    'GIT_INDEX_FILE': str(fd_path(private_fd) / 'index')},
               error='cannot populate the detached review worktree')


@contextmanager
def prompt_workspace(root):
    # Check the ancestry before tempfile can write anything, even during fallback.
    candidates = (root, *(os.environ.get(key) for key in ('TMPDIR', 'TEMP', 'TMP')),
                  '/tmp', '/var/tmp', '/usr/tmp')
    for candidate in filter(None, candidates):
        try:
            parent = Path(candidate).resolve(strict=True)
            if not parent.is_dir() or next(git_metadata_candidates(parent), None):
                continue
            temporary = tempfile.TemporaryDirectory(prefix='prompt-', dir=parent)
        except (OSError, RuntimeError):
            continue
        with temporary as directory:
            workspace = Path(directory)
            if workspace.resolve(strict=True) != workspace or next(git_metadata_candidates(workspace), None):
                raise OSError('the prompt workspace must have Git-free ancestry')
            yield workspace
            return
    raise OSError('no writable temporary directory with Git-free ancestry for the prompt workspace')


@contextmanager
def review_worktree(product, source, revision, *, prompt_only=False, executable=None,
                    expected_common=None, expected_common_fd=None, deadline=None, workspace_root=None):
    source = source.resolve()
    root = workspace_root or work_repo(product).parent.resolve()
    if prompt_only:
        with prompt_workspace(root) as directory:
            yield directory, None, None, None, Path('.'), None, None
        return
    if deadline is None:
        deadline = time.monotonic() + REVIEW_SETUP_SECONDS
    executable = executable or trusted_git_executable()
    git = partial(bounded_git, executable=executable, deadline=deadline)
    prefix = Path(os.fsdecode(review_git(git, source, 'rev-parse', '--show-prefix',
                                        error='cannot locate the review source')))
    if prefix.is_absolute() or '..' in prefix.parts:
        raise OSError('invalid review source subdirectory')
    owner = source.parents[len(prefix.parts) - 1] if prefix.parts else source
    common, _ = review_common_dir(git, source)
    if root.is_relative_to(owner) or root.is_relative_to(common):
        raise OSError('review worktrees must be outside the source checkout')
    boot, namespace = Path('/proc/sys/kernel/random/boot_id').read_text().strip(), os.readlink('/proc/self/ns/pid')
    lease_owner, worker_lease = ExitStack(), ExitStack()
    lease_fd = None
    try:
        common_fd = lease_owner.enter_context(open_directory(common))
        info = os.fstat(common_fd)
        if ((expected_common_fd is not None and not os.path.samestat(info, os.fstat(expected_common_fd)))
                or (expected_common is not None and not os.path.samestat(info, Path(expected_common).stat()))):
            raise OSError('the review source belongs to a different Git repository')
        if expected_common_fd is not None:
            common_fd = expected_common_fd
        private = Path(os.fsdecode(review_git(git, source, 'rev-parse', '--absolute-git-dir',
                                              error='cannot identify review metadata'))).resolve(strict=True)
        if private != common and private.parent != common / 'worktrees':
            raise OSError('the review source Git metadata is outside the common directory')
        source_fd = common_fd if private == common else lease_owner.enter_context(open_directory(private))
        if review_common_dir(git, source)[0] != common:
            raise OSError('the review source Git identity changed')

        def source_git(_folder, *args, **_options):
            return git(fd_path(source_fd), '--git-dir=.', *args, pass_fds=tuple({source_fd, common_fd}),
                       env_overrides={'GIT_COMMON_DIR': os.fspath(fd_path(common_fd))})

        sha = resolve_review_sha(source, revision, git=source_git)
        work_fd = lease_owner.enter_context(open_directory(root.parent, create=True))
        with suppress(FileExistsError):
            os.mkdir(root.name, mode=0o700, dir_fd=work_fd)
        root_fd = lease_owner.enter_context(open_directory(Path(root.name), parent=work_fd))
        with review_admin(common_fd, deadline) as lock, ExitStack() as setup:
            verify_directory(common, common_fd)
            verify_directory(root, root_fd)
            worktrees_fd = lease_owner.enter_context(open_directory(Path('worktrees'), parent=lock, create=True))
            verify_directory(common / 'worktrees', worktrees_fd)
            with suppress(FileExistsError):
                os.mkdir('.retired-reviews', mode=0o700, dir_fd=root_fd)
            retirement_fd = lease_owner.enter_context(open_directory(Path('.retired-reviews'), parent=root_fd))
            info = os.fstat(retirement_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError('review retirement storage must be private')
            admin = setup.enter_context(review_admin_view(lock, worktrees_fd))
            view = fd_path(admin[-1])
            reclaim_reviews(git, root, root_fd, retirement_fd, common, boot, namespace, admin, deadline)
            with egress_proxy.blocked_cleanup_signals():
                name = Path(tempfile.mkdtemp(prefix='review-', dir=fd_path(root_fd))).name
                area = root / name
                directory = lease_owner.enter_context(open_directory(Path(name), parent=root_fd))
                os.mkdir(name, mode=0o700, dir_fd=directory)
                checkout_fd = lease_owner.enter_context(open_directory(Path(name), parent=directory))
                pid = os.getpid()
                lease = dict(run_id=name, pid=pid, start_time=_start_time(pid), boot_id=boot,
                             pid_namespace=namespace, common=str(common), common_inode=review_inode(common_fd),
                             worktrees_inode=review_inode(worktrees_fd), checkout_inode=review_inode(checkout_fd),
                             area_inode=review_inode(directory),
                             pending_gitdir=f'gitdir: {Path(os.readlink(view)) / "worktrees" / name}',
                             lock='area')
                lease_fd = worker_lease.enter_context(review_admin(directory))
                write_review_lease(lease_fd, lease)
            (view / name).symlink_to(fd_path(checkout_fd))
            admin_git(git, *admin, '-c', 'worktree.useRelativePaths=false', 'worktree', 'add', '--quiet',
                      '--detach', '--no-checkout', '--', view / name, sha,
                      fds=(lease_fd, checkout_fd), error='cannot create the detached review worktree')
            verify_directory(common, common_fd)
            verify_directory(area, lease_fd)
            checkout = area / name
            verify_directory(checkout, checkout_fd)
            durable = common / 'worktrees' / name
            verify_directory(durable.parent, worktrees_fd)
            private_fd = lease_owner.enter_context(open_directory(Path(name), parent=worktrees_fd))
            verify_directory(durable, private_fd)
            if (checkout_pointer(checkout_fd) != os.fsencode(lease['pending_gitdir'])
                    or review_file(private_fd, Path('gitdir')).removesuffix(b'\n') != os.fsencode(checkout / '.git')
                    or review_file(private_fd, Path('commondir')).removesuffix(b'\n') != b'../..'
                    or review_file(private_fd, Path('HEAD')).removesuffix(b'\n') != sha.encode('ascii')):
                raise OSError('cannot verify the new review registration')
            replace_review_pointer(checkout_fd, '.git', f'gitdir: {durable}')
            verify_linked_checkout(checkout_fd, fd_path(worktrees_fd) / name)
            populate_review(git, common_fd, lease_fd, checkout_fd, private_fd, sha)
            verify_directory(common, common_fd)
            verify_directory(durable, private_fd)
            verify_directory(checkout, checkout_fd)
        yield checkout, sha, common, owner, prefix, (checkout_fd, common_fd, private_fd), lease_fd
    finally:
        with lease_owner, worker_lease:
            if lease_fd is not None:
                deadline = time.monotonic() + REVIEW_CLEANUP_SECONDS
                git = partial(bounded_git, executable=executable, deadline=deadline)
                try:
                    # A sandbox reaper may still hold the inherited description.
                    worker_lease.close()
                    with (review_admin(directory, deadline) as lease_fd, review_admin(common_fd, deadline) as lock,
                          review_admin_view(lock, worktrees_fd) as admin):
                        clean_review(git, common, root_fd, retirement_fd, area, admin, lease_fd, lease)
                except OSError as error:
                    print(f'WARNING: {area.name}: cannot lock review area for cleanup: {error}', file=sys.stderr)


def usage_from(events_path, *, include_turns=False):
    """Sum reported usage and optionally return the number of completed turns."""
    usage, turns, _ = read_event_stats(events_path)
    if include_turns:
        return usage, turns
    return usage


def read_event_stats(events_path):
    total = {}
    turns = 0
    commands_run = 0
    try:
        for line in events_path.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get('type')
            if event_type == 'item.completed':
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') in (
                    'command_execution', 'mcp_tool_call', 'dynamic_tool_call', 'collab_tool_call',
                    'web_search', 'image_generation', 'image_view',
                ):
                    commands_run += 1
            if event_type != 'turn.completed':
                continue
            turns += 1
            if isinstance(event.get('usage'), dict):
                for key, value in event['usage'].items():
                    if isinstance(value, (int, float)):
                        total[key] = total.get(key, 0) + value
    except OSError:
        pass
    return total or None, turns, commands_run


def event_limit(events_path):
    """Return the terminal provider limit, its reset time, and any limit-like evidence."""
    terminal = None
    saw_limit = False
    checked_at = time.time()
    with events_path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if not saw_limit:
                saw_limit, _ = limit_details(line, checked_at)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') in ('turn.failed', 'error'):
                terminal = event
                saw_limit = saw_limit or any(
                    payload.get(key) in (429, '429')
                    for payload in (event, event.get('error')) if isinstance(payload, dict)
                    for key in ('code', 'status', 'status_code')
                )
            elif event.get('type') == 'turn.completed':
                terminal = None
    if terminal is None:
        return False, None, saw_limit
    payloads = [payload for payload in (terminal, terminal.get('error')) if isinstance(payload, dict)]
    message = '\n'.join(value for payload in payloads if isinstance(value := payload.get('message'), str))
    if any(payload.get('code') == 'usage_limit_reached' for payload in payloads):
        message = 'usage_limit_reached\n' + message
    elif not re.search(r'\busage limit\b', message, re.I):
        return False, None, saw_limit
    _, retry = limit_details(message, checked_at)
    if retry is None:
        _, retry = limit_details(json.dumps(terminal), checked_at)
    return True, retry, saw_limit


def warning_thresholds():
    thresholds = {'context_warn_tokens': 1_500_000, 'uncached_warn_tokens': 400_000}
    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text())
    except (OSError, ValueError):
        return thresholds
    if isinstance(config, dict):
        for key in thresholds:
            value = config.get(key)
            if type(value) in (int, float):
                thresholds[key] = value
    return thresholds


class VerificationError(ValueError):
    pass


def staged_secrets(git):
    # Use Git's quoted patch filenames for diagnostics. --text scans binary blobs too.
    diff = git('diff', '--cached', '--no-color', '--no-ext-diff', '--no-textconv',
               '--submodule=short', '--find-renames', '--text', '--no-relative',
               '--src-prefix=a/', '--dst-prefix=b/', '--output-indicator-new=+', '-U0')
    if diff.returncode:
        raise VerificationError('cannot scan the staged diff')
    findings = set()
    path = None
    in_hunk = False
    for line in diff.stdout.split(b'\n'):
        if line.startswith(b'diff --git '):
            path = None
            in_hunk = False
        elif line.startswith(b'@@ '):
            in_hunk = True
        elif not in_hunk and line.startswith(b'+++ '):
            name = line[4:].removesuffix(b'\t')
            if name.startswith(b'b/'):
                path = os.fsdecode(name[2:])
            elif name.startswith(b'"b/'):
                path = os.fsdecode(b'"' + name[3:])
        elif in_hunk and line.startswith(b'+'):
            if path is None:
                raise VerificationError('cannot identify the file for an added line in the staged diff')
            content = line[1:].decode('utf-8', errors='replace')
            for kind, pattern in SECRETS:
                for match in pattern.finditer(content):
                    findings.add((path, kind, redact(match.group())))
    return sorted(findings)


def pathspec_input(paths):
    return b''.join(os.fsencode(path) + b'\0' for path in paths)


def staging_git(git, *args, error):
    result = git(*args)
    if result.returncode:
        raise VerificationError(error)
    return result.stdout


def staging_checkout(cwd, clone, executable):
    # Record the real Git directories before the worker can change checkout files.
    start = clone if cwd.is_relative_to(clone) else cwd
    marker = start / '.git'
    if not (marker.is_file() or (marker / 'HEAD').is_file()):
        return None
    if executable is None:
        raise VerificationError('Git metadata appeared after launcher preflight')
    discover = partial(launcher_git, start, executable=executable)

    def location(option):
        return Path(os.fsdecode(staging_git(discover, 'rev-parse', '--path-format=absolute', option,
                                           error='cannot locate the checkout Git metadata').removesuffix(b'\n'))).resolve()

    return executable, location('--show-toplevel'), location('--git-common-dir'), location('--absolute-git-dir')


def stage_changes(git, checkout, task, model, *, verify):
    status = staging_git(git, '--no-optional-locks', 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                         '--ignore-submodules=dirty', error='cannot read Git status')
    entries = iter(os.fsdecode(status).split('\0'))
    paths = set()
    for entry in filter(None, entries):
        paths.add(entry[3:].rstrip('/'))
        if 'R' in entry[:2] or 'C' in entry[:2]:
            source = next(entries)
            if 'R' in entry[:2]:
                paths.add(source)
    if not paths:
        return None
    if verify:
        blocked = sorted(path for path in paths if Path(path).parts[0] == '.korolev'
                         or sandbox.credential_name(Path(path).name))
        if blocked:
            raise VerificationError('\n'.join(f'protected path {path!r}' for path in blocked))

    # An already-staged deletion is absent from both the worktree and the index.
    # It needs verification, but passing it to git add would fail the whole batch.
    indexed = staging_git(git, 'ls-files', '--cached', '-z', error='cannot read the staging index')
    tracked = set(os.fsdecode(indexed).split('\0'))
    to_add = sorted(path for path in paths if path in tracked or os.path.lexists(checkout / path))
    if to_add and git('add', '--pathspec-from-file=-', '--pathspec-file-nul',
                      input=pathspec_input(to_add)).returncode:
        raise VerificationError('could not stage the collected paths')

    modes = staging_git(git, 'diff', '--cached', '--raw', '-z', '--no-renames', '--ignore-submodules=none',
                        error='cannot inspect staged file modes')
    for header in modes.split(b'\0')[::2]:
        if header and header.split(b' ', 2)[1] == b'160000':
            raise VerificationError('new or changed staged Git links are unsupported')

    findings = staged_secrets(git) if verify else ()
    if findings:
        raise VerificationError('\n'.join(f'{kind} in {path!r} ({sample})' for path, kind, sample in findings))
    title = next((line.lstrip('# ').strip() for line in task.splitlines() if line.startswith('#')), 'Korolev builder change')
    staging_git(git, '-c', 'user.name=Korolev builder', '-c', 'user.email=korolev@localhost',
                'commit', '-q', '-m', f'{title[:72]}\n\nBuilt by Codex {model} via Korolev run_codex.py.',
                error='could not commit the staged paths')
    return staging_git(git, 'rev-parse', 'HEAD', error='cannot read the staged commit').strip().decode('ascii')


@contextmanager
def kept_staging_pack(git, objects, directory, commit, old):
    # The parent is already reachable. Include everything else, even objects
    # that staging read from an alternate or that Git already packed locally.
    revisions = f'{commit}\n' + (f'^{old}\n' if old else '')
    base = directory / 'import'
    result = git('pack-objects', '--revs', '--no-sparse', str(base), input=revisions.encode('ascii'))
    digest = result.stdout.strip()
    if result.returncode or not re.fullmatch(rb'[0-9a-f]{%d}' % len(commit), digest):
        raise VerificationError('cannot pack the staging commit for publication')
    pack_id = digest.decode('ascii')
    pack_dir = objects / 'pack'
    pack_dir.mkdir(exist_ok=True)
    candidate = directory / 'import.keep'
    candidate.touch()
    keep = pack_dir / f'pack-{pack_id}.keep'
    # Publish Git's keep marker first. Exclusive links also refuse an existing
    # pack that a repack could already have selected for deletion.
    os.link(candidate, keep)
    imported = []
    complete = False
    try:
        for suffix in ('pack', 'idx'):
            source, target = directory / f'import-{pack_id}.{suffix}', keep.with_suffix(f'.{suffix}')
            os.link(source, target)
            imported.append((source, target))
        complete = True
        yield
    finally:
        if not os.path.samestat(keep.lstat(), candidate.stat()):
            raise VerificationError('the staging pack keep changed during publication')
        if not complete:
            for source, target in reversed(imported):
                if not os.path.samestat(target.lstat(), source.stat()):
                    raise VerificationError('cannot verify an incomplete staging pack link')
                target.unlink()
        keep.unlink()


def publication_head(host, metadata):
    with sandbox.git_source(None, metadata / 'HEAD') as head:
        binding = head.read(4097).removesuffix(b'\n') if head else b''
    if binding.startswith(b'ref: refs/heads/'):
        target = staging_git(host, 'symbolic-ref', '--quiet', 'HEAD', error='cannot resolve the checkout HEAD')
        if target.removesuffix(b'\n') == binding[5:]:
            return binding
    elif re.fullmatch(COMMIT_ID.encode(), binding):
        return binding
    raise VerificationError('unsupported checkout HEAD for publication')


@contextmanager
def prepared_publication(host, git_args, executable, pinned, common, metadata, binding, commit, old):
    error = 'cannot publish the staging commit because the checkout HEAD or ref changed or is unavailable'
    command = [executable, '-c', 'core.hooksPath=/dev/null', *git_args, 'update-ref', '--stdin']
    with subprocess.Popen(command, cwd=pinned, env={**HOST_GIT_ENV, 'GIT_COMMON_DIR': str(common)},
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        def exchange(data, answer):
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except BrokenPipeError as cause:
                raise VerificationError(error) from cause
            if process.stdout.readline() != answer:
                raise VerificationError(error)

        try:
            exchange(b'start\n', b'start: ok\n')
            update = f'update HEAD {commit} {old or "0" * len(commit)}\nprepare\n'
            exchange(update.encode('ascii'), b'prepare: ok\n')
            # The files backend locks HEAD and, for a symref, its target on prepare.
            if not (metadata / 'HEAD.lock').is_file() or publication_head(host, metadata) != binding:
                raise VerificationError(error)
            yield partial(exchange, b'commit\n', b'commit: ok\n')
        finally:
            with suppress(BrokenPipeError):
                process.stdin.close()  # An uncommitted transaction aborts at EOF.


def commit_changes(staging, cwd, task, model, *, checkout_fd, expected_common=None):
    verify = expected_common is not None
    if staging is None:
        if verify:
            raise VerificationError('cannot read the task worktree')
        return None
    executable, checkout, common, metadata = staging
    if verify and (checkout != cwd or common != Path(expected_common).resolve()
                   or metadata.parent != common / 'worktrees'):
        raise VerificationError('the task worktree Git metadata changed during the run')
    verify_directory(checkout, checkout_fd)
    if verify:
        verify_linked_checkout(checkout_fd, metadata)
    pinned = Path(f'/proc/{os.getpid()}/fd/{checkout_fd}')
    if metadata == common == checkout / '.git':
        metadata = common = pinned / '.git'
    git_args = (f'--git-dir={metadata}', f'--work-tree={pinned}')
    host = partial(launcher_git, pinned, *git_args,
                   executable=executable, env_overrides={'GIT_COMMON_DIR': str(common)})
    objects = common / 'objects'
    index, lockpath = metadata / 'index', metadata / 'index.lock'
    with (tempfile.TemporaryDirectory(prefix='korolev-stage-', dir=objects) as directory,
          tempfile.TemporaryDirectory(prefix='korolev-index-', dir=metadata) as index_storage,
          lockpath.open('xb') as lock):
        try:
            backend = host('config', '--get', 'extensions.refStorage')
            if backend.returncode != 1 and (backend.returncode != 0 or backend.stdout != b'files\n'):
                raise VerificationError('unsupported Git ref backend for publication')
            binding = publication_head(host, metadata)
            head = host('rev-parse', '--verify', '--quiet', 'HEAD')
            if (head.returncode not in (0, 1) or publication_head(host, metadata) != binding
                    or (head.returncode == 1 and not binding.startswith(b'ref: '))):
                raise VerificationError('cannot capture the checkout HEAD')
            old = head.stdout.strip().decode('ascii')
            view = Path(directory) / 'checkout'
            sandbox.copy_checkout(checkout_fd, view)
            fmt = staging_git(host, 'rev-parse', '--show-object-format', error='cannot read the Git object format')
            private = Path(directory) / 'git'
            private.mkdir()
            staging_git(partial(launcher_git, private, executable=executable), 'init', '--bare', '--quiet',
                        '--template=', f'--object-format={fmt.strip().decode("ascii")}', error='cannot initialize staging Git')
            alternate = '"' + ''.join(f'\\{byte:03o}' for byte in os.fsencode(objects)) + '"'
            filemode = host('config', '--type=bool', '--get', 'core.filemode').stdout.strip() or b'true'
            git = partial(launcher_git, view, f'--git-dir={private}', f'--work-tree={view}',
                          '-c', f'core.filemode={filemode.decode("ascii")}',
                          executable=executable, env_overrides={'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternate})
            if old:
                (private / 'HEAD').write_text(old + '\n', encoding='ascii')
            backup, prepared_index = (Path(index_storage) / name for name in ('original', 'prepared'))
            original = sandbox.git_entry_identity(None, index)
            if original is not None:
                if not stat.S_ISREG(original[2]):
                    raise VerificationError('the checkout index must be a regular file')
                os.link(index, backup, follow_symlinks=False)
                linked = sandbox.git_entry_identity(None, backup)
                if linked is None or linked[:-1] != original[:-1] or linked != sandbox.git_entry_identity(None, index):
                    raise VerificationError('cannot verify the checkout index backup')
                original = linked
                shutil.copyfile(backup, private / 'index')
                shared = staging_git(host, 'rev-parse', '--path-format=absolute', '--shared-index-path',
                                     error='cannot locate the split index').removesuffix(b'\n')
                if shared:
                    path = Path(os.fsdecode(shared))
                    shutil.copyfile(path, private / path.name)
                    staging_git(git, 'update-index', '--no-split-index', error='cannot expand the split index')
            exclude = common / 'info' / 'exclude'
            if exclude.is_file() and not exclude.is_symlink():
                (private / 'info').mkdir(exist_ok=True)
                shutil.copyfile(exclude, private / 'info' / 'exclude')
            verify_directory(checkout, checkout_fd)
            commit = stage_changes(git, view, task, model, verify=verify)
            if commit is None:
                return None
            shutil.copyfile(private / 'index', prepared_index)
            # Finish the ref/index transaction (or rollback) before handling termination.
            signals = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                with kept_staging_pack(git, objects, Path(directory), commit, old):
                    staging_git(host, 'rev-list', '--objects', '--quiet', '--missing=error', commit,
                                error='the staging commit is not connected in the destination object store')
                    with prepared_publication(host, git_args, executable, pinned, common, metadata, binding,
                                              commit, old) as publish_ref:
                        verify_directory(checkout, checkout_fd)
                        if verify:
                            verify_linked_checkout(checkout_fd, metadata)
                        if (sandbox.git_entry_identity(None, index) != original
                                or not os.path.samestat(lockpath.lstat(), os.fstat(lock.fileno()))):
                            raise VerificationError('the checkout index or its lock changed during publication')
                        prepared = prepared_index.stat()
                        prepared_index.replace(index)
                        try:
                            publish_ref()
                        except (OSError, VerificationError):
                            if not os.path.samestat(index.lstat(), prepared):
                                raise VerificationError('cannot verify the checkout index for rollback')
                            if original is None:
                                index.unlink()
                            else:
                                backup.replace(index)
                            raise
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, signals)
            return commit
        finally:
            if lockpath.exists() and os.path.samestat(lockpath.lstat(), os.fstat(lock.fileno())):
                lockpath.unlink()


def verify_directory(path, fd):
    if not os.path.samestat(os.fstat(fd), path.lstat()):
        raise OSError(f'directory changed during launch: {path}')


def protected_output_dirs(out, requested_out, checkouts, project, git, deadline):
    roots = {root.resolve() for root, *_ in checkouts.values()}
    metadata = {common.resolve() for _, common, *_ in checkouts.values()}
    enclosing = {candidate for root in roots for candidate in git_metadata_candidates(root.parent)}
    for root, marker in sorted(enclosing):
        roots.add(root.resolve())
        common, _ = verified_git_common_dir(git, root, marker)
        metadata.add(common)
    if project is not None:
        roots.add(Path(project['folder']).resolve())
        metadata.add(Path(project['git_common_dir']).resolve())
    metadata.update(objects.resolve() for common in tuple(metadata)
                    if (objects := common / 'objects').is_symlink() and objects.is_dir())
    roots.update(metadata)
    if not any(out.is_relative_to(root) for root in roots):
        for path in chain((out, *out.parents), output_route(requested_out)):
            if any(path.is_relative_to(root) for root in metadata):
                raise ValueError(OUTPUT_GIT_ERROR)
            if path.name == '.git' or plausible_git_metadata(path):
                if git is None:
                    git = partial(bounded_git, executable=trusted_git_executable(), deadline=deadline)
                source = path.parent if path.name == '.git' else path
                verified_git_common_dir(git, source, path)
                raise ValueError(OUTPUT_GIT_ERROR)
    return roots


def output_route(path):
    # Visit intermediate symlink targets as well as the spelled-out components.
    # The output leaf is checked separately without following it.
    current = Path(path.anchor)
    pending = deque(path.parts[1:])
    followed = 0
    while pending:
        part = pending.popleft()
        if part == '..':
            current = current.parent
            continue
        current /= part
        yield current
        if pending and current.is_symlink():
            target = current.readlink()
            followed += 1
            if followed > 40:
                raise OSError('too many symlinks in worker output path')
            if target.is_absolute():
                current = Path(target.anchor)
                pending.extendleft(reversed(target.parts[1:]))
            else:
                current = current.parent
                pending.extendleft(reversed(target.parts))


def metadata_exists(path):
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def plausible_git_metadata(path):
    objects = path / 'objects'
    return (metadata_exists(path / 'HEAD') and (metadata_exists(objects) or metadata_exists(path / 'commondir'))
            or metadata_exists(path / 'config') and objects.is_dir() and (path / 'refs').is_dir())


def git_metadata_candidates(path):
    # Git can discover a worktree or bare checkout above the starting directory.
    for parent in (path, *path.parents):
        marker = parent / '.git'
        if metadata_exists(marker) and (
                marker.is_symlink() or not marker.is_dir() or plausible_git_metadata(marker)):
            yield parent, marker
        elif plausible_git_metadata(parent):
            yield parent, parent


def check_output_leaf(out, out_fd):
    with suppress(FileNotFoundError):
        if stat.S_ISLNK(os.stat(out.name, dir_fd=out_fd, follow_symlinks=False).st_mode):
            raise ValueError('worker output cannot be a symlink')


def require_finished_writers():
    # Include adopted descendants that left the worker's process group.
    with suppress(ChildProcessError):
        while os.waitpid(-1, os.WNOHANG | WAIT_ALL_CHILDREN)[0]:
            pass
        raise OSError('worker descendants are still running')


def report_chunks(source, size):
    if size > REPORT_LIMIT:
        raise ValueError('report exceeds the logical byte limit')
    for offset in range(0, size, REPORT_CHUNK):
        length = min(size - offset, REPORT_CHUNK)
        data = source.read(length)
        if len(data) != length:
            raise ValueError('report shrank during copying')
        yield data
    if source.read(1) or os.fstat(source.fileno()).st_size != size:
        raise ValueError('report grew or shrank during copying')


def move_deliverable(outbox_fd, out, out_fd, receipt):
    """Publish a nonempty worker file without following links out of its sandbox."""
    require_finished_writers()
    with ExitStack() as stack:
        try:
            source, info = stack.enter_context(open_context_file(outbox_fd, Path('deliverable.md')))
        except (FileNotFoundError, ValueError):
            return
        if not info.st_size:
            return
        directory = Path(tempfile.mkdtemp(prefix='.report-', dir=fd_path(out_fd)))
        staging_fd = stack.enter_context(open_directory(Path(directory.name), parent=out_fd))
        staged = 'report'
        owned = None
        try:
            digest = hashlib.sha256()
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=staging_fd)
            with os.fdopen(fd, 'wb') as sink:
                owned = os.fstat(fd)
                for data in report_chunks(source, info.st_size):
                    sink.write(data)
                    digest.update(data)
                copied = sink.tell()
                sink.flush()
                if os.fstat(source.fileno()).st_mtime_ns != info.st_mtime_ns:
                    raise ValueError('report changed during copying')
                verify_directory(out.parent, out_fd)
                check_output_leaf(out, out_fd)
                verify_directory(directory, staging_fd)
                if not os.path.samestat(owned, os.stat(staged, dir_fd=staging_fd, follow_symlinks=False)):
                    raise OSError('staged report changed before publication')
                with egress_proxy.blocked_cleanup_signals():
                    os.replace(staged, out.name, src_dir_fd=staging_fd, dst_dir_fd=out_fd)
                    receipt.update(out_bytes=copied, out_sha256=digest.hexdigest())
        finally:
            with suppress(OSError):
                if owned is not None and os.path.samestat(
                        owned, os.stat(staged, dir_fd=staging_fd, follow_symlinks=False)):
                    os.unlink(staged, dir_fd=staging_fd)
            with suppress(OSError):
                verify_directory(directory, staging_fd)
                os.rmdir(directory.name, dir_fd=out_fd)


def verified_log_path(path, fd, runs_fd):
    if path is None:
        return None
    try:
        verify_directory(path.parent, runs_fd)
        if os.path.samestat(path.lstat(), os.fstat(fd)):
            return str(path)
    except OSError:
        pass
    print(f'WARNING: cannot verify run log path: {path}', file=sys.stderr)
    return None


def main():
    with (open_directory(sandbox.INSTALL_ROOT) as install_fd, open_directory(SKILL) as skill_fd,
          ExitStack() as admission):
        return launch(skill_fd, os.fstat(install_fd), admission)


def launch(skill_fd, install_identity, admission):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--role', required=True)
    parser.add_argument('--product', required=True)
    parser.add_argument('--prompt', required=True, help='prompt or task file')
    parser.add_argument('--out', required=True, help='deliverable file (relative to the product folder)')
    location = parser.add_mutually_exclusive_group()
    location.add_argument('--cwd', help='working directory (default: the project folder). '
                                        'Registered writes require a task worktree.')
    location.add_argument('--task', help='run in this registered task worktree')
    parser.add_argument('--write', action='store_true', help='allow writes in a registered task worktree or a legacy clone')
    parser.add_argument('--sha', help='commit for a read-only run (default: HEAD)')
    parser.add_argument('--prompt-only', action='store_true', help='required for read-only runs without a Git checkout')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--timeout', type=int, help='minutes')
    parser.add_argument('--context', nargs='+', action='extend', default=[],
                        help='extra files inside the product folder, relative to that folder or absolute')
    args = parser.parse_args()

    if not valid_product(args.product):
        parser.error('invalid product name')
    products = product_dir('').resolve()
    pdir = products / args.product
    if pdir.is_symlink() or not pdir.is_dir():
        parser.error('product must be a directory at its canonical name under products/')
    role = roles().get(args.role)
    if not role:
        sys.exit(f'unknown role {args.role}; known: {", ".join(roles())}')
    project = read_projects().get(args.product)
    write = args.write or role.get('write', False)
    if write and args.sha is not None:
        parser.error('--sha is only for read-only roles')
    if args.sha == '':
        parser.error('--sha must not be empty')
    if args.prompt_only and (write or args.sha is not None or args.task is not None):
        parser.error('--prompt-only cannot be used with a write, --sha or --task')
    if code_review_prompt(args.prompt) and (write or args.sha is None):
        parser.error('the code-review template requires a read-only role and --sha')
    if args.task is not None:
        if project is None:
            sys.exit('--task requires a registered project; run onboard.py attach first')
        record = read_tasks(args.product).get(args.task)
        if record is None:
            print(f'task {args.task!r} is not recorded; create it with tasks.py new', file=sys.stderr)
            return 77 if write else 1
        cwd = Path(record['path']).resolve()
    else:
        default_cwd = project['folder'] if project is not None else work_repo(args.product)
        cwd = Path(args.cwd or default_cwd).expanduser().resolve()
    repo_path = Path(project['folder']) if project is not None else work_repo(args.product)
    repo = repo_path.resolve()
    deadline = time.monotonic() + REVIEW_SETUP_SECONDS
    try:
        requested_out = pdir / args.out
        parent = requested_out.parent.resolve()
        out = parent.parent if requested_out.name == '..' else parent / requested_out.name
        if not out.parent.is_relative_to(pdir) or out.is_relative_to((pdir / '.runs').resolve()):
            raise ValueError('worker output must be inside the product folder and outside .runs')
        if write:
            sandbox.check_install_overlap(cwd, repo if project is None and cwd.is_relative_to(repo) else None)
        git_executable = git = None
        task_workspace = False
        paths = [cwd]
        if os.path.lexists(repo_path):
            paths.append(repo)
            # A dangling repo symlink can have Git metadata above either path.
            if not os.path.lexists(repo):
                paths.append(repo_path.absolute())
        else:
            paths.append(repo.parent)
        candidates = {path: candidate for path in dict.fromkeys(paths)
                      if (candidate := next(git_metadata_candidates(path), None))}
        if project is not None or candidates:
            git_executable = trusted_git_executable()
            git = (partial(launcher_git, executable=git_executable) if write else
                   partial(bounded_git, executable=git_executable, deadline=deadline))
        checkouts = {path: (root, *verified_git_common_dir(git, root, marker,
                                                         source=path if path != repo.parent or path == cwd else None,
                                                         resources=None if write else admission))
                     for path, (root, marker) in candidates.items()}
        if project is not None:
            task_workspace = (is_task_worktree(args.product, cwd, executable=git_executable) if write else
                              review_task_workspace(args.product, cwd, project, git))
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Worker sandbox refused to run: {error}', file=sys.stderr)
        return 78
    if not task_workspace and ((project is not None and write) or args.task is not None):
        print('a registered task worktree is required; use tasks.py new and pass --task', file=sys.stderr)
        return 77 if write else 1
    try:
        with ExitStack() as locks:
            if write:
                locks.enter_context(run_lock(f'{args.product}-{args.role}-write'))
                if task_workspace:
                    locks.enter_context(run_lock(task_lock_name(args.product, cwd)))
                    if not is_task_worktree(args.product, cwd, executable=git_executable):
                        print('the task worktree is unavailable; use tasks.py new', file=sys.stderr)
                        return 77
            return run(args, role, pdir, cwd, write, locks, skill_fd=skill_fd, install_identity=install_identity,
                       git_executable=git_executable, git=git, deadline=deadline, project=project,
                       task_workspace=task_workspace, checkouts=checkouts, out=out, requested_out=requested_out)
    except LockBusyError as error:
        print(error, file=sys.stderr)
        return 75


def stop_worker(proc):
    if proc.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@contextmanager
def worker_cleanup(on_interrupt, set_interruptible):
    try:
        with ExitStack() as resources:
            resources.callback(set_interruptible, False)
            set_interruptible(True)
            yield resources
    except SystemExit as error:
        on_interrupt(error)


def run(args, role, pdir, cwd, write, resources, *, skill_fd, install_identity, git_executable, git, deadline, project,
        task_workspace, checkouts, out, requested_out):
    model = args.model or role['model']
    effort = args.effort or role['effort']
    timeout = 60 * (args.timeout or role.get('timeout_minutes', 60))
    owner = Path(project['folder']).resolve() if project is not None else None
    repo = owner or work_repo(args.product).resolve()
    source = repo if args.task is not None else cwd
    git_backed = not write and source in checkouts
    common = project['git_common_dir'] if project is not None else None
    expected_common_fd = checkouts[source][2] if git_backed else None
    if git_backed and args.prompt_only:
        print('Worker sandbox failed: --prompt-only requires a source without a Git repository', file=sys.stderr)
        return 78
    workspace_root = repo.parent if project is None else None
    if git_backed and project is None:
        legacy = Path(os.path.abspath(work_repo(args.product)))
        try:
            if os.path.lexists(legacy):
                info = legacy.lstat()
                legacy = workspace_root / legacy.name
                if (not stat.S_ISDIR(info.st_mode) or not os.path.samestat(info, legacy.lstat())
                        or legacy not in checkouts or checkouts[legacy][0] != legacy):
                    raise OSError('cannot verify the legacy review checkout')
                top = review_git(git, legacy, 'rev-parse', '--show-toplevel',
                                 error='cannot verify the legacy review checkout')
                if Path(os.fsdecode(top)).resolve() != legacy:
                    raise OSError('cannot verify the legacy review checkout')
                legacy_fd = checkouts[legacy][2]
                if not os.path.samestat(os.fstat(legacy_fd), os.fstat(expected_common_fd)):
                    raise OSError('the review source belongs to a different Git repository')
                expected_common_fd = legacy_fd
        except (OSError, RuntimeError, ValueError) as error:
            print(f'Worker sandbox failed: {error}', file=sys.stderr)
            return 78
    try:
        if requested_out.name in ('', '..') or normalized_name(requested_out.name) == 'product.json':
            raise ValueError('worker output must name a file outside reserved product metadata')
        if out.parent.is_relative_to(sandbox.INSTALL_ROOT):
            raise ValueError('worker output must be outside the Korolev installation')
        protected = protected_output_dirs(out, requested_out, checkouts, project, git, deadline)
        runs = pdir / '.runs'
        if next(git_metadata_candidates(runs), None) or any(
                path.is_relative_to(root) for root in protected for path in (out, runs)):
            raise ValueError(OUTPUT_GIT_ERROR)
        identities = [(install_identity, 'worker output must be outside the Korolev installation')]
        identities.extend((root.stat(), OUTPUT_GIT_ERROR) for root in protected)
        product_fd = resources.enter_context(open_directory(pdir, protected=identities))
        with suppress(FileNotFoundError), open_directory(Path(runs.name), parent=product_fd, protected=identities):
            pass
        out_fd = resources.enter_context(open_directory(out.parent, create=True, protected=identities))
        check_output_leaf(out, out_fd)
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Invalid worker output: {error}', file=sys.stderr)
        return 77
    touch_heartbeat(args.product, f'Starting a {args.role} job')
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{args.role}'

    try:
        runs_fd = resources.enter_context(private_runs_dir(product_fd))
        run_storage = fd_path(runs_fd)
        stored, task, paths = build_context_bundle(pdir, product_fd, skill_fd, runs_fd, stamp, args.prompt, args.context)
        bundle = runs / stored.name
        bundle_fd = resources.enter_context(open_directory(Path(bundle.name), parent=runs_fd))
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Cannot build worker context: {error}', file=sys.stderr)
        return 1
    stamp = bundle.name.removesuffix('-ctx')
    config = sandbox.worker_config()
    sandboxed = sandbox.enabled(config)
    try:
        network = sandbox.network_mode(config)
    except OSError as error:
        print(f'Worker sandbox failed: {error}', file=sys.stderr)
        return 78
    codex_mode = 'workspace-write' if write else 'read-only'
    if sandboxed:
        codex_mode = 'danger-full-access'
    isolated = sandboxed or isolate_codex_home()
    # Bubblewrap's PID-namespace reaper must start without the launcher env too.
    # The sandbox supplies the worker environment.
    env = {}
    egress_log = egress_fd = None
    reviewed_sha = lease_fd = proc = None
    sandbox_warnings = [sandbox.HOST_NETWORK_WARNING] if network == 'host' else []
    interrupted, interruptible = None, False

    def remember_interrupt(error):
        nonlocal interrupted
        if proc is None or write:
            raise error
        interrupted = interrupted or error.code

    def review_signal(signum, frame):
        remember_interrupt(SystemExit(128 + signum))
        if interruptible:
            exit_on_signal(signum, frame)

    def set_interruptible(value):
        nonlocal interruptible
        interruptible = value

    with worker_cleanup(remember_interrupt, set_interruptible) as worker_resources:
        try:
            clone = cwd if task_workspace or not repo.is_dir() or (project is None and (git_backed or args.prompt_only)) else repo
            if (sandboxed or not write) and not cwd.is_relative_to(clone):
                raise OSError(f'working directory is outside the project clone: {cwd}')
            review_fds = review_cwd_fd = None
            if not write:
                if (git_backed or project is not None) and not sandboxed:
                    raise OSError('Git reviews require the worker sandbox to hide the owner folder')
                if not git_backed and (project is not None or not args.prompt_only):
                    raise OSError('a Git repository is required for review; use --prompt-only for analysis')
                if task_workspace:
                    # Keep the registered task's credential preflight for an
                    # explicit task selection, even though it will not be mounted.
                    sandbox.credential_masks(clone)
                snapshot, reviewed_sha, review_common, source_root, relative, review_fds, lease_fd = worker_resources.enter_context(
                    review_worktree(args.product, source, args.sha, prompt_only=args.prompt_only,
                                    executable=git_executable, expected_common=common,
                                    expected_common_fd=expected_common_fd, deadline=deadline,
                                    workspace_root=workspace_root))
                if git_backed:
                    owner, common = owner or source_root, review_common
                elif sandboxed and clone == repo:
                    # Preserve the legacy clone preflight for prompt-only runs.
                    sandbox.socket_masks({repo: ('--ro-bind', str(repo))}, on_budget_exceeded=(
                        sandbox_warnings.append if network == 'isolated' else None))
                clone, cwd = snapshot, snapshot / relative
                if not cwd.is_relative_to(clone) or not cwd.is_dir():
                    raise OSError('the working directory is unavailable in the reviewed commit')
                if review_fds is not None:
                    review_cwd_fd = worker_resources.enter_context(open_directory(relative, parent=review_fds[0]))
                    verify_directory(cwd, review_cwd_fd)
            cmd = ['codex', 'exec', '-C', str(cwd), '-m', model, '-c', f'model_reasoning_effort={effort}',
                   '-s', codex_mode, '--ephemeral', '--skip-git-repo-check', '--json']
            egress_proxy.prune_stale_sockets(run_storage)
            auth_fd = None
            mount_fds = ()
            outbox = runs / f'{stamp}-out'
            os.mkdir(outbox.name, mode=0o700, dir_fd=runs_fd)
            outbox_fd = resources.enter_context(open_directory(Path(outbox.name), parent=runs_fd))
            staging = staging_checkout(cwd, clone, git_executable) if write else None
            staging_fd = resources.enter_context(sandbox.git_root(staging[1])) if staging is not None else None
            if sandboxed:
                worker_home = worker_resources.enter_context(sandbox.private_codex_home())
                auth_fd = worker_resources.enter_context(sandbox.worker_auth())
                if network == 'isolated':
                    patterns = egress_proxy.allowlist(config.get('worker_egress_allow', []))
                    limits = egress_proxy.limits(config)
                deliverable = outbox / 'deliverable.md'
                git_metadata = worker_resources.enter_context(tempfile.TemporaryDirectory(dir='/tmp',
                                                                                           prefix='korolev-git-'))
                checkout_fd = review_fds[0] if review_fds is not None else (
                    staging_fd if staging is not None and staging[1] == clone
                    else worker_resources.enter_context(sandbox.git_root(clone)))
                layout = dict(clone=clone, product_dir=pdir, codex_home=worker_home, write=write,
                              outbox=outbox, context_dir=bundle, git_metadata_dir=git_metadata, config=config,
                              git_executable=git_executable, context_fd=bundle_fd, outbox_fd=outbox_fd)
                if owner is not None:
                    layout.update(owner_folder=owner, git_common_dir=common)
                if git_backed:
                    layout.update(git_deadline=deadline, review_git_fds=review_fds[1:])
                # Check the actual mount layout without invoking the worker CLI twice.
                sandbox.selftest(**layout, check_version=False, checkout_fd=checkout_fd if git_backed else None)
                proxy_socket = None
                if network == 'isolated':
                    egress_log = runs / f'{stamp}.egress.jsonl'
                    egress_fd = os.open(egress_log.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=runs_fd)
                    resources.callback(os.close, egress_fd)
                    proxy_socket = sandbox.worker_proxy(worker_resources, fd_path(egress_fd), patterns, **limits)
                worker_cmd = [*cmd, '-o', str(deliverable), '-']
                bind_fds = []
                cmd = sandbox.wrap(worker_cmd, **layout,
                                   proxy_socket=proxy_socket, auth_fd=auth_fd, warnings=sandbox_warnings,
                                   checkout_fd=checkout_fd, bind_stack=worker_resources, bind_fds=bind_fds)
                if reviewed_sha is not None:
                    offset = len(cmd) - len(worker_cmd)
                    cmd[offset:offset] = ['/bin/sh', '-eu', '-c', '''
                        export GIT_NO_REPLACE_OBJECTS=1
                        if [ "$("$3" -C "$2" rev-parse HEAD)" != "$1" ]; then
                            echo "review checkout HEAD does not match the pinned commit" >&2
                            exit 78
                        fi
                        shift 3
                        exec "$@"
                    ''', 'verify-review-head', reviewed_sha, str(cwd), str(git_executable)]
                mount_fds = sandbox.mount_fds(cmd, checkout_fd, bundle_fd, outbox_fd, *bind_fds)
                if lease_fd is not None:
                    cmd[1:1] = ['--sync-fd', str(lease_fd)]
                    mount_fds += (lease_fd,)
            else:
                verify_directory(out.parent, out_fd)
                # Tool subprocesses can close inherited fds. The launcher keeps
                # these directories open by its own PID until the job exits.
                worker_fds = Path(f'/proc/{os.getpid()}/fd')
                bundle = worker_fds / str(bundle_fd)
                deliverable = worker_fds / str(outbox_fd) / 'deliverable.md'
                cmd[0] = shutil.which('codex') or cmd[0]
                cmd += ['-o', str(deliverable), '-']
                home = Path.home().resolve()
                sandbox.check_install_overlap(codex_home(create=False) if isolated else os.environ.get('CODEX_HOME'))
                worker_home = codex_home() if isolated else os.environ.get('CODEX_HOME')
                env = sandbox.worker_env(config, home=home, toolchains=sandbox.toolchain_dirs(home),
                                         codex_home=worker_home, sandboxed=False)
                if args.prompt_only:
                    env = {key: value for key, value in env.items()
                           if key == 'GIT_TERMINAL_PROMPT' or not key.upper().startswith('GIT_')}
        except sandbox.CredentialMaskError as error:
            print(f'Worker sandbox refused to run: {error}', file=sys.stderr)
            return 78
        except sandbox.BubblewrapError as error:
            print(f'Worker sandbox failed: {error}\n{sandbox.disable_hint()}', file=sys.stderr)
            return 78
        except (OSError, VerificationError) as error:
            print(f'Worker sandbox failed: {error}', file=sys.stderr)
            return 78
        prompt_task = f'Commit under review: {reviewed_sha}\n\n{task}' if reviewed_sha else task
        try:
            display = sanitize_inventory(display_name(args.product), 'product display name')
        except (TypeError, ValueError):
            display = args.product
        prompt = HEADER.format(role=args.role, display=display, cwd=cwd, bundle=bundle,
                               write_rule=('You may edit files in the repository. Do NOT run git commit, add, reset or push: '
                                           '.git is read-only in your sandbox. Leave finished changes in the working tree; '
                                           'the Korolev launcher commits them after you exit.' if write
                                           else 'READ-ONLY: do not modify any file.')) + prompt_task
        if paths:
            prompt += '\n\nContext files in the bundle:\n' + '\n'.join(f'- {bundle / path}' for path in paths)
        events = runs / f'{stamp}.events.jsonl'
        started = now()
        begin = time.time()
        timed_out = False
        sink = resources.enter_context((run_storage / events.name).open('xb'))
        stored_events = fd_path(sink.fileno())
        stderr_log = resources.enter_context(tempfile.TemporaryFile(dir=run_storage))

        @resources.callback
        def append_worker_stderr():
            stderr_log.seek(0)
            shutil.copyfileobj(stderr_log, sink)

        if reviewed_sha is not None:
            try:
                review_remaining(deadline)
                verify_directory(clone, checkout_fd)
                verify_directory(cwd, review_cwd_fd)
            except OSError as error:
                print(f'Worker sandbox failed: {error}', file=sys.stderr)
                return 78
        if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            print('Cannot establish report writer containment', file=sys.stderr)
            return 77
        if not write:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                resources.callback(signal.signal, signum, signal.signal(signum, review_signal))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=sink, stderr=stderr_log,
                                text=True, start_new_session=True, env=env,
                                pass_fds=mount_fds + ((auth_fd,) if auth_fd is not None else ()))
        try:
            proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except SystemExit as error:
            remember_interrupt(error)
        finally:
            if write:
                with egress_proxy.blocked_cleanup_signals():
                    stop_worker(proc)
            else:
                while True:
                    try:
                        stop_worker(proc)
                        break
                    except SystemExit as error:
                        remember_interrupt(error)
    worker_exit = proc.returncode
    receipt = dict(out_bytes=0, out_sha256=None)
    with worker_cleanup(remember_interrupt, set_interruptible):
        try:
            if interrupted is None:
                move_deliverable(outbox_fd, out, out_fd, receipt)
        except (OSError, ValueError) as error:
            print(f'Cannot publish worker output: {error}', file=sys.stderr)
            if interrupted is None:
                return 77
    terminal_limit, retry_after, saw_limit = False, None, False
    usage, turns, commands_run = None, 0, 0
    with worker_cleanup(remember_interrupt, set_interruptible):
        terminal_limit, retry_after, saw_limit = event_limit(stored_events)
        usage, turns, commands_run = read_event_stats(stored_events)
        if not saw_limit:
            stderr_log.seek(0)
            saw_limit = any(limit_details(line.decode(errors='replace'), begin)[0] for line in stderr_log)
    code = interrupted or (124 if timed_out else worker_exit)
    # A negative worker return code is a signal termination.
    limit_hit = terminal_limit and worker_exit > 0 and not timed_out and interrupted is None
    if limit_hit:
        code = 75
    commit = None
    if write and code == 0:
        try:
            commit = commit_changes(staging, cwd, task, model, checkout_fd=staging_fd,
                                    expected_common=common if task_workspace else None)
        except (OSError, VerificationError) as error:
            if task_workspace:
                print(f'Task verification failed:\n{error}', file=sys.stderr)
                code = 4
            else:
                print(f'WARNING: auto-commit failed: {error}', file=sys.stderr)
    if code == 0 and receipt['out_bytes'] == 0:
        print('WARNING: Codex exited 0 but wrote no deliverable', file=sys.stderr)
        code = 3
    record = {'role': args.role, 'product': args.product, 'model': model, 'effort': effort,
              'codex_home': 'isolated' if isolated else 'user',
              'sandbox': 'bwrap' if sandboxed else 'off',
              'network': network,
              'codex_sandbox': f'{codex_mode} (inside bwrap)' if sandboxed else codex_mode,
              'started': started, 'ended': now(),
              'seconds': round(time.time() - begin), 'exit': code, 'worker_exit': worker_exit, 'timed_out': timed_out,
              'out': str(out), **receipt,
              'usage': usage, 'commit': commit,
              'uncached_input_tokens': None, 'commands_run': commands_run}
    if sandbox_warnings:
        record['sandbox_warnings'] = list(dict.fromkeys(sandbox_warnings))
    if not write:
        record['reviewed_sha'] = reviewed_sha
    if limit_hit:
        record['limit_hit'] = True
        if retry_after is not None:
            record['retry_after'] = retry_after
    elif saw_limit or terminal_limit:
        record['limit_diagnostic'] = True
        print('WARNING: Usage-limit diagnostic did not establish a terminal usage-limit stop', file=sys.stderr)
    input_tokens = (usage or {}).get('input_tokens')
    if input_tokens is not None:
        uncached_input_tokens = input_tokens - usage.get('cached_input_tokens', 0)
        record['uncached_input_tokens'] = uncached_input_tokens
        if turns:
            record['input_tokens_per_turn'] = input_tokens / turns
        thresholds = warning_thresholds()
        threshold = thresholds['context_warn_tokens']
        if input_tokens > threshold:
            warning = f'large context: {input_tokens:,} input tokens exceeded the {threshold:,} token threshold'
            record['warning'] = warning
            print(f'WARNING: {warning}', file=sys.stderr)
        uncached_threshold = thresholds['uncached_warn_tokens']
        if uncached_input_tokens > uncached_threshold:
            warning = (f'large uncached input: {uncached_input_tokens:,} uncached input tokens exceeded '
                       f'the {uncached_threshold:,} token threshold')
            record['uncached_warning'] = warning
            print(f'WARNING: {warning}', file=sys.stderr)
    record['events'] = verified_log_path(events, sink.fileno(), runs_fd)
    record['egress_log'] = verified_log_path(egress_log, egress_fd, runs_fd)
    # The masked append commits the exit status observed at this point.
    with egress_proxy.blocked_cleanup_signals():
        record['exit'] = code = interrupted or code
        if interrupted is not None and record.pop('limit_hit', False):
            record.pop('retry_after', None)
            record['limit_diagnostic'] = True
        append_line(fd_path(product_fd) / 'USAGE.jsonl', record)
    print(json.dumps(record, ensure_ascii=False))
    return code


def exit_on_signal(signum, _frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, exit_on_signal)
    sys.exit(main())
