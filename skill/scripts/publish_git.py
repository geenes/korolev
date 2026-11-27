"""Pin and verify destinations for privileged Git ref transactions."""
import os
import re
import shlex
import subprocess
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

import approvals
from common import HOST_GIT_ENV, transport_environment, trusted_git_executable, trusted_system_executable
from privacy_scan import OBJECT_ENV
from safety_review import BLOB_LIMIT, GitReads, ReviewError, _tree

FULL_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
GITHUB_REPO = re.compile(r'/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)')
GIT_HELPERS = Path('/usr/lib/git-core')
NETWORK_URL = re.compile(r'(https|ssh)://(?:([a-zA-Z0-9_.][a-zA-Z0-9_.-]*)@)?'
                         r'([a-zA-Z0-9.][a-zA-Z0-9.-]*)(?::([0-9]{1,5}))?(/[a-zA-Z0-9_./~%+-]+)')
SCP_URL = re.compile(r'([a-zA-Z0-9_.-]+)@([a-zA-Z0-9.-]+):([a-zA-Z0-9_./~-]+)')
REF_ACTIONS = 'Actions (operation, destination, expected old, exact new)'
CREATE_CI = 'CAS create CI ref (visible before publication, leased to this attempt)'
DELETE_CI = 'CAS delete CI ref at attempt close'
CREATE_PR = 'CAS create PR head'
RELEASE_TARGET = 'CAS update target after CI'
RELEASE_TAG = 'CAS create annotated tag after CI'


class PublishError(ValueError):
    pass


class DestinationChanged(PublishError):
    pass


class GitError(PublishError):
    pass


def transport_config(protocol):
    if protocol not in ('ssh', 'https'):
        return ()
    trusted_system_executable('sh', (Path('/bin'),))  # Git uses its compiled shell path.
    if protocol == 'ssh':
        ssh = shlex.quote(str(trusted_system_executable('ssh')))
        return ('-c', f'core.sshCommand={ssh} -F /dev/null -o BatchMode=yes')
    trusted_system_executable('git-remote-https', (GIT_HELPERS,))
    gh = shlex.quote(str(trusted_system_executable('gh')))
    return ('-c', 'credential.helper=', '-c', f'credential.https://github.com.helper=!{gh} auth git-credential')


def _git(repo, *args, config=(), data=None, lock_fd=None, protocol='none'):
    executable = trusted_git_executable()
    env = HOST_GIT_ENV | OBJECT_ENV
    if 'HOME' in os.environ:
        env['HOME'] = os.environ['HOME']
    if protocol != 'none':
        env.update(transport_environment(), GIT_EXEC_PATH=str(GIT_HELPERS), GIT_ALLOW_PROTOCOL=protocol,
                   GIT_TERMINAL_PROMPT='0')
        config = [*config, *transport_config(protocol)]
    result = subprocess.run([executable, *config, *args], cwd=repo, env=env, input=data,
                            stdin=subprocess.DEVNULL if data is None else None, capture_output=True,
                            pass_fds=() if lock_fd is None else (lock_fd,))
    if result.returncode:
        raise GitError(f'Git exited {result.returncode} running {args[0]}')
    return result.stdout


def isolated_git(repo, *args, protocol='none', data=None, lock_fd=None):
    config = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
              '-c', 'core.attributesFile=/dev/null', '-c', 'protocol.allow=never', '-c', 'http.followRedirects=false',
              '-c', 'push.followTags=false', '-c', 'submodule.recurse=false', '-c', 'maintenance.auto=false']
    if protocol in ('https', 'ssh', 'file'):
        config += ['-c', f'protocol.{protocol}.allow=always']
    return _git(repo, *args, config=config, data=data, lock_fd=lock_fd, protocol=protocol)


def initialize_repository(repo, folder, *, pins=()):
    source = os.fsdecode(isolated_git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
                         .removesuffix(b'\n'))
    if '\n' in source:
        raise PublishError('publication pending: unsupported Git object directory path')
    object_format = isolated_git(repo, 'rev-parse', '--show-object-format').decode().strip()
    isolated_git(folder, 'init', '--bare', '--quiet', '--template=', f'--object-format={object_format}')
    alternates = folder / 'objects' / 'info' / 'alternates'
    alternates.write_bytes(os.fsencode(Path(source) / 'objects') + b'\n')
    if pins:
        for index, oid in enumerate(pins):
            if not FULL_OID.fullmatch(oid):
                raise PublishError('publication pending: expected a pinned Git object ID')
            isolated_git(folder, 'update-ref', f'refs/korolev/input/{index}', oid)
        isolated_git(folder, 'repack', '-a', '-d', '-q')
        alternates.unlink()


@contextmanager
def isolated_repository(repo):
    with tempfile.TemporaryDirectory(prefix='korolev-publish-git-', dir='/tmp') as path:
        folder = Path(path)
        initialize_repository(repo, folder)
        yield folder


