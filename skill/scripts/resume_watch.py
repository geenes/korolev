#!/usr/bin/env python3
"""Run one unattended resume check for the Korolev coordinator."""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heartbeat  # noqa: E402
import notify as telegram  # noqa: E402
from common import BOT_STATE, HOME, SKILL, append_line, now  # noqa: E402
from procs import LockBusyError, is_busy, run_lock  # noqa: E402

OLDER_THAN_MINUTES = 20
PROBE_TIMEOUT = 15

LIMIT = re.compile(
    r'\b(?: (?:usage|rate|credit)[ _-]*(?:limits?|quota)(?![a-z])'
    r'| rate[ _-]*limited\b'
    r'| (?:insufficient|exceeded)[ _-]+quota | quota[ _-]+(?:exceeded|exhausted)'
    r'| too[ _-]+many[ _-]+requests | (?:insufficient|out[ _-]+of)[ _-]+credits'
    r'| credit\s+balance\s+is\s+too\s+low | (?:hit|reached)\s+(?:your|the)\s+limit'
    r'| (?:http|status(?:\s*code)?)[" :]+429\b )',
    re.I | re.X,
)
RESET = re.compile(
    r'\b(?:resets?(?:[ _]+(?:at|time|timestamp|epoch|date))?|retry[ _-]+after|try again at)\b["\s:=]+'
    r'(?P<value>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?'
    r'|\d{13}\b|\d{10}\b|\d{1,2}(?::\d{2})?\s*(?:[ap]m)\b|\d{1,2}:\d{2}\b)'
    r'(?:\s*\((?P<zone>[A-Za-z0-9_+/-]+)\))?',
    re.I,
)


def limit_details(text, clock):
    """Return whether text reports a quota error and an optional UTC retry timestamp."""
    if not LIMIT.search(text):
        return False, None
    for match in RESET.finditer(text):
        value, zone_name = match.group('value', 'zone')
        try:
            zone = ZoneInfo(zone_name) if zone_name else None
            if value.isdecimal():
                reset = float(value) / (1000 if len(value) == 13 else 1)
            elif '-' in value[:10]:
                parsed = datetime.fromisoformat(value.upper().replace('Z', '+00:00'))
                reset = (parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed).timestamp()
            else:
                current = datetime.fromtimestamp(clock, zone)
                time_text = value.lower().replace(' ', '')
                if time_text.endswith(('am', 'pm')):
                    fmt = '%I:%M%p' if ':' in time_text else '%I%p'
                else:
                    fmt = '%H:%M'
                target = datetime.combine(current.date(), datetime.strptime(time_text, fmt).time(), zone)
                if target.timestamp() <= clock:
                    target += timedelta(days=1)
                reset = target.timestamp()
            return True, heartbeat.timestamp(reset)
        except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
            continue
    return True, None


def pending(record, current, stale_seconds):
    if record.get('status') not in ('working', 'waiting_limit') or not heartbeat.valid_product(record.get('product')):
        return False
    if not isinstance(record.get('doing'), str) or not record['doing'].strip():
        return False
    try:
        return (current - heartbeat.parse_timestamp(record.get('updated_at')) > stale_seconds
                and (record.get('retry_after') is None
                     or current >= heartbeat.parse_timestamp(record['retry_after'])))
    except (ValueError, OverflowError, OSError):
        return False


