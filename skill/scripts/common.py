"""Shared paths and settings for Korolev scripts. Standard library only."""
import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get('KOROLEV_HOME', '~/korolev')).expanduser()
CONFIG_DIR = Path(os.environ.get('KOROLEV_CONFIG_DIR', '~/.config/korolev')).expanduser()
TELEGRAM_ENV = CONFIG_DIR / 'telegram.env'
BOT_STATE = Path(os.environ.get('KOROLEV_BOT_STATE', '~/.local/share/korolev/bot')).expanduser()


def receiver_state():
    """Fixed service state for the effective OS account, provisioned by the launcher."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / '.config' / 'korolev' / 'receiver'

DEFAULT_ROLES = {
    'analyst': {'model': 'gpt-6.2-sol', 'effort': 'high', 'write': False, 'timeout_minutes': 45},
    'builder': {'model': 'gpt-6.2-sol', 'effort': 'high', 'write': True, 'timeout_minutes': 90},
    'astra': {'model': 'gpt-6.2-astra', 'effort': 'xhigh', 'write': False, 'timeout_minutes': 45},
    'sol56': {'model': 'gpt-5.6-sol', 'effort': 'xhigh', 'write': False, 'timeout_minutes': 45},
}


def roles():
    merged = {k: dict(v) for k, v in DEFAULT_ROLES.items()}
    path = CONFIG_DIR / 'config.json'
    if path.exists():
        for name, values in json.loads(path.read_text()).get('roles', {}).items():
            merged.setdefault(name, {}).update(values)
    return merged


def language():
    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return 'en'
    value = config.get('language') if isinstance(config, dict) else None
    return value if value in ('en', 'ru') else 'en'


def isolate_codex_home():
    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    return not isinstance(config, dict) or config.get('isolate_codex_home') is not False


MINIMAL_CODEX_CONFIG = b'model_reasoning_summary = "none"\n'


def user_codex_home():
    return Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser().absolute()


def codex_home(*, create=True):
    """Return the worker's Codex home, preparing it unless only inspecting it."""
    home = Path(os.environ.get('KOROLEV_CODEX_HOME') or '~/.local/share/korolev/codex-home').expanduser().resolve()
    if not create:
        return home

    user_home = user_codex_home()
    real_home = user_home.resolve()
    if home.is_relative_to(real_home) or real_home.is_relative_to(home):
        raise OSError('isolated Codex home overlaps the user Codex home')

    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    auth = home / 'auth.json'
    source = user_home / 'auth.json'
    if auth.exists() and not auth.is_symlink():
        raise OSError('isolated Codex auth.json already exists as a non-symlink')
    needs_auth = not auth.is_symlink() or auth.readlink() != source
    config = home / 'config.toml'
    needs_config = config.is_symlink() or not config.is_file() or config.read_bytes() != MINIMAL_CODEX_CONFIG
    if needs_auth or needs_config:
        # Worker launches can overlap. Publish each file atomically from a private staging directory.
        with tempfile.TemporaryDirectory(dir=home) as directory:
            staging = Path(directory)
            if needs_auth:
                (staging / auth.name).symlink_to(source)
                (staging / auth.name).replace(auth)
            if needs_config:
                (staging / config.name).write_bytes(MINIMAL_CODEX_CONFIG)
                (staging / config.name).replace(config)
    return home


def derive_product_id(text):
    value = re.sub(r'[^A-Za-z0-9-]+', '-', text).strip('-').lower()[:60]
    if not valid_product(value):
        raise SystemExit('cannot derive a project name; pass --name')
    return value


def normalized_name(name):
    return unicodedata.normalize('NFKC', name).casefold()


def product_dir(name):
    return HOME / 'products' / name


def work_repo(name):
    return HOME / 'work' / name / 'repo'


def tasks_root(name):
    return HOME / 'tasks' / name


def task_worktree(name, slug):
    return tasks_root(name) / slug