def resolve_push_url(repo, remote='origin'):
    try:
        url = isolated_git(repo, 'remote', 'get-url', '--push', '--all', '--', remote)
        url = url.removesuffix(b'\n').decode('utf-8')
    except (GitError, UnicodeError) as error:
        raise PublishError('publication pending: could not resolve the Git push endpoint') from error
    if not url or any(character.isspace() or not character.isprintable() for character in url):
        raise PublishError('publication pending: expected exactly one unambiguous Git push URL')
    return url


def endpoint(url, *, allow_file=False):
    """Return the supported protocol and, for GitHub, the exact repository slug."""
    if scp := SCP_URL.fullmatch(url):
        user, host, path = scp.groups()
        protocol, port, path = 'ssh', None, '/' + path
    elif network := NETWORK_URL.fullmatch(url):
        protocol, user, host, port, path = network.groups()
    elif allow_file and Path(url).is_absolute():
        return 'file', None
    else:
        raise PublishError('publication pending: unsupported Git endpoint')
    if (host.startswith('-') or (user or '').startswith('-') or (protocol == 'ssh') != bool(user)
            or port and not 0 < int(port) <= 65535):
        raise PublishError('publication pending: unsupported Git endpoint')
    host = host.lower().rstrip('.')
    if host not in ('github.com', 'ssh.github.com'):
        return protocol, None
    default_port = 22 if protocol == 'ssh' and host == 'github.com' else 443
    if port and int(port) != default_port or protocol == 'ssh' and user != 'git':
        raise PublishError('publication pending: unsupported GitHub Git endpoint')
    match = GITHUB_REPO.fullmatch(path.removesuffix('.git'))
    if not match or any(part in ('.', '..') for part in match[1].split('/')):
        raise PublishError('publication pending: could not identify the GitHub repository')
    return protocol, match[1]


def verify_destination(repo, destination, identity, *, remote='origin', allow_file=False):
    url = resolve_push_url(repo, remote)
    if url != destination.get('url'):
        raise DestinationChanged('effective Git push URL changed')
    protocol, slug = endpoint(url, allow_file=allow_file)
    if slug is None:
        return url, protocol, None
    expected_id = destination.get('repository_id')
    if not isinstance(expected_id, str) or not expected_id or identity is None:
        raise PublishError('publication pending: missing approved GitHub repository ID')
    actual_id, visibility = identity(slug)
    if actual_id and actual_id != expected_id:
        raise DestinationChanged('GitHub repository identity changed')
    if not actual_id or visibility not in ('PRIVATE', 'PUBLIC', 'INTERNAL'):
        raise PublishError('publication pending: GitHub repository identity or visibility could not be verified')
    return url, protocol, visibility


def check_repository(repo, *, checkout=False):
    # These commands only read configuration and the effective hook path.
    config = _git(repo, 'config', '--includes', '--null', '--list')
    hook = Path(os.fsdecode(_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks/pre-push')
                            .removesuffix(b'\n')))
    if hook.exists() or hook.is_symlink():
        raise PublishError('publication pending: repository has a pre-push hook')
    for entry in config.split(b'\0'):
        key, _, value = entry.partition(b'\n')
        if key.startswith(b'filter.') and (key.lower().startswith(b'filter.lfs.')
                                           or re.search(rb'\bgit[- ]lfs\b', value, re.I)):
            raise PublishError('publication pending: repository has a Git LFS filter')
        if checkout and key.startswith(b'filter.') and key.endswith((b'.required', b'.process', b'.clean', b'.smudge')) and value:
            raise PublishError('publication pending: release checkout requires a configured Git filter')


def check_attributes(repo, commit):
    git = GitReads(repo)
    try:
        with git.output('cat-file', '--batch', batch=True) as blobs:
            for path, (mode, oid) in _tree(git, commit).items():
                if path.rsplit('/', 1)[-1] != '.gitattributes' or mode == b'040000':
                    continue
                kind, data = git.batch(blobs, oid, limit=BLOB_LIMIT)
                if kind != b'blob' or re.search(rb'^[ \t]*[^# \t\r\n][^\r\n]*[ \t]filter(?:[= \t\r]|$)', data, re.M):
                    raise PublishError('release pending: pinned attributes require a Git filter')
    except ReviewError as error:
        raise PublishError(str(error)) from error


def check_outgoing(transport, updates):
    """Inspect outgoing Git objects with a shared budget for headers and small bodies."""
    tips = [new for _, _, new in updates if new]
    if not tips:
        return
    git = GitReads(transport)
    try:
        with (git.output('cat-file', '--batch-check', batch=True) as headers,
              git.output('cat-file', '--batch', batch=True) as blobs,
              closing(git.records('rev-list', '--objects', '--no-object-names',
                                  f'--max-count={git.commits + 1}', *tips,
                                  *(f'^{old}' for _, old, _ in updates if old), '--')) as objects):
            for oid in objects:
                git.charge('objects', 1)
                if not FULL_OID.fullmatch(os.fsdecode(oid)):
                    raise ReviewError('pending: unverifiable outgoing Git object ID')
                kind, size = git.batch(headers, oid, limit=0)
                if kind == b'commit':
                    git.charge('commits', 1)
                if kind != b'blob' or size > 1024:
                    continue
                _, body = git.batch(blobs, oid, limit=1024)
                if re.search(rb'^\s*version[ \t]+(?:https://(?:git-lfs|hawser)\.github\.com/spec/v1|http://git-media\.io/v/2)\s*$', body, re.M):
                    raise PublishError('publication pending: outgoing history contains a Git LFS pointer')
    except ReviewError as error:
        raise PublishError(str(error)) from error


