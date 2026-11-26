#!/usr/bin/env python3
"""Record coordinator progress or launch a coordinator under its run lock."""
import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (HOME, heartbeat_lock, parse_timestamp, read_heartbeat, timestamp,  # noqa: E402
                    valid_product, write_heartbeat)
from procs import LockBusyError, _start_time, run_lock  # noqa: E402

COORDINATOR_LOCK = 'coordinator'
STATUSES = ('working', 'idle', 'waiting_limit')
DEFAULT_RETRY_SECONDS = 30 * 60


def find_session(cwd):
    projects = Path.home() / '.claude' / 'projects' / str(cwd).replace('/', '-')
    newest = None
    for path in projects.glob('*.jsonl'):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            session = (info.st_mtime_ns, path.stem)
            if newest is None or session > newest:
                newest = session
    return newest[1] if newest else None


def process_identity(pid):
    try:
        return {'pid': pid, 'start_time': _start_time(pid)}
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return {'pid': pid}


def recorded_process_running(record):
    pid, start_time = record.get('pid'), record.get('start_time')
    return (bool(record.get('session')) and type(pid) is int and pid > 0 and type(start_time) is int
            and process_identity(pid).get('start_time') == start_time)


def set_status(args):
    cwd = Path.cwd()
    managed_session = os.environ.get('KOROLEV_COORDINATOR_SESSION')
    session = (find_session(cwd) or managed_session) if args.session == 'auto' else args.session or managed_session
    launcher_pid = os.environ.get('KOROLEV_COORDINATOR_PID', '')
    identity = process_identity(int(launcher_pid)) if launcher_pid.isdecimal() else {}
    with heartbeat_lock():
        previous = read_heartbeat()
        same_launcher = all(previous.get(key) == value for key, value in identity.items())
        if not args.session and same_launcher and session in (None, previous.get('session')):
            session = previous.get('session')
            cwd = previous.get('cwd') or cwd
        current = time.time()
        record = {'product': args.product, 'status': args.status, 'doing': args.doing,
                  'updated_at': timestamp(current), 'cwd': str(cwd)}
        if session:
            record['session'] = session
            if not identity and session == previous.get('session'):
                identity = {key: previous[key] for key in ('pid', 'start_time') if key in previous}
        record.update(identity)
        if args.status == 'waiting_limit':
            retry = parse_timestamp(args.retry_after) if args.retry_after else current + DEFAULT_RETRY_SECONDS
            record['retry_after'] = timestamp(retry)
            record['limit_set_at'] = current
        write_heartbeat(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    update = commands.add_parser('set', help='record current coordinator work')
    update.add_argument('--product', required=True)
    update.add_argument('--status', choices=STATUSES, required=True)
    update.add_argument('--doing', required=True, help='short description, without secrets or private details')
    update.add_argument('--session', help='Claude session ID, or auto to find the newest transcript for this cwd')
    update.add_argument('--retry-after', help='ISO timestamp with timezone, when waiting for a reset')
    commands.add_parser('show', help='print the current heartbeat as JSON')
    run = commands.add_parser('run', help='start an interactive coordinator while holding its lock')
    run.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == 'show':
            print(json.dumps(read_heartbeat(), ensure_ascii=False))
        elif args.action == 'set':
            if not valid_product(args.product):
                parser.error('invalid product directory name')
            if args.retry_after and args.status != 'waiting_limit':
                parser.error('--retry-after requires waiting_limit')
            set_status(args)
        else:
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            with run_lock(COORDINATOR_LOCK) as fd:
                os.chdir(HOME)
                os.set_inheritable(fd, True)
                env = dict(os.environ, KOROLEV_COORDINATOR_PID=str(os.getpid()))
                env.pop('KOROLEV_COORDINATOR_SESSION', None)
                command = command or ['claude']
                os.execvpe(command[0], command, env)
    except LockBusyError:
        parser.exit(75, 'A Korolev coordinator already holds the lock.\n')
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(1, f'Could not update or read coordinator state ({type(error).__name__}).\n')


if __name__ == '__main__':
    main()
