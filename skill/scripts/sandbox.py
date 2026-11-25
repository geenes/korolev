#!/usr/bin/env python3
"""Build and check Korolev's bubblewrap worker filesystem."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG_DIR  # noqa: E402

SYSTEM_DIRS = tuple(map(Path, ('/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/opt')))
HIDDEN_DIRS = tuple(map(Path, ('/home', '/root', '/var', '/srv', '/mnt', '/media')))
TOOLCHAIN_TOOLS = ('python', 'python3', 'node', 'deno', 'bun', 'ruby', 'go', 'cargo', 'rustc',
                   'java', 'javac', 'dotnet', 'php', 'perl', 'swift', 'gcc', 'clang', 'julia')


def enabled():
    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    return not isinstance(config, dict) or config.get('worker_sandbox') is not False


def disable_hint():
    return f'To disable, set "worker_sandbox": false in {CONFIG_DIR / "config.json"}.'


def bwrap_path():
    return shutil.which('bwrap') or shutil.which('/usr/bin/bwrap')


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


def wrap(cmd, *, clone, product_dir, codex_home, write, outbox=None):
    """Return the bwrap argv for a worker, keeping its project paths intact."""
    bwrap = bwrap_path()
    if bwrap is None:
        raise OSError('bwrap is missing')
    clone, product_dir = (Path(path).expanduser().resolve() for path in (clone, product_dir))
    home = Path.home().resolve()
    # Keep an explicitly selected user Codex home private even when the separate
    # persistent Codex home is disabled. Only its auth file enters the namespace.
    persistent_home = codex_home is not None
    worker_home = Path(codex_home or os.environ.get('CODEX_HOME') or home / '.codex').expanduser().absolute()
    if persistent_home:
        worker_home = worker_home.resolve()

    argv = [bwrap, '--die-with-parent', '--unshare-pid', '--proc', '/proc', '--dev', '/dev',
            '--setenv', 'HOME', str(home), '--unsetenv', 'KOROLEV_BOT_TOKEN']
    mounts = {path: ('--ro-bind', str(path)) for path in SYSTEM_DIRS if path.is_dir()}
    for directory in toolchain_dirs(home):
        mounts[directory] = ('--ro-bind', str(directory))

    cmd = list(cmd)
    executable = shutil.which(cmd[0]) if Path(cmd[0]).name == 'codex' else shutil.which('codex')
    if executable:
        binary = Path(executable).resolve()
        mounts[binary.parent] = ('--ro-bind', str(binary.parent))
        if Path(cmd[0]).name == 'codex':
            cmd[0] = str(binary)
    elif Path(cmd[0]).name == 'codex':
        raise OSError('codex is missing')

    # On systems using systemd-resolved, /etc/resolv.conf points outside /etc.
    resolver = Path('/etc/resolv.conf')
    if resolver.is_symlink() and resolver.is_file():
        target = resolver.resolve()
        if not any(target.is_relative_to(path) for path in mounts):
            mounts[target] = ('--ro-bind', str(target))

    mounts[clone] = ('--bind' if write else '--ro-bind', str(clone))
    git_metadata = clone / '.git'
    # Keep the launcher's Git metadata read-only without binding symlink targets.
    if write and not git_metadata.is_symlink() and git_metadata.exists():
        mounts[git_metadata] = ('--ro-bind', str(git_metadata))
    mounts[product_dir] = ('--ro-bind', str(product_dir))
    if outbox is not None:
        outbox = Path(outbox).resolve()
        mounts[outbox] = ('--bind', str(outbox))
    mounts[Path('/tmp')] = ('--tmpfs',)
    mounts[home] = ('--tmpfs',)
    if persistent_home:
        mounts[worker_home] = ('--bind', str(worker_home))
        argv += ['--setenv', 'CODEX_HOME', str(worker_home)]
    elif worker_home not in (home, Path('/tmp')):
        mounts[worker_home] = ('--tmpfs',)

    auth = worker_home / 'auth.json'
    if auth.is_file():
        # Preserve the isolated home's symlink and expose just its target.
        destination = auth
        if persistent_home and auth.is_symlink():
            destination = Path(os.path.abspath(auth.parent / auth.readlink()))
        mounts[destination] = ('--ro-bind', str(auth.resolve()))

    # Apply parent mounts first. A clone under /tmp and paths under HOME
    # must be mounted after those tmpfs mounts have hidden their host contents.
    for destination, options in sorted(mounts.items(), key=lambda item: len(item[0].parts)):
        argv += [*options, str(destination)]
    return [*argv, '--chdir', str(clone), '--', *cmd]


def selftest(*, clone=None, product_dir=None, codex_home=None, write=False, outbox=None,
             dry_run=False, check_version=True):
    """Check an actual worker layout or a temporary one without real credentials."""
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='korolev-sandbox-') as directory:
        probe = Path(directory)
        outside = probe / 'outside'
        outside.touch()
        if clone is None:
            clone = probe / 'clone'
            product_dir = probe / 'product'
            codex_home = probe / 'codex-home'
            for path in (clone, product_dir, codex_home):
                path.mkdir()

        home = Path.home().resolve()
        hidden = [outside, home / '.config' / 'korolev' / 'telegram.env', home / '.ssh',
                  (CONFIG_DIR / 'telegram.env').resolve(), *HIDDEN_DIRS[1:]]
        hidden = list(dict.fromkeys(str(path) for path in hidden if path != home))
        check = r'''
            if ! /bin/ls -a . >/dev/null; then
                echo "sandbox clone is unreadable" >&2
                exit 1
            fi
            codex=$1
            version=$2
            shift 2
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
            printf 'sandbox checks passed\n'
            if [ "$version" = yes ]; then
                exec "$codex" --version
            fi
        '''
        codex = shutil.which('codex')
        if codex is None:
            raise OSError('codex is missing')
        version_arg = 'yes' if check_version else 'no'
        argv = wrap(['/bin/sh', '-eu', '-c', check, 'sandbox-selftest',
                     str(Path(codex).resolve()), version_arg, *hidden],
                    clone=clone, product_dir=product_dir, codex_home=codex_home, write=write, outbox=outbox)
        if dry_run:
            return shlex.join(argv)
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            raise OSError('bubblewrap self-test timed out') from error
        if result.returncode:
            raise OSError(result.stderr.strip() or f'bubblewrap self-test exited {result.returncode}')
        marker, _, version = result.stdout.partition('\n')
        if marker != 'sandbox checks passed':
            raise OSError('bubblewrap self-test did not report success')
        codex_result = version.strip() or ('codex --version exited 0' if check_version else 'codex executable')
        return f'clone readable, private paths hidden, {codex_result}'


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
