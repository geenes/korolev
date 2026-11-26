#!/usr/bin/env python3
"""Publish a new Korolev version: CHANGELOG entry, version bump, tag, GitHub Release, owner notice.

  release.py notes   [--repo PATH]                  draft the release notes (prints them; writes RELEASE-NOTES.draft.md)
  release.py propose [--notes FILE] [--level L]      privacy scan, then ask the owner on Telegram to approve
  release.py approve                                 read the owner's reply from the inbox; record approved or rejected
  release.py publish [--no-notify]                   only after approval, for the exact approved commit

Nothing is pushed without the owner's explicit approval of that exact commit, and
every outgoing commit must pass privacy_scan.py: no personal emails, home paths,
secrets or private terms.

The notes are built from lessons applied since the last release (lesson.py) and from
the commits since the last tag. Lessons are grouped as Added / Changed / Fixed, with
their feature title, ID and reason for the change. Edit the draft before publishing
if needed. Publish:
  1. refuses unless the repo is on main, clean, and up to date with origin/main;
  2. runs the offline tests (python3 -m unittest discover -s tests);
  3. bumps VERSION (minor if anything was Added, else patch) and prepends CHANGELOG.md;
  4. commits, creates an annotated tag vX.Y.Z, pushes main and the tag;
  5. waits for CI on that exact commit (ci_wait.py if available, else gh run watch);
  6. creates the GitHub Release with the notes;
  7. marks the included lessons as released, and tells the owner on Telegram with the link.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import BOT_STATE, CONFIG_DIR, HOME, append_line, now, touch_heartbeat  # noqa: E402
from i18n import t  # noqa: E402
from inbox import RELEASE_APPROVAL_REPLY, RELEASE_REJECTION_REPLY  # noqa: E402

LOG = HOME / 'lessons' / 'lessons.jsonl'
GROUP = {'improvement': 'Added', 'tooling': 'Changed', 'cost': 'Changed', 'mistake': 'Fixed'}


def sh(cmd, cwd, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd[:4])}\n{result.stderr.strip()[-600:]}")
    return result.stdout.strip()


def lesson_state():
    state = {}
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            event = json.loads(line)
            if event['event'] == 'add':
                state[event['id']] = dict(event, status='pending', released=False)
            elif event['id'] in state:
                item = state[event['id']]
                if event['event'] == 'retitle':
                    item['change'] = event['change']
                elif event.get('status') == 'released':
                    item['released'] = True
                else:
                    item['status'] = event.get('status', item['status'])
    return state


def last_tag(repo):
    tag = sh(['git', 'describe', '--tags', '--abbrev=0', '--match', 'v*'], repo, check=False)
    return tag or None


def current_version(repo):
    path = repo / 'VERSION'
    return path.read_text().strip() if path.exists() else '0.0.0'


def bump(version, level):
    major, minor, patch = (int(x) for x in version.split('.'))
    if level == 'major':
        return f'{major + 1}.0.0'
    if level == 'minor':
        return f'{major}.{minor + 1}.0'
    return f'{major}.{minor}.{patch + 1}'


def draft(repo):
    tag = last_tag(repo)
    commits = sh(['git', 'log', '--format=%h %s', f'{tag}..HEAD' if tag else 'HEAD'], repo).splitlines()
    commits = [c for c in commits if not re.search(r'^\w+ Release v\d', c)]
    lessons = [l for l in lesson_state().values() if l['status'] == 'applied' and not l['released']]
    groups = {'Added': [], 'Changed': [], 'Fixed': []}
    for item in lessons:
        change = item.get('change')
        entry = (f"- **{change or item['title']}** ({item['id']}{', safety rule' if item.get('safety') else ''})\n"
                 f"  - Why: {item['detail']}")
        if not change:
            entry += (f"\n  - What changed: {item['proposal']}"
                      f"\n  - Warning: no change title. Set one with `lesson.py retitle {item['id']} --change \"…\"`.")
        groups[GROUP.get(item['kind'], 'Changed')].append(entry)
    lines = []
    for name in ('Added', 'Changed', 'Fixed'):
        if groups[name]:
            lines += [f'### {name}', *groups[name], '']
    if commits:
        lines += ['### Commits', *[f'- {c}' for c in commits], '']
    if not lessons and not commits:
        sys.exit('nothing to release')
    level = 'minor' if groups['Added'] or any('feat' in c.lower() or ' add' in c.lower() for c in commits) else 'patch'
    return '\n'.join(lines).rstrip() + '\n', level, [l['id'] for l in lessons]


def wait_ci(repo, sha):
    slug = sh(['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'], repo)
    if (HERE / 'ci_wait.py').exists():
        result = subprocess.run([sys.executable, '-I', str(HERE / 'ci_wait.py'), slug, sha, '--branch', 'main'],
                                capture_output=True, text=True)
        print(result.stdout.strip()[-800:])
        return result.returncode == 0
    run_id = None
    for _ in range(30):
        run_id = sh(['gh', 'api', f'repos/{slug}/actions/runs?head_sha={sha}', '--jq', '.workflow_runs[0].id // empty'],
                    repo, check=False)
        if run_id:
            break
        time.sleep(10)
    if not run_id:
        print('no CI run found for', sha)
        return False
    return subprocess.run(['gh', 'run', 'watch', run_id, '-R', slug, '--exit-status', '--interval', '30'],
                          capture_output=True).returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['notes', 'propose', 'approve', 'publish'])
    parser.add_argument('--repo', default=str(HERE.parents[1]))
    parser.add_argument('--notes', help='edited notes file to use instead of the generated draft')
    parser.add_argument('--level', choices=['patch', 'minor', 'major'])
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    touch_heartbeat('korolev', 'Working on Korolev release tasks')
    repo = Path(args.repo).resolve()
    if args.command != 'notes' and not args.dry_run:
        check_maintainer(repo)
    state_dir = HOME / 'products' / 'korolev'
    state_dir.mkdir(parents=True, exist_ok=True)
    pending_path = state_dir / 'release-pending.json'

    if args.command in ('notes', 'propose') or args.dry_run:
        notes, level, lesson_ids = draft(repo)
        if args.notes:
            notes = Path(args.notes).read_text()
        version = bump(current_version(repo), args.level or level)
        if args.command == 'notes' or args.dry_run:
            (repo / 'RELEASE-NOTES.draft.md').write_text(f'## v{version}\n\n{notes}')
            print(f'## v{version} ({args.level or level})\n\n{notes}')
            return
        ensure_clean_main(repo)
        scan = privacy(repo, 'origin/main..HEAD')
        if not scan['clean']:
            sys.exit('privacy scan NOT clean, nothing proposed:\n' + json.dumps(scan['findings'], ensure_ascii=False, indent=1))
        commits = sh(['git', 'log', '--format=- %h %s', 'origin/main..HEAD'], repo) or t('release_commit_only')
        files = sh(['git', 'diff', '--stat', 'origin/main', 'HEAD'], repo).splitlines()
        titles = [l.strip()[2:].split('** (')[0].strip('*') for l in notes.splitlines() if l.startswith('- **')]
        changes = '\n'.join(f'• {title}' for title in titles) or t('release_see_commits')
        text = t('release_proposal', version=version, changes=changes, commits=commits,
                 files=files[-1].strip() if files else '—', commit_count=scan['commits'])
        notes_file = state_dir / f'release-notes-v{version}.md'
        notes_file.write_text(notes)
        result = subprocess.run([sys.executable, '-I', str(HERE / 'notify.py'), '--project', 'korolev', text],
                                capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit('could not ask the owner: ' + result.stderr.strip())
        message_id = json.loads(result.stdout)['sent'][0]
        pending = {'version': version, 'level': args.level or level, 'head': sh(['git', 'rev-parse', 'HEAD'], repo),
                   'notes_file': str(notes_file), 'notes_sha256': hashlib.sha256(notes.encode()).hexdigest(),
                   'lessons': lesson_ids, 'message_id': message_id, 'asked': now(), 'status': 'waiting'}
        pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2))
        print(json.dumps({'proposed': version, 'message_id': message_id, 'head': pending['head'][:7]}))
        return

    if not pending_path.exists():
        sys.exit('no release proposal; run: release.py propose')
    pending = json.loads(pending_path.read_text())

    if args.command == 'approve':
        inbox = BOT_STATE / 'INBOX.jsonl'
        replies = [json.loads(l) for l in inbox.read_text().splitlines()] if inbox.exists() else []
        replies = [r for r in replies if r.get('reply_to') == pending['message_id']]
        if not replies:
            print(f"no owner reply yet to message {pending['message_id']}")
            sys.exit(2)
        answer = replies[-1]['text'].strip().lower()
        if RELEASE_APPROVAL_REPLY.match(answer):
            pending.update(status='approved', approval_message=replies[-1]['message_id'], approved=now())
        elif RELEASE_REJECTION_REPLY.match(answer):
            pending.update(status='rejected', rejected=now())
        else:
            print(f'unclear reply: {answer[:60]!r}; ask the owner again')
            sys.exit(2)
        pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2))
        print(pending['status'])
        return

    # publish
    if pending.get('status') != 'approved':
        sys.exit(f"not approved by the owner (status: {pending.get('status')}); nothing pushed")
    ensure_clean_main(repo)
    if sh(['git', 'rev-parse', 'HEAD'], repo) != pending['head']:
        sys.exit('HEAD changed since the owner approved; propose again')
    notes = Path(pending['notes_file']).read_text()
    if hashlib.sha256(notes.encode()).hexdigest() != pending['notes_sha256']:
        sys.exit('release notes changed since approval; propose again')
    version, lesson_ids = pending['version'], pending['lessons']
    if subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=repo,
                      capture_output=True).returncode != 0:
        sys.exit('offline tests failed; not releasing')
    (repo / 'VERSION').write_text(version + '\n')
    changelog = repo / 'CHANGELOG.md'
    old = changelog.read_text() if changelog.exists() else '# Changelog\n\nAll notable changes to Korolev.\n'
    head, _, rest = old.partition('\n## ')
    entry = f'\n## v{version} ({now()[:10]})\n\n{notes}'
    changelog.write_text(head.rstrip() + '\n' + entry + ('\n## ' + rest if rest else ''))
    sh(['git', 'add', 'VERSION', 'CHANGELOG.md'], repo)
    sh(['git', '-c', 'user.name=Korolev release', '-c', 'user.email=korolev@localhost', 'commit', '-q', '-m',
        f'Release v{version}\n\n{notes}'], repo)
    sh(['git', '-c', 'user.name=Korolev release', '-c', 'user.email=korolev@localhost', 'tag', '-a',
        f'v{version}', '-m', f'Korolev v{version}\n\n{notes}'], repo)
    final = privacy(repo, 'origin/main..HEAD')
    if not final['clean']:
        sh(['git', 'tag', '-d', f'v{version}'], repo)
        sh(['git', 'reset', '-q', '--hard', 'HEAD~1'], repo)
        sys.exit('privacy scan failed on the release commit; rolled back, nothing pushed:\n'
                 + json.dumps(final['findings'], ensure_ascii=False))
    sh(['git', 'push', '-q', 'origin', 'main', f'v{version}'], repo)
    sha = sh(['git', 'rev-parse', 'HEAD'], repo)
    if not wait_ci(repo, sha):
        sys.exit(f'CI did not pass for {sha}; tag v{version} is pushed but no GitHub Release was created')
    notes_file = repo / '.release-notes.tmp'
    notes_file.write_text(notes)
    url = sh(['gh', 'release', 'create', f'v{version}', '--title', f'Korolev v{version}', '--notes-file',
              str(notes_file), '--verify-tag'], repo)
    notes_file.unlink()
    for lesson_id in lesson_ids:
        append_line(LOG, {'event': 'status', 'id': lesson_id, 'time': now(), 'status': 'released',
                          'version': version})
    pending.update(status='published', published=now(), release=url, sha=sha)
    pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2))
    print(json.dumps({'version': version, 'sha': sha, 'release': url, 'lessons': lesson_ids}))
    if not args.no_notify:
        changes = '\n'.join('• ' + line.strip()[2:] for line in notes.splitlines() if line.startswith('- **'))
        summary = t('release_published', version=version, url=url, changes=changes)
        subprocess.run([sys.executable, '-I', str(HERE / 'notify.py'), '--project', 'korolev', summary])


MAINTAINERS = {'geenes'}
UPSTREAM = 'github.com/geenes/korolev'


def check_maintainer(repo):
    """Self-publishing is maintainer-only: upstream origin + maintainer GitHub login + local opt-in file."""
    if os.environ.get('KOROLEV_TEST_SKIP_MAINTAINER') == '1':  # offline tests only
        return
    reason = None
    origin = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=repo, capture_output=True, text=True).stdout.strip()
    if UPSTREAM not in origin.replace('git@github.com:', 'github.com/').removesuffix('.git'):
        reason = 'origin is not the upstream Korolev repo'
    elif not (CONFIG_DIR / 'maintainer.json').exists():
        reason = 'no local maintainer opt-in (~/.config/korolev/maintainer.json)'
    else:
        login = subprocess.run(['gh', 'api', 'user', '--jq', '.login'], capture_output=True, text=True).stdout.strip()
        if login not in MAINTAINERS:
            reason = 'the logged-in GitHub account is not a Korolev maintainer'
    if reason:
        sys.exit(f'self-publishing is maintainer-only: {reason}. Korolev users improve their own copy locally '
                 '(lesson.py) and can contribute through pull requests.')


def ensure_clean_main(repo):
    if sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo) != 'main' or sh(['git', 'status', '--porcelain'], repo):
        sys.exit('repo must be on a clean main branch')
    sh(['git', 'fetch', '-q', 'origin'], repo)
    if subprocess.run(['git', 'merge-base', '--is-ancestor', 'origin/main', 'HEAD'], cwd=repo).returncode != 0:
        sys.exit('local main is behind or diverged from origin/main; pull/rebase first')


def privacy(repo, rng):
    result = subprocess.run([sys.executable, '-I', str(HERE / 'privacy_scan.py'), '--repo', str(repo), '--range', rng,
                             '--json'], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except ValueError:
        sys.exit('privacy scan could not run: ' + result.stderr.strip()[-300:])

if __name__ == '__main__':
    main()
