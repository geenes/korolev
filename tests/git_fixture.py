"""Private directories and subprocess environments for Git-sensitive tests."""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


SYSTEM_TEMP_DIRS = (Path('/tmp'), Path('/var/tmp'), Path('/dev/shm'))


def git_free_ancestry(path):
    for parent in (path, *path.parents):
        objects = parent / 'objects'
        if (os.path.lexists(parent / '.git')
                or os.path.lexists(parent / 'HEAD')
                and (os.path.lexists(objects) or os.path.lexists(parent / 'commondir'))
                or os.path.lexists(parent / 'config') and objects.is_dir() and (parent / 'refs').is_dir()):
            return False
    return True


def fixture_directory():
    # The launcher walks ancestors itself, so Git's ceiling alone is insufficient.
    for candidate in (Path(tempfile.gettempdir()), *SYSTEM_TEMP_DIRS, Path.home()):
        try:
            parent = candidate.resolve(strict=True)
            if os.statvfs(parent).f_flag & os.ST_NOEXEC or not git_free_ancestry(parent):
                continue
            return tempfile.TemporaryDirectory(dir=parent)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    raise RuntimeError('no writable, executable temporary directory with Git-free ancestry')


@contextmanager
def git_fixture():
    with fixture_directory() as directory:
        root = Path(directory)
        base = root / 'case'
        base.mkdir()
        temp = root / 'tmp'
        temp.mkdir(mode=0o700)
        env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_CEILING_DIRECTORIES=str(root),
                   TMPDIR=str(temp))
        yield base, env