def read_tasks(name):
    try:
        tasks = json.loads((product_dir(name) / 'tasks.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(tasks, dict):
        raise ValueError('tasks.json must contain an object keyed by task slug')
    return tasks


def read_projects(parent_fd=None):
    try:
        path = HOME / 'projects.json'
        with path.open(encoding='utf-8') if parent_fd is None else metadata_file(path.name, parent_fd) as stream:
            projects = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(projects, dict):
        raise ValueError('projects.json must contain an object keyed by project name')
    return projects


@contextmanager
def edit_projects():
    path = HOME / 'projects.json'
    lock_name = path.name + '.lock'
    with metadata_parent(path) as parent, metadata_file(lock_name, parent, lock=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = os.fstat(lock.fileno())
        if not same_metadata_entry(lock_name, parent, info) or not os.path.samestat(path.parent.lstat(), os.fstat(parent)):
            raise OSError('the project registry lock or parent changed')
        projects = read_projects(parent)
        yield projects
        if not same_metadata_entry(lock_name, parent, info):
            raise OSError('the project registry lock changed')
        publish_metadata(path, json.dumps(projects, indent=2) + '\n', parent)


@contextmanager
def metadata_parent(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        parent = None
        for part in Path(os.path.abspath(path.parent)).parts:
            try:
                parent = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=parent)
                parent = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, parent)
        if os.fstat(parent).st_uid != os.geteuid():
            raise OSError('metadata parent is not owned by the launcher')
        yield parent


@contextmanager
def metadata_file(name, parent, *, lock=False):
    flags = os.O_RDWR | os.O_CREAT if lock else os.O_RDONLY
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise OSError('cannot verify metadata file ownership')
        yield stream


def same_metadata_entry(name, parent, info):
    return os.path.samestat(info, os.stat(name, dir_fd=parent, follow_symlinks=False))


def publish_metadata(path, contents, parent):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}-', dir=f'/proc/self/fd/{parent}')
    name = Path(temporary).name
    info = os.fstat(fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(fd)
            if not os.path.samestat(path.parent.lstat(), os.fstat(parent)) or not same_metadata_entry(name, parent, info):
                raise OSError('metadata staging or parent changed')
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        with suppress(FileNotFoundError):
            if same_metadata_entry(name, parent, info):
                os.unlink(name, dir_fd=parent)


def project_folder(name):
    entry = read_projects().get(name)
    return Path(entry['folder']) if entry is not None else work_repo(name)


HOST_GIT_ENV = {
    'PATH': os.defpath, 'LC_ALL': 'C',
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
    'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_LAZY_FETCH': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
}
SYSTEM_BIN_DIRS = (Path('/usr/bin'), Path('/bin'))
GIT_DIRS = SYSTEM_BIN_DIRS
SYSTEM_GIT = ContextVar('system_git', default=None)


def system_mounts():
    def unescape(value):
        return Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value))

    mounts = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields, fs = (part.split() for part in line.split(' - ', 1))
        mounts[int(fields[0])] = (fields[2], unescape(fields[3]), unescape(fields[4]),
                                  'ro' in fields[5].split(','), fs[0])
    uid_map = [tuple(map(int, line.split())) for line in Path('/proc/self/uid_map').read_text().splitlines()]
    return mounts, uid_map, int(Path('/proc/sys/kernel/overflowuid').read_text())


def effective_mount(path, mounts):
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    try:
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines():
            if line.startswith('mnt_id:'):
                return mounts[int(line.split()[1])]
    finally:
        os.close(fd)
    raise OSError('cannot verify system mount')


def system_owned(path, info, view):
    mounts, uid_map, overflow = view
    device, backing, target, readonly, fs = effective_mount(path, mounts)
    # Overlay and userspace filesystems can synthesize ownership from other backing trees.
    if fs not in {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'tmpfs', 'zfs', 'squashfs', 'erofs'}:
        return False
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
        return False
    if info.st_uid == 0 and uid_map == [(0, 0, 4294967295)]:
        return os.geteuid() == 0 or not os.access(path, os.W_OK)
    # A single-ID sandbox may map its launcher-owned runtime to a nonzero UID.
    mapped_owner = len(uid_map) == 1 and uid_map[0][2] == 1 and info.st_uid == uid_map[0][0]
    ambiguous = info.st_uid == 0 or mapped_owner or (info.st_uid == overflow and
        not any(start <= overflow < start + size for start, _, size in uid_map))
    # A read-only bind is insufficient if a writable view reaches the same backing subtree.
    entry = backing / path.relative_to(target)
    return ambiguous and readonly and not any(
        not ro and (kind == 'overlay' or dev == device and
                    (entry.is_relative_to(root) or root.is_relative_to(entry)))
        for dev, root, _, ro, kind in mounts.values())