def probe(claude, clock):
    command = [*claude, '-p', 'Reply OK', '--output-format', 'json', '--tools', '',
               '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config',
               '--settings', '{"disableAllHooks":true}']
    try:
        result = subprocess.run(command, cwd=HOME, stdin=subprocess.DEVNULL, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        output = b'\n'.join((error.stdout or b'', error.stderr or b'')).decode('utf-8', errors='replace')
        hit, retry = limit_details(output, clock())
        return False, hit, retry
    except OSError:
        return False, False, None
    hit, retry = limit_details(result.stdout + '\n' + result.stderr, clock())
    if result.returncode != 0 or hit:
        return False, hit, retry
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        return False, False, None
    ok = (isinstance(reply, dict) and reply.get('is_error') is not True
          and reply.get('subtype', 'success') == 'success' and isinstance(reply.get('result'), str))
    return ok, False, None


def resume_command(claude, product, session):
    scripts = SKILL / 'scripts'
    state = f'products/{product}'
    project = shlex.quote(product)

    def python(script):
        path = scripts / f'{script}.py'
        return f'python3 -I {shlex.quote(str(path))}'

    prompt = (
        f'Use the korolev skill ({SKILL / "SKILL.md"}). Read {state}/STATE.md, including the Session handoff, '
        f'and {state}/DECISIONS.md. Read the inbox with {python("inbox")} --show --project {project}. '
        'Continue the approved build loop only. Check existing worker locks and run records before starting a package. '
        'Ask the owner on Telegram for anything else. Do not push, publish, deploy, spend money, or delete files. '
        'Apply these restrictions to worker tasks too. '
        'Before stopping, write the session handoff, then mark the heartbeat idle if finished or waiting for the owner. '
        'If a worker hits a usage limit, write the handoff, mark waiting_limit, and exit.'
    )
    allow = ['Skill(korolev)', f'Write(./{state}/**)', f'Edit(./{state}/**)']
    shell = [
        f'{python("inbox")} --show --project {project}',
        f'{python("heartbeat")} set --product {project} *',
        f'{python("handoff")} --product {project} --file {shlex.quote(state)}/*',
        f'{python("notify")} --project {project} *',
        f'{python("run_codex")} --role builder --product {project} *',
        f'{python("run_codex")} --role analyst --product {project} *',
        f'{python("procs")} status *',
        f'{python("testcount")} check *',
        'python3 -m unittest discover -s tests',
    ]
    repo = shlex.quote(f'work/{product}/repo')
    shell.extend(f'git -C {repo} {cmd} *' for cmd in ('status', 'log', 'show', 'diff', 'rev-parse'))
    settings = json.dumps({'disableAllHooks': True,
                           'permissions': {'allow': allow + [f'Bash({rule})' for rule in shell]}})
    return [*claude, '-p', prompt, '--session-id', session, '--permission-mode', 'dontAsk',
            '--setting-sources', '', '--strict-mcp-config', '--settings', settings,
            '--tools', 'Bash,Read,Write,Edit,Glob,Grep,Skill']


def notify_owner(product, body):
    message_id = telegram.send(telegram.load_env(), body)
    append_line(BOT_STATE / 'SENT.jsonl', {'message_id': message_id, 'project': product, 'time': now()})


def watch(*, claude=('claude',), clock=time.time, notify=notify_owner, launch=subprocess.Popen,
          stale_seconds=OLDER_THAN_MINUTES * 60):
    if not pending(heartbeat.read(), clock(), stale_seconds):
        return
    try:
        with run_lock('resume'):
            if is_busy(heartbeat.COORDINATOR_LOCK):
                return
            with run_lock(heartbeat.COORDINATOR_LOCK) as fd:
                previous = heartbeat.read()
                if not pending(previous, clock(), stale_seconds):
                    return
                product = previous['product']
                if not (HOME / 'products' / product).is_dir():
                    return
                ok, hit, retry = probe(claude, clock)
                with heartbeat.state_lock():
                    if heartbeat.read() != previous:
                        return
                    if hit:
                        retry = retry or heartbeat.timestamp(clock() + heartbeat.DEFAULT_RETRY_SECONDS)
                        heartbeat.write(dict(previous, status='waiting_limit', retry_after=retry))
                        return
                    if not ok:
                        return
                    session = str(uuid.uuid4())
                    process = launch(resume_command(claude, product, session), cwd=HOME,
                                     env=dict(os.environ, KOROLEV_COORDINATOR_SESSION=session),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, pass_fds=(fd,))
                    resumed = dict(previous, status='working', updated_at=heartbeat.timestamp(clock()),
                                   session=session, pid=process.pid)
                    resumed.pop('retry_after', None)
                    heartbeat.write(resumed)
            try:
                notify(product, f'Resumed after your usage limit reset: continuing {previous["doing"]}')
            except (Exception, SystemExit):
                print('Could not send the resume notice on Telegram.', file=sys.stderr)
    except LockBusyError:
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stale-minutes', type=float,
                        default=os.environ.get('KOROLEV_RESUME_STALE_MINUTES', str(OLDER_THAN_MINUTES)))
    args = parser.parse_args()
    if not math.isfinite(args.stale_minutes) or args.stale_minutes < 0:
        parser.error('--stale-minutes must be a finite, non-negative number')
    try:
        watch(stale_seconds=args.stale_minutes * 60)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Could not check coordinator resume state ({type(error).__name__}).\n')


if __name__ == '__main__':
    main()
