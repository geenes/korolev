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
import tempfile
import urllib.request
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (BOT_STATE, HOME, SKILL, TELEGRAM_ENV, codex_home, git_common_dir,  # noqa: E402
                    isolate_codex_home, read_projects, roles, shared_remotes)
import sandbox  # noqa: E402

TEMP_SCAN_ROOT = Path('/')
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


def check_stray_git():
    temp_dirs = [TEMP_SCAN_ROOT / name for name in ('tmp', 'var/tmp', 'dev/shm')]
    try:
        temp_dirs.append(Path(tempfile.gettempdir()))
    except FileNotFoundError:
        pass

    paths = {TEMP_SCAN_ROOT}
    for directory in temp_dirs:
        candidates = [directory]
        try:
            candidates.append(directory.resolve())
        except (OSError, RuntimeError):
            pass
        for candidate in candidates:
            paths.update(path for path in (candidate, *candidate.parents) if path.is_relative_to(TEMP_SCAN_ROOT))

    for path in sorted(paths):
        marker = path / '.git'
        objects = path / 'objects'
        if os.path.lexists(marker):
            found = marker
        # Mirror run_codex.plausible_git_metadata without importing the launcher.
        elif (os.path.lexists(path / 'HEAD') and (os.path.lexists(objects) or os.path.lexists(path / 'commondir'))
              or os.path.lexists(path / 'config') and objects.is_dir() and (path / 'refs').is_dir()):
            found = path
        else:
            continue
        report('WARN', 'stray Git metadata', f'Git discovery from temp folders may find {found}')


def check_registry():
    try:
        projects = read_projects()
    except (OSError, ValueError) as error:
        report('WARN', 'project registry', str(error))
        return
    for name, entry in sorted(projects.items()):
        folder = Path(entry['folder'])
        if not folder.is_dir():
            report('WARN', f'project {name}', f'attached folder is missing: {folder}')
            continue
        try:
            current = git_common_dir(folder)
        except (OSError, ValueError):
            report('WARN', f'project {name}', f'Git common directory unavailable for {folder}')
        else:
            if str(current) != entry['git_common_dir']:
                report('WARN', f'project {name}', f"Git common directory changed from {entry['git_common_dir']} to {current}")
    for (name, entry), (other_name, other) in combinations(sorted(projects.items()), 2):
        pair = f'projects {name} and {other_name}'
        if entry['folder'] == other['folder']:
            report('WARN', pair, f"share attached folder {entry['folder']}")
        if entry['git_common_dir'] == other['git_common_dir']:
            report('WARN', pair, f"share Git common directory {entry['git_common_dir']}")
        for url in sorted(shared_remotes(entry, other)):
            report('WARN', pair, f'share remote {url}')


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
    isolated = isolate_codex_home()
    report('PASS', 'codex home isolation', 'on' if isolated else 'off')
    auth_problem = 'FAIL' if isolated else 'WARN'
    try:
        auth = codex_home(create=isolated) / 'auth.json'
        if auth.is_symlink() and auth.is_file():
            report('PASS', 'isolated codex auth', 'valid symlink')
        else:
            report(auth_problem, 'isolated codex auth', 'missing auth symlink or target')
    except OSError as error:
        report(auth_problem, 'isolated codex auth', str(error))
    if sandbox.enabled():
        try:
            report('PASS', 'worker sandbox', sandbox.selftest())
        except OSError as error:
            report('FAIL', 'worker sandbox', f'{error} {sandbox.disable_hint()}')
    else:
        report('WARN', 'worker sandbox', 'off by configuration')
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
    check_stray_git()
    check_registry()
    for link in ('~/.claude/skills/korolev', '~/.codex/skills/korolev'):
        path = Path(link).expanduser()
        report('PASS' if path.exists() and path.resolve() == SKILL else 'WARN', f'skill installed at {link}',
               'ok' if path.exists() else 'missing: run install.sh')
    enabled, state = cmd_ok(['systemctl', '--user', 'is-enabled', 'korolev-resume.timer'])
    state = state[0] if state else ''
    if enabled and state in ('enabled', 'enabled-runtime'):
        report('PASS', 'resume timer', 'enabled')
    elif (state in ('disabled', 'not-found', 'masked', 'masked-runtime', 'linked', 'linked-runtime', 'static')
          or state.endswith(': No such file or directory')):
        report('WARN', 'resume timer', 'disabled')
    else:
        report('WARN', 'resume timer', 'status unavailable')
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