def trusted_system_executable(command, directories=SYSTEM_BIN_DIRS, *, view=None, regular=False):
    """Resolve every component through system-owned, worker-nonwritable entries."""
    view = system_mounts() if view is None else view
    root = Path('/')
    for directory in directories:
        try:
            path = root
            parts = list(reversed((directory / command).parts[1:]))
            links = 0
            while True:
                info = path.lstat()
                link = stat.S_ISLNK(info.st_mode)
                if not system_owned(path, info, view) and not (
                        path == root and parts and effective_mount(root / parts[-1], view[0])[2] == root / parts[-1]):
                    break
                if link:
                    links += 1
                    if links > 40:
                        break
                    target = path.readlink()
                    path = root if target.is_absolute() else path.parent
                    parts.extend(reversed(target.parts[1:] if target.is_absolute() else target.parts))
                    continue
                if parts and stat.S_ISDIR(info.st_mode):
                    part = parts.pop()
                    path = path.parent if part == '..' else path / part
                    continue
                if not parts and stat.S_ISREG(info.st_mode) and (regular or (
                        path.parent in (*directories, *SYSTEM_BIN_DIRS) and (command != 'git' or path.name == command)
                        and os.access(path, os.X_OK))):
                    return path
                break
        except (OSError, RuntimeError, KeyError, ValueError):
            continue
    label = 'Git' if command == 'git' else command
    raise OSError(f'no trusted {label} executable in system directories')


def trusted_system_path(path, view):
    return trusted_system_executable(path.name, (path.parent,), view=view, regular=True)


def trusted_git_executable():
    return SYSTEM_GIT.get() or trusted_system_executable('git', GIT_DIRS)


def transport_environment():
    allowed = ('HOME', 'GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK')
    return {key: os.environ[key] for key in allowed if key in os.environ} | dict(
        PATH=os.pathsep.join(map(str, SYSTEM_BIN_DIRS)), LC_ALL='C', GH_HOST='github.com', GH_PROMPT_DISABLED='1')


def launcher_git(folder, *args, input=None, timeout=None, env_overrides=None, executable=None, pass_fds=()):
    """Run host-side Git against the supplied checkout, with hooks disabled."""
    env = HOST_GIT_ENV.copy() if executable is not None else os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'):
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)
    isolation = ('-c', 'core.attributesFile=/dev/null', '-c', 'maintenance.auto=false') if executable else ()
    return subprocess.run([executable or 'git', '--literal-pathspecs', '-c', 'core.hooksPath=/dev/null',
                           '-c', 'core.fsmonitor=false', *isolation, *args], cwd=folder, env=env,
                          input=input, capture_output=True, timeout=timeout, pass_fds=pass_fds)


def git_common_dir(folder, *, executable=None):
    result = launcher_git(folder, 'rev-parse', '--path-format=absolute', '--git-common-dir', executable=executable)
    if result.returncode:
        raise ValueError(f'cannot determine Git common directory for {folder}')
    return Path(os.fsdecode(result.stdout.removesuffix(b'\n'))).resolve()


def verify_worktree_backlink(private_fd, gitfile_fd):
    error = 'Git worktree backlink does not name the pinned checkout'
    try:
        with ExitStack() as stack:
            fd = os.open('gitdir', os.O_PATH | os.O_NOFOLLOW, dir_fd=private_fd)
            stack.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(error)
            with open(f'/proc/self/fd/{fd}', 'rb') as stream:
                backlink = stream.read(4097)
            if len(backlink) == 4097:
                raise OSError(error)
            target = os.open(backlink.rstrip(b'\r\n'), os.O_PATH, dir_fd=private_fd)
            stack.callback(os.close, target)
            gitfile = os.fstat(gitfile_fd)
            if not stat.S_ISREG(gitfile.st_mode) or not os.path.samestat(os.fstat(target), gitfile):
                raise OSError(error)
    except (OSError, ValueError) as cause:
        raise OSError(error) from cause


