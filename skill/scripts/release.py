#!/usr/bin/env python3
"""Publish a new Korolev version: CHANGELOG entry, version bump, tag, GitHub Release, owner notice.

  release.py notes [--repo PATH]                 draft the release notes (prints them; writes RELEASE-NOTES.draft.md)
  release.py publish [--repo PATH] [--notes FILE] [--level patch|minor|major] [--no-notify] [--dry-run]

The notes are built from lessons applied since the last release (lesson.py) and from
the commits since the last tag. They are grouped as Added / Changed / Fixed, and each
lesson contributes its ID, what happened and what changed. Edit the draft before
publishing if needed. Publish:
  1. refuses unless the repo is on main, clean, and up to date with origin/main;
  2. runs the offline tests (python3 -m unittest discover -s tests);
  3. bumps VERSION (minor if anything was Added, else patch) and prepends CHANGELOG.md;
  4. commits, creates an annotated tag vX.Y.Z, pushes main and the tag;
  5. waits for CI on that exact commit (ci_wait.py if available, else gh run watch);
  6. creates the GitHub Release with the notes;
  7. marks the included lessons as released, and tells the owner on Telegram with the link.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import HOME, append_line, now  # noqa: E402

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
                if event.get('status') == 'released':
                    state[event['id']]['released'] = True
                else:
                    state[event['id']]['status'] = event.get('status', state[event['id']]['status'])
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
        groups[GROUP.get(item['kind'], 'Changed')].append(
            f"- **{item['title']}** ({item['id']}{', safety rule' if item.get('safety') else ''})\n"
            f"  - Why: {item['detail']}\n  - What changed: {item['proposal']}")
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
        result = subprocess.run([sys.executable, '-I', str(HERE / 'ci_wait.py'), slug, sha], capture_output=True, text=True)
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
    parser.add_argument('command', choices=['notes', 'publish'])
    parser.add_argument('--repo', default=str(HERE.parents[1]))
    parser.add_argument('--notes', help='edited notes file to use instead of the generated draft')
    parser.add_argument('--level', choices=['patch', 'minor', 'major'])
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    notes, level, lesson_ids = draft(repo)
    if args.notes:
        notes = Path(args.notes).read_text()
    version = bump(current_version(repo), args.level or level)
    if args.command == 'notes' or args.dry_run:
        (repo / 'RELEASE-NOTES.draft.md').write_text(f'## v{version}\n\n{notes}')
        print(f'## v{version} ({args.level or level})\n\n{notes}')
        return
    if sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo) != 'main' or sh(['git', 'status', '--porcelain'], repo):
        sys.exit('repo must be on a clean main branch')
    sh(['git', 'fetch', '-q', 'origin'], repo)
    if subprocess.run(['git', 'merge-base', '--is-ancestor', 'origin/main', 'HEAD'], cwd=repo).returncode != 0:
        sys.exit('local main is behind or diverged from origin/main; pull/rebase first')
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
    sh(['git', 'tag', '-a', f'v{version}', '-m', f'Korolev v{version}\n\n{notes}'], repo)
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
    print(json.dumps({'version': version, 'sha': sha, 'release': url, 'lessons': lesson_ids}))
    if not args.no_notify:
        summary = f'Вышла новая версия Korolev v{version}.\n{url}\n\nЧто нового:\n' + '\n'.join(
            line.strip()[2:] for line in notes.splitlines() if line.startswith('- **'))
        subprocess.run([sys.executable, '-I', str(HERE / 'notify.py'), '--project', 'korolev', summary])


if __name__ == '__main__':
    main()
