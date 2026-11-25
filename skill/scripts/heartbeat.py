#!/usr/bin/env python3
"""Record coordinator progress or launch a coordinator under its run lock."""
import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME  # noqa: E402
from procs import LockBusyError, run_lock  # noqa: E402

COORDINATOR_LOCK = 'coordinator'
STATUSES = ('working', 'idle', 'waiting_limit')
DEFAULT_RETRY_SECONDS = 30 * 60


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_timestamp(value):
    if type(value) in (float, int):
        return value
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs a timezone')
    return parsed.timestamp()


def valid_product(product):
    return isinstance(product, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', product) is not None


def read():
    try:
        result = json.loads((HOME / 'coordinator.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


@contextmanager
def state_lock():
    HOME.mkdir(parents=True, exist_ok=True)
    with (HOME / 'coordinator.json.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write(record):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=HOME, delete=False) as stream:
        staging = Path(stream.name)
        try:
            json.dump(record, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            staging.replace(HOME / 'coordinator.json')
        finally:
            staging.unlink(missing_ok=True)


def set_status(args):
    with state_lock():
        previous = read()
        managed_session = os.environ.get('KOROLEV_COORDINATOR_SESSION')
        session = args.session or managed_session
        record = {'product': args.product, 'status': args.status, 'doing': args.doing,
                  'updated_at': timestamp(time.time())}
        if session:
            record['session'] = session
        if managed_session and previous.get('session') == managed_session and 'pid' in previous:
            record['pid'] = previous['pid']
        elif (pid := os.environ.get('KOROLEV_COORDINATOR_PID', '')).isdecimal():
            record['pid'] = int(pid)
        if args.status == 'waiting_limit':
            retry = parse_timestamp(args.retry_after) if args.retry_after else time.time() + DEFAULT_RETRY_SECONDS
            record['retry_after'] = timestamp(retry)
        write(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    update = commands.add_parser('set', help='record current coordinator work')
    update.add_argument('--product', required=True)
    update.add_argument('--status', choices=STATUSES, required=True)
    update.add_argument('--doing', required=True, help='short description, without secrets or private details')
    update.add_argument('--session')
    update.add_argument('--retry-after', help='ISO timestamp with timezone, when waiting for a reset')
    commands.add_parser('show', help='print the current heartbeat as JSON')
    run = commands.add_parser('run', help='start an interactive coordinator while holding its lock')
    run.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == 'show':
            print(json.dumps(read(), ensure_ascii=False))
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
