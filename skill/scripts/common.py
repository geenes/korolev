"""Shared paths and settings for Korolev scripts. Standard library only."""
import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get('KOROLEV_HOME', '~/korolev')).expanduser()
CONFIG_DIR = Path(os.environ.get('KOROLEV_CONFIG_DIR', '~/.config/korolev')).expanduser()
TELEGRAM_ENV = CONFIG_DIR / 'telegram.env'
BOT_STATE = Path(os.environ.get('KOROLEV_BOT_STATE', '~/.local/share/korolev/bot')).expanduser()

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


def codex_home(*, create=True):
    """Return the worker's Codex home, preparing it unless only inspecting it."""
    home = Path(os.environ.get('KOROLEV_CODEX_HOME') or '~/.local/share/korolev/codex-home').expanduser().resolve()
    if not create:
        return home

    user_home = Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser().absolute()
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
    minimal_config = b'model_reasoning_summary = "none"\n'
    needs_config = config.is_symlink() or not config.is_file() or config.read_bytes() != minimal_config
    if needs_auth or needs_config:
        # Worker launches can overlap. Publish each file atomically from a private staging directory.
        with tempfile.TemporaryDirectory(dir=home) as directory:
            staging = Path(directory)
            if needs_auth:
                (staging / auth.name).symlink_to(source)
                (staging / auth.name).replace(auth)
            if needs_config:
                (staging / config.name).write_bytes(minimal_config)
                (staging / config.name).replace(config)
    return home


def slug(text):
    value = re.sub(r'[^A-Za-z0-9._-]+', '-', text.strip()).strip('-.').lower()
    if not value:
        raise SystemExit('cannot derive a project name; pass --name')
    return value[:60]


def product_dir(name):
    return HOME / 'products' / name


def work_repo(name):
    return HOME / 'work' / name / 'repo'


def product_meta(name):
    path = product_dir(name) / 'product.json'
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
    return isinstance(product, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', product) is not None


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