def verify_linked_checkout(checkout_fd, metadata):
    with ExitStack() as stack:
        private_fd = os.open(metadata, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, private_fd)
        fd = os.open('.git', os.O_PATH | os.O_NOFOLLOW, dir_fd=checkout_fd)
        stack.callback(os.close, fd)
        verify_worktree_backlink(private_fd, fd)
        with open(f'/proc/self/fd/{fd}', 'rb') as stream:
            pointer = stream.read(4097)
        if len(pointer) == 4097 or b'\0' in pointer or not pointer.startswith(b'gitdir: '):
            raise OSError('expected a Git worktree pointer file')
        target = os.open(pointer[8:].rstrip(b'\r\n'), os.O_PATH | os.O_DIRECTORY, dir_fd=checkout_fd)
        stack.callback(os.close, target)
        if not os.path.samestat(os.fstat(target), os.fstat(private_fd)):
            raise OSError('the task worktree Git metadata changed during the run')


def is_task_worktree(name, path, *, executable=None):
    project = read_projects().get(name)
    if project is None:
        return False
    path = Path(path).resolve()
    gitfile = path / '.git'
    if (not gitfile.is_file() or gitfile.is_symlink()
            or path.is_relative_to(Path(project['folder']).resolve())
            or not any(path == Path(task['path']) for task in read_tasks(name).values())):
        return False
    try:
        common_dir = git_common_dir(path, executable=executable)
        if common_dir != Path(project['git_common_dir']).resolve():
            return False
        private_dir = launcher_git(path, 'rev-parse', '--absolute-git-dir', executable=executable)
        if private_dir.returncode:
            return False
        metadata = Path(os.fsdecode(private_dir.stdout.removesuffix(b'\n'))).resolve()
        if metadata.parent != common_dir / 'worktrees':
            return False
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            verify_linked_checkout(fd, metadata)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError):
        return False


def task_lock_name(name, path):
    return f'{name}-task-{path.name}-write'


def git_status(folder):
    """Yield (status, path, source) records from Git's NUL-delimited status."""
    result = launcher_git(folder, '--no-optional-locks', 'status', '--porcelain=v1', '-z',
                          '--untracked-files=all')
    if result.returncode:
        raise ValueError('cannot read Git status')
    fields = iter(os.fsdecode(result.stdout).split('\0'))
    for item in fields:
        if item:
            status = item[:2]
            source = next(fields) if 'R' in status or 'C' in status else None
            yield status, item[3:], source


def shared_remotes(first, second):
    return (set(first['remotes'].values()) & set(second['remotes'].values())) - {''}


def product_meta(name, parent_fd=None):
    path = product_dir(name) / 'product.json'
    if parent_fd is not None:
        with suppress(FileNotFoundError), metadata_file(path.name, parent_fd) as stream:
            return json.load(stream)
        return {}
    return json.loads(path.read_text()) if path.exists() else {}


def display_name(name):
    return product_meta(name).get('display') or name


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


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
    return isinstance(product, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]*', product) is not None


def read_heartbeat():
    try:
        result = json.loads((HOME / 'coordinator.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


@contextmanager
def heartbeat_lock():
    HOME.mkdir(parents=True, exist_ok=True)
    with (HOME / 'coordinator.json.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_heartbeat(record):
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


def touch_heartbeat(product, doing):
    """Record coordinator activity. Unscoped commands use the active product."""
    try:
        with heartbeat_lock():
            record = read_heartbeat()
            product = record.get('product') if product is None else product
            if not valid_product(product):
                return
            current = time.time()
            if record.get('status') == 'waiting_limit':
                try:
                    waiting_since = parse_timestamp(record.get('limit_set_at', record.get('updated_at')))
                    if current - waiting_since < 60:
                        return
                except (ValueError, OverflowError, OSError):
                    pass
            record.update(product=product, doing=doing, status='working', updated_at=timestamp(current))
            record.pop('retry_after', None)
            record.pop('limit_set_at', None)
            write_heartbeat(record)
    except OSError:
        # Commands such as inbox --show must also work in read-only environments.
        pass


def write_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def append_line(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
