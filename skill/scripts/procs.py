#!/usr/bin/env python3
"""Inspect Korolev run locks and their recorded process identities."""
import argparse
import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME, git_common_dir, project_folder, read_projects, valid_product  # noqa: E402


class LockBusyError(Exception):
    """Another run holds the lock."""

    def __init__(self, message, holder=None):
        super().__init__(message)
        self.holder = holder


def _lock_path(name):
    if not name or Path(name).name != name:
        raise ValueError('expected a lock name without path components')
    return HOME / 'locks' / f'{name}.lock'


def _start_time(pid):
    stat = Path(f'/proc/{pid}/stat').read_bytes()
    # The parenthesized process name can contain spaces and right parentheses.
    return int(stat.rsplit(b')', 1)[1].split()[19])  # Field 22, counted from field 3.


def _write_owner(lock, **fields):
    pid = os.getpid()
    metadata = {'pid': pid, 'start_time': _start_time(pid), **fields}
    lock.truncate(0)
    lock.write(json.dumps(metadata) + '\n')
    lock.flush()


@contextmanager
def run_lock(name):
    """Hold an exclusive lock and yield its fd for a worker that must share it."""
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file in place so contenders always lock the same inode.
    with path.open('a+', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(f'lock {name!r} is busy') from error
        _write_owner(lock)
        # Closing releases the lock once any inheriting worker closes its fd too.
        yield lock.fileno()


def _repo_lock_name(common_dir):
    canonical = Path(common_dir).resolve()
    return hashlib.sha256(os.fsencode(canonical)).hexdigest()[:16]


@contextmanager
def repo_lock(git_common_dir, *, session=None, label=None):
    """Yield the lock fd or raise LockBusyError with the current holder's record."""
    name = _repo_lock_name(git_common_dir)
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as lock:
        # Serialize publishing and reading the record, without waiting for the running session.
        with path.with_suffix('.guard').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LockBusyError(f'repository lock {name!r} is busy', _read_owner(name)) from error
            _write_owner(lock, session=session, label=label)
        yield lock.fileno()


def is_busy(name):
    """Return whether a run holds the exclusive lock, regardless of metadata."""
    try:
        with _lock_path(name).open('rb') as lock:
            try:
                # Status readers can probe the lock concurrently.
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def _read_owner(name):
    try:
        metadata = json.loads(_lock_path(name).read_text(encoding='utf-8'))
        pid, start_time = metadata['pid'], metadata['start_time']
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}
    if type(pid) is not int or pid <= 0 or type(start_time) is not int or start_time < 0:
        return {}
    return metadata


def _status(name, *, repo=False):
    result = {'name': name, 'busy': is_busy(name), 'pid': None, 'start_time': None,
              'same_process': False}
    metadata = _read_owner(name)
    if repo:
        result.update(session=metadata.get('session'), label=metadata.get('label'))
    if not metadata:
        return result
    pid, start_time = metadata['pid'], metadata['start_time']
    result.update(pid=pid, start_time=start_time)
    try:
        result['same_process'] = _start_time(pid) == start_time
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return result


def repo_status(common_dir):
    name = _repo_lock_name(common_dir)
    try:
        with _lock_path(name).with_suffix('.guard').open('rb') as guard:
            fcntl.flock(guard, fcntl.LOCK_SH)
            return _status(name, repo=True)
    except FileNotFoundError:
        return _status(name, repo=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', help='show lock state and recorded holder')
    status.add_argument('name')
    commands.add_parser('list', help='show all run locks, including released ones')
    repo = commands.add_parser('repo-lock', help='show the repository lock for a project')
    repo.add_argument('--product', required=True)
    repo.add_argument('--status', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = _status(args.name)
        elif args.command == 'repo-lock':
            if not valid_product(args.product):
                raise ValueError('expected a project name')
            entry = read_projects().get(args.product)
            common_dir = entry['git_common_dir'] if entry else git_common_dir(project_folder(args.product))
            result = repo_status(common_dir)
        else:
            result = [_status(path.stem) for path in sorted((HOME / 'locks').glob('*.lock'))]
    except (OSError, ValueError) as error:
        parser.exit(1, f'{parser.prog}: {error}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