def _allowed_ref(ref, old, new, *, action, target, visibility):
    if ref.startswith('refs/heads/korolev/'):
        name = ref.removeprefix('refs/heads/korolev/')
        if visibility and action in ('release', 'target'):
            return not (old and new) and name in (f'release-ci/{new or old}', f'release-pr/{new or old}')
        return (visibility == 'PRIVATE' and action == 'backup'
                and re.fullmatch(r'(?:[0-9]{2}-[a-z0-9][a-z0-9-]*|archive/[a-zA-Z0-9._/-]+)', name))
    if not new or action not in ('target', 'release'):
        return False
    return ((ref == target and ref.startswith('refs/heads/'))
            or action == 'release' and re.fullmatch(r'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+', ref))


def _ref_operation(action, ref, old, new):
    if action == 'backup':
        verb = 'update' if old and new else 'create' if new else 'delete'
        return f'CAS {verb} backup ref'
    if action in ('release', 'target'):
        if ref.startswith('refs/heads/korolev/release-ci/'):
            return CREATE_CI if new else DELETE_CI
        if ref.startswith('refs/heads/korolev/release-pr/'):
            return CREATE_PR if new else None
        if ref.startswith('refs/tags/'):
            return RELEASE_TAG if not old else None
        return RELEASE_TARGET if action == 'release' else 'CAS update target'
    return None


@contextmanager
def _execution(decision_id, execution_id, action):
    try:
        with approvals.active_execution(decision_id, execution_id, action) as decision:
            yield decision
    except approvals.ReceiptExpired:
        raise
    except approvals.ApprovalError as error:
        raise PublishError(str(error)) from error


def verified_push(repo, updates, *, action, decision_id, execution_id=None, before_write=lambda: None, identity=None,
                  remote='origin', lock_fd=None, github_transport=None, allow_file=False, object_repo=None):
    """Apply signed ref leases with repository-ID and receive-time visibility enforcement on GitHub."""
    with _execution(decision_id, execution_id, action) as decision:
        destination = decision['destination']
        plan = decision['details'].get('action_set', {})
        approved = plan.get(REF_ACTIONS, []) if isinstance(plan, dict) else []
    with isolated_repository(object_repo or repo) as transport:
        seen = set()
        refs = []
        for operation, ref, old, new in updates:
            if (not all(isinstance(value, str) for value in (operation, ref, old, new))
                    or ref in seen or not (old or new)
                    or operation != _ref_operation(action, ref, old, new)
                    or [operation, ref, old or 'absent', new or 'absent'] not in approved
                    or any(oid and not FULL_OID.fullmatch(oid) for oid in (old, new))):
                raise PublishError('publication pending: Git ref update is outside the exact approved action set')
            isolated_git(transport, 'check-ref-format', ref)
            seen.add(ref)
            refs.append((ref, old, new))
        if not seen:
            raise PublishError('publication pending: no exact Git ref updates')
        check_outgoing(transport, refs)
        check_repository(repo)
        url, protocol, visibility = verify_destination(repo, destination, identity, remote=remote, allow_file=allow_file)
        if any(not _allowed_ref(*update, action=action, target=destination.get('branch'), visibility=visibility)
               for update in refs):
            raise PublishError('publication pending: Git ref is not allowed at this destination')
        args = ('push', '--atomic', '--no-verify', '--quiet', '--recurse-submodules=no',
                *(f'--force-with-lease={ref}:{old}' for ref, old, _ in refs),
                '--', url, *(f'{new}:{ref}' for ref, _, new in refs))
        dispatch = isolated_git
        kwargs = {'protocol': protocol, 'lock_fd': lock_fd}
        if visibility and action in ('backup', 'target'):
            method = 'push_private_by_repository_id' if action == 'backup' else 'push_by_repository_id'
            dispatch = getattr(github_transport, method, None)
            if not callable(dispatch):
                raise PublishError('publication pending: GitHub transport lacks required repository-ID or visibility authority')
            kwargs['repository_id'] = destination['repository_id']
        before_write()
        with _execution(decision_id, execution_id, action) as current:
            if current['receipt'] != decision['receipt']:
                raise PublishError('receiver receipt changed before Git dispatch')
            if dispatch is not isolated_git:
                dispatch(transport, args, **kwargs)
            else:
                dispatch(transport, *args, **kwargs)
