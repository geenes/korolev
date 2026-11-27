#!/usr/bin/env python3
"""Build and check Korolev's bubblewrap worker filesystem."""
import argparse
import errno
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from fnmatch import fnmatchcase
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIG_DIR, HOST_GIT_ENV, MINIMAL_CODEX_CONFIG, SKILL, SYSTEM_BIN_DIRS,  # noqa: E402
                    codex_home as shared_codex_home, launcher_git, trusted_git_executable,
                    trusted_system_executable, user_codex_home, verify_worktree_backlink)
import egress_proxy  # noqa: E402

SYSTEM_DIRS = tuple(map(Path, ('/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc')))
HIDDEN_DIRS = tuple(map(Path, ('/home', '/root', '/var', '/srv', '/mnt', '/media')))
TOOLCHAIN_TOOLS = ('python', 'python3', 'node', 'deno', 'bun', 'ruby', 'go', 'cargo', 'rustc',
                   'java', 'javac', 'dotnet', 'php', 'perl', 'swift', 'gcc', 'clang', 'julia')
CREDENTIAL_PATTERNS = ('*.env', '.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*',
                       '*.p12', '*.sqlite', '*.sqlite3', '*.db', '*.sqlite-*', 'credentials*.json',
                       'token.json', '*.token.json', 'service-account*.json')
CREDENTIAL_TEMPLATES = ('.example', '.sample', '.template', '.dist')
SYSTEM_PATH = ('/usr/local/bin', '/usr/bin', '/bin')
SECRET_ENV = re.compile(r'^(?!(?-i:PWD|OLDPWD)\Z).*?(?:TOKEN|SECRET|KEY|PASS|PWD|CRED|BEARER|PRIVATE|CERT|DSN|NETRC|'
                        r'AUTH|COOKIE|SESSION|_PAT$|WEBHOOK|DATABASE_URL)', re.IGNORECASE | re.DOTALL)
UNSAFE_ENV = re.compile(r'^(?:LD_|KOROLEV_|WORKER_KOROLEV_|BASH_ENV$|ENV$|PYTHON|NODE_OPTIONS$|GIT_)')
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
PROXY_ENV = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY')
BWRAP = Path('/usr/bin/bwrap')
RUNTIME_DIRS = (Path('/run'), Path('/var/run'))
SOCKET_WALK_DEPTH = 12
SOCKET_WALK_ENTRIES = 20_000
HOST_NETWORK_WARNING = 'worker_network is set to host. Host abstract sockets and local ports are reachable.'
executable_info = Path.lstat
# Pin the launcher paths and helper source before preparing any worker checkout.
INSTALL_ROOT = SKILL.parent if any((SKILL.parent / name).exists() for name in ('.git', 'install.sh')) else SKILL
WORKER_EXEC_SOURCE = Path(__file__).resolve().with_name('worker_exec.py').read_text(encoding='utf-8')


class CredentialMaskError(OSError):
    pass


class BubblewrapError(OSError):
    pass


class SocketWalkBudgetExceeded(OSError):
    pass


def credential_name(name):
    from common import normalized_name

    name = normalized_name(name)
    return (not name.endswith(CREDENTIAL_TEMPLATES)
            and any(fnmatchcase(name, pattern) for pattern in CREDENTIAL_PATTERNS))


def empty_mask_file(product_dir):
    """Return a read-only empty regular file used to hide credential-like files."""
    path = Path(product_dir) / '.sandbox-empty'
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size)):
        raise OSError(f'mask file is not an empty regular file: {path}')
    if not path.exists():
        path.touch(mode=0o444)
    return path.resolve()


def credential_masks(root):
    def walk_error(error):
        raise CredentialMaskError('cannot complete the credential walk') from error

    matches = []
    for directory, dirs, files in os.walk(root, onerror=walk_error):
        directory = Path(directory)
        if len(directory.relative_to(root).parts) > 12:
            raise CredentialMaskError('credential walk exceeds depth 12')
        dirs[:] = [name for name in dirs if name not in ('.git', 'node_modules')
                   and not (directory / name).is_symlink()]
        for name in files:
            path = directory / name
            if not credential_name(name):
                continue
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                walk_error(error)
            if stat.S_ISREG(mode):
                matches.append(path)
                if len(matches) > 2000:
                    raise CredentialMaskError('credential walk exceeds 2000 matches')
    return matches


def worker_config():
    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def enabled(config=None):
    config = worker_config() if config is None else config
    return config.get('worker_sandbox') is not False


def network_mode(config):
    mode = config.get('worker_network', 'isolated')
    if mode not in ('host', 'isolated'):
        raise OSError('worker_network must be "isolated" or "host"')
    if not enabled(config) and mode != 'host':
        raise OSError('worker_sandbox: false requires explicit worker_network: "host"')
    return mode


def disable_hint():
    return (f'To disable, set "worker_sandbox": false and "worker_network": "host" '
            f'in {CONFIG_DIR / "config.json"}.')


def bwrap_path():
    try:
        info = BWRAP.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise BubblewrapError(f'{BWRAP} must be a root-owned regular file writable only by root')
    return str(trusted_system_executable(BWRAP.name, (BWRAP.parent,)))


def check_helper_mounts(helper, mounts):
    covered = False
    for destination, (option, *sources) in mounts.items():
        visible = destination.resolve()
        if helper.is_relative_to(destination) or helper.is_relative_to(visible):
            if (option != '--ro-bind' or destination not in SYSTEM_DIRS
                    or Path(sources[0]) not in (destination, visible)):
                raise OSError(f'worker sandbox mount shadows trusted {helper.name} at {destination}')
            covered = True
    if not covered:
        raise OSError(f'worker sandbox is missing the trusted {helper.name} system mount')


def check_install_overlap(*writable):
    for path in writable:
        if path is not None:
            path = Path(path).resolve()
            if path.is_relative_to(INSTALL_ROOT) or INSTALL_ROOT.is_relative_to(path):
                raise OSError('a writable worker path overlaps the Korolev installation')


def trusted_executable(command, *writable, system_fallback=False):
    """Pin a launcher executable outside the paths a worker can modify."""
    command = Path(command)
    search = os.get_exec_path()
    if system_fallback:
        search.extend(os.defpath.split(os.pathsep))
    entries = tuple(Path(entry) for entry in dict.fromkeys(search) if os.path.isabs(entry))
    home = Path.home().resolve()
    blocked = {Path(path).resolve() for path in writable if path is not None}
    for entry in entries:
        real = entry.resolve()
        if ((entry.is_relative_to(home) or real.is_relative_to(home))
                and any(shutil.which(tool, path=str(entry)) for tool in TOOLCHAIN_TOOLS)):
            blocked.update((entry, real))
    candidates = (command,) if command.is_absolute() else (entry / command for entry in entries)
    for candidate in candidates:
        try:
            real = candidate.resolve(strict=True)
            if any(path.is_relative_to(root) for path in (candidate, real) for root in blocked):
                continue
            for path in {candidate, real, *candidate.parents, *real.parents}:
                info = executable_info(path)
                if info.st_uid not in (0, os.getuid()) or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                    break
            else:
                if stat.S_ISREG(executable_info(real).st_mode) and os.access(real, os.X_OK):
                    return real
        except (OSError, RuntimeError):
            continue
    raise OSError(f'no trusted {command.name} executable found outside worker-writable paths')


