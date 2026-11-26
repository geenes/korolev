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
from common import (BOT_STATE, HOME, SKILL, append_line, heartbeat_lock, now, parse_timestamp,  # noqa: E402
                    read_heartbeat, timestamp, valid_product, write_heartbeat)
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
            return True, timestamp(reset)
        except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
            continue
    return True, None


def latest_message(log, product, time_key):
    latest = None
    latest_order = (-math.inf, -1)
    try:
        stream = (BOT_STATE / log).open(encoding='utf-8')
    except FileNotFoundError:
        return latest_order, latest
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get('project') != product:
                    continue
                date = parse_timestamp(row.get(time_key))
                message_id = row.get('message_id')
                order = (date, message_id if type(message_id) is int else -1)
            except (ValueError, OverflowError, OSError):
                continue
            # Telegram message IDs order events in the same chat within one second.
            if order > latest_order:
                latest_order, latest = order, row
    return latest_order, latest


def unanswered_message(product, updated):
    received, message = latest_message('INBOX.jsonl', product, 'date')
    if received[0] > updated:
        sent, _ = latest_message('SENT.jsonl', product, 'time')
        if sent <= received:
            return message
    return None


def pending(record, product_dir, current, stale_seconds):
    if not isinstance(record.get('doing'), str) or not record['doing'].strip():
        return False, None
    try:
        updated = parse_timestamp(record.get('updated_at'))
        if record.get('retry_after') is not None and current < parse_timestamp(record['retry_after']):
            return False, None
    except (ValueError, OverflowError, OSError):
        return False, None
    product = record['product']
    owner = unanswered_message(product, updated)
    if owner is not None or (record.get('status') in ('working', 'waiting_limit') and current - updated > stale_seconds):
        return True, owner
    if is_busy(f'{product}-builder-write'):
        return False, None
    return any(task.is_file() and not task.with_name('HANDOFF.md').exists()
               for task in product_dir.glob('packages/*/TASK.md')), None


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


def resume_command(claude, product, session, *, cwd=None, resume=False):
    scripts = SKILL / 'scripts'
    state = f'products/{product}'
    repo = f'work/{product}/repo'
    state_pattern = f'./{state}'
    if cwd is not None and cwd.resolve() != HOME.resolve():
        state = str(HOME.resolve() / state)
        repo = str(HOME.resolve() / repo)
        state_pattern = f'/{state}'
    project = shlex.quote(product)

    def python(script):
        path = scripts / f'{script}.py'
        return f'python3 -I {shlex.quote(str(path))}'

    prompt = (
        'Your usage limit has reset. Continue where you stopped. '
        f'Use the korolev skill ({SKILL / "SKILL.md"}). Read the inbox with {python("inbox")} --show --project {project}. '
        f'Read {state}/STATE.md, including the Session handoff, and {state}/DECISIONS.md. '
        f'Check running jobs with {python("procs")} status {project}-builder-write and read {state}/USAGE.jsonl '
        'as needed before starting a package. Continue the approved build loop only. '
        'Ask the owner on Telegram for anything else. Do not push, publish, deploy, spend money, or delete files. '
        'Apply these restrictions to worker tasks too. '
        'Before stopping, write the session handoff, then mark the heartbeat idle if finished or waiting for the owner. '
        'If a worker hits a usage limit, write the handoff, mark waiting_limit, and exit.'
    )
    allow = ['Skill(korolev)', f'Write({state_pattern}/**)', f'Edit({state_pattern}/**)']
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
    shell.extend(f'git -C {shlex.quote(repo)} {cmd} *' for cmd in ('status', 'log', 'show', 'diff', 'rev-parse'))
    home = Path.home()
    # Claude Code permission patterns use // for absolute paths.
    credentials = [f'/{home / path}' for path in ('.config/korolev/**', '.ssh/**', '.codex/auth.json', '.gemini/**')]
    credentials.append('**/*.env')
    deny = [f'{tool}({path})' for tool in ('Read', 'Glob', 'Grep', 'Write', 'Edit') for path in credentials]
    settings = json.dumps({'disableAllHooks': True,
                           'permissions': {'allow': allow + [f'Bash({rule})' for rule in shell], 'deny': deny}})
    chat = ['--resume', session, '-p', prompt] if resume else ['-p', prompt, '--session-id', session]
    return [*claude, *chat, '--permission-mode', 'dontAsk',
            '--setting-sources', '', '--strict-mcp-config', '--settings', settings,
            '--tools', 'Bash,Read,Write,Edit,Glob,Grep,Skill']


def notify_owner(product, body):
    message_id = telegram.send(telegram.load_env(), body)
    append_line(BOT_STATE / 'SENT.jsonl', {'message_id': message_id, 'project': product, 'time': now()})


def watch(*, claude=('claude',), clock=time.time, notify=notify_owner, launch=subprocess.Popen,
          stale_seconds=OLDER_THAN_MINUTES * 60):
    if not valid_product(read_heartbeat().get('product')):
        return
    try:
        with run_lock('resume'):
            with run_lock(heartbeat.COORDINATOR_LOCK) as fd:
                previous = read_heartbeat()
                product = previous.get('product')
                if not valid_product(product) or heartbeat.recorded_process_running(previous):
                    return
                product_dir = HOME / 'products' / product
                if not product_dir.is_dir() or not pending(previous, product_dir, clock(), stale_seconds)[0]:
                    return
                ok, hit, retry = probe(claude, clock)
                with heartbeat_lock():
                    if read_heartbeat() != previous:
                        return
                    current = clock()
                    ready, owner = pending(previous, product_dir, current, stale_seconds)
                    if not ready:
                        return
                    if hit:
                        retry = retry or timestamp(current + heartbeat.DEFAULT_RETRY_SECONDS)
                        write_heartbeat(dict(previous, status='waiting_limit', retry_after=retry,
                                             limit_set_at=current))
                        return
                    if not ok:
                        return
                    same_chat = bool(previous.get('session'))
                    session = previous['session'] if same_chat else str(uuid.uuid4())
                    # Older heartbeats recorded sessions launched from HOME without a cwd.
                    cwd = Path(previous.get('cwd') or HOME) if same_chat else HOME
                    env = dict(os.environ, KOROLEV_COORDINATOR_SESSION=session)
                    env.pop('KOROLEV_COORDINATOR_PID', None)
                    command = resume_command(claude, product, session, cwd=cwd, resume=same_chat)
                    process = launch(command, cwd=cwd, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, pass_fds=(fd,))
                    resumed = dict(product=product, status='working', updated_at=timestamp(clock()),
                                   doing=previous['doing'], session=session, cwd=str(cwd),
                                   **heartbeat.process_identity(process.pid))
                    write_heartbeat(resumed)
            notice = f"Your usage is back. I'm continuing where I stopped: {previous['doing']}"
            if owner and isinstance(owner.get('text'), str):
                quote = ' '.join(owner['text'].split())
                if len(quote) > 80:
                    quote = quote[:79] + '…'
                notice += f'\nOwner message: “{quote}”'
            if not same_chat:
                notice += '\nI started a fresh chat because no session was recorded.'
            try:
                notify(product, notice)
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
