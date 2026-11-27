"""Run unittest discovery without bubblewrap on PATH (pass -v for test names)."""
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .git_fixture import git_fixture
else:
    # Release preflight invokes this script using runpy under python -I.
    git_fixture = runpy.run_path(str(Path(__file__).with_name('git_fixture.py')))['git_fixture']


def main():
    root = Path(__file__).resolve().parents[1]
    directories = [str(Path(entry).absolute()) for entry in os.get_exec_path()]
    directories = [directory for directory in directories if not shutil.which('bwrap', path=directory)]

    with git_fixture() as (bin_dir, env):
        # Bubblewrap and Git are commonly installed in the same system directory.
        git = shutil.which('git')
        if git is None:
            sys.exit('git is required to run the test suite')
        (bin_dir / 'git').symlink_to(Path(git).absolute())
        path = os.pathsep.join([str(bin_dir), *directories])
        if shutil.which('bwrap', path=path):
            sys.exit('bwrap is still visible on the test PATH')
        env['PATH'] = path
        env.pop('KOROLEV_REQUIRE_BWRAP', None)
        env.pop('KOROLEV_REQUIRE_HOST_SANDBOX', None)
        return subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', *sys.argv[1:]],
                              cwd=root, env=env).returncode


if __name__ == '__main__':
    sys.exit(main())
