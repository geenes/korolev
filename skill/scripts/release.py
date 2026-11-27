#!/usr/bin/env python3
"""Prepare and publish a Korolev release bound to the owner's exact approval.

  release.py notes [--repo PATH]
  release.py propose [--notes FILE]
  release.py approve                read the owner's reply to the proposal
  release.py publish                publish the exact approved release

The owner replies to the proposal message with a to publish or b to reject.
A patch is the default. Use --allow-minor or --allow-major for another version level.
Use propose --dry-run to preview the version from the latest remote release tag and commit that
version in VERSION before proposing.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify  # noqa: E402
from common import (CONFIG_DIR, HOME, SYSTEM_GIT as RELEASE_GIT, append_line,
                    now, parse_timestamp, read_projects, touch_heartbeat, transport_environment,
                    trusted_git_executable, trusted_system_executable, write_atomic)  # noqa: E402
from i18n import t  # noqa: E402
from inbox import RELEASE_APPROVAL_REPLY, RELEASE_REJECTION_REPLY, records  # noqa: E402
from privacy_scan import OBJECT_LIMIT, ScanBudgetError, scan_new_objects, scan_text  # noqa: E402
from publish_git import (FULL_OID, GitError, PublishError as ReleaseError, check_attributes,
                         check_outgoing, check_repository, endpoint, initialize_repository, isolated_git,
                         isolated_repository, resolve_push_url, transport_config, verify_destination)  # noqa: E402

LOG = HOME / 'lessons' / 'lessons.jsonl'
RELEASE_ROOT = HOME / 'release'
PENDING = HOME / 'products' / 'korolev' / 'release-pending.json'
PROJECT = 'korolev'
BRANCH = 'refs/heads/main'
PREPARED_TAG = 'refs/worktree/korolev/tag'
SLUG = 'geenes/korolev'
MAINTAINERS = {'geenes'}
GROUP = {'improvement': 'Added', 'tooling': 'Changed', 'cost': 'Changed', 'mistake': 'Fixed'}
IDENTITY = ('-c', 'user.name=Korolev release', '-c', 'user.email=korolev@localhost')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
UPSTREAM = re.compile(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                      + SLUG + r'(?:\.git)?')


class Drift(ReleaseError):
    pass


@contextmanager
def release_git_environment():
    # The publisher and its nested object readers share this absolute executable.
    executable = trusted_git_executable()
    token = RELEASE_GIT.set(executable)
    try:
        yield
    finally:
        RELEASE_GIT.reset(token)


def git(repo, *args, data=None):
    return isolated_git(repo, *args, data=data).decode('utf-8', 'replace').strip()


def guard_rollback(repo):
    folder = Path(repo).resolve()
    if any(folder == Path(project['folder']).resolve() for project in read_projects().values()):
        raise ReleaseError(f'release rollback refused in registered owner folder {folder}')


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


def candidate_version(repo, base):
    entry = git(repo, 'ls-tree', base, '--', 'VERSION')
    if not entry.startswith(('100644 blob ', '100755 blob ')):
        raise ReleaseError('candidate must commit VERSION as a regular file')
    oid = entry.split()[2]
    if int(git(repo, 'cat-file', '-s', oid)) > 1024:
        raise ReleaseError('release pending: VERSION exceeds the text budget')
    version = git(repo, 'cat-file', 'blob', oid)
    version_tuple(version)
    return version


def bump(version, level):
    major, minor, patch = (int(x) for x in version.split('.'))
    if level == 'major':
        return f'{major + 1}.0.0'
    if level == 'minor':
        return f'{major}.{minor + 1}.0'
    return f'{major}.{minor}.{patch + 1}'


def draft(repo, candidate, published):
    revision = f'{published}..{candidate}' if published else candidate
    commits = git(repo, 'log', '--format=%h %s', revision).splitlines()
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
    return '\n'.join(lines).rstrip() + '\n', [l['id'] for l in lessons]


class GitHubPublisher:
    """The only production backend. Validation precedes every possible network call."""

    def __init__(self, url):
        if not UPSTREAM.fullmatch(url):
            raise ReleaseError('self-publishing is maintainer-only: destination must be the upstream Korolev repo')
        self.protocol = 'https' if url.startswith('https:') else 'ssh'
        self.gh_executable, self.python = (trusted_system_executable(name) for name in ('gh', 'python3'))
        transport_config(self.protocol)

    def gh(self, *args, data=None, lock_fd=None):
        result = subprocess.run([self.gh_executable, *args], input=data, capture_output=True,
                                pass_fds=() if lock_fd is None else (lock_fd,),
                                env=transport_environment())
        if result.returncode:
            raise ReleaseError(f'GitHub CLI exited {result.returncode} running {args[0]}; retry this release')
        return result.stdout

    def api(self, endpoint, *args, **kwargs):
        return self.gh('api', '--hostname', 'github.com', f'repos/{SLUG}/{endpoint}', *args, **kwargs)

    def check_maintainer(self):
        if not (CONFIG_DIR / 'maintainer.json').exists():
            raise ReleaseError('self-publishing is maintainer-only: no local maintainer opt-in')
        if self.gh('api', '--hostname', 'github.com', 'user', '--jq', '.login').decode().strip() not in MAINTAINERS:
            raise ReleaseError('self-publishing is maintainer-only: GitHub login is not a Korolev maintainer')

    def repository_identity(self, slug):
        row = json.loads(self.gh('api', '--hostname', 'github.com', f'repos/{slug}'))
        if row.get('full_name', '').casefold() != slug.casefold():
            raise ReleaseError('publication pending: GitHub returned a different repository name')
        return row.get('node_id'), str(row.get('visibility')).upper()

    def verify_ci(self, sha):
        result = subprocess.run([self.python, '-ISB', str(HERE / 'ci_wait.py'), SLUG, sha,
                                 '--branch', 'main'], capture_output=True, env=transport_environment())
        if result.returncode:
            raise ReleaseError(f'CI did not pass for {sha}; retry when CI is green')

    def list_releases(self):
        output = self.api('releases?per_page=100', '--paginate')
        decoder, pages = json.JSONDecoder(), []
        try:
            output = output.decode().lstrip(' \t\r\n')
            while True:
                page, end = decoder.raw_decode(output)
                if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                    raise ValueError
                pages.append(page)
                if not (output := output[end:].lstrip(' \t\r\n')):
                    break
        except (ValueError, RecursionError):
            raise ReleaseError('release pending: malformed hosted Release list')
        return [row for page in pages for row in page]

    def find_release(self, tag):
        matches = [row for row in self.list_releases() if row['tag_name'] == tag]
        if len(matches) > 1:
            raise ReleaseError('multiple GitHub Releases have the approved tag; inspect them before retrying')
        return matches[0] if matches else None


def destination_refs(repo, url, publisher, *patterns):
    protocol, _ = endpoint(url, allow_file=publisher.protocol == 'file')
    with isolated_repository(repo) as transport:
        result = isolated_git(transport, 'ls-remote', '--refs', '--', url, *patterns, protocol=protocol)
    refs = {}
    for line in result.decode().splitlines():
        oid, ref = line.split('\t', 1)
        if not FULL_OID.fullmatch(oid) or ref in refs:
            raise ReleaseError('destination returned invalid Git refs')
        refs[ref] = oid
    return refs


def version_tuple(version):
    match = VERSION.fullmatch(version)
    if not match:
        raise ReleaseError('VERSION must contain a numeric major.minor.patch version')
    return tuple(map(int, match.groups()))


def version_level(args):
    level = args.level or ('minor' if args.allow_minor else 'major' if args.allow_major else 'patch')
    if (args.allow_minor, args.allow_major) != (level == 'minor', level == 'major'):
        raise ReleaseError(f'version level {level} requires the matching --allow flag')
    return level


def version_tag(tag):
    return tag.startswith('v') and VERSION.fullmatch(tag[1:])


def plan_version(args, released):
    level = version_level(args)
    return bump(released, level), level


def verify_annotated_tag(repo, tag, oid, commit):
    if (not isinstance(oid, str) or not FULL_OID.fullmatch(oid)
            or not isinstance(commit, str) or not FULL_OID.fullmatch(commit)
            or git(repo, 'cat-file', '-t', oid) != 'tag'
            or int(git(repo, 'cat-file', '-s', oid)) > OBJECT_LIMIT
            or git(repo, 'cat-file', 'tag', oid).split('\n', 3)[:3] != [f'object {commit}', 'type commit', f'tag {tag}']
            or git(repo, 'rev-parse', oid + '^{commit}') != commit):
        raise ReleaseError('release pending: annotated tag and peeled commit could not be verified')


def published_boundary(repo, candidate, url, publisher):
    dest = {'url': url, 'branch': BRANCH}
    _, slug = endpoint(url, allow_file=publisher.protocol == 'file')
    repository_id, _ = publisher.repository_identity(slug or SLUG)
    if not isinstance(repository_id, str) or not repository_id:
        raise ReleaseError('release pending: destination repository ID is unverified')
    if slug:
        dest['repository_id'] = repository_id
    refs = destination_refs(repo, url, publisher, BRANCH, 'refs/tags/v*')
    tags = [ref.removeprefix('refs/tags/') for ref in refs if ref.startswith('refs/tags/')]
    tag = max((name for name in tags if version_tag(name)),
              key=lambda name: version_tuple(name[1:]), default=None)
    if tag is None:
        raise ReleaseError('release pending: no version tag on the destination')
    ref = 'refs/tags/' + tag
    tag_oid = refs[ref]
    try:
        if git(repo, 'show-ref', '--verify', '--hash', ref) != tag_oid:
            raise ReleaseError('release pending: local and destination version tags differ')
        commit = git(repo, 'rev-parse', tag_oid + '^{commit}')
        verify_annotated_tag(repo, tag, tag_oid, commit)
        require_ancestor(repo, commit, candidate)
        if old := refs.get(BRANCH):
            require_ancestor(repo, commit, old)
    except GitError as error:
        raise ReleaseError('release pending: published tag object is unavailable locally') from error
    except Drift as error:
        raise ReleaseError('release pending: published boundary ancestry could not be verified') from error
    verify_destination(repo, dest, publisher.repository_identity, allow_file=publisher.protocol == 'file')
    return dest, refs, dict(repository_id=repository_id, tag=tag, tag_sha=tag_oid, commit=commit)


def preview_version(repo, args, publisher_factory, candidate):
    try:
        url = resolve_push_url(repo)
    except ReleaseError:
        if args.command != 'notes' or 'origin' in git(repo, 'remote').splitlines():
            raise
        return None, None, ''  # An unversioned draft for a repository with no origin.
    _, _, published = published_boundary(repo, candidate, url, publisher_factory(url))
    return (*plan_version(args, published['tag'][1:]), published['commit'])


def require_ancestor(repo, old, sha):
    if old:
        try:
            git(repo, 'merge-base', '--is-ancestor', old, sha)
        except GitError as error:
            raise Drift('approved commit is not a verified descendant of the destination branch') from error


def notes_path(worktree):
    return worktree / 'korolev-release-notes'


def prepare(worktree, base, version, notes):
    entry = git(worktree, 'ls-tree', base, '--', 'CHANGELOG.md')
    if entry and not entry.startswith(('100644 blob ', '100755 blob ')):
        raise ReleaseError('CHANGELOG.md must be a regular Git blob')
    old = '# Changelog\n\nAll notable changes to Korolev.\n'
    if entry:
        oid = entry.split()[2]
        if int(git(worktree, 'cat-file', '-s', oid)) > OBJECT_LIMIT:
            raise ReleaseError('release pending: CHANGELOG.md exceeds the text budget')
        old = isolated_git(worktree, 'cat-file', 'blob', oid).decode('utf-8')
    head, separator, rest = old.partition('\n## ')
    entry = f'\n## v{version} ({now()[:10]})\n\n{notes}'
    if not entry.endswith('\n'):
        entry += '\n'
    changelog_text = head.rstrip() + '\n' + entry + separator + rest
    for filename, expected in (('VERSION', version + '\n'), ('CHANGELOG.md', changelog_text)):
        blob = git(worktree, 'hash-object', '-w', '--stdin', data=expected.encode('utf-8'))
        mode = git(worktree, 'ls-tree', base, '--', filename).partition(' ')[0] or '100644'
        git(worktree, 'update-index', '--cacheinfo', f'{mode},{blob},{filename}')
    tree = git(worktree, 'write-tree')
    sha = git(worktree, *IDENTITY, 'commit-tree', tree, '-p', git(worktree, 'rev-parse', 'HEAD'),
              data=f'Release v{version}\n\n{notes}'.encode('utf-8'))
    verify_parent(worktree, sha, base)
    git(worktree, 'update-ref', '--no-deref', 'HEAD', sha, base)
    epoch = git(worktree, 'show', '-s', '--format=%ct', sha)
    tag = (f'object {sha}\ntype commit\ntag v{version}\n'
           f'tagger Korolev release <korolev@localhost> {epoch} +0000\n\n'
           f'Korolev v{version}\n\n{notes}')
    tag_sha = git(worktree, 'mktag', data=tag.encode('utf-8'))
    git(worktree, 'update-ref', PREPARED_TAG, tag_sha)
    notes_path(worktree).write_bytes(notes.encode('utf-8'))
    return sha, tag_sha


def verify_parent(repo, sha, base):
    if git(repo, 'rev-list', '--parents', '-n', '1', sha).split() != [sha, base]:
        raise Drift('prepared release commit must have the candidate base as its sole parent')


def scan_or_refuse(repo, published, tag_sha):
    try:
        commits, findings = scan_new_objects(repo, published, tag_sha, executable=RELEASE_GIT.get())
    except ScanBudgetError as error:
        raise ReleaseError(f'privacy scan refused the release: {error}') from error
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise ReleaseError('privacy scan could not verify outgoing history') from error
    if findings:
        raise ReleaseError('privacy scan blocked the release:\n' + json.dumps(findings, ensure_ascii=False))
    return commits


def notify_owner(body, lock_fd):
    result = subprocess.run([sys.executable, '-I', str(HERE / 'notify.py'), '--project', PROJECT, body],
                            capture_output=True, pass_fds=(lock_fd,))
    if result.returncode:
        raise ReleaseError('owner notification could not be confirmed; retry or inspect the owner inbox')
    return json.loads(result.stdout)['sent']


def scan_review_text(payload):
    if findings := scan_text(payload):
        raise ReleaseError('privacy scan blocked the rendered payload:\n' + json.dumps(findings, ensure_ascii=False))


def prepared_location(details):
    worktree = Path(details['worktree'])
    info = worktree.lstat()
    if (worktree.parent != RELEASE_ROOT.resolve() or worktree.resolve() != worktree or not worktree.is_dir()
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or [info.st_dev, info.st_ino] != details.get('prepared_id')):
        raise Drift('prepared repository identity changed')
    return worktree


def read_prepared(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Drift('prepared repository metadata is invalid')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise Drift('prepared repository metadata exceeds its read limit')
    return data


def remove_worktree(details):
    if not details.get('prepared_id'):
        return
    try:
        worktree = prepared_location(details)
        guard_rollback(worktree)
    except (OSError, Drift):
        return
    shutil.rmtree(worktree)


@contextmanager
def project_lock(name):
    RELEASE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RELEASE_ROOT / f'{PROJECT}-{name}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError(f'this project already has an active {name} process') from error
        yield lock.fileno()


def propose(repo, args, publisher_factory):
    check_repository(repo, checkout=True)
    base = git(repo, 'rev-parse', 'HEAD')
    if (git(repo, 'symbolic-ref', 'HEAD') != BRANCH
            or git(repo, 'diff-index', '--cached', '--no-ext-diff', '--name-only', base)):
        raise ReleaseError('repo must be on a clean main branch')
    url = resolve_push_url(repo)
    publisher = publisher_factory(url)
    publisher.check_maintainer()
    with project_lock('publisher'):
        build_proposal(repo, args, base, url, publisher)


def build_proposal(repo, args, base, url, publisher):
    dest, refs, published = published_boundary(repo, base, url, publisher)
    released = published['tag'][1:]
    version, level = plan_version(args, released)
    candidate = candidate_version(repo, base)
    if candidate != version:
        raise ReleaseError(f'candidate VERSION {candidate!r} does not match the planned version {version} '
                           f'from published v{released}; commit VERSION {version} before proposing')
    dest['tag'] = 'v' + version
    if 'refs/tags/' + dest['tag'] in refs:
        raise ReleaseError('the destination tag already exists; choose a new release version')
    old = refs.get(BRANCH, '')
    require_ancestor(repo, old, base)
    if publisher.find_release(dest['tag']) is not None:
        raise ReleaseError('release pending: a hosted Release already exists for this tag')
    previous = json.loads(PENDING.read_text()) if PENDING.exists() else {}
    stamp = time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime())
    worktree = Path(tempfile.mkdtemp(prefix=stamp, dir=RELEASE_ROOT)).resolve()
    info = worktree.stat()
    location = {'worktree': str(worktree), 'prepared_id': [info.st_dev, info.st_ino]}
    proposed = False
    try:
        check_repository(repo, checkout=True)
        initialize_repository(repo, worktree, pins=tuple(dict.fromkeys(filter(None, (
            base, old, published['tag_sha'],
        )))))
        config_digest = hashlib.sha256(read_prepared(worktree / 'config', 4096)).hexdigest()
        check_attributes(worktree, base)
        git(worktree, 'update-ref', '--no-deref', 'HEAD', base)
        git(worktree, 'read-tree', base)
        if git(worktree, '--no-optional-locks', '-c', 'core.bare=false', f'--work-tree={repo}',
               'status', '--porcelain'):
            raise ReleaseError('repo must be on a clean main branch')
        notes, lesson_ids = release_notes(worktree, args, base, published['commit'])
        payload = notes.encode('utf-8')
        scan_review_text(payload)
        sha, tag_sha = prepare(worktree, base, version, notes)
        require_ancestor(worktree, old, sha)
        if git(worktree, 'rev-parse', PREPARED_TAG) != tag_sha:
            raise ReleaseError('release pending: prepared annotated tag ref changed')
        verify_annotated_tag(worktree, dest['tag'], tag_sha, sha)
        check_outgoing(worktree, [(BRANCH, published['commit'], tag_sha)])
        commits = scan_or_refuse(worktree, published['commit'], tag_sha)
        notes_digest = hashlib.sha256(payload).hexdigest()
        owner = notify.load_env()
        if owner['KOROLEV_CHAT_ID'] != owner.get('KOROLEV_OWNER_ID') or int(owner['KOROLEV_CHAT_ID']) <= 0:
            raise ReleaseError('release proposal requires the private owner chat')
        summary = ''.join(char if char == '\n' or char.isprintable() else ' ' for char in notes.strip()[:1400])
        text = t('release_proposal', version=version, changes=summary,
                 commits='\n'.join('• ' + oid[:12] for oid in commits[:12]), files='—',
                 commit_count=len(commits))
        text += f'\n\nCommit: {sha}\nAnnotated tag: {tag_sha}\nNotes SHA-256: {notes_digest}'
        filename = f'release-notes-v{version}.md'
        notify.prepare_review_payload(filename, payload)
        notify.preflight_approval_request(text)
        notify.send_review_payload(owner, PROJECT, filename, payload)
        from http.client import HTTPException
        from urllib.parse import urlencode

        try:
            data = urlencode({'chat_id': owner['KOROLEV_CHAT_ID'], 'text': text,
                              'disable_web_page_preview': 'true'}).encode()
            message = notify.payload_result(owner, 'sendMessage', data)
            message_id = notify.review_identity(owner, message)['message_id']
            if message.get('text') != text:
                raise ValueError('Telegram returned different content')
        except (HTTPException, OSError, ValueError, KeyError, TypeError):
            raise ReleaseError('approval request delivery could not be confirmed') from None
        if type(date := message.get('date')) is not int or date <= 0:
            raise ReleaseError('approval request delivery did not return a valid Telegram date')
        append_line(notify.BOT_STATE / 'SENT.jsonl', {'message_id': message_id, 'project': PROJECT, 'time': now()})
        pending = dict(version=version, level=level, head=base, sha=sha, tag_sha=tag_sha, destination=dest,
                       published_boundary=published, notes_sha256=notes_digest, expected_old_oid=old,
                       lessons=lesson_ids, prepared_config=config_digest, asked=now(), status='waiting',
                       notes_file=str(notes_path(worktree)), message_id=message_id, date=date, **location)
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(PENDING, json.dumps(pending, ensure_ascii=False, indent=2))
        proposed = True
        remove_worktree(previous)
    finally:
        if not proposed:
            remove_worktree(location)
    print(json.dumps({'proposed': version, 'sha': sha, 'tag_sha': tag_sha, 'message_id': message_id}))


def release_notes(repo, args, candidate, published):
    if args.notes:
        with Path(args.notes).open('rb') as stream:
            payload = stream.read(OBJECT_LIMIT + 1)
        if len(payload) > OBJECT_LIMIT:
            raise ReleaseError('release notes exceed the 20 MiB text budget')
        notes = payload.decode('utf-8')
        lesson_ids = [item['id'] for item in lesson_state().values()
                      if item['status'] == 'applied' and not item['released']]
        return notes, lesson_ids
    return draft(repo, candidate, published)


def approve(pending):
    if not pending.get('prepared_id') or pending['status'] == 'published':
        raise ReleaseError('no current release proposal; run release.py propose')
    require_unexpired(pending)
    if type(date := pending.get('date')) is not int or date <= 0:
        raise ReleaseError('release proposal has no valid Telegram date; run release.py propose again')
    replies = [reply for reply in records() if reply.get('reply_to') == pending['message_id']
               and type(reply.get('date')) is int and reply['date'] >= date]
    if not replies:
        print(f"no owner reply yet to message {pending['message_id']}")
        sys.exit(2)
    answer = replies[-1]['text'].strip().lower().rstrip('.!')
    if RELEASE_APPROVAL_REPLY.match(answer):
        pending.update(status='approved', approval_message=replies[-1]['message_id'], approved=now())
    elif RELEASE_REJECTION_REPLY.match(answer):
        pending.update(status='rejected', rejected=now())
    else:
        print('unclear reply; ask the owner again')
        sys.exit(2)
    write_atomic(PENDING, json.dumps(pending, ensure_ascii=False, indent=2))
    print(pending['status'])


def release_objects(repo, details):
    head = details['head']
    if git(repo, 'rev-parse', '--abbrev-ref', 'HEAD') != 'main' or git(repo, 'rev-parse', 'HEAD') != head:
        raise Drift('HEAD changed since the owner approved; propose again')
    worktree = prepared_location(details)
    if (hashlib.sha256(read_prepared(worktree / 'config', 4096)).hexdigest() != details['prepared_config']
            or any(os.path.lexists(worktree / name) for name in ('.git', 'commondir', 'objects/info/alternates'))):
        raise Drift('prepared repository configuration changed')
    if (git(worktree, 'rev-parse', 'HEAD') != details['sha']
            or git(worktree, 'rev-parse', PREPARED_TAG) != details['tag_sha']):
        raise Drift('prepared repository HEAD or annotated tag changed')
    verify_parent(worktree, details['sha'], details['head'])
    verify_annotated_tag(worktree, details['destination']['tag'], details['tag_sha'], details['sha'])
    notes = read_prepared(notes_path(worktree), OBJECT_LIMIT)
    if hashlib.sha256(notes).hexdigest() != details['notes_sha256']:
        raise Drift('release notes changed since approval; propose again')
    with isolated_repository(worktree) as source:
        git(source, 'update-ref', '--no-deref', 'HEAD', head)
        git(source, 'read-tree', head)
        if (git(repo, 'diff-index', '--cached', '--no-ext-diff', '--name-only', head)
                or git(source, '--no-optional-locks', '-c', 'core.bare=false', f'--work-tree={repo}',
                       'status', '--porcelain')):
            raise Drift('repo must be on a clean main branch')
    return worktree, notes


def require_unexpired(pending):
    if time.time() >= parse_timestamp(pending['asked']) + 24 * 60 * 60:
        raise ReleaseError('approval expired; propose again')


def matching_notes(left, right):
    return left.replace('\r\n', '\n').rstrip('\n') == right.replace('\r\n', '\n').rstrip('\n')


def publish(repo, pending, args, publisher_factory, lock_fd):
    if pending.get('status') != 'approved' or not pending.get('prepared_id'):
        raise ReleaseError('not approved for an exact release; run release.py propose and approve')
    require_unexpired(pending)
    check_repository(repo, checkout=True)
    worktree, notes = release_objects(repo, pending)
    version, sha, tag_sha, old = (pending[key] for key in ('version', 'sha', 'tag_sha', 'expected_old_oid'))
    dest = pending['destination']
    tag, tag_ref = dest['tag'], f'refs/tags/{dest["tag"]}'
    publisher = publisher_factory(dest['url'])
    publisher.check_maintainer()
    require_ancestor(worktree, old, sha)
    scan_review_text(notes)
    scan_or_refuse(worktree, pending['published_boundary']['commit'], tag_sha)
    check_outgoing(worktree, [(BRANCH, old, tag_sha)])
    check_attributes(worktree, sha)
    with tempfile.TemporaryDirectory() as checkout:
        git(worktree, 'read-tree', sha)
        git(worktree, f'--work-tree={checkout}', 'checkout-index', '--all', f'--prefix={checkout}/')
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                                cwd=checkout, env={'PATH': os.defpath, 'HOME': checkout}, capture_output=True)
        if result.returncode or b'Ran 0 tests' in result.stderr:
            raise ReleaseError('offline tests failed; not releasing')

    url, protocol, _ = verify_destination(repo, dest, publisher.repository_identity,
                                          allow_file=publisher.protocol == 'file')
    expected = {BRANCH: sha, tag_ref: tag_sha}
    refs = destination_refs(repo, url, publisher, BRANCH, tag_ref)
    if refs != expected:
        if refs != ({BRANCH: old} if old else {}) or publisher.find_release(tag) is not None:
            raise Drift('destination refs or hosted Release differ from the approved release; inspect before retrying')
        check_repository(repo, checkout=True)
        with isolated_repository(worktree) as transport:
            require_unexpired(pending)
            isolated_git(transport, 'push', '--atomic', '--no-verify', '--quiet', '--recurse-submodules=no',
                         f'--force-with-lease={BRANCH}:{old}', f'--force-with-lease={tag_ref}:',
                         '--', url, f'{sha}:{BRANCH}', f'{tag_sha}:{tag_ref}', protocol=protocol, lock_fd=lock_fd)

    publisher.verify_ci(sha)
    verify_destination(repo, dest, publisher.repository_identity, allow_file=publisher.protocol == 'file')
    if destination_refs(repo, url, publisher, BRANCH, tag_ref) != expected:
        raise Drift('destination refs changed after the push; inspect before retrying')
    hosted = publisher.find_release(tag)
    require_unexpired(pending)
    if hosted is None:
        release_url = publisher.gh('release', 'create', tag, '--title', f'Korolev {tag}', '--notes-file', '-',
                                   '--verify-tag', '--repo', f'github.com/{SLUG}', data=notes, lock_fd=lock_fd).decode().strip()
    elif (not hosted['draft'] and hosted['name'] == f'Korolev {tag}'
          and matching_notes(hosted['body'] or '', notes.decode('utf-8'))):
        release_url = hosted['html_url']
    else:
        raise ReleaseError('hosted Release differs from the approval; inspect before retrying')
    if not release_url:
        raise ReleaseError('GitHub publication has no release URL; inspect before retrying')

    lessons = lesson_state()
    for lesson_id in pending['lessons']:
        if lesson_id in lessons and not lessons[lesson_id]['released']:
            append_line(LOG, {'event': 'status', 'id': lesson_id, 'time': now(), 'status': 'released',
                              'version': version})
    if not args.no_notify:
        changes = '\n'.join('• ' + line[2:] for line in notes.decode('utf-8').splitlines()
                            if line.startswith('- **'))
        body = t('release_published', version=version, url=release_url, changes=changes)
        scan_review_text(body.encode())
        notify_owner(body, lock_fd)
    pending.update(status='published', published=now(), release=release_url)
    write_atomic(PENDING, json.dumps(pending, ensure_ascii=False, indent=2))
    remove_worktree(pending)
    print(json.dumps({'version': version, 'sha': sha, 'release': release_url, 'lessons': pending['lessons']}))


def main(argv=None, *, publisher_factory=GitHubPublisher):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['notes', 'propose', 'approve', 'publish'])
    parser.add_argument('--repo', default=str(HERE.parents[1]))
    parser.add_argument('--notes', help='edited notes file to use instead of the generated draft')
    parser.add_argument('--level', choices=['patch', 'minor', 'major'])
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--allow-minor', action='store_true')
    flags.add_argument('--allow-major', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.command in ('approve', 'publish') and args.dry_run:
        parser.error('--dry-run applies only to notes or propose')
    repo = Path(args.repo).resolve()
    try:
        with release_git_environment():
            HOME.mkdir(parents=True, mode=0o700, exist_ok=True)
            touch_heartbeat(PROJECT, 'Working on Korolev release tasks')
            if args.command == 'notes' or args.dry_run:
                candidate = git(repo, 'rev-parse', 'HEAD')
                version, level, published = preview_version(repo, args, publisher_factory, candidate)
                notes, _ = release_notes(repo, args, candidate, published)
                title = f'## v{version}' if version else '## Local release notes draft'
                if args.command == 'notes':
                    (repo / 'RELEASE-NOTES.draft.md').write_text(f'{title}\n\n{notes}', encoding='utf-8')
                print(f'{title} ({level})\n\n{notes}' if level else f'{title}\n\n{notes}')
            elif args.command == 'propose':
                propose(repo, args, publisher_factory)
            else:
                with project_lock('publisher') as lock_fd:
                    if not PENDING.exists():
                        raise ReleaseError('no release proposal; self-publishing is maintainer-only. Run release.py propose')
                    pending = json.loads(PENDING.read_text())
                    if args.command == 'approve':
                        approve(pending)
                    else:
                        publish(repo, pending, args, publisher_factory, lock_fd)
    except (ValueError, OSError, KeyError) as error:
        sys.exit(str(error))


if __name__ == '__main__':
    main()