def worker_env(config, *, home, codex_home, toolchains=(), proxy=False, warn=True, sandboxed=True):
    user = pwd.getpwuid(os.getuid()).pw_name
    env = {'PATH': os.pathsep.join(dict.fromkeys((*map(str, toolchains), *SYSTEM_PATH))),
           'HOME': str(home), 'LANG': os.environ.get('LANG') or 'C.UTF-8',
           'LC_ALL': os.environ.get('LC_ALL') or 'C.UTF-8', 'TERM': 'dumb', 'TMPDIR': '/tmp',
           'GIT_TERMINAL_PROMPT': '0', 'USER': user, 'LOGNAME': user}
    if codex_home is not None:
        env['CODEX_HOME'] = str(codex_home)
    if sandboxed:
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    if proxy:
        address = f'http://127.0.0.1:{egress_proxy.PORT}'
        for name in PROXY_ENV:
            env[name] = env[name.lower()] = '' if name == 'NO_PROXY' else address
    if not sandboxed:
        env.update({name: value for name, value in os.environ.items() if name.upper() in PROXY_ENV})
    extra = config.get('worker_env_allow', [])
    if not isinstance(extra, list) or any(not isinstance(name, str) or not ENV_NAME.fullmatch(name)
                                          for name in extra):
        raise OSError('worker_env_allow must be a list of environment variable names')
    for name in dict.fromkeys(extra):
        if SECRET_ENV.search(name):
            if warn:
                print(f'WARNING: refusing secret-looking worker_env_allow name {name}', file=sys.stderr)
        elif sandboxed and UNSAFE_ENV.match(name.upper()):
            if warn:
                print(f'WARNING: refusing unsafe worker_env_allow name {name}', file=sys.stderr)
        elif name != 'CODEX_HOME' and name in os.environ and not (proxy and name.upper() in PROXY_ENV):
            env.setdefault(name, os.environ[name])
    return env


BRIDGE = r'''
    if [ -L "/proc/$$/fd/9" ]; then
        echo "worker egress bridge cannot save stdin: fd 9 is in use" >&2
        exit 78
    fi
    exec 9<&0
    "$1" "TCP-LISTEN:$2,bind=127.0.0.1,fork,reuseaddr,max-children=32" "UNIX-CONNECT:$3" </dev/null 9<&- &
    bridge=$!
    trap 'kill "$bridge" 2>/dev/null || :; wait "$bridge" 2>/dev/null || :' EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    trap 'exit 129' HUP
    listen=$4
    pause=$5
    shift 5
    unset ready
    attempt=0
    while [ "$attempt" -lt 100 ]; do
        attempt=$((attempt + 1))
        kill -0 "$bridge" 2>/dev/null || break
        while read -r slot local remote state rest; do
            if [ "$local" = "$listen" ] && [ "$state" = 0A ]; then
                ready=1
                break
            fi
        done </proc/net/tcp
        if [ -n "${ready:-}" ]; then
            # Preserve the launcher's prompt while checking /proc/net/tcp.
            "$@" <&9 9<&-
            exit $?
        fi
        "$pause" 0.05
    done
    echo "worker egress bridge failed to start" >&2
    exit 78
'''


def toolchain_dirs(home):
    """Only expose PATH entries that actually provide a language toolchain."""
    excluded = (*SYSTEM_DIRS, *HIDDEN_DIRS, home, Path('/run'))
    for entry in os.get_exec_path():
        if not os.path.isabs(entry):
            continue
        directory = Path(os.path.abspath(entry))
        real = directory.resolve()
        if any(candidate in (Path('/'), Path('/tmp')) or
               any(candidate.is_relative_to(path) for path in excluded) for candidate in (directory, real)):
            continue
        if any(shutil.which(tool, path=entry) for tool in TOOLCHAIN_TOOLS):
            yield directory
            yield real


def socket_masks(mounts, *, git_common_dir=None, on_budget_exceeded=None):
    """Walk the visible, non-system directory binds with one shared entry budget."""
    entries = 0
    matches = []

    def walk(source, destination, depth):
        nonlocal entries
        if depth > SOCKET_WALK_DEPTH:
            raise OSError(f'Unix socket walk exceeds depth {SOCKET_WALK_DEPTH}')
        with os.scandir(source) as children:
            for entry in children:
                entries += 1
                if entries > SOCKET_WALK_ENTRIES:
                    raise SocketWalkBudgetExceeded(f'Unix socket walk exceeds {SOCKET_WALK_ENTRIES} entries')
                path = destination / entry.name
                if path in mounts:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == 'objects' and (source.name == '.git' or source == git_common_dir):
                        continue
                    walk(Path(entry.path), path, depth + 1)
                elif stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode):
                    matches.append(path)

    try:
        for destination, options in mounts.items():
            if options[0] not in ('--bind', '--ro-bind') or destination in SYSTEM_DIRS:
                continue
            if git_common_dir is not None and destination == git_common_dir:
                continue
            source = Path(options[1]).resolve(strict=False)
            if source.is_dir():
                walk(source, destination, 0)
            elif destination != Path(egress_proxy.SANDBOX_SOCKET) and source.is_socket():
                matches.append(destination)
    except SocketWalkBudgetExceeded as error:
        if on_budget_exceeded is None:
            raise
        on_budget_exceeded(f'{error}. The worker socket filter is still enforced.')
    return matches


@contextmanager
def private_codex_home():
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='korolev-codex-') as directory:
        home = Path(directory)
        (home / 'config.toml').write_bytes(MINIMAL_CODEX_CONFIG)
        yield home


def worker_proxy(stack, log_path, patterns, **options):
    directory = stack.enter_context(tempfile.TemporaryDirectory(dir='/tmp', prefix='korolev-egress-'))
    return stack.enter_context(egress_proxy.serve(directory, log_path, patterns, **options))


