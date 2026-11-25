#!/usr/bin/env python3
"""Inspect Korolev run locks and their recorded process identities."""
import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME  # noqa: E402


class LockBusyError(Exception):
    """Another run holds the lock."""


def _lock_path(name):
    if not name or Path(name).name != name:
        raise ValueError('expected a lock name without path components')
    return HOME / 'locks' / f'{name}.lock'


def _start_time(pid):
    stat = Path(f'/proc/{pid}/stat').read_bytes()
    # The parenthesized process name can contain spaces and right parentheses.
    return int(stat.rsplit(b')', 1)[1].split()[19])  # Field 22, counted from field 3.


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
        pid = os.getpid()
        metadata = json.dumps({'pid': pid, 'start_time': _start_time(pid)})
        lock.truncate(0)
        lock.write(metadata + '\n')
        lock.flush()
        # Closing releases the lock once any inheriting worker closes its fd too.
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


def _status(name):
    result = {'name': name, 'busy': is_busy(name), 'pid': None, 'start_time': None,
              'same_process': False}
    try:
        metadata = json.loads(_lock_path(name).read_text(encoding='utf-8'))
        pid, start_time = metadata['pid'], metadata['start_time']
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return result
    if type(pid) is not int or pid <= 0 or type(start_time) is not int or start_time < 0:
        return result
    result.update(pid=pid, start_time=start_time)
    try:
        result['same_process'] = _start_time(pid) == start_time
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', help='show lock state and recorded holder')
    status.add_argument('name')
    commands.add_parser('list', help='show all run locks, including released ones')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = _status(args.name)
        else:
            result = [_status(path.stem) for path in sorted((HOME / 'locks').glob('*.lock'))]
    except (OSError, ValueError) as error:
        parser.exit(1, f'{parser.prog}: {error}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
