#!/usr/bin/env python3
"""Preflight check for Korolev. Prints PASS / WARN / FAIL lines; exit 1 if any FAIL.

  doctor.py [--offline]     (--offline skips the Telegram API call)
"""
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOT_STATE, HOME, SKILL, TELEGRAM_ENV, roles  # noqa: E402

results = []


def report(level, item, detail=''):
    results.append(level)
    print(f'{level:4} {item}' + (f': {detail}' if detail else ''))


def cmd_ok(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.returncode == 0, (out.stdout or out.stderr).strip().splitlines()[:1]
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, [type(error).__name__]


def main():
    offline = '--offline' in sys.argv
    report('PASS' if sys.version_info >= (3, 10) else 'FAIL', 'python', sys.version.split()[0])
    for tool in ('git', 'codex'):
        ok, line = cmd_ok([tool, '--version'])
        report('PASS' if ok else 'FAIL', tool, line[0] if line else 'not found')
    if shutil.which('gh'):
        ok, _ = cmd_ok(['gh', 'auth', 'status'])
        report('PASS' if ok else 'WARN', 'gh auth', 'logged in' if ok else 'not logged in: private GitHub repos will fail')
    else:
        report('WARN', 'gh', 'not installed: private GitHub repos need git credentials')
    cache = Path('~/.codex/models_cache.json').expanduser()
    if cache.exists():
        text = cache.read_text(errors='replace')
        missing = sorted({r['model'] for r in roles().values() if f'"{r["model"]}"' not in text})
        report('WARN' if missing else 'PASS', 'codex models', f'not in local cache: {missing}' if missing else 'all role models known')
    else:
        report('WARN', 'codex models', 'no local model cache to check against')
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        probe = HOME / '.write-test'
        probe.write_text('ok')
        probe.unlink()
        usage = shutil.disk_usage(HOME)
        pct = 100 * usage.free / usage.total
        report('PASS' if pct >= 15 else 'WARN', 'KOROLEV_HOME', f'{HOME} writable; {pct:.1f}% disk free')
    except OSError as error:
        report('FAIL', 'KOROLEV_HOME', f'{HOME}: {error}')
    for link in ('~/.claude/skills/korolev', '~/.codex/skills/korolev'):
        path = Path(link).expanduser()
        report('PASS' if path.exists() and path.resolve() == SKILL else 'WARN', f'skill installed at {link}',
               'ok' if path.exists() else 'missing: run install.sh')
    if not TELEGRAM_ENV.exists():
        report('WARN', 'telegram', f'{TELEGRAM_ENV} missing: questions go to chat only (setup_telegram.py)')
    else:
        mode = stat.S_IMODE(TELEGRAM_ENV.stat().st_mode)
        env = dict(line.split('=', 1) for line in TELEGRAM_ENV.read_text().splitlines() if '=' in line)
        keys = [k for k in ('KOROLEV_BOT_TOKEN', 'KOROLEV_CHAT_ID', 'KOROLEV_OWNER_ID') if not env.get(k, '').strip()]
        report('FAIL' if keys else ('WARN' if mode & 0o077 else 'PASS'), 'telegram config',
               f'missing {keys}' if keys else f'mode {oct(mode)}')
        if not keys and not offline:
            try:
                token = env['KOROLEV_BOT_TOKEN'].strip()
                me = json.load(urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getMe', timeout=20))
                report('PASS' if me.get('ok') else 'FAIL', 'telegram bot', '@' + me['result']['username'])
            except Exception as error:  # never print the URL
                report('FAIL', 'telegram bot', type(error).__name__)
        lock = BOT_STATE / 'inbox.lock'
        if lock.exists():
            with lock.open('a+') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    report('PASS', 'inbox listener', 'not running')
                except BlockingIOError:
                    report('PASS', 'inbox listener', 'running (another session is listening)')
    sys.exit(1 if 'FAIL' in results else 0)


if __name__ == '__main__':
    main()