@contextmanager
def worker_auth():
    # O_PATH inspects the inode without opening devices or blocking on FIFOs.
    try:
        home_fd = os.open(user_codex_home(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield None
        return
    try:
        info = os.fstat(home_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError('Codex home must be user-owned and writable only by the user')
        try:
            path_fd = os.open('auth.json', os.O_PATH | os.O_NOFOLLOW, dir_fd=home_fd)
        except FileNotFoundError:
            yield None
            return
    finally:
        os.close(home_fd)
    try:
        info = os.fstat(path_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & ~0o600 or info.st_nlink != 1):
            raise OSError('Codex auth.json must be a singly linked, user-owned regular file with mode 0600 or stricter')
        source_fd = os.open(f'/proc/self/fd/{path_fd}', os.O_RDONLY | os.O_CLOEXEC)
        try:
            # bwrap consumes the snapshot FD. Keep it clear of the bridge's saved stdin.
            fd = fcntl.fcntl(source_fd, fcntl.F_DUPFD_CLOEXEC, 10)
        finally:
            os.close(source_fd)
    finally:
        os.close(path_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def git_source(parent, name, *, directory=False):
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_RDONLY | os.O_DIRECTORY if directory else os.O_PATH
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        yield None
        return
    try:
        if directory:
            yield fd
        else:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f'Git metadata must be a regular file: {name}')
            # Reopen the verified inode for reading, without looking up its name again.
            with open(f'/proc/self/fd/{fd}', 'rb') as stream:
                yield stream
    finally:
        os.close(fd)


@contextmanager
def git_root(path):
    with ExitStack() as stack:
        fd = None
        for part in path.parts:
            fd = stack.enter_context(git_source(fd, part, directory=True))
            if fd is None:
                raise OSError(f'Git projection directory is unavailable: {path}')
        yield fd


def projection_timeout(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('review Git deadline expired')
    return remaining


def sanitized_git_config(config, *, executable=None, deadline=None):
    # Use Git's parser without loading includes or emitting any other values.
    git = executable or trusted_git_executable()
    result = subprocess.run([git, 'config', '--no-includes', '--file', '-', '--null', '--get-regexp',
                             r'^(core\.(repositoryformatversion|filemode|logallrefupdates)|extensions\.objectformat)$'],
                            env=HOST_GIT_ENV, stdin=config, capture_output=True, timeout=projection_timeout(deadline))
    if result.returncode not in (0, 1):
        raise OSError('cannot read core settings from Git config')
    settings = {b'repositoryformatversion': b'0', b'filemode': b'true', b'logallrefupdates': b'true',
                b'extensions.objectformat': None}
    for entry in result.stdout.split(b'\0'):
        if not entry:
            continue
        key, separator, value = entry.removeprefix(b'core.').partition(b'\n')
        value = value.lower() if separator else b'true'
        if key == b'extensions.objectformat':
            allowed = (b'sha1', b'sha256')
        elif key == b'repositoryformatversion':
            allowed = (b'0', b'1')
        else:
            allowed = (b'true', b'false', b'yes', b'no', b'on', b'off', b'1', b'0', b'')
            if key == b'logallrefupdates':
                allowed += (b'always',)
        if key not in settings or value not in allowed:
            setting = 'object format' if key == b'extensions.objectformat' else 'core setting'
            raise OSError(f'Git config has an invalid {setting}')
        settings[key] = value
    object_format = settings.pop(b'extensions.objectformat')
    if object_format is not None and settings[b'repositoryformatversion'] != b'1':
        raise OSError('Git object format requires repository format version 1')
    extensions = b'[extensions]\n\tobjectFormat = ' + object_format + b'\n' if object_format else b''
    return b'[core]\n\tbare = false\n' + b''.join(b'\t' + key + b' = ' + value + b'\n'
                                                 for key, value in settings.items()) + extensions


def copy_git_file(parent, name, destination, deadline=None):
    projection_timeout(deadline)
    with git_source(parent, name) as source:
        if source is None:
            return False
        with (destination / name).open('wb') as target:
            if name == 'packed-refs':
                skipped, first = False, True
                while line := source.readline(65536):
                    projection_timeout(deadline)
                    if first and not line.startswith(b'^'):
                        skipped = line.partition(b' ')[2].startswith(b'refs/replace/')
                    if not skipped:
                        target.write(line)
                    first = line.endswith(b'\n')
            else:
                while chunk := source.read(65536):
                    projection_timeout(deadline)
                    target.write(chunk)
    projection_timeout(deadline)
    return True


def git_entries(directory, deadline):
    with os.scandir(directory) as entries:
        for entry in entries:
            projection_timeout(deadline)
            yield entry
    projection_timeout(deadline)


def copy_git_refs(parent, name, destination, deadline, *, skip=()):
    destination.mkdir()
    with git_source(parent, name, directory=True) as directory:
        if directory is None:
            return
        for entry in git_entries(directory, deadline):
            if entry.name in skip or entry.name.endswith('.lock'):
                continue
            if entry.is_dir(follow_symlinks=False):
                copy_git_refs(directory, entry.name, destination / entry.name, deadline)
            else:
                copy_git_file(directory, entry.name, destination, deadline)


def copy_git_objects(common, destination, deadline):
    destination.mkdir()
    (destination / 'info').mkdir()
    with git_source(common, 'objects', directory=True) as objects:
        if objects is None:
            return
        with git_source(objects, 'info', directory=True) as info:
            if info is not None and any(entry.name in {'alternates', 'http-alternates'}
                                        for entry in git_entries(info, deadline)):
                raise OSError('Git alternates-based object stores are unsupported')
        for entry in git_entries(objects, deadline):
            name = entry.name
            if name == 'pack':
                pattern = r'pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.(?:pack|idx|rev|bitmap|mtimes)'
            elif re.fullmatch(r'[0-9a-f]{2}', name):
                pattern = r'[0-9a-f]{38}|[0-9a-f]{62}'
            else:
                continue
            with git_source(objects, name, directory=True) as directory:
                if directory is not None:
                    target = destination / name
                    target.mkdir()
                    for leaf in git_entries(directory, deadline):
                        if re.fullmatch(pattern, leaf.name):
                            copy_git_file(directory, leaf.name, target, deadline)


def reject_git_entries(path, names):
    if path != Path('.') and '.git' in names:
        raise OSError(f'embedded Git repository is unsupported: {path / ".git"}')
    if 'HEAD' in names and ('objects' in names or 'commondir' in names):
        raise OSError(f'bare Git repository is unsupported: {path}')


def reject_embedded_git(clone_fd, deadline=None):
    def walk_error(error):
        raise OSError('cannot check the checkout for embedded Git repositories') from error

    for path, dirs, files, _ in os.fwalk(dir_fd=clone_fd, onerror=walk_error):
        projection_timeout(deadline)
        reject_git_entries(Path(path), {*dirs, *files})
        if '.git' in dirs:
            dirs.remove('.git')


CHECKOUT_COPY_BUDGET = 1024 ** 3  # Total logical bytes, including holes.
CHECKOUT_ENTRY_LIMIT = 100_000
CHECKOUT_METADATA_BUDGET = 128 * 1024 ** 2
CHECKOUT_ENTRY_CHARGE = 1024


def copy_checkout_file(source, target, info):
    size = info.st_size
    position = copied = 0
    while position < size:
        try:
            start = os.lseek(source.fileno(), position, os.SEEK_DATA)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break
            if error.errno in (errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError('checkout filesystem cannot report sparse file extents') from error
            raise
        if start >= size:
            break
        end = min(os.lseek(source.fileno(), start, os.SEEK_HOLE), size)
        if not position <= start < end:
            raise OSError('invalid checkout file extent')
        copied += end - start
        if copied > info.st_blocks * 512:
            raise OSError('checkout filesystem did not report sparse file holes')
        source.seek(start)
        target.seek(start)
        remaining = end - start
        while remaining:
            data = source.read(min(remaining, 1024 * 1024))
            if not data:
                raise OSError('checkout file shrank during export')
            target.write(data)
            remaining -= len(data)
        position = end
    target.truncate(size)
    if os.fstat(source.fileno()).st_size != size:
        raise OSError('checkout file size changed during export')


def copy_checkout(source, destination, *, skip_special=False):
    """Freeze worktree contents without following names to other inodes."""
    budget = CHECKOUT_COPY_BUDGET
    metadata_budget = CHECKOUT_METADATA_BUDGET
    remaining_entries = CHECKOUT_ENTRY_LIMIT

    def charge_metadata(size):
        nonlocal metadata_budget
        metadata_budget -= size
        if metadata_budget < 0:
            raise OSError('checkout export exceeds its cumulative metadata budget')

    def copy_directory(directory, relative):
        nonlocal budget, remaining_entries
        target = destination / relative
        names = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                remaining_entries -= 1
                if remaining_entries < 0:
                    raise OSError('checkout export exceeds its cumulative entry limit')
                charge_metadata(CHECKOUT_ENTRY_CHARGE + len(os.fsencode(entry.name)))
                names.add(entry.name)
        reject_git_entries(relative, names)
        root_git = None
        if relative == Path('.'):
            # A lookup can find the root metadata under a differently cased name.
            with suppress(FileNotFoundError):
                root_git = os.stat('.git', dir_fd=directory, follow_symlinks=False)
        omitted = False
        target.mkdir()
        for name in sorted(names):
            fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if root_git is not None and os.path.samestat(info, root_git):
                    if (omitted or ('.git' in names and name != '.git')
                            or (not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1)):
                        raise OSError('ambiguous or multiply linked root Git metadata')
                    omitted = True
                    continue
                if relative == Path('.') and name == '.git':
                    raise OSError('root Git metadata changed during export')
                mode = info.st_mode
                pinned = f'/proc/self/fd/{fd}'
                output = target / name
                if stat.S_ISDIR(mode):
                    child = os.open(pinned, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        copy_directory(child, relative / name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(mode):
                    if info.st_size > budget:
                        raise OSError('checkout export exceeds its cumulative byte budget')
                    budget -= info.st_size
                    with open(pinned, 'rb', buffering=0) as data, output.open('xb') as file:
                        copy_checkout_file(data, file, info)
                    output.chmod(stat.S_IMODE(mode) & 0o777)
                elif stat.S_ISLNK(mode):
                    link = os.readlink('', dir_fd=fd)
                    charge_metadata(len(os.fsencode(link)))
                    output.symlink_to(link)
                elif not skip_special:
                    raise OSError(f'unsupported checkout entry: {relative / name}')
            finally:
                os.close(fd)

        if root_git is not None and not omitted:
            raise OSError('root Git metadata changed during export')
        # Git looks up this name on the frozen filesystem, which can fold case.
        try:
            (target / '.git').lstat()
        except FileNotFoundError:
            return
        raise OSError(f'embedded Git repository is unsupported: {relative / ".git"}')

    copy_directory(source, Path('.'))


def reject_split_index(directory, deadline):
    if any(entry.name.startswith('sharedindex.') for entry in git_entries(directory, deadline)):
        raise OSError('Git split index (sharedindex.*) is unsupported')


def git_entry_identity(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def git_ref_identities(parent, name, deadline):
    identity = git_entry_identity(parent, name)
    if identity is None or not stat.S_ISDIR(identity[2]):
        return identity
    try:
        with git_source(parent, name, directory=True) as directory:
            entries = {} if directory is None else {
                entry.name: git_ref_identities(directory, entry.name, deadline)
                for entry in git_entries(directory, deadline) if not entry.name.endswith('.lock')
            }
    except NotADirectoryError:
        return identity
    return identity, entries


def git_capture_identity(common, private, deadline):
    # Include the parent directories so even a transiently created entry can cause drift.
    projection_timeout(deadline)
    return (git_entry_identity(common, '.'), git_entry_identity(common, 'HEAD'),
            git_entry_identity(common, 'packed-refs'),
            git_ref_identities(common, 'refs', deadline),
            git_entry_identity(common if private is None else private, 'index'),
            None if private is None else (git_entry_identity(private, '.'), git_entry_identity(private, 'HEAD'),
                                          git_ref_identities(private, 'refs', deadline)))


def has_private_ref(tree, deadline):
    projection_timeout(deadline)
    return tree is not None and (len(tree) != 2 or any(has_private_ref(entry, deadline) for entry in tree[1].values()))


def copy_git_metadata(common_fd, private_fd, visible, private, executable, deadline):
    visible.mkdir()
    if private_fd is not None:
        record = visible / 'worktrees' / private.name
        record.mkdir(parents=True)
        if not copy_git_file(private_fd, 'HEAD', record, deadline):
            return False
        copy_git_file(private_fd, 'index', record, deadline)
        if git_entry_identity(private_fd, 'refs') is not None:
            copy_git_refs(private_fd, 'refs', record / 'refs', deadline)
        reject_split_index(private_fd, deadline)
        (record / 'commondir').write_bytes(b'../..\n')
        # List this worktree in fsck without giving host Git a live checkout path.
        (record / 'gitdir').write_bytes(os.fsencode(visible.parent / 'checkout' / '.git') + b'\n')
    else:
        copy_git_file(common_fd, 'index', visible, deadline)
    reject_split_index(common_fd, deadline)
    with git_source(common_fd, 'config') as config:
        if config is not None:
            (visible / 'config').write_bytes(sanitized_git_config(config, executable=executable, deadline=deadline))
    for name in ('HEAD', 'packed-refs'):
        copy_git_file(common_fd, name, visible, deadline)
    copy_git_objects(common_fd, visible / 'objects', deadline)
    copy_git_refs(common_fd, 'refs', visible / 'refs', deadline, skip=('replace',))
    return True


def capture_git(common_fd, private_fd, visible, private, executable, deadline):
    before = git_capture_identity(common_fd, private_fd, deadline)
    try:
        copied = copy_git_metadata(common_fd, private_fd, visible, private, executable, deadline)
    except TimeoutError:
        raise
    except OSError:
        if git_capture_identity(common_fd, private_fd, deadline) != before:
            return False
        raise
    if git_capture_identity(common_fd, private_fd, deadline) != before or not copied:
        return False
    if private_fd is not None and has_private_ref(before[-1][-1], deadline):
        raise OSError('Git snapshot has unsupported private worktree refs')
    # With no explicit object IDs fsck includes refs, HEAD and the worktree index.
    return launcher_git(visible, f'--git-dir={visible}', 'fsck', '--connectivity-only',
                        '--no-reflogs', '--no-dangling', executable=executable,
                        timeout=projection_timeout(deadline)).returncode == 0


def git_mounts(clone, common, storage, *, write=False, git_executable=None, checkout_fd=None, deadline=None,
               review_git_fds=None):
    if storage is None:
        raise OSError('private storage is required for Git metadata')
    if storage.is_relative_to(clone):
        raise OSError('worker sandbox directory bind exposes Git metadata storage')
    metadata = clone / '.git'
    linked = common is not None and common != metadata
    common = common or metadata
    prefix = b'gitdir: '
    with ExitStack() as stack:
        checkout = checkout_fd if checkout_fd is not None else stack.enter_context(git_root(clone))
        if review_git_fds and not os.path.samestat(os.fstat(checkout), clone.lstat()):
            raise OSError('checkout directory changed before the sandbox bind')
        private = private_fd = None
        if linked:
            gitfile = stack.enter_context(git_source(checkout, '.git'))
            pointer = gitfile.read().rstrip(b'\r\n') if gitfile is not None else b''
            if not pointer.startswith(prefix):
                raise OSError('expected a Git worktree pointer file')
            private = Path(os.path.abspath(clone / os.fsdecode(pointer[len(prefix):])))
            if private.parent != common / 'worktrees':
                raise OSError('Git worktree metadata is outside the common directory')
            common_fd = review_git_fds[0] if review_git_fds else stack.enter_context(git_root(common))
        else:
            common_fd = stack.enter_context(git_source(checkout, '.git', directory=True))
            if common_fd is None:
                if write:
                    raise OSError('writable worker checkout has no .git directory')
                # Create the mountpoint in private storage, outside the original checkout.
                view = Path(tempfile.mkdtemp(dir=storage, prefix='gitless-')) / 'checkout'
                try:
                    copy_checkout(checkout, view, skip_special=True)
                    (view / '.git').mkdir()
                except BaseException:
                    shutil.rmtree(view.parent)
                    raise
                return {clone: ('--ro-bind', str(view))}
        if checkout_fd is None:
            raise OSError('a pinned checkout is required for a Git-backed bind')
        reject_embedded_git(checkout, deadline)
        if linked:
            if review_git_fds:
                private_fd = review_git_fds[1]
                if not os.path.samestat(os.fstat(private_fd), private.lstat()):
                    raise OSError('review registration changed before the sandbox bind')
            else:
                worktrees_fd = stack.enter_context(git_source(common_fd, 'worktrees', directory=True))
                if worktrees_fd is not None:
                    private_fd = stack.enter_context(git_source(worktrees_fd, private.name, directory=True))
            if private_fd is None:
                raise OSError('Git worktree metadata is unavailable')
            verify_worktree_backlink(private_fd, gitfile.fileno())
        executable = git_executable or trusted_git_executable()
        for _ in range(2):
            projection_timeout(deadline)
            snapshot = Path(tempfile.mkdtemp(dir=storage, prefix='metadata-'))
            visible = snapshot / 'git'
            captured = False
            try:
                captured = capture_git(common_fd, private_fd, visible, private, executable, deadline)
                if captured:
                    break
            except subprocess.TimeoutExpired as error:
                raise TimeoutError('review Git deadline expired') from error
            finally:
                if not captured:
                    shutil.rmtree(snapshot)
        else:
            raise OSError('Git snapshot connectivity or metadata stability check failed after retry')
        mounts = {common: ('--ro-bind', str(visible))}
        if linked:
            record = visible / 'worktrees' / private.name
            (record / 'gitdir').write_bytes(os.fsencode(metadata) + b'\n')
            safe_pointer = snapshot / 'gitfile'
            safe_pointer.write_bytes(prefix + os.fsencode(private) + b'\n')
            mounts[metadata] = ('--ro-bind', str(safe_pointer))
        return mounts


def wrap(cmd, *, clone, product_dir, codex_home, write, outbox=None, proxy_socket=None,
         context_dir=None, owner_folder=None, git_common_dir=None, git_metadata_dir=None,
         config=None, warn_env=True, auth_fd=None, warnings=None, git_executable=None, checkout_fd=None,
         context_fd=None, outbox_fd=None, git_deadline=None, review_git_fds=None, bind_stack, bind_fds):
    """Return the bwrap argv for a worker, keeping its project paths intact."""
    bwrap = bwrap_path()
    if bwrap is None:
        raise BubblewrapError(f'bwrap is missing at {BWRAP}')
    clone, product_dir = (Path(path).expanduser().resolve() for path in (clone, product_dir))
    owner_folder = Path(owner_folder).resolve() if owner_folder is not None else None
    task_layout = owner_folder is not None and clone != owner_folder
    home = Path.home().resolve()
    config = worker_config() if config is None else config
    isolated_net = network_mode(config) == 'isolated'
    worker_home = Path(codex_home).resolve()
    check_install_overlap(clone if write else None, outbox, worker_home)

    argv = [bwrap, '--die-with-parent', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
            '--hostname', 'korolev-worker', '--unshare-cgroup-try', '--clearenv', '--proc', '/proc', '--dev', '/dev']
    if isolated_net:
        argv += ['--unshare-net']
    mounts = {path: ('--ro-bind', str(path)) for path in SYSTEM_DIRS if path.is_dir()}
    if git_executable is not None and not any(git_executable.is_relative_to(path) for path in SYSTEM_DIRS):
        mounts[git_executable] = ('--ro-bind', str(git_executable))
    toolchains = []
    for directory in toolchain_dirs(home):
        if task_layout:
            source = directory.resolve()
            if source.is_relative_to(owner_folder) or (source != directory and owner_folder.is_relative_to(source)):
                continue
        mounts[directory] = ('--ro-bind', str(directory))
        toolchains.append(directory)

    cmd = list(cmd)
    executable = shutil.which(cmd[0]) if Path(cmd[0]).name == 'codex' else shutil.which('codex')
    if executable:
        binary = Path(executable).resolve()
        if not any(binary.is_relative_to(path) for path in SYSTEM_DIRS):
            mounts[binary.parent] = ('--ro-bind', str(binary.parent))
        if Path(cmd[0]).name == 'codex':
            cmd[0] = str(binary)
    elif Path(cmd[0]).name == 'codex':
        raise OSError('codex is missing')

    # On systems using systemd-resolved, /etc/resolv.conf points outside /etc.
    resolver = Path('/etc/resolv.conf')
    if not isolated_net and resolver.is_symlink() and resolver.is_file():
        target = resolver.resolve()
        if not any(target.is_relative_to(path) for path in mounts):
            mounts[target] = ('--ro-bind', str(target))

    mounts[clone] = ('--bind' if write else '--ro-bind', str(clone))
    runs = product_dir / '.runs'
    if context_dir is not None:
        context_dir = Path(context_dir).resolve()
        if context_dir.parent != runs or not context_dir.name.endswith('-ctx') or not context_dir.is_dir():
            raise OSError('the context bundle must be a per-run directory in the product .runs folder')
        mounts[context_dir] = ('--ro-bind', str(context_dir))
    if outbox is not None:
        outbox = Path(outbox).resolve()
        if product_dir.is_relative_to(outbox) or (context_dir is not None and
                (outbox.is_relative_to(context_dir) or context_dir.is_relative_to(outbox))):
            raise OSError('the outbox overlaps the product directory or context bundle')
        if outbox.is_relative_to(runs) and (outbox.parent != runs or not outbox.name.endswith('-out')):
            raise OSError('the outbox must belong to a single run')
        mounts[outbox] = ('--bind', str(outbox))
    run_mounts = {path: fd for path, fd in ((context_dir, context_fd), (outbox, outbox_fd)) if fd is not None}
    for path, fd in run_mounts.items():
        if path is None or not os.path.samestat(os.fstat(fd), path.lstat()):
            raise OSError(f'run directory changed before the sandbox bind: {path}')
    mounts[Path('/tmp')] = ('--tmpfs',)
    mounts[home] = ('--tmpfs',)
    mounts[worker_home] = ('--bind', str(worker_home))
    if auth_fd is not None:
        # bwrap reads the verified FD and mounts a read-only snapshot at a fixed path.
        mounts[worker_home / 'auth.json'] = ('--perms', '0600', '--ro-bind-data', str(auth_fd))

    proxy = isolated_net and proxy_socket is not None
    env = worker_env(config, home=home, codex_home=worker_home,
                     toolchains=toolchains, proxy=proxy, warn=warn_env)
    cwd = str(clone)
    helpers = []
    if isolated_net:
        # Apply the job's cwd and environment only after seccomp is installed.
        python = trusted_system_executable('python3')
        helpers.append(python)
        cmd = [str(python), '-IS', '-c', WORKER_EXEC_SOURCE, cwd, str(len(env)), *env, *env.values(), *cmd]
        cwd, env = '/', {'PATH': os.defpath}
    if proxy:
        argv += ['--remount-ro', '/proc']
        proxy_socket = Path(proxy_socket).absolute()
        proxy_dir = proxy_socket.parent
        info = proxy_dir.lstat()
        if (proxy_socket.name != egress_proxy.SOCKET_NAME or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise OSError('the worker egress socket needs a private directory owned by the launcher user with mode 0700')
        try:
            bridge = trusted_system_executable('socat', SYSTEM_BIN_DIRS)
        except OSError as error:
            raise OSError(f'trusted socat is required for isolated worker networking. Install socat or set '
                          f'"worker_network": "host" in {CONFIG_DIR / "config.json"}.') from error
        shell, pause = (trusted_system_executable(name) for name in ('sh', 'sleep'))
        helpers.extend((bridge, shell, pause))
        mounts[Path(egress_proxy.SANDBOX_SOCKET)] = ('--ro-bind', str(proxy_socket))
        cmd = [str(shell), '-c', BRIDGE, 'worker-egress', str(bridge), str(egress_proxy.PORT),
               egress_proxy.SANDBOX_SOCKET, f'0100007F:{egress_proxy.PORT:04X}', str(pause), *cmd]
    for name, value in env.items():
        argv += ['--setenv', name, value]

    if task_layout:
        for destination, options in mounts.items():
            if destination.is_relative_to(owner_folder):
                raise OSError('a task sandbox mount overlaps the owner folder')
            if len(options) == 2:
                source = Path(options[1]).resolve()
                if source.is_relative_to(owner_folder) or (source != destination and owner_folder.is_relative_to(source)):
                    raise OSError('a task sandbox mount would expose owner files')
        # The owner folder might sit on a system or toolchain mount.
        mounts[owner_folder] = ('--tmpfs',)
    if git_common_dir is not None:
        git_common_dir = Path(os.path.abspath(git_common_dir))
    if git_metadata_dir is not None:
        git_metadata_dir = Path(git_metadata_dir).resolve()
    common = git_common_dir or clone / '.git'
    for destination, options in mounts.items():
        if options[0] not in ('--bind', '--ro-bind'):
            continue
        source = Path(options[1]).resolve()
        for metadata in {common, clone / '.git'}:
            if source.is_relative_to(metadata) or (metadata.is_relative_to(source) and
                    destination / metadata.relative_to(source) != metadata):
                raise OSError('worker sandbox bind would expose unprojected Git metadata')
    git_view = git_mounts(clone, git_common_dir, git_metadata_dir, write=write,
                          git_executable=git_executable, checkout_fd=checkout_fd, deadline=git_deadline,
                          review_git_fds=review_git_fds)
    mounts.update(git_view)
    product_mounts = {context_dir, outbox}
    if proxy:
        product_mounts.add(Path(egress_proxy.SANDBOX_SOCKET))
    for destination, options in mounts.items():
        if options[0] not in ('--bind', '--ro-bind'):
            continue
        source = Path(options[1]).resolve()
        if destination not in product_mounts and any(
                path.is_relative_to(product_dir) or product_dir.is_relative_to(path) for path in (destination, source)):
            raise OSError('worker sandbox bind overlaps the product directory')
        if source.is_dir():
            if git_metadata_dir is not None and git_metadata_dir.is_relative_to(source):
                raise OSError('worker sandbox directory bind exposes Git metadata storage')
            if proxy and proxy_dir.resolve().is_relative_to(source):
                raise OSError('worker sandbox directory bind exposes the private egress socket directory')
            if any(path.is_relative_to(runtime) for path in (destination, source)
                   for runtime in RUNTIME_DIRS):
                raise OSError('worker sandbox directory bind overlaps /run or /var/run')
    for shared in {user_codex_home().resolve(), shared_codex_home(create=False)}:
        for destination, options in list(mounts.items()):
            if options[0] not in ('--bind', '--ro-bind') or not Path(options[1]).is_dir():
                continue
            source = Path(options[1]).resolve()
            if source == shared or (options[0] == '--bind' and source.is_relative_to(shared)):
                raise OSError('worker directory bind overlaps a shared Codex home')
            if shared.is_relative_to(source):
                mounts[destination / shared.relative_to(source)] = ('--tmpfs',)
    masks = []
    if owner_folder is not None:
        knowledge = clone / '.korolev'
        if write and knowledge.is_symlink():
            raise OSError('.korolev is a symlink and cannot be mounted read-only')
        if write and knowledge.exists():
            mounts[knowledge] = ('--ro-bind', str(knowledge))
        masks.extend(credential_masks(clone))

    def budget_warning(message):
        if warnings is not None:
            warnings.append(message)
        elif warn_env:
            print(f'WARNING: {message}', file=sys.stderr)

    masks.extend(socket_masks(mounts, git_common_dir=common,
                              on_budget_exceeded=budget_warning if isolated_net else None))
    if masks:
        # A regular empty file preserves Git's ability to hash tracked masked files.
        empty = empty_mask_file(product_dir)
        for path in masks:
            mounts[path] = ('--ro-bind', str(empty))

    for helper in helpers:
        check_helper_mounts(helper, mounts)
    if proxy:
        mounts[bridge] = ('--ro-bind', str(bridge))

    if clone in git_view:
        if checkout_fd is None or not os.path.samestat(os.fstat(checkout_fd), clone.lstat()):
            raise OSError('checkout directory changed before the sandbox bind')
        checkout = os.fstat(checkout_fd)
        private_mounts = product_mounts | {worker_home, clone, *masks}
        for destination, (option, *args) in mounts.items():
            if option not in ('--bind', '--ro-bind'):
                continue
            fd = run_mounts.get(destination)
            if fd is None:
                fd = os.open(args[0], os.O_PATH | os.O_CLOEXEC)
                bind_stack.callback(os.close, fd)
                bind_fds.append(fd)
            source = Path(args[0]).resolve(strict=True)
            info = os.fstat(fd)
            if not os.path.samestat(info, source.stat()):
                raise OSError('worker sandbox bind source changed before validation')
            if (source.is_relative_to(clone) or os.path.samestat(info, checkout)
                    or any(os.path.samestat(checkout, parent.stat()) for parent in source.parents)
                    or (clone.is_relative_to(source) and destination / clone.relative_to(source) != clone)
                    or (source not in clone.parents
                        and any(os.path.samestat(info, parent.stat()) for parent in clone.parents))
                    or (destination not in private_mounts and
                        (destination.is_relative_to(clone) or destination.resolve().is_relative_to(clone)))):
                raise OSError('worker sandbox bind overlaps the frozen Git-less checkout')
            if destination not in run_mounts:
                mounts[destination] = (option + '-fd', str(fd))

    # Apply parent mounts first. A clone under /tmp and paths under HOME
    # (including a task worktree's Git common dir) must be mounted after the
    # tmpfs mounts have hidden their host contents.
    for destination, options in sorted(mounts.items(), key=lambda item: len(item[0].parts)):
        if proxy and destination == bridge:
            continue
        if destination == clone and options[0] in ('--bind', '--ro-bind') and options[1] == str(clone):
            if not os.path.samestat(os.fstat(checkout_fd), clone.lstat()):
                raise OSError('checkout directory changed before the sandbox bind')
            # The fd form makes bwrap bind via /proc/self/fd.
            options = (options[0] + '-fd', str(checkout_fd))
        if destination in run_mounts:
            fd = run_mounts[destination]
            if not os.path.samestat(os.fstat(fd), destination.lstat()):
                raise OSError(f'run directory changed before the sandbox bind: {destination}')
            options = (options[0] + '-fd', str(fd))
        argv += [*options, str(destination)]
    if proxy:
        # Project the verified executable after the worker's directory and mask mounts.
        argv += [*mounts[bridge], str(bridge)]
    return [*argv, '--chdir', cwd, '--', *cmd]


def mount_fds(argv, *pinned):
    used = {value for flag, value in zip(argv, argv[1:]) if flag in ('--bind-fd', '--ro-bind-fd')}
    return tuple(fd for fd in pinned if str(fd) in used)


PROBE = r'''
import errno
import os
import re
import socket
import ssl
import sys

secret, proxy_port, *listeners = sys.argv[1:]
listeners = listeners[:listeners.index('--')]
if any(re.search(secret, name, re.I | re.S) for name in os.environ):
    raise SystemExit('sandbox has a secret-looking environment name')
if proxy_port:
    try:
        unix_socket = socket.socket(socket.AF_UNIX)
    except OSError as error:
        if error.errno != errno.EACCES:
            raise
    else:
        unix_socket.close()
        raise SystemExit('sandbox can create a Unix socket')
    for listener in listeners:
        host, port = listener.rsplit(':', 1)
        try:
            connection = socket.create_connection((host, int(port)), timeout=0.5)
        except OSError:
            continue
        connection.close()
        raise SystemExit('sandbox can reach a host listener')
    for host, expected in (('denied.invalid', b'403'), ('private.invalid', b'403'),
                           ('allowed.invalid', b'200')):
        with socket.create_connection(('127.0.0.1', int(proxy_port)), timeout=2) as connection:
            connection.sendall(f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode())
            with connection.makefile('rb') as response:
                if response.readline().split()[1:2] != [expected] or response.readline() != b'\r\n':
                    raise SystemExit('sandbox egress proxy returned an unexpected response')
                if expected == b'200':
                    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_bio(
                        incoming, outgoing, server_hostname=host)
                    try:
                        client.do_handshake()
                    except ssl.SSLWantReadError:
                        pass
                    connection.sendall(outgoing.read())
                    connection.shutdown(socket.SHUT_WR)
                    if response.read(5) != b'probe':
                        raise SystemExit('sandbox egress tunnel did not relay upstream bytes')
'''


def network_probe(stack, probe):
    listeners = []
    local = egress_proxy.local_interfaces()
    interface = next((str(addr) for addr in sorted(local[0], key=str)
                      if addr.version == 4 and not addr.is_loopback), None)
    for bind, host in (('127.0.0.1', '127.0.0.1'), ('0.0.0.0', interface)):
        if host is not None:
            listener = stack.enter_context(socket.socket())
            listener.bind((bind, 0))
            listener.listen()
            listeners.append(f'{host}:{listener.getsockname()[1]}')

    public = next((addr for addr in ('192.0.0.9', '8.8.8.8', '1.1.1.1')
                   if egress_proxy.public_address(addr, local)), None)
    if public is None:
        raise OSError('no usable fixture address for the egress self-test')

    def resolve(host, port, **_):
        address = public if host == 'allowed.invalid.' else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))]

    def connect(_family, _address, timeout):
        upstream, peer = socket.socketpair()

        def respond():
            with peer:
                peer.settimeout(timeout)
                try:
                    while peer.recv(4096):
                        pass
                    peer.sendall(b'probe')
                except OSError:
                    pass

        threading.Thread(target=respond, daemon=True).start()
        return upstream

    path = worker_proxy(stack, probe / 'egress.jsonl', ('allowed.invalid', 'private.invalid'),
                        resolve=resolve, connect=connect)
    return path, listeners


def selftest(*, clone=None, product_dir=None, codex_home=None, write=False, outbox=None,
             context_dir=None, owner_folder=None, git_common_dir=None, git_metadata_dir=None,
             dry_run=False, check_version=True, config=None, git_executable=None, context_fd=None, outbox_fd=None,
             git_deadline=None, checkout_fd=None, review_git_fds=None):
    """Check an actual worker layout or a temporary one without real credentials."""
    config = worker_config() if config is None else config
    isolated_net = network_mode(config) == 'isolated'
    if not dry_run and clone is None and not isolated_net:
        # doctor.py calls the standalone self-test and displays its diagnostics.
        print(f'WARN worker network: {HOST_NETWORK_WARNING}')
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(dir='/tmp', prefix='korolev-sandbox-'))
        probe = Path(directory)
        outside = probe / 'outside'
        outside.touch()
        if clone is None:
            clone = probe / 'clone'
            product_dir = probe / 'product'
            for path in (clone, product_dir):
                path.mkdir()
        if codex_home is None:
            codex_home = stack.enter_context(private_codex_home())
        if git_metadata_dir is None:
            git_metadata_dir = probe / 'git-metadata'
            git_metadata_dir.mkdir(mode=0o700)
        proxy_socket, listeners = None, []
        if isolated_net:
            if outbox is None:
                outbox = probe / 'outbox'
                outbox.mkdir(mode=0o700)
            if dry_run:
                proxy_dir = probe / 'egress'
                proxy_dir.mkdir(mode=0o700)
                proxy_socket = proxy_dir / egress_proxy.SOCKET_NAME
            else:
                proxy_socket, listeners = network_probe(stack, probe)

        home = Path.home().resolve()
        # Mounting a single file or directory can create an empty ancestor.
        mounted = [Path(path).resolve() for path in (clone, context_dir, codex_home, outbox,
                                                     owner_folder, git_common_dir) if path is not None]
        hidden_dirs = [directory for directory in HIDDEN_DIRS[1:]
                       if not any(path.is_relative_to(directory) for path in mounted)]
        hidden = [outside, home / '.config' / 'korolev' / 'telegram.env', home / '.ssh',
                  (CONFIG_DIR / 'telegram.env').resolve(), *hidden_dirs,
                  *(Path(product_dir).resolve() / name for name in ('INBOX.jsonl', 'approvals.sqlite', 'USAGE.jsonl'))]
        hidden = list(dict.fromkeys(str(path) for path in hidden if path != home))
        check = r'''
            if ! /bin/ls -a . >/dev/null; then
                echo "sandbox clone is unreadable" >&2
                exit 1
            fi
            codex=$1
            version=$2
            check_git=$3
            probe=$4
            port=$5
            secret_pattern=$6
            shift 6
            python3 -I -c "$probe" "$secret_pattern" "$port" "$@"
            while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
            shift
            for path do
                if [ -e "$path" ] || [ -L "$path" ]; then
                    echo "sandbox unexpectedly exposes $path" >&2
                    exit 1
                fi
            done
            if [ ! -x "$codex" ]; then
                echo "codex is not executable inside the sandbox" >&2
                exit 1
            fi
            if [ -n "$check_git" ]; then
                "$check_git" --no-optional-locks -c core.fsmonitor=false status --porcelain >/dev/null
            fi
            printf 'sandbox checks passed\n'
            if [ "$version" = yes ]; then
                exec "$codex" --version
            fi
        '''
        codex = shutil.which('codex')
        if codex is None:
            raise OSError('codex is missing')
        version_arg = 'yes' if check_version else 'no'
        git_arg = ''
        if (Path(clone) / '.git' / 'HEAD').is_file() or git_common_dir is not None:
            git_executable = git_executable or trusted_git_executable()
            git_arg = str(git_executable)
        if checkout_fd is None:
            checkout_fd = stack.enter_context(git_root(Path(clone).resolve()))
        bind_fds = []
        argv = wrap(['/bin/sh', '-eu', '-c', check, 'sandbox-selftest',
                     str(Path(codex).resolve()), version_arg, git_arg, PROBE,
                     str(egress_proxy.PORT) if isolated_net else '', SECRET_ENV.pattern,
                     *listeners, '--', *hidden],
                    clone=clone, product_dir=product_dir, codex_home=codex_home, write=write, outbox=outbox,
                    proxy_socket=proxy_socket, context_dir=context_dir, owner_folder=owner_folder,
                    git_common_dir=git_common_dir, git_metadata_dir=git_metadata_dir, config=config, warn_env=check_version,
                    git_executable=git_executable, checkout_fd=checkout_fd, context_fd=context_fd, outbox_fd=outbox_fd,
                    git_deadline=git_deadline, review_git_fds=review_git_fds, bind_stack=stack, bind_fds=bind_fds)
        if dry_run:
            return shlex.join(argv)
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=min(10, projection_timeout(git_deadline)) if git_deadline else 10,
                                    env={}, pass_fds=mount_fds(argv, checkout_fd, context_fd, outbox_fd, *bind_fds))
        except subprocess.TimeoutExpired as error:
            raise BubblewrapError('bubblewrap self-test timed out') from error
        if result.returncode:
            raise BubblewrapError(result.stderr.strip() or f'bubblewrap self-test exited {result.returncode}')
        marker, _, version = result.stdout.partition('\n')
        if marker != 'sandbox checks passed':
            raise BubblewrapError('bubblewrap self-test did not report success')
        codex_result = version.strip() or ('codex --version exited 0' if check_version else 'codex executable')
        network_result = 'host ports blocked and egress checked' if isolated_net else 'host network enabled'
        return f'clone readable, private paths hidden, worker env checked, {network_result}, {codex_result}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['selftest'], default='selftest')
    parser.add_argument('--dry-run', action='store_true', help='print the shell-quoted self-test argv')
    args = parser.parse_args()
    try:
        result = selftest(dry_run=args.dry_run)
    except OSError as error:
        print(f'FAIL worker sandbox: {error}', file=sys.stderr)
        return 1
    print(result if args.dry_run else f'PASS worker sandbox: {result}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
