import errno
import fcntl
import hashlib
import json
import os
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__:
    from .git_fixture import SYSTEM_TEMP_DIRS, git_fixture
else:
    from git_fixture import SYSTEM_TEMP_DIRS, git_fixture


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'
EXECUTABLE_FIXTURE = (
    f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); '
    'from test_sandbox import fixture_executable_info; sandbox.executable_info = fixture_executable_info; '
)

def fixture_executable_info(path):
    info = path.lstat()
    if path in (*SYSTEM_TEMP_DIRS, Path(tempfile.gettempdir()).resolve()) and info.st_mode & stat.S_ISVTX:
        # Test-only executables are created in private children of the sticky temp root.
        return SimpleNamespace(st_uid=info.st_uid, st_mode=info.st_mode & ~0o022)
    if (info.st_uid == 65534 and any(path.is_relative_to(root) for root in (Path('/usr'), Path('/bin')))
            and os.statvfs(path).f_flag & os.ST_RDONLY):
        # The builder's user namespace maps read-only system files to the overflow UID.
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
    return info


def unix_socket_denial():
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except OSError as error:
        if error.errno != errno.EACCES:
            raise
        return 'worker socket enforcement denies socket(AF_UNIX) with EACCES'
    return None


def require_host_sandbox():
    return os.environ.get('KOROLEV_REQUIRE_HOST_SANDBOX') == '1'


def filter_decision(machine, arch, number, *args):
    from skill.scripts import worker_exec as worker

    data = {0: number & 0xffffffff, 4: arch}
    data.update({16 + 8 * index: value & 0xffffffff for index, value in enumerate(args)})
    jumps = {worker.JUMP_EQUAL: lambda a, k: a == k, worker.JUMP_GREATER: lambda a, k: a > k,
             worker.JUMP_AT_LEAST: lambda a, k: a >= k, worker.JUMP_BITS_SET: lambda a, k: a & k}
    accumulator, index = 0, 0
    rules = worker.socket_filter(machine)
    while True:
        operation, yes, no, operand = rules[index]
        index += 1
        if operation == worker.LD_WORD_ABS:
            accumulator = data.get(operand, 0)
        elif operation == worker.AND_CONSTANT:
            accumulator &= operand
        elif operation == worker.RETURN:
            return operand
        else:
            index += yes if jumps[operation](accumulator, operand) else no


def load_tests(loader, suite, pattern):
    if require_host_sandbox():
        # The release tests use the original flag for their real-bwrap checks.
        os.environ['KOROLEV_REQUIRE_BWRAP'] = '1'
    # The egress fixture module is outside this task's edit scope. Discovery
    # has already imported it, and all of its tests start a Unix listener.
    egress_tests = sys.modules.get(f'{__package__}.test_egress_proxy' if __package__ else 'test_egress_proxy')
    if egress_tests is not None and (reason := unix_socket_denial()):
        def unavailable(test):
            if require_host_sandbox():
                test.fail(reason)
            test.skipTest(reason)

        egress_tests.EgressProxyTests.setUp = unavailable
    return suite


class SandboxTests(unittest.TestCase):
    def setUp(self):
        from skill.scripts import sandbox

        self.enterContext(patch.object(sandbox, 'executable_info', fixture_executable_info))
        self.base, self.env = self.enterContext(git_fixture())
        self.home = self.base / 'user'
        self.product = self.base / 'korolev' / 'products' / 'demo'
        self.clone = self.base / 'korolev' / 'work' / 'demo' / 'repo'
        self.isolated = self.home / '.local' / 'share' / 'korolev' / 'codex-home'
        self.private = self.base / 'private-codex'
        self.private.mkdir(mode=0o700)
        self.git_metadata = self.base / 'git-metadata'
        self.git_metadata.mkdir(mode=0o700)
        self.checkout_handles = self.enterContext(ExitStack())
        self.auth = self.home / '.codex' / 'auth.json'
        self.binary_dir = self.home / '.codex' / 'packages' / 'release' / 'bin'
        self.argv_bin = self.base / 'argv-bin'
        self.config = self.home / '.config' / 'korolev'
        self.secret = self.home / 'secret' / 'hidden.txt'
        self.other_home = self.base / 'other-home'
        for directory in (self.product, self.clone, self.isolated, self.auth.parent,
                          self.binary_dir, self.argv_bin, self.config, self.secret.parent, self.other_home):
            directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.binary_dir, *self.binary_dir.parents, self.argv_bin):
            if directory.is_relative_to(self.base):
                directory.chmod(0o700)
        self.auth.parent.chmod(0o700)
        self.auth.write_text('{}')
        self.auth.chmod(0o600)
        (self.isolated / 'auth.json').symlink_to(self.auth)
        self.secret.write_text('private')
        (self.clone / 'visible.txt').write_text('clone')
        self.make_executable(self.binary_dir / 'codex', 'exit 0\n')
        # Supply a stub for tests that inspect the generated arguments.
        self.make_executable(self.argv_bin / 'bwrap', 'exit 1\n')
        self.make_executable(self.binary_dir / 'socat', 'exit 1\n')
        self.env.update(HOME=str(self.home),
                        PATH=os.pathsep.join([str(self.binary_dir), os.defpath]),
                        KOROLEV_HOME=str(self.product.parents[1]), KOROLEV_CONFIG_DIR=str(self.config),
                        KOROLEV_CODEX_HOME=str(self.isolated), CODEX_HOME=str(self.auth.parent))

    def make_executable(self, path, body):
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)

    def require_unix_socket(self):
        if reason := unix_socket_denial():
            if require_host_sandbox():
                self.fail(reason)
            self.skipTest(reason)

    def wrap(self, cmd, *, write=False, bwrap_dir=None, config=None, **layout):
        from skill.scripts import sandbox

        bwrap = bwrap_dir / 'bwrap' if bwrap_dir is not None else self.argv_bin / 'bwrap'
        clone = layout.pop('clone', self.clone)
        checkout_fd = self.checkout_handles.enter_context(sandbox.git_root(clone.resolve()))
        storage = layout.pop('git_metadata_dir', self.git_metadata)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)):
            return sandbox.wrap(cmd, clone=clone, product_dir=self.product,
                                codex_home=self.private, write=write, config=config or {},
                                git_metadata_dir=storage, checkout_fd=checkout_fd,
                                bind_stack=self.checkout_handles, bind_fds=[], **layout)

    def launcher(self, script, *args, bwrap=None, socket_walk_entries=None, proxy_path_log=None, setup=''):
        source = (
            f'import runpy, sys; sys.path.insert(0, {str(SCRIPTS)!r}); import sandbox; '
            + EXECUTABLE_FIXTURE +
            f'sandbox.bwrap_path = lambda: {str(bwrap or (self.binary_dir / "bwrap"))!r}; '
        )
        if socket_walk_entries is not None:
            source += f'sandbox.SOCKET_WALK_ENTRIES = {socket_walk_entries!r}; '
        if proxy_path_log is not None:
            source += f'''
from pathlib import Path
worker_proxy = sandbox.worker_proxy
def record_proxy_path(*args, **kwargs):
    path = worker_proxy(*args, **kwargs)
    Path({str(proxy_path_log)!r}).write_text(str(path))
    return path
sandbox.worker_proxy = record_proxy_path
'''
        source += f'\n{setup}\nrunpy.run_path({str(SCRIPTS / script)!r}, run_name="__main__")'
        return [sys.executable, '-I', '-c', source, *args]

    def skip_real_bwrap(self, reason):
        if require_host_sandbox() or os.environ.get('KOROLEV_REQUIRE_BWRAP') == '1':
            self.fail(reason)
        print(f'SKIP real bubblewrap test: {reason}', file=sys.stderr)
        self.skipTest(reason)

    def real_bwrap(self):
        # Check the test runner's PATH directly so the production /usr/bin fallback
        # cannot pick up the host installation in the no-bwrap run.
        bwrap = shutil.which('bwrap')
        if bwrap is None:
            self.skip_real_bwrap('bwrap is missing from PATH')
        bwrap = Path(bwrap).absolute()
        probe = subprocess.run([bwrap, '--ro-bind', '/', '/', '--unshare-pid', '--unshare-net',
                                '--', '/bin/true'],
                               env=dict(self.env, LC_ALL='C'), capture_output=True, text=True, timeout=10)
        permission_errors = ('no permissions to create new namespace', 'operation not permitted', 'permission denied')
        if probe.returncode != 0 and any(error in probe.stderr.lower() for error in permission_errors):
            self.skip_real_bwrap(f'namespace probe failed: {probe.stderr.strip()}')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return bwrap

    def real_socat(self):
        socat = shutil.which('socat')
        if socat is None:
            self.skip_real_bwrap('socat is missing from PATH')
        return Path(socat).absolute()

    def mounts(self, argv, option, *, include_pinned=True):
        return {
            Path(os.readlink(f'/proc/self/fd/{source}')) if arg == option + '-fd' else Path(source): Path(dest)
            for arg, source, dest in zip(argv, argv[1:], argv[2:])
            if arg == option or (include_pinned and arg == option + '-fd' and option in ('--bind', '--ro-bind'))
        }

    def checkout_bind_fds(self, argv):
        return tuple(int(fd) for flag, fd in zip(argv, argv[1:]) if flag in ('--bind-fd', '--ro-bind-fd'))

    def assert_checkout_bind(self, argv, clone, *, write):
        flag = '--bind-fd' if write else '--ro-bind-fd'
        fd, = (int(fd) for option, fd, dest in zip(argv, argv[1:], argv[2:])
               if option == flag and dest == str(clone))
        self.assertTrue(os.path.samestat(os.fstat(fd), clone.stat()))

    def test_argv_uses_private_filesystem_and_real_codex_binary(self):
        argv = self.wrap(['codex', 'exec'])
        self.assertEqual(argv[-2:], [str(self.binary_dir / 'codex'), 'exec'])
        self.assertEqual(argv[argv.index('--') + 1:][:3], [str(Path('/usr/bin/python3').resolve()), '-IS', '-c'])
        self.assertIn('--unshare-pid', argv)
        self.assertIn('--die-with-parent', argv)
        self.assertIn('--unshare-net', argv)
        self.assertIn('--clearenv', argv)
        for option, dest in (('--proc', '/proc'), ('--dev', '/dev'), ('--tmpfs', '/tmp'),
                             ('--tmpfs', str(self.home))):
            self.assertIn((option, dest), list(zip(argv, argv[1:])))
        self.assertEqual(self.mounts(argv, '--ro-bind')[self.binary_dir], self.binary_dir)

    def test_only_verified_auth_fd_is_bound_read_only(self):
        from skill.scripts import sandbox

        with patch.dict(os.environ, self.env, clear=True), patch.object(os, 'open', wraps=os.open) as open_file, \
                sandbox.worker_auth() as fd:
            self.assertGreaterEqual(fd, 10)
            self.assertFalse(os.get_inheritable(fd))
            argv = self.wrap(['codex', 'exec'], auth_fd=fd)
            self.auth.rename(self.auth.with_suffix('.old'))
            self.auth.write_text('replacement')
            self.assertEqual(os.read(fd, 64), b'{}')
        reopen, = (call.args[1] for call in open_file.call_args_list if str(call.args[0]).startswith('/proc/self/fd/'))
        self.assertTrue(reopen & os.O_CLOEXEC)
        auth_mount = argv.index('--ro-bind-data')
        self.assertEqual(argv[auth_mount - 2:auth_mount + 3],
                         ['--perms', '0600', '--ro-bind-data', str(fd), str(self.private / 'auth.json')])
        ro, rw = self.mounts(argv, '--ro-bind'), self.mounts(argv, '--bind')
        self.assertEqual(rw[self.private], self.private)
        for hidden in (self.auth, self.isolated, self.secret, self.other_home,
                       self.config / 'telegram.env', self.home / '.ssh', Path('/var')):
            self.assertFalse(any(hidden.is_relative_to(path) for path in (*ro, *rw)), hidden)

    def test_rejects_auth_symlink_loose_permissions_and_non_regular_file(self):
        from skill.scripts import sandbox

        target = self.auth.with_suffix('.old')
        self.auth.rename(target)
        with patch.dict(os.environ, self.env, clear=True):
            for create in (lambda: self.auth.symlink_to(target),
                           lambda: os.link(target, self.auth),
                           lambda: os.mkfifo(self.auth, 0o600),
                           lambda: self.auth.touch(mode=0o644)):
                create()
                with self.assertRaisesRegex(OSError, 'user-owned regular file'), sandbox.worker_auth():
                    self.fail('invalid credential file accepted')
                self.auth.unlink()

    def test_auth_requires_a_safe_codex_home_directory(self):
        from skill.scripts import sandbox

        with patch.dict(os.environ, self.env, clear=True):
            self.auth.parent.chmod(0o770)
            with self.assertRaisesRegex(OSError, 'Codex home must be user-owned'), sandbox.worker_auth():
                self.fail('group-writable Codex home accepted')
            self.auth.parent.chmod(0o700)
            moved = self.auth.parent.rename(self.home / 'moved-codex')
            self.auth.parent.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(OSError), sandbox.worker_auth():
                self.fail('symlinked Codex home accepted')

    def test_bwrap_executable_is_canonical_and_checked(self):
        from skill.scripts import sandbox

        with patch.dict(os.environ, dict(PATH=str(self.argv_bin))), patch.object(sandbox, 'BWRAP', self.base / 'absent'):
            self.assertIsNone(sandbox.bwrap_path())
        untrusted = self.argv_bin / 'bwrap'
        untrusted.chmod(0o777)
        with patch.object(sandbox, 'BWRAP', untrusted):
            with self.assertRaisesRegex(sandbox.BubblewrapError, 'root-owned regular file'):
                sandbox.bwrap_path()

    def test_clone_mount_respects_write_mode(self):
        self.git('init', '-q', '-b', 'main')
        read_only = self.wrap(['codex'], write=False)
        writable = self.wrap(['codex'], write=True)
        self.assert_checkout_bind(read_only, self.clone, write=False)
        self.assert_checkout_bind(writable, self.clone, write=True)
        self.assertNotIn(self.clone, self.mounts(writable, '--ro-bind').values())
        self.projected_git(writable, self.clone / '.git')
        for argv in (read_only, writable):
            self.assertNotIn(self.product, self.mounts(argv, '--ro-bind'))
            self.assertNotIn(self.product, self.mounts(argv, '--bind'))

    def test_path_only_exposes_toolchain_directories_outside_home(self):
        toolchain = self.base / 'toolchain' / 'bin'
        private_bin = self.home / 'bin'
        for directory in (toolchain, private_bin):
            directory.mkdir(parents=True)
            self.make_executable(directory / 'python3', 'exit 0\n')
        self.env['PATH'] += os.pathsep + os.pathsep.join(map(str, (toolchain, private_bin, self.secret.parent)))
        argv = self.wrap(['codex'])
        mounts = self.mounts(argv, '--ro-bind')
        self.assertEqual(mounts[toolchain], toolchain)
        self.assertNotIn(private_bin, mounts)
        self.assertNotIn(self.secret.parent, mounts)
        path = self.argv_worker_env(argv)['PATH'].split(os.pathsep)
        self.assertEqual(path, [str(toolchain), '/usr/local/bin', '/usr/bin', '/bin'])

    def argv_env(self, argv):
        return {name: value for option, name, value in zip(argv, argv[1:], argv[2:]) if option == '--setenv'}

    def argv_worker_env(self, argv):
        from skill.scripts import sandbox

        _, count, *args = argv[argv.index(sandbox.WORKER_EXEC_SOURCE) + 1:]
        count = int(count)
        return dict(zip(args[:count], args[count:2 * count]))

    def test_writable_mounts_refuse_installation_ancestors_and_descendants(self):
        from skill.scripts import sandbox

        install = self.base / 'install' / 'repo'
        child = install / 'skill' / 'scripts'
        child.mkdir(parents=True)
        alias = self.base / 'alias'
        alias.symlink_to(child, target_is_directory=True)
        with patch.object(sandbox, 'INSTALL_ROOT', install):
            for layout in ({'clone': install.parent, 'write': True}, {'outbox': alias}):
                with self.subTest(layout=layout), self.assertRaisesRegex(OSError, 'overlaps the Korolev installation'):
                    self.wrap(['codex'], **layout)
            self.private = alias
            with self.assertRaisesRegex(OSError, 'overlaps the Korolev installation'):
                self.wrap(['codex'])

    def test_env_allowlist_rejects_secret_names_and_keeps_fixed_settings(self):
        from contextlib import redirect_stderr
        from io import StringIO

        self.env.update(FAKE_API_TOKEN='fixture', AWS_SECRET_ACCESS_KEY='fixture',
                        CLAUDE_CODE_SOMETHING='fixture', TEST_SETTING='fixture', TERM='xterm')
        warnings = StringIO()
        secret_names = ['TEST_PASS', 'TEST_CRED', 'TEST_CREDENTIAL', 'TEST_BEARER', 'TEST_PRIVATE',
                        'TEST_CERT', 'TEST_DSN', 'TEST_NETRC', 'MYSQL_PWD', 'pwd', 'OLDPWD_BACKUP']
        self.env.update(dict.fromkeys(secret_names, 'fixture'))
        self.env.update(PWD=str(self.clone), OLDPWD=str(self.product))
        self.env['PYTHONPATH'] = 'fixture'
        with redirect_stderr(warnings):
            argv = self.wrap(['/usr/bin/env'], config={'worker_env_allow': ['TEST_SETTING', 'FAKE_API_TOKEN', 'KOROLEV_HOME',
                                                            'KOROLEV_CONFIG_DIR', 'PYTHONPATH', 'PWD', 'OLDPWD',
                                                            *secret_names]})
        env = self.argv_worker_env(argv)
        self.assertEqual(env['TEST_SETTING'], 'fixture')
        self.assertEqual(env['TERM'], 'dumb')
        self.assertEqual(env['HOME'], str(self.home))
        self.assertEqual(env['CODEX_HOME'], str(self.private))
        self.assertEqual(env['USER'], env['LOGNAME'])
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual((env['PWD'], env['OLDPWD']), (str(self.clone), str(self.product)))
        self.assertFalse({'KOROLEV_HOME', 'KOROLEV_CONFIG_DIR', 'KOROLEV_CODEX_HOME', 'PYTHONPATH',
                          *secret_names} & env.keys())
        self.assertFalse({'FAKE_API_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'CLAUDE_CODE_SOMETHING'} & env.keys())
        self.assertIn('refusing secret-looking worker_env_allow name FAKE_API_TOKEN', warnings.getvalue())
        self.assertNotIn('fixture', warnings.getvalue())

    def test_real_bwrap_env_keeps_only_allowlisted_settings(self):
        bwrap = self.real_bwrap()
        self.env.update(FAKE_API_TOKEN='fixture', AWS_SECRET_ACCESS_KEY='fixture',
                        CLAUDE_CODE_SOMETHING='fixture', TEST_SETTING='fixture')
        argv = self.wrap(['/usr/bin/env', '-0'], bwrap_dir=bwrap.parent)
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10,
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split('=', 1) for line in result.stdout.split('\0') if line)
        self.assertFalse({'FAKE_API_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'CLAUDE_CODE_SOMETHING',
                          'TEST_SETTING'} & env.keys())

    def test_real_bwrap_env_config_passes_harmless_name_and_refuses_secret_name(self):
        bwrap = self.real_bwrap()
        self.env.update(TEST_SETTING='fixture', Test_Password='fake')
        argv = self.wrap(['/usr/bin/env', '-0'], bwrap_dir=bwrap.parent,
                         config={'worker_env_allow': ['TEST_SETTING', 'Test_Password']})
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10,
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split('=', 1) for line in result.stdout.split('\0') if line)
        self.assertEqual(env['TEST_SETTING'], 'fixture')
        self.assertNotIn('Test_Password', env)

    def test_real_bwrap_hides_secret_and_exposes_clone(self):
        from skill.scripts import sandbox

        bwrap = self.real_bwrap()
        with patch.dict(os.environ, self.env, clear=True), sandbox.worker_auth() as fd:
            argv = self.wrap(['/bin/sh', '-eu', '-c',
                              'test ! -L "/proc/$$/fd/$4"; '
                              'test ! -e "$1"; test "$(cat "$2")" = clone; test "$(cat "$3")" = "{}"; '
                              'if (printf altered > "$3") 2>/dev/null; then exit 1; fi',
                              'probe', str(self.secret), str(self.clone / 'visible.txt'),
                              str(self.private / 'auth.json'), str(fd)], bwrap_dir=bwrap.parent, auth_fd=fd)
            result = subprocess.run(argv, env={}, capture_output=True, text=True, timeout=10, pass_fds=(*self.checkout_bind_fds(argv), fd))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.auth.read_text(), '{}')

    def test_real_bwrap_worker_sees_only_its_context_and_outbox(self):
        bwrap = self.real_bwrap()
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        for name in ('INBOX.jsonl', 'approvals.sqlite', '.runs/other-ctx/TASK.md', '.runs/other-out/report'):
            path = self.product / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private fixture')
        (self.product / 'CLI.md').write_text('from the command line')
        (self.product / 'With spaces.md').write_text('from the task')
        (self.product / 'STATE.md').write_text(
            '# Product state\nPrivate state body\n\n## Session handoff\n\n- Pending lessons: 5\n')
        self.make_executable(self.binary_dir / 'codex', f'product={shlex.quote(str(self.product))}\n' + '''
            set -eux
            bundle=$(sed -n 's/^Run context bundle: //p')
            test "$(head -n1 "$bundle/TASK.md")" = 'Context fixture'
            test "$(cat "$bundle/CLI.md")" = 'from the command line'
            test "$(cat "$bundle/With spaces.md")" = 'from the task'
            grep -Fx -- '- Pending lessons: 5' "$bundle/STATE-brief.md"
            if grep -F 'Private state body' "$bundle/STATE-brief.md"; then exit 1; fi
            test -d .git
            if (touch .git/HEAD) 2>/dev/null; then exit 1; fi
            for file in INBOX.jsonl approvals.sqlite TASK.md CLI.md STATE.md .runs/other-ctx .runs/other-out; do
                test ! -e "$product/$file"
                test ! -L "$product/$file"
            done
            if (printf changed > "$bundle/TASK.md") 2>/dev/null; then exit 1; fi
            while [ "$1" != -o ]; do shift; done
            printf '%s' 'bundle verified' > "$2"
        ''')
        result = self.worker('--context', 'CLI.md', bwrap=bwrap,
                             task_text='Context fixture\nContext: "With spaces.md"\n')
        self.assertEqual(result.returncode, 0, self.worker_diagnostics(result))
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'bundle verified')

    def worker_diagnostics(self, result):
        events = (path.read_text(errors='replace') for path in (self.product / '.runs').glob('*.events.jsonl'))
        return '\n'.join((result.stdout or '', result.stderr or '', *events))

    def review_repo(self, object_format='sha1'):
        owner = self.base / f'owner-{object_format}'
        owner.mkdir()
        self.git('init', '-q', '-b', 'main', f'--object-format={object_format}', cwd=owner)
        file = owner / 'visible.txt'
        file.write_text('pinned content\n')
        self.git('add', 'visible.txt', cwd=owner)
        signature = ('-c', 'user.name=Test', '-c', 'user.email=test@example.test')
        self.git(*signature, 'commit', '-qm', 'pinned', cwd=owner)
        sha = self.git('rev-parse', 'HEAD', cwd=owner).stdout.decode().strip()
        file.write_text('newer content\n')
        self.git(*signature, 'commit', '-qam', 'newer', cwd=owner)
        return owner, sha

    def assert_review_cleaned(self, owner):
        registration = self.git('worktree', 'list', '--porcelain', cwd=owner).stdout.splitlines()
        self.assertEqual([line for line in registration if line.startswith(b'worktree ')],
                         [b'worktree ' + os.fsencode(owner)])
        self.assertFalse(list(self.clone.parent.glob('review-*')))

    def test_real_bwrap_reviewer_sees_pinned_sha_and_read_only_projection(self):
        bwrap = self.real_bwrap()
        shutil.rmtree(self.clone)  # These reviews use --cwd without a legacy clone.
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        for object_format in ('sha1', 'sha256'):
            with self.subTest(object_format=object_format):
                owner, sha = self.review_repo(object_format)
                marker = owner / 'owner-private-marker'
                marker.touch()
                self.git('replace', sha, 'HEAD', cwd=owner)
                self.git('pack-refs', '--all', cwd=owner)
                head = self.git('rev-parse', 'HEAD', cwd=owner).stdout.decode().strip()
                self.git('replace', head, sha, cwd=owner)
                self.make_executable(self.binary_dir / 'codex',
                                     f'marker={shlex.quote(str(marker))}\n'
                                     f'objects={shlex.quote(str(owner / ".git" / "objects"))}\n'
                                     f'sha={sha}\nformat={object_format}\n' + '''
                    set -eux
                    test "$GIT_NO_REPLACE_OBJECTS" = 1
                    test ! -e "$marker"
                    for fd in /proc/self/fd/*; do
                        test "$(readlink "$fd" || :)" != "${PWD%/*}"
                    done
                    test -d "$objects"
                    test "$(git rev-parse HEAD)" = "$sha"
                    test "$(git rev-parse --show-object-format)" = "$format"
                    test -z "$(git replace -l)"
                    test "$(git show HEAD:visible.txt)" = 'pinned content'
                    test "$(cat visible.txt)" = 'pinned content'
                    if (touch checkout-write) 2>/dev/null; then exit 1; fi
                    if (touch "$objects/object-write") 2>/dev/null; then exit 1; fi
                    while [ "$1" != -o ]; do shift; done
                    printf 'review verified\n' > "$2"
                ''')
                result = self.worker('--cwd', str(owner), '--sha', sha, bwrap=bwrap)
                self.assertEqual(result.returncode, 0, self.worker_diagnostics(result))
                self.assertEqual(json.loads(result.stdout)['reviewed_sha'], sha)
                self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'review verified\n')
                self.assert_review_cleaned(owner)

    def test_real_bwrap_reviewer_cleans_up_after_failure_timeout_and_sigterm(self):
        self.check_review_cleanup(self.real_bwrap())

    def test_real_bwrap_holds_review_lease_after_launcher_sigkill_until_worker_exits(self):
        self.check_review_sigkill(direct_parent=False)

    def test_real_bwrap_direct_parent_sigkill_stops_worker_before_reclaiming_review(self):
        self.check_review_sigkill(direct_parent=True)

    def check_review_sigkill(self, *, direct_parent):
        from skill.scripts import run_codex

        bwrap = self.real_bwrap()
        shutil.rmtree(self.clone)
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        owner, sha = self.review_repo()
        worker = r'''
import fcntl, sys, time
from pathlib import Path
deliverable = Path(sys.argv[sys.argv.index('-o') + 1])
out = deliverable.parent
with (out / 'worker-lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    (out / 'pending').write_text(str(Path.cwd()))
    (out / 'pending').rename(out / 'ready')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (out / 'shutdown').exists():
            (out / 'draining').touch()
        if (out / 'finish').exists():
            deliverable.write_text('done\n')
            break
        time.sleep(0.01)
    else:
        raise SystemExit(99)
'''
        self.make_executable(self.binary_dir / 'codex', f'exec /usr/bin/python3 -I -c {shlex.quote(worker)} "$@"')
        closed = self.base / 'supervisor-closed-fds'
        exited = self.base / 'bwrap-exited'
        direct_pid = self.base / 'bwrap-pid'
        # Delay the parent-death notification without changing bubblewrap's
        # actual argv. The supervisor drops every inherited fd after exec.
        supervisor = f'''
import os, subprocess, sys
from pathlib import Path
count = int(sys.argv[1])
fds = tuple(map(int, sys.argv[2:2 + count]))
child = subprocess.Popen(sys.argv[2 + count:], pass_fds=fds)
for fd in fds:
    os.close(fd)
Path({str(closed)!r}).touch()
code = child.wait()
Path({str(exited)!r}).touch()
sys.exit(code)
'''
        setup = f'''
import subprocess
popen = subprocess.Popen
def delay_shutdown(argv, **kwargs):
    if '--sync-fd' in argv:
        fds = kwargs['pass_fds']
        argv = [sys.executable, '-I', '-c', {supervisor!r}, str(len(fds)), *map(str, fds), *argv]
    return popen(argv, **kwargs)
subprocess.Popen = delay_shutdown
'''
        if direct_parent:
            setup = f'''
import subprocess
from pathlib import Path
popen = subprocess.Popen
def identify_child(argv, **kwargs):
    child = popen(argv, **kwargs)
    if '--sync-fd' in argv:
        assert argv[0] == {str(bwrap)!r}
        Path({str(direct_pid)!r}).write_text(str(child.pid))
    return child
subprocess.Popen = identify_child
'''
        runs = self.product / '.runs'
        task = self.product / 'TASK.md'
        task.write_text('Synthetic lease test\n')

        def wait_for(check, description):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if result := check():
                    return result
                time.sleep(0.01)
            self.fail(description)

        def launch(stack, name, setup=''):
            existing = set(runs.glob('*-out/ready'))
            cmd = self.launcher('run_codex.py', '--product', 'demo', '--role', 'analyst', '--prompt', str(task),
                                '--out', name, '--cwd', str(owner), '--sha', sha, bwrap=bwrap, setup=setup)
            proc = stack.enter_context(subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE,
                                                         stderr=subprocess.PIPE, text=True))
            stack.callback(lambda: proc.kill() if proc.poll() is None else None)
            ready, = wait_for(lambda: set(runs.glob('*-out/ready')) - existing, f'{name} did not start')
            if not (direct_parent and setup):
                stack.callback((ready.parent / 'finish').touch)
            return proc, Path(ready.read_text().strip()), ready.parent

        def registrations():
            return self.git('worktree', 'list', '--porcelain', cwd=owner).stdout

        with ExitStack() as stack, patch.object(run_codex, 'work_repo', return_value=self.clone):
            first, abandoned, outbox = launch(stack, 'first.md', setup)
            other, active, other_box = launch(stack, 'active.md')
            if direct_parent:
                pid = int(direct_pid.read_text())
                self.assertIn(f'PPid:\t{first.pid}\n', Path(f'/proc/{pid}/status').read_text())
                pid_fd = os.pidfd_open(pid)
                stack.callback(os.close, pid_fd)
                worker_lock = stack.enter_context((outbox / 'worker-lock').open('rb'))
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn(b'worktree ' + os.fsencode(abandoned) + b'\n', registrations())
            else:
                wait_for(closed.is_file, 'supervisor did not close its inherited descriptors')
            first.kill()
            self.assertEqual(first.wait(timeout=5), -signal.SIGKILL)
            if direct_parent:
                self.assertTrue(select.select([pid_fd], [], [], 5)[0], 'bubblewrap survived its parent')

                def worker_exited():
                    try:
                        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
                    return True

                wait_for(worker_exited, 'sandboxed worker survived launcher and bubblewrap exit')
                self.assertFalse((outbox / 'finish').exists())
            else:
                (outbox / 'shutdown').touch()
                wait_for((outbox / 'draining').is_file, 'the sandboxed worker was not still shutting down')
                with run_codex.review_worktree('demo', owner, sha):
                    self.assertFalse(exited.exists())
                    self.assertTrue((abandoned.parent / 'lease.json').is_file())
                    self.assertEqual((abandoned / 'visible.txt').read_text(), 'pinned content\n')
                    self.assertIn(b'worktree ' + os.fsencode(abandoned) + b'\n', registrations())
                (outbox / 'finish').touch()
                wait_for(exited.is_file, 'bubblewrap did not finish shutting down')
            with run_codex.review_worktree('demo', owner, sha):
                self.assertFalse(abandoned.parent.exists())
                listing = registrations()
                self.assertNotIn(b'worktree ' + os.fsencode(abandoned) + b'\n', listing)
                self.assertIn(b'worktree ' + os.fsencode(active) + b'\n', listing)
                self.assertEqual((active / 'visible.txt').read_text(), 'pinned content\n')
                self.assertIsNone(other.poll())
            (other_box / 'finish').touch()
            stdout, stderr = other.communicate(timeout=10)
            self.assertEqual(other.returncode, 0, stdout + stderr)
        self.assert_review_cleaned(owner)

    def test_reviewer_cleans_up_after_failure_timeout_and_sigterm(self):
        self.fake_bwrap()
        self.check_review_cleanup(None)

    def check_review_cleanup(self, bwrap):
        shutil.rmtree(self.clone)
        (self.config / 'config.json').write_text(json.dumps({
            'worker_network': 'host', 'roles': {'analyst': {'timeout_minutes': 0.05}},
        }))
        owner, sha = self.review_repo()
        for body, code in (('exit 7\n', 7), ('echo worker-ready\nexec sleep 60\n', 124)):
            with self.subTest(code=code):
                self.make_executable(self.binary_dir / 'codex', body)
                result = self.worker('--cwd', str(owner), '--sha', sha, bwrap=bwrap)
                self.assertEqual(result.returncode, code, self.worker_diagnostics(result))
                record = json.loads(result.stdout)
                self.assertEqual((record['reviewed_sha'], record['exit'], record['timed_out']),
                                 (sha, code, code == 124))
                self.assert_review_cleaned(owner)

        task = self.product / 'TASK.md'
        ready = self.product / 'signal-ready'
        self.make_executable(self.binary_dir / 'codex', '''
            while [ "$1" != -o ]; do shift; done
            echo ready > "$2"
            exec sleep 60
        ''')
        cmd = self.launcher('run_codex.py', '--product', 'demo', '--role', 'analyst', '--prompt', str(task),
                            '--out', str(ready), '--cwd', str(owner), '--sha', sha, '--timeout', '1', bwrap=bwrap)
        with subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as proc:
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and proc.poll() is None:
                    if any(path.stat().st_size for path in (self.product / '.runs').glob('*-out/deliverable.md')):
                        break
                    time.sleep(0.01)
                else:
                    self.fail('review worker failed to signal readiness')
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, stdout + stderr)
        self.assertEqual(json.loads(stdout)['exit'], 128 + signal.SIGTERM)
        self.assert_review_cleaned(owner)

    def test_real_bwrap_blocks_host_listeners_and_connects_through_the_bridge(self):
        from skill.scripts import sandbox

        bwrap = self.real_bwrap()
        self.require_unix_socket()
        socat = self.real_socat()
        self.env['PATH'] = os.pathsep.join((str(socat.parent), self.env['PATH']))
        if not any(addr.version == 4 and not addr.is_loopback for addr in sandbox.egress_proxy.local_interfaces()[0]):
            self.skip_real_bwrap('no non-loopback IPv4 address for the wildcard listener')
        env = dict(self.env, PATH=os.pathsep.join([str(bwrap.parent), str(socat.parent), self.env['PATH']]))
        outbox = self.product / '.runs' / 'current-out'
        outbox.mkdir(parents=True, mode=0o700)
        sibling = outbox.with_name('sibling-out')
        sibling.mkdir()
        with patch.dict(os.environ, env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)):
            report = sandbox.selftest(clone=self.clone, product_dir=self.product, codex_home=self.private,
                                      check_version=False, outbox=outbox, config={})
        self.assertIn('host ports blocked and egress checked', report)
        argv = self.wrap(['/bin/sh', '-ec', 'test -d "$1"; test ! -e "$2"', 'probe', str(outbox), str(sibling)],
                         bwrap_dir=bwrap.parent, outbox=outbox)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env={},
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Regression: the worker must read the launcher's stdin (its prompt), not /proc/net/tcp.
        with socket.socket(socket.AF_UNIX) as fake_proxy:
            proxy_dir = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/tmp')))
            proxy_socket = proxy_dir / sandbox.egress_proxy.SOCKET_NAME
            fake_proxy.bind(str(proxy_socket))
            fake_proxy.listen()
            argv = self.wrap(['/bin/sh', '-c', 'cat'], bwrap_dir=bwrap.parent, outbox=outbox,
                             proxy_socket=proxy_socket)
            env_with_socat = dict(PATH=os.pathsep.join([str(socat.parent), '/usr/bin', '/bin']))
            result = subprocess.run(argv, input='prompt-from-launcher\n', capture_output=True, text=True,
                                    timeout=10, env=env_with_socat, pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'prompt-from-launcher\n')

    def test_real_bwrap_host_escape_hatch_reaches_a_host_listener(self):
        bwrap = self.real_bwrap()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            argv = self.wrap(['python3', '-I', '-c',
                              f'import socket; socket.create_connection(("127.0.0.1", {port}), 1).close()'],
                             bwrap_dir=bwrap.parent, config={'worker_network': 'host'})
            result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10,
                                    pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_bwrap_worker_cannot_open_bridge_memory_for_writing(self):
        from skill.scripts import sandbox

        bwrap = self.real_bwrap()
        self.require_unix_socket()
        socat = self.real_socat()
        self.env['PATH'] = os.pathsep.join((str(socat.parent), self.env['PATH']))
        probe = '''
import errno
import os
import sys
from pathlib import Path

shell = Path('/proc') / str(os.getppid())
assert b'worker-egress' in (shell / 'cmdline').read_bytes().split(b'\\0')
bridge, = (entry.parent for entry in Path('/proc').glob('[0-9]*/cmdline')
           if entry.read_bytes().split(b'\\0', 1)[0] == os.fsencode(sys.argv[1]))
for process in (shell, bridge):
    memory = process / 'mem'
    assert memory.is_file(), memory
    try:
        fd = os.open(memory, os.O_WRONLY)
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM), error
    else:
        os.close(fd)
        raise AssertionError(f'worker opened {memory} for writing')
'''
        with ExitStack() as stack:
            proxy_socket, _ = sandbox.network_probe(stack, self.base)
            argv = self.wrap(['/usr/bin/python3', '-I', '-c', probe, str(socat.resolve())],
                             bwrap_dir=bwrap.parent, proxy_socket=proxy_socket)
            result = subprocess.run(argv, env={}, capture_output=True, text=True, timeout=10,
                                    pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_bwrap_masks_clone_socket_while_egress_works(self):
        from skill.scripts import sandbox

        bwrap = self.real_bwrap()
        self.require_unix_socket()
        socat = self.real_socat()
        toolchain = self.base / 'toolchain'
        toolchain.mkdir()
        self.make_executable(toolchain / 'go', 'exit 0\n')
        self.env['PATH'] = os.pathsep.join((str(socat.parent), str(toolchain), self.env['PATH']))
        outbox = self.product / 'probe-out'
        outbox.mkdir(mode=0o700)
        host_socket = self.clone / 'host.sock'
        self.git('init', '-q', '-b', 'main')
        objects = self.clone / '.git' / 'objects'
        late_sockets = [directory / 'late.sock' for directory in (self.clone, toolchain)]
        late_git_socket = objects / 'late.sock'
        ready = outbox / 'ready'
        with ExitStack() as stack:
            listener = stack.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(str(host_socket))
            listener.listen()
            proxy_socket, listeners = sandbox.network_probe(stack, self.base)
            probe = '''
import errno
import socket
import stat
import sys
from pathlib import Path

ready, original, host_proxy, worker_proxy, git_socket, *paths = sys.argv[1:8]
del sys.argv[1:8]
assert Path(original).is_file(), 'the original host socket was not masked'
assert Path(worker_proxy).is_socket(), 'the bridge socket mount is missing'
assert not Path(host_proxy).parent.exists(), 'the proxy directory is visible'
Path(ready).touch()
assert sys.stdin.readline() == 'ready\\n', 'did not receive the launch command'
assert not Path(git_socket).exists(), 'a late Git objects entry is visible'
for path in paths:
    assert stat.S_ISSOCK(Path(path).stat().st_mode), 'the late socket fixture is not visible'
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(path)
    except OSError as error:
        assert error.errno == errno.EACCES, error
    else:
        raise SystemExit('worker reached a newly created host Unix listener')
''' + sandbox.PROBE
            argv = self.wrap(['/usr/bin/python3', '-I', '-c', probe, str(ready), str(host_socket),
                              str(proxy_socket), sandbox.egress_proxy.SANDBOX_SOCKET, str(late_git_socket),
                              *map(str, late_sockets),
                              sandbox.SECRET_ENV.pattern, str(sandbox.egress_proxy.PORT), *listeners, '--'],
                             bwrap_dir=bwrap.parent, outbox=outbox, proxy_socket=proxy_socket, write=True)
            proc = subprocess.Popen(argv, env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, pass_fds=self.checkout_bind_fds(argv))
            try:
                self.wait_for_worker_file(ready, proc)
                for path in (*late_sockets, late_git_socket):
                    listener = stack.enter_context(socket.socket(socket.AF_UNIX))
                    listener.bind(str(path))
                    listener.listen()
                _, stderr = proc.communicate('ready\n', timeout=10)
                self.assertEqual(proc.returncode, 0, stderr)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)

    def wait_for_worker_file(self, path, proc):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            if proc.poll() is not None:
                self.fail(proc.communicate()[1])
            time.sleep(0.01)
        self.fail(f'worker did not signal readiness through {path.name}')

    def test_argv_masks_sockets_from_clone_and_toolchains_and_omits_product(self):
        self.require_unix_socket()
        self.git('init', '-q', '-b', 'main')
        toolchain = self.base / 'toolchain'
        toolchain.mkdir()
        self.make_executable(toolchain / 'python3', 'exit 0\n')
        self.env['PATH'] += os.pathsep + str(toolchain)
        paths = [directory / 'sock' for directory in (self.clone, self.product, toolchain)]
        with ExitStack() as stack:
            for path in paths:
                listener = stack.enter_context(socket.socket(socket.AF_UNIX))
                listener.bind(str(path))
            argv = self.wrap(['codex'], write=True)
        mask = str(self.product / '.sandbox-empty')
        self.assertEqual({dest for arg, source, dest in zip(argv, argv[1:], argv[2:])
                          if arg == '--ro-bind' and source == mask}, {str(paths[0]), str(paths[2])})
        self.assertNotIn(str(self.product), argv)
        self.assertNotIn('/run', argv)
        self.assertNotIn('/var/run', argv)

    def test_socket_walk_entry_bound_and_git_objects_exclusion(self):
        from skill.scripts import sandbox

        objects = self.clone / '.git' / 'objects'
        objects.mkdir(parents=True)
        (objects / 'ignored').touch()
        mounts = {self.clone: ('--bind', str(self.clone))}
        with patch.object(sandbox, 'SOCKET_WALK_ENTRIES', 3):
            self.assertEqual(sandbox.socket_masks(mounts), [])
            (self.clone / 'fourth-entry').touch()
            with self.assertRaisesRegex(OSError, 'Unix socket walk exceeds 3 entries'):
                sandbox.socket_masks(mounts)

    def test_worker_refuses_socket_walk_above_depth_bound(self):
        self.fake_bwrap()
        self.clone.joinpath(*('nested' for _ in range(13))).mkdir(parents=True)
        result = self.worker()
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('Unix socket walk exceeds depth 12', result.stderr)

    def test_socket_walk_budget_warns_only_for_filtered_worker(self):
        bwrap = self.real_bwrap()
        self.require_unix_socket()
        socat = self.real_socat()
        self.env['PATH'] = os.pathsep.join((str(socat.parent), self.env['PATH']))
        path = self.clone / 'over-budget.sock'
        self.git('init', '-q', '-b', 'main')
        codex = self.binary_dir / 'codex'
        codex.write_text('#!/usr/bin/python3\n' + f'''
import errno
import socket
import stat
import sys
from pathlib import Path

path = Path({str(path)!r})
assert stat.S_ISSOCK(path.stat().st_mode), 'the socket was masked despite the exhausted budget'
try:
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
except OSError as error:
    assert error.errno == errno.EACCES, error
else:
    raise AssertionError('worker reached the host listener after the socket walk stopped')
Path(sys.argv[sys.argv.index('-o') + 1]).write_text('socket denied')
''')
        codex.chmod(0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen()
            result = self.worker(role='builder', bwrap=bwrap, socket_walk_entries=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = json.loads(result.stdout)['sandbox_warnings']
        self.assertEqual(len(warnings), 1)
        self.assertIn('Unix socket walk exceeds 0 entries', warnings[0])
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'socket denied')

    def test_host_network_worker_refuses_socket_walk_over_budget(self):
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        self.make_executable(self.binary_dir / 'bwrap', 'exit 99\n')
        result = self.worker(socket_walk_entries=1)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('Unix socket walk exceeds 1 entries', result.stderr)

    def test_worker_exec_filter_inherits_and_keeps_stream_socketpairs(self):
        from skill.scripts import worker_exec

        probe = '''
import ctypes, errno, socket
from pathlib import Path

for create in (lambda: socket.socket(socket.AF_UNIX), lambda: socket.socket(40),  # AF_VSOCK
               lambda: socket.socket(socket.AF_PACKET),
               lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 15),  # NETLINK_KOBJECT_UEVENT
               lambda: socket.socketpair(socket.AF_INET),
               lambda: socket.socketpair(40),  # AF_VSOCK
               lambda: socket.socketpair(30),  # AF_TIPC
               lambda: socket.socketpair(type=socket.SOCK_DGRAM),
               lambda: socket.socketpair(type=socket.SOCK_SEQPACKET)):
    try:
        create()
    except OSError as error:
        assert error.errno == errno.EACCES, error
    else:
        raise AssertionError('worker created a socket outside the allowlist')
with socket.socket(socket.AF_INET):
    pass
with socket.socket(socket.AF_INET6), socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE):
    pass
left, right = socket.socketpair(type=socket.SOCK_STREAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)
with left, right:
    left.sendall(b'fixture')
    assert right.recv(7) == b'fixture'
libc = ctypes.CDLL(None, use_errno=True)
for number in calls:
    assert libc.syscall(number, 0, 0, 0, 0, 0, 0) == -1, number
    assert ctypes.get_errno() == errno.EACCES, (number, ctypes.get_errno())
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
'''
        machine = worker_exec.platform.machine()
        process_calls = {'x86_64': (101, 310, 311), 'aarch64': (117, 270, 271)}
        calls = (425, 426, 427, 438, *process_calls.get(machine, ()))
        if machine == 'x86_64':
            calls += tuple(range(512, 548))
        probe = f'calls = {calls!r}\n' + probe
        command = [sys.executable, '-I', str(SCRIPTS / 'worker_exec.py'), str(self.clone), '0',
                   sys.executable, '-I', '-c', probe]
        result = subprocess.run(command, env={}, capture_output=True, text=True, timeout=10)
        if machine in worker_exec.SYSCALLS and sys.maxsize > 2**32 and sys.byteorder == 'little':
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn('unsupported syscall architecture', result.stderr)

    def test_filter_decision_restricts_inet_types_and_protocols(self):
        from skill.scripts import worker_exec as worker

        flags = socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK
        for machine, (arch, socket_nr, pair_nr, *_) in worker.SYSCALLS.items():
            for family in (socket.AF_INET, socket.AF_INET6):
                for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW,
                             getattr(socket, 'SOCK_PACKET', 10), socket.SOCK_SEQPACKET, socket.SOCK_STREAM | (1 << 30)):
                    for protocol in (0, socket.IPPROTO_TCP, socket.IPPROTO_UDP, socket.IPPROTO_ICMP):
                        for extra in (0, flags):
                            with self.subTest(machine=machine, family=family, kind=kind, protocol=protocol, flags=extra):
                                allowed = ((kind == socket.SOCK_STREAM and protocol in (0, socket.IPPROTO_TCP))
                                           or (kind == socket.SOCK_DGRAM and protocol in (0, socket.IPPROTO_UDP)))
                                self.assertEqual(filter_decision(machine, arch, socket_nr, family, kind | extra,
                                                                 protocol), worker.ALLOW if allowed else worker.DENY)
            for family in (socket.AF_INET, 40, 30):
                self.assertEqual(filter_decision(machine, arch, pair_nr, family, socket.SOCK_STREAM, 0), worker.DENY)
            self.assertEqual(filter_decision(machine, arch, socket_nr, socket.AF_NETLINK,
                                             socket.SOCK_RAW, socket.NETLINK_ROUTE), worker.ALLOW)

    def test_filter_decision_rejects_tagged_x32_and_foreign_arch(self):
        from skill.scripts import worker_exec as worker

        for machine, (arch, socket_nr, *_) in worker.SYSCALLS.items():
            args = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            with self.subTest(machine=machine):
                self.assertEqual(filter_decision(machine, arch, 0x40000000 | socket_nr, *args), worker.KILL_PROCESS)
                for foreign in (0x40000003, *(entry[0] for entry in worker.SYSCALLS.values() if entry[0] != arch)):
                    self.assertEqual(filter_decision(machine, foreign, socket_nr, *args), worker.KILL_PROCESS)

    def test_filter_install_failure_stops_target_with_and_without_bridge(self):
        from skill.scripts import sandbox

        self.git('init', '-q', '-b', 'main')
        proxy_dir = self.base / 'proxy'
        proxy_dir.mkdir(mode=0o700)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        socat = self.binary_dir / 'socat'
        socat.write_text(f'#!{sys.executable}\n' + '''
import socket, sys, time
time.sleep(0.15)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(sys.argv[1].split(',')[0].split(':')[1])))
    listener.listen()
    time.sleep(8)
''')
        socat.chmod(0o700)
        toolchain = self.clone / 'bin'
        toolchain.mkdir(mode=0o700)
        untrusted_sleep = self.base / 'untrusted-sleep'
        self.make_executable(toolchain / 'sleep', f': > {shlex.quote(str(untrusted_sleep))}\n')
        self.make_executable(toolchain / 'python3', 'exit 87\n')
        self.env['PATH'] = f'{toolchain}:{self.env["PATH"]}'
        marker = self.base / 'target-started'
        helper = SCRIPTS / 'worker_exec.py'
        source = helper.read_text().replace('    prctl = ctypes.CDLL(None, use_errno=True).prctl',
                                            '    def prctl(option, *_):\n'
                                            '        ctypes.set_errno(errno.EPERM)\n'
                                            '        return -1 if option == PR_SET_SECCOMP else 0')
        for proxy_socket in (None, proxy_dir / sandbox.egress_proxy.SOCKET_NAME):
            with self.subTest(proxy=proxy_socket is not None), patch.object(sandbox.egress_proxy, 'PORT', port), \
                    patch.object(sandbox, 'WORKER_EXEC_SOURCE', source):
                argv = self.wrap(['/bin/sh', '-c', ': > "$1"', 'target', str(marker)], proxy_socket=proxy_socket)
                # Execute the exact inner command, including the bridge, without requiring bwrap privileges.
                command = argv[argv.index('--') + 1:]
                if proxy_socket is not None:
                    command[4] = str(socat)  # Local bridge fixture for the raw inner command.
                result = subprocess.run(command, env=self.argv_env(argv), capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn('worker socket isolation could not start the command', result.stderr)
                self.assertIn(os.strerror(errno.EPERM), result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(untrusted_sleep.exists())

    def test_bridge_wrappers_use_system_path_before_the_worker_environment(self):
        from skill.scripts import sandbox

        self.git('init', '-q', '-b', 'main')
        toolchain = self.clone / 'bin'
        toolchain.mkdir()
        marker = self.base / 'toolchain-started'
        for name in ('sh', 'python3'):
            self.make_executable(toolchain / name, f': > {shlex.quote(str(marker))}\nexit 87\n')
        self.env.update(PATH=f'{toolchain}:{self.env["PATH"]}', FIXTURE_SETTING='worker',
                        FIXTURE_A='a' * 70_000, FIXTURE_B='b' * 70_000)
        proxy_dir = self.base / 'proxy'
        proxy_dir.mkdir(mode=0o700)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        socat = self.binary_dir / 'socat'
        socat.write_text('''#!/usr/bin/env python3
import os, socket, subprocess, sys, time
assert 'FIXTURE_SETTING' not in os.environ
assert os.getcwd() == '/'
subprocess.run(['sh', '-c', ':'], check=True)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(sys.argv[1].split(',')[0].split(':')[1])))
    listener.listen()
    time.sleep(8)
''')
        socat.chmod(0o700)
        probe = '''
import os, sys
assert os.environ['FIXTURE_SETTING'] == 'worker'
assert os.environ['FIXTURE_A'] == 'a' * 70_000
assert os.environ['FIXTURE_B'] == 'b' * 70_000
assert os.environ['PATH'].split(os.pathsep)[0] == sys.argv[1]
assert os.getcwd() == sys.argv[2]
'''
        read_text = Path.read_text

        def refuse_worker_source_reread(path, *args, **kwargs):
            self.assertNotEqual(path.name, 'worker_exec.py', 'reread launcher text')
            return read_text(path, *args, **kwargs)

        with patch.object(sandbox.egress_proxy, 'PORT', port), \
                patch.object(Path, 'read_text', refuse_worker_source_reread):
            argv = self.wrap(['/usr/bin/python3', '-I', '-c', probe, str(toolchain), str(self.clone)],
                             config={'worker_env_allow': ['FIXTURE_SETTING', 'FIXTURE_A', 'FIXTURE_B']},
                             proxy_socket=proxy_dir / sandbox.egress_proxy.SOCKET_NAME)
        self.assertEqual(self.argv_env(argv), {'PATH': os.defpath})
        self.assertEqual(argv[argv.index('--chdir') + 1], '/')
        command = argv[argv.index('--') + 1:]
        command[4] = str(socat)  # Instrument only the local bridge fixture.
        result = subprocess.run(command, cwd='/', env=self.argv_env(argv),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_bridge_rejects_an_occupied_saved_stdin_fd(self):
        from skill.scripts import sandbox

        script = 'exec 9</dev/null\n' + sandbox.BRIDGE
        result = subprocess.run(['/bin/sh', '-c', script, 'bridge'], capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('fd 9 is in use', result.stderr)

    def lock_probe(self):
        codex = self.binary_dir / 'codex'
        codex.write_text(
            f'#!{sys.executable}\n'
            'import os, stat, sys\n'
            'from pathlib import Path\n'
            'for fd in Path("/proc/self/fd").iterdir():\n'
            '    try: target = os.readlink(fd)\n'
            '    except FileNotFoundError: continue\n'
            '    assert not target.endswith((".lock", "/auth.json")), target\n'
            'home = Path(os.environ["CODEX_HOME"])\n'
            'assert stat.S_IMODE(home.stat().st_mode) == 0o700\n'
            'assert (home / "config.toml").read_text() == \'model_reasoning_summary = "none"\\n\'\n'
            'assert "Test task" in sys.stdin.read()\n'
            'Path(sys.argv[sys.argv.index("-o") + 1]).write_text("fd check passed")\n'
        )
        codex.chmod(0o700)

    def test_fake_bwrap_receives_only_its_mount_descriptors(self):
        self.fake_bwrap()
        self.lock_probe()
        self.git('init', '-q', '-b', 'main')
        result = self.worker(role='builder')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'fd check passed')

    def test_real_bwrap_worker_does_not_inherit_lock_fds(self):
        bwrap = self.real_bwrap()
        socat = self.real_socat()
        self.env['PATH'] = os.pathsep.join((str(socat.parent), self.env['PATH']))
        self.lock_probe()
        self.git('init', '-q', '-b', 'main')
        result = self.worker(role='builder', bwrap=bwrap)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'fd check passed')

    def task_layout(self):
        owner = self.home / 'owner'
        common_dir = owner / '.git'

        def git(*args, cwd=self.base):
            subprocess.run(['git', *args], cwd=cwd, env=self.env, check=True, capture_output=True, timeout=10)

        git('init', '-q', '-b', 'main', owner)
        (owner / '.korolev').mkdir()
        (owner / '.korolev' / 'x').write_text('knowledge')
        (owner / 'config').mkdir()
        (owner / 'config' / '.env').write_text('fixture data')
        (owner / '.env.example').write_text('template')
        git('add', '.korolev', 'config', '.env.example', cwd=owner)
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'initial', cwd=owner)
        (owner / 'owner-only-marker').write_text('fixture marker')
        registry = self.product.parents[1] / 'projects.json'
        registry.write_text(json.dumps({'demo': {'folder': str(owner), 'git_common_dir': str(common_dir),
                                                  'branch': 'main'}}))
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'tasks.py'), 'new', '--product', 'demo',
                                 '--slug', '19-task'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        outbox = self.product / 'outbox'
        outbox.mkdir(mode=0o700)
        return dict(clone=Path(json.loads(result.stdout)['path']), owner_folder=owner,
                    git_common_dir=common_dir, outbox=outbox)

    def add_private_git_data(self, common):
        with (common / 'config').open('a') as stream:
            stream.write('''[remote "origin"]
    url = https://fixture-token@example.test/example/repo.git
[includeIf "gitdir:/unmatched/"]
    path = /unmatched/config
''')
        (common / 'hooks' / 'private-hook').touch()
        (common / 'info' / 'credentials-store').touch()
        (common / 'config.worktree').write_text('fixture-extra-config')
        (common / 'modules' / 'x').mkdir(parents=True)
        (common / 'modules' / 'x' / 'config').write_text('fixture-submodule-config')
        (common / 'FETCH_HEAD').write_text('fixture-fetch-head')
        (common / 'objects' / 'info' / 'private').touch()

    git_privacy_probe = '''
        git status --porcelain >/dev/null
        git diff >/dev/null
        git log -1 >/dev/null
        test -z "$(git remote -v)"
        common=$(git rev-parse --path-format=absolute --git-common-dir)
        git_dir=$(git rev-parse --absolute-git-dir)
        if grep -Ei 'fixture-token|includeif|remote' "$common/config"; then exit 1; fi
        for path in "$common/config.worktree" "$common/modules/x/config" "$common/FETCH_HEAD" \
                    "$common/hooks" "$common/info" "$common/logs" "$common/worktrees/sibling" \
                    "$common/objects/info/private" "$git_dir/config.worktree" "$git_dir/logs"; do
            test ! -e "$path"
            test ! -L "$path"
        done
        if (touch "$common/hooks/created") 2>/dev/null; then exit 1; fi
    '''

    def projected_git(self, argv, common, *, include_pinned=True):
        ro = self.mounts(argv, '--ro-bind', include_pinned=include_pinned)
        self.assertNotIn(common, ro)
        projected, = (source for source, target in ro.items() if target == common)
        return projected

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.clone, env=self.env,
                              check=True, capture_output=True, timeout=10)

    def init_git_fixture(self):
        self.git('init', '-q', '-b', 'main')
        self.git('add', 'visible.txt')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'initial')

    def test_git_snapshot_freezes_objects_refs_and_index(self):
        self.init_git_fixture()
        self.git('pack-refs', '--all')
        self.git('repack', '-ad')
        common = self.clone / '.git'
        objects = common / 'objects'
        shutil.rmtree(objects / 'info')
        (self.clone / 'visible.txt').write_text('staged fixture\n')
        self.git('add', 'visible.txt')
        loose, = (directory for directory in objects.iterdir() if len(directory.name) == 2)
        argv = self.wrap(['codex'])
        projected = self.projected_git(argv, common)
        ro = self.mounts(argv, '--ro-bind')
        self.assertFalse(any(path.is_relative_to(common) and path != common for path in ro.values()))
        self.assertFalse(any(path.is_relative_to(common) for path in ro))

        (objects / 'info').mkdir()
        (objects / 'info' / 'alternates').write_text(str(self.base / 'host-objects') + '\n')
        (common / 'refs' / 'heads' / 'late').write_bytes(
            (common / 'packed-refs').read_bytes().splitlines()[-1].split()[0] + b'\n')
        late_object = loose / ('f' * 38)
        late_object.touch()
        (common / 'index').unlink()
        for pack in (objects / 'pack').iterdir():
            pack.unlink()

        self.assertEqual(list((projected / 'objects' / 'info').iterdir()), [])
        self.assertFalse((projected / 'refs' / 'heads' / 'late').exists())
        self.assertFalse((projected / 'objects' / loose.name / late_object.name).exists())
        args = (f'--git-dir={projected}', f'--work-tree={self.clone}')
        self.assertEqual(self.git(*args, 'log', '-1', '--format=%s').stdout, b'initial\n')
        self.assertIn(b'+staged fixture', self.git(*args, 'diff', '--cached').stdout)
        self.assertEqual(self.git(*args, 'status', '--porcelain').stdout, b'M  visible.txt\n')

    def test_git_snapshot_retries_a_ref_updated_during_the_object_copy(self):
        from skill.scripts import sandbox

        self.init_git_fixture()
        common = self.clone / '.git'
        copy = sandbox.copy_git_objects
        for races in (1, 2):
            with self.subTest(races=races):
                calls = 0

                def copy_then_advance_ref(source, destination, deadline):
                    nonlocal calls
                    copy(source, destination, deadline)
                    calls += 1
                    if calls <= races:
                        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                                 'commit', '--allow-empty', '-qm', f'concurrent update {calls}')

                with patch.object(sandbox, 'copy_git_objects', side_effect=copy_then_advance_ref):
                    if races == 1:
                        projected = self.projected_git(self.wrap(['codex']), common)
                        self.assertEqual(self.git(f'--git-dir={projected}', 'rev-parse', 'HEAD').stdout,
                                         self.git('rev-parse', 'HEAD').stdout)
                    else:
                        with self.assertRaisesRegex(OSError, 'snapshot connectivity.*failed after retry'):
                            self.wrap(['codex'])
                self.assertEqual(calls, 2)

    def test_git_snapshot_retries_metadata_drift_even_with_all_objects_present(self):
        from skill.scripts import sandbox

        self.init_git_fixture()
        common = self.clone / '.git'
        first = self.git('rev-parse', 'HEAD').stdout.strip().decode()
        (self.clone / 'visible.txt').write_text('second version')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qam', 'second version')
        second = self.git('rev-parse', 'HEAD').stdout.strip().decode()
        linked = self.base / 'linked'
        self.git('worktree', 'add', '--detach', linked, first)
        copy = sandbox.copy_git_objects

        cases = ((self.clone, 'ref'), (self.clone, 'both'), (self.clone, 'packed'),
                 (linked, 'ref'), (linked, 'index'), (linked, 'both'))
        for clone, change in cases:
            for drifts in (1, 2):
                with self.subTest(clone=clone.name, change=change, drifts=drifts):
                    layout = common if clone == linked else None
                    self.git('reset', '--hard', first, cwd=clone)
                    packed = common / 'packed-refs'
                    packed.unlink(missing_ok=True)
                    calls = 0

                    def drift(source, target, deadline):
                        nonlocal calls
                        copy(source, target, deadline)
                        calls += 1
                        if calls > drifts:
                            return
                        revision = second if calls == 1 else first
                        # Both revisions and their index blobs are in the copied store.
                        if change == 'index':
                            self.git('read-tree', revision, cwd=clone)
                        elif change == 'packed':
                            if calls == 1:
                                packed.write_text(f'{revision} refs/tags/during-copy\n')
                            else:
                                packed.unlink()
                        else:
                            self.git('reset', '--soft' if change == 'ref' else '--hard', revision, cwd=clone)

                    storage = self.git_metadata / f'{clone.name}-{change}-{drifts}'
                    storage.mkdir()
                    with sandbox.git_root(clone) as fd, patch.object(sandbox, 'copy_git_objects', side_effect=drift):
                        if drifts == 2:
                            with self.assertRaisesRegex(OSError, 'metadata stability check failed after retry'):
                                sandbox.git_mounts(clone, layout, storage, checkout_fd=fd)
                            self.assertEqual(list(storage.iterdir()), [])
                        else:
                            mounts = sandbox.git_mounts(clone, layout, storage, checkout_fd=fd)
                            visible = Path(mounts[common][1])
                            metadata = visible / 'worktrees' / linked.name if layout else visible
                            git = (f'--git-dir={metadata}', f'--work-tree={clone}')
                            self.assertEqual(self.git(*git, 'rev-parse', 'HEAD', ':visible.txt').stdout,
                                             self.git('rev-parse', 'HEAD', ':visible.txt', cwd=clone).stdout)
                            self.assertEqual(self.git(*git, 'show-ref', '--head').stdout,
                                             self.git('show-ref', '--head', cwd=clone).stdout)
                            self.assertEqual(list(storage.iterdir()), [visible.parent])
                    self.assertEqual(calls, 2)

    def test_git_snapshot_refuses_missing_linked_index_blob(self):
        layout = self.task_layout()
        clone, common = layout['clone'], layout['git_common_dir']
        (clone / 'only-in-index').write_text('staged fixture')
        self.git('add', 'only-in-index', cwd=clone)
        oid = self.git('rev-parse', ':only-in-index', cwd=clone).stdout.strip().decode()
        (common / 'objects' / oid[:2] / oid[2:]).unlink()
        with self.assertRaisesRegex(OSError, 'snapshot connectivity.*failed after retry'):
            self.wrap(['codex'], **layout)

    def test_git_projection_validates_sha256_and_omits_loose_and_packed_replace_refs(self):
        owner, sha = self.review_repo('sha256')
        common = owner / '.git'
        self.git('replace', sha, 'HEAD', cwd=owner)
        self.git('pack-refs', '--all', cwd=owner)
        self.git('replace', 'HEAD', sha, cwd=owner)
        self.assertIn(b'refs/replace/', (common / 'packed-refs').read_bytes())
        self.assertTrue(list((common / 'refs' / 'replace').iterdir()))
        projected = self.projected_git(self.wrap(['codex'], clone=owner), common)
        self.assertFalse((projected / 'refs' / 'replace').exists())
        self.assertNotIn(b'refs/replace/', (projected / 'packed-refs').read_bytes())
        self.assertIn(b'refs/heads/main', (projected / 'packed-refs').read_bytes())
        self.assertEqual(self.git(f'--git-dir={projected}', 'show', f'{sha}:visible.txt').stdout,
                         b'pinned content\n')
        self.git('config', '--file', str(common / 'config'), 'extensions.objectFormat', 'invalid', cwd=owner)
        with self.assertRaisesRegex(OSError, 'invalid object format'):
            self.wrap(['codex'], clone=owner)

    def test_refuses_split_index_with_staged_changes(self):
        self.init_git_fixture()
        for layout in ({}, self.task_layout()):
            with self.subTest(linked=bool(layout)):
                clone = layout.get('clone', self.clone)
                (clone / 'staged.txt').write_text('staged fixture\n')
                self.git('add', 'staged.txt', cwd=clone)
                self.git('update-index', '--split-index', cwd=clone)
                self.assertTrue(self.git('rev-parse', '--shared-index-path', cwd=clone).stdout.strip())
                self.assertIn(b'staged.txt', self.git('diff', '--cached', '--name-only', cwd=clone).stdout)
                with self.assertRaisesRegex(OSError, 'Git split index.*sharedindex'):
                    self.wrap(['codex'], write=True, **layout)

    def test_refuses_alternates_clone_with_staged_changes(self):
        self.init_git_fixture()
        shared = self.base / 'shared'
        self.git('clone', '--shared', self.clone, shared)
        self.assertTrue((shared / '.git' / 'objects' / 'info' / 'alternates').is_file())
        (shared / 'visible.txt').write_text('staged fixture\n')
        self.git('add', 'visible.txt', cwd=shared)
        self.assertIn(b'+staged fixture', self.git('diff', '--cached', cwd=shared).stdout)
        with self.assertRaisesRegex(OSError, 'Git alternates-based object stores'):
            self.wrap(['codex'], clone=shared, write=True)

    def test_refuses_embedded_git_file_and_directory(self):
        self.git('init', '-q', '-b', 'main')
        embedded = self.clone / 'node_modules' / 'embedded' / '.git'
        embedded.parent.mkdir(parents=True)
        embedded.write_text('gitdir: elsewhere\n')
        with self.assertRaisesRegex(OSError, r'embedded Git repository.*embedded/\.git'):
            self.wrap(['codex'], write=True)
        embedded.unlink()
        embedded.mkdir()
        with self.assertRaisesRegex(OSError, r'embedded Git repository.*embedded/\.git'):
            self.wrap(['codex'], write=True)

    def test_refuses_bare_checkout_and_embedded_bare_repository(self):
        for bare in (self.base / 'bare-root', self.clone / 'embedded'):
            self.git('init', '--bare', '-q', bare)
            with self.subTest(bare=bare), self.assertRaisesRegex(OSError, 'bare Git repository'):
                self.wrap(['codex'], clone=bare if bare.parent == self.base else self.clone)

    def test_linked_pointer_cannot_select_sibling_worktree_record(self):
        layout = self.task_layout()
        sibling = self.base / 'sibling'
        self.git('worktree', 'add', '--detach', sibling, cwd=layout['owner_folder'])
        (layout['clone'] / '.git').write_bytes((sibling / '.git').read_bytes())
        with self.assertRaisesRegex(OSError, 'backlink does not name the pinned checkout'):
            self.wrap(['codex'], **layout)

    def test_linked_backlink_requires_existing_intermediate_components(self):
        layout = self.task_layout()
        gitfile = layout['clone'] / '.git'
        record = Path(gitfile.read_text().strip().removeprefix('gitdir: '))
        backlink = record / 'gitdir'
        relative = os.path.relpath(gitfile, record)
        backlink.write_text(relative + '\n')
        projected = self.projected_git(self.wrap(['codex'], **layout), layout['git_common_dir'])
        self.assertEqual((projected / 'worktrees' / record.name / 'gitdir').read_text(), str(gitfile) + '\n')
        dangling = 'absent/../' + relative
        self.assertEqual(os.path.abspath(record / dangling), str(gitfile))
        backlink.write_text(dangling + '\n')
        with self.assertRaisesRegex(OSError, 'backlink does not name the pinned checkout'):
            self.wrap(['codex'], **layout)

    def test_linked_snapshot_refuses_private_refs_but_keeps_empty_directories(self):
        layout = self.task_layout()
        clone = layout['clone']
        common = layout['git_common_dir']
        record = Path((clone / '.git').read_text().strip().removeprefix('gitdir: '))
        ref = 'refs/worktree/fixture'
        self.git('update-ref', ref, 'HEAD', cwd=clone)
        self.assertTrue((record / ref).is_file())
        with self.assertRaisesRegex(OSError, 'unsupported private worktree refs'):
            self.wrap(['codex'], **layout)
        self.assertEqual(list(self.git_metadata.iterdir()), [])
        self.git('update-ref', '-d', ref, cwd=clone)
        empty = record / 'refs/worktree/empty'
        empty.mkdir(parents=True, exist_ok=True)
        (empty / 'fixture.lock').touch()
        projected = self.projected_git(self.wrap(['codex'], **layout), common)
        self.assertEqual(list((projected / 'worktrees' / record.name / 'refs/worktree/empty').iterdir()), [])

    def test_gitless_export_cumulative_entry_and_metadata_limits(self):
        from skill.scripts import sandbox

        for name in ('a', 'b'):
            child = self.clone / name
            child.mkdir()
            (child / 'empty').touch()
            (child / 'link').symlink_to('x' * 200)
        names = ('a', 'b', 'visible.txt', 'empty', 'link', 'empty', 'link')
        metadata = len(names) * sandbox.CHECKOUT_ENTRY_CHARGE + sum(map(len, names)) + 200
        for limit, value, error in (('CHECKOUT_ENTRY_LIMIT', 6, 'entry limit'),
                                    ('CHECKOUT_METADATA_BUDGET', metadata, 'metadata budget')):
            with self.subTest(limit=limit), patch.object(sandbox, limit, value):
                with self.assertRaisesRegex(OSError, error):
                    self.wrap(['codex'])
                self.assertEqual(list(self.git_metadata.iterdir()), [])

    def test_gitless_checkout_refuses_writes_and_projects_read_only_placeholder(self):
        with self.assertRaisesRegex(OSError, 'private storage is required'):
            self.wrap(['codex'], git_metadata_dir=None)
        with self.assertRaisesRegex(OSError, r'writable worker checkout has no \.git'):
            self.wrap(['codex'], write=True)
        os.mkfifo(self.clone / 'do-not-open')
        argv = self.wrap(['codex'])
        checkout = self.projected_git(argv, self.clone)
        self.assertTrue(checkout.is_relative_to(self.git_metadata))
        self.assertFalse((checkout / 'do-not-open').exists())
        (self.clone / '.git').mkdir()
        (self.clone / '.git' / 'HEAD').write_text('late fixture\n')
        (self.clone / 'visible.txt').write_text('changed after export')
        self.assertEqual(list((checkout / '.git').iterdir()), [])
        self.assertEqual((checkout / 'visible.txt').read_text(), 'clone')

    def test_gitless_export_copies_a_pinned_descendant_after_rename(self):
        from skill.scripts import sandbox

        directory = self.clone / 'descendant'
        directory.mkdir()
        (directory / 'visible').write_text('original')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'visible').write_text('outside')
        inode = directory.stat()
        fstat = os.fstat

        def replace_checked_directory(fd):
            info = fstat(fd)
            if os.path.samestat(info, inode):
                directory.rename(self.base / 'moved')
                directory.symlink_to(outside, target_is_directory=True)
            return info

        with patch.object(sandbox.os, 'fstat', side_effect=replace_checked_directory):
            export = self.projected_git(self.wrap(['codex']), self.clone)
        self.assertTrue(directory.is_symlink())
        self.assertEqual((export / 'descendant' / 'visible').read_text(), 'original')

    def test_frozen_export_checks_for_embedded_git_using_destination_lookup(self):
        child = self.clone / 'child'
        child.mkdir()
        (child / '.GIT').symlink_to('missing')
        exported = []
        lstat = Path.lstat

        def case_folding_lookup(path):
            if path.name == '.git' and path.is_relative_to(self.git_metadata):
                exported.append(path)
                return lstat(path.with_name('.GIT'))
            return lstat(path)

        with patch.object(Path, 'lstat', case_folding_lookup):
            with self.assertRaisesRegex(OSError, 'embedded Git repository is unsupported: child/\\.git'):
                self.wrap(['codex'])
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].parent.name, 'child')
        self.assertEqual(list(self.git_metadata.iterdir()), [])

    def test_gitless_export_cleans_up_when_sparse_extents_are_unavailable(self):
        from skill.scripts import sandbox

        with (self.clone / 'sparse').open('wb') as file:
            file.truncate(1024 * 1024)
        (self.clone / 'visible.txt').unlink()
        with patch.object(sandbox.os, 'lseek', side_effect=OSError(errno.EINVAL, 'unsupported operation')):
            with self.assertRaisesRegex(OSError, 'cannot report sparse file extents'):
                self.wrap(['codex'])
        self.assertEqual(list(self.git_metadata.iterdir()), [])

    def test_gitless_export_refuses_live_toolchain_child_and_alias_before_launch(self):
        from skill.scripts import sandbox

        inside, outside = self.clone / 'toolchain', self.base / 'other-toolchain'
        for directory in (inside, outside):
            directory.mkdir()
            self.make_executable(directory / 'python3', 'exit 0\n')
        alias = self.base / 'alias-toolchain'
        alias.symlink_to(inside, target_is_directory=True)
        marker = self.base / 'bwrap-started'
        bwrap = self.argv_bin / 'bwrap'
        self.make_executable(bwrap, f': > {shlex.quote(str(marker))}\necho "sandbox checks passed"\n')
        with patch.dict(os.environ, self.env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)):
            layout = dict(clone=self.clone, product_dir=self.product, codex_home=self.private,
                          git_metadata_dir=self.git_metadata, check_version=False, config={'worker_network': 'host'})
            for toolchain in (inside, alias):
                with self.subTest(toolchain=toolchain), patch.object(sandbox, 'toolchain_dirs', return_value=[toolchain]):
                    with self.assertRaisesRegex(OSError, 'bind overlaps the frozen Git-less checkout'):
                        sandbox.selftest(**layout)
                    self.assertFalse(marker.exists())
            with patch.object(sandbox, 'toolchain_dirs', return_value=[outside]):
                sandbox.selftest(**layout)
                self.assertTrue(marker.exists())

    def test_gitless_preflight_mount_inherits_validated_source_after_replacement(self):
        from skill.scripts import sandbox

        toolchain = self.base / 'toolchain'
        toolchain.mkdir()
        (toolchain / 'file').write_text('checked')
        inside = self.clone / 'replacement'
        inside.mkdir()
        (inside / 'file').write_text('live')
        bwrap = self.argv_bin / 'bwrap'
        bwrap.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
args = sys.argv[1:]
flag, fd = args[args.index({str(toolchain)!r}) - 2:args.index({str(toolchain)!r})]
assert flag == '--ro-bind-fd'
assert (Path('/proc/self/fd') / fd / 'file').read_text() == 'checked'
print('sandbox checks passed')
""")
        bwrap.chmod(0o700)
        run, opened = subprocess.run, []

        def replace_before_mount(argv, **kwargs):
            toolchain.rename(self.base / 'original-toolchain')
            toolchain.symlink_to(inside, target_is_directory=True)
            opened.extend(kwargs['pass_fds'])
            return run(argv, **kwargs)

        with (patch.dict(os.environ, self.env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)),
              patch.object(sandbox, 'toolchain_dirs', return_value=[toolchain]),
              patch.object(sandbox.subprocess, 'run', side_effect=replace_before_mount)):
            sandbox.selftest(clone=self.clone, product_dir=self.product, codex_home=self.private,
                             git_metadata_dir=self.git_metadata, check_version=False, config={'worker_network': 'host'})
        self.assertTrue(toolchain.is_symlink())
        self.assertTrue(opened)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        opened.clear()
        open_file = os.open

        def track_bind(path, flags, *args, **kwargs):
            fd = open_file(path, flags, *args, **kwargs)
            if path == str(toolchain):
                opened.append(fd)
            return fd

        with (patch.dict(os.environ, self.env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)),
              patch.object(sandbox, 'toolchain_dirs', return_value=[toolchain]),
              patch.object(sandbox.os, 'open', side_effect=track_bind)):
            with self.assertRaisesRegex(OSError, 'bind overlaps the frozen Git-less checkout'):
                sandbox.selftest(clone=self.clone, product_dir=self.product, codex_home=self.private,
                                 git_metadata_dir=self.git_metadata, check_version=False,
                                 config={'worker_network': 'host'})
        refused, = opened
        with self.assertRaises(OSError):
            os.fstat(refused)

    def test_git_snapshot_reads_verified_inode_after_path_replacement(self):
        from skill.scripts import sandbox

        self.git('init', '-q', '-b', 'main')
        common = self.clone / '.git'
        head = common / 'HEAD'
        original = 'ref: refs/heads/main\n'
        head.write_text(original)
        inode = head.stat()
        replacement = self.base / 'replacement'
        replacement.write_text('replacement fixture\n')
        fstat = os.fstat

        def swap_after_check(fd):
            info = fstat(fd)
            if (info.st_dev, info.st_ino) == (inode.st_dev, inode.st_ino):
                head.rename(common / 'original-HEAD')
                head.symlink_to(replacement)
            return info

        projected = self.git_metadata / 'projection'
        projected.mkdir()
        with sandbox.git_root(common) as fd, patch.object(sandbox.os, 'fstat', side_effect=swap_after_check):
            self.assertTrue(sandbox.copy_git_file(fd, 'HEAD', projected))
        self.assertTrue(head.is_symlink())
        self.assertEqual((projected / 'HEAD').read_text(), original)
        with self.assertRaisesRegex(OSError, 'Git metadata must be a regular file: HEAD'):
            self.wrap(['codex'])

    def test_sanitized_git_config_ignores_git_from_ambient_path(self):
        from skill.scripts import sandbox

        git = shutil.which('git')
        self.assertIsNotNone(git)
        git_bin = self.base / 'git-bin'
        git_bin.mkdir(mode=0o700)
        selected = git_bin / 'git'
        trace = self.base / 'git-called'
        self.make_executable(selected, f': > {shlex.quote(str(trace))}\nexec {shlex.quote(git)} "$@"\n')
        config = self.clone / '.git' / 'config'
        config.parent.mkdir()
        config.write_text('[core]\nfilemode = false\n[include]\npath = missing.ini\n')
        with patch.dict(os.environ, {'PATH': str(git_bin)}, clear=True), config.open('rb') as source:
            result = sandbox.sanitized_git_config(source)
        self.assertIn(b'filemode = false', result)
        self.assertNotIn(b'include', result)
        self.assertFalse(trace.exists())

    def test_executable_lookup_rejects_unsafe_files_and_ancestors(self):
        from skill.scripts import sandbox

        group_writable = self.base / 'group-writable'
        scratch = self.product / 'outbox'
        toolchain = self.home / 'toolchain'
        safe = self.base / 'trusted-bin'
        for entry in (group_writable, scratch, toolchain, safe):
            entry.mkdir(mode=0o700)
        self.make_executable(toolchain / 'node', 'exit 1\n')
        group_writable.chmod(0o770)
        for entry in (self.clone, scratch, toolchain, group_writable, safe):
            self.make_executable(entry / 'git', 'exit 1\n')
        search = os.pathsep.join(map(str, (self.clone, scratch, toolchain, group_writable, safe)))
        with patch.dict(os.environ, dict(self.env, PATH=search), clear=True):
            self.assertEqual(sandbox.trusted_executable('git', self.clone, scratch), safe / 'git')
            (safe / 'git').chmod(0o702)
            with self.assertRaisesRegex(OSError, 'no trusted git'):
                sandbox.trusted_executable('git', self.clone, scratch)

    def test_task_mount_order_and_masks_for_both_roles(self):
        layout = self.task_layout()
        clone, owner, common = (layout[key] for key in ('clone', 'owner_folder', 'git_common_dir'))
        self.add_private_git_data(common)
        (clone / 'node_modules').mkdir()
        (clone / 'node_modules' / 'skip.env').touch()
        for write in (False, True):
            with self.subTest(write=write):
                argv = self.wrap(['codex'], write=write, **layout)
                ro, rw = (self.mounts(argv, option) for option in ('--ro-bind', '--bind'))
                self.assert_checkout_bind(argv, clone, write=write)
                self.assertEqual(rw[layout['outbox']], layout['outbox'])
                projected = self.projected_git(argv, common)
                self.assertTrue(projected.is_relative_to(self.git_metadata))
                self.assertEqual({path.name for path in projected.iterdir()},
                                 {'HEAD', 'config', 'objects', 'refs', 'worktrees'})
                record, = (projected / 'worktrees').iterdir()
                self.assertEqual({path.name for path in record.iterdir()}, {'HEAD', 'commondir', 'gitdir', 'index'})
                self.assertEqual((record / 'index').read_bytes(),
                                 (common / 'worktrees' / record.name / 'index').read_bytes())
                self.assertFalse(any(source.is_relative_to(common) for source in ro))
                self.assertFalse(any(target.is_relative_to(common) and target != common for target in ro.values()))
                self.assertEqual((projected / 'config').read_text().splitlines(),
                                 ['[core]', '\tbare = false', '\trepositoryformatversion = 0',
                                  '\tfilemode = true', '\tlogallrefupdates = true'])
                self.assertEqual(ro[(self.product / '.sandbox-empty').resolve()], clone / 'config' / '.env')
                self.assertNotIn(Path('/dev/null'), ro)
                self.assertNotIn(clone / '.env.example', ro.values())
                self.assertNotIn(clone / 'node_modules' / 'skip.env', ro.values())
                self.assertNotIn(owner, (*ro, *rw))
                self.assertLess(argv.index(str(self.home), argv.index('--tmpfs')), argv.index(str(common)))
                self.assertLess(argv.index(str(owner)), argv.index(str(common)))
                if write:
                    self.assertEqual(ro[clone / '.korolev'], clone / '.korolev')

    def test_real_bwrap_task_status_owner_isolation_masks_knowledge_and_outbox(self):
        from skill.scripts import sandbox

        bwrap = self.real_bwrap()
        socat = self.real_socat()
        layout = self.task_layout()
        owner, outbox = layout['owner_folder'], layout['outbox']
        common = layout['git_common_dir']
        subprocess.run(['git', 'worktree', 'add', '--detach', str(self.base / 'sibling')],
                       cwd=owner, env=self.env, check=True, capture_output=True, timeout=10)
        self.add_private_git_data(common)
        env = dict(self.env, PATH=os.pathsep.join([str(bwrap.parent), str(socat.parent), self.env['PATH']]))
        with patch.dict(os.environ, env, clear=True), patch.object(sandbox, 'bwrap_path', return_value=str(bwrap)):
            sandbox.selftest(**layout, product_dir=self.product, codex_home=self.private,
                             write=True, check_version=False, config={})
        for workspace, write in (('task', True), ('task', False), ('owner', False)):
            with self.subTest(workspace=workspace, write=write):
                current = layout if workspace == 'task' else dict(layout, clone=owner)
                check = self.git_privacy_probe + '''
                    if [ "$3" = task ]; then test ! -e "$1"; fi
                    test "$(cat config/.env)" = ''
                    test "$(cat .env.example)" = template
                    if (echo changed > .korolev/x) 2>/dev/null; then exit 1; fi
                    if [ "$4" = yes ]; then
                        touch writable
                    elif (touch writable) 2>/dev/null; then
                        exit 1
                    fi
                    echo report > "$2/report"
                '''
                argv = self.wrap(['/bin/sh', '-eu', '-c', check, 'probe', str(owner / 'owner-only-marker'),
                                  str(outbox), workspace, 'yes' if write else 'no'],
                                 bwrap_dir=bwrap.parent, write=write, **current)
                result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10,
                                        pass_fds=self.checkout_bind_fds(argv))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((outbox / 'report').read_text(), 'report\n')
                if write:
                    (layout['clone'] / 'writable').unlink()
        self.assertTrue((common / 'worktrees' / 'sibling').is_dir())
        self.assertTrue((common / 'hooks' / 'private-hook').is_file())

    def test_real_bwrap_legacy_clone_masks_git_config_and_hooks(self):
        bwrap = self.real_bwrap()
        self.init_git_fixture()
        common = self.clone / '.git'
        head = self.git('rev-parse', 'HEAD').stdout
        self.add_private_git_data(common)
        info = common / 'objects' / 'info'
        shutil.rmtree(info)
        (self.clone / 'visible.txt').write_text('modified fixture\n')
        outbox = self.product / 'git-visibility-out'
        outbox.mkdir(mode=0o700)
        ready = outbox / 'ready'
        lock = common / 'config.lock'
        ref = common / 'refs' / 'heads' / 'late'
        alternates = info / 'alternates'
        check = self.git_privacy_probe + '''
            git diff | grep -F '+modified fixture'
            test -d "$2"
            touch "$1"
            read -r ready
            test "$ready" = ready
            shift 2
            for path do
                test ! -e "$path"
                test ! -L "$path"
            done
            git log -1 >/dev/null
        '''
        for write in (False, True):
            with self.subTest(write=write):
                argv = self.wrap(['/bin/sh', '-eu', '-c', check, 'probe',
                                  *map(str, (ready, info, lock, ref, alternates))],
                                 bwrap_dir=bwrap.parent, write=write, outbox=outbox)
                proc = subprocess.Popen(argv, env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, pass_fds=self.checkout_bind_fds(argv))
                try:
                    self.wait_for_worker_file(ready, proc)
                    lock.write_text('created after launch')
                    ref.write_bytes(head)
                    info.mkdir()
                    alternates.write_text(str(self.base / 'host-objects') + '\n')
                    _, stderr = proc.communicate('ready\n', timeout=10)
                    self.assertEqual(proc.returncode, 0, stderr)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=10)
                    lock.unlink(missing_ok=True)
                    ref.unlink(missing_ok=True)
                    if info.exists():
                        shutil.rmtree(info)
                    ready.unlink(missing_ok=True)
        self.assertIn('fixture-token', (common / 'config').read_text())
        self.assertTrue((common / 'hooks' / 'private-hook').is_file())

    def test_registered_selftest_allows_ancestors_of_intentional_mounts(self):
        from skill.scripts import sandbox

        layout = self.task_layout()
        ancestor = layout['owner_folder'].parent
        env = dict(self.env, PATH=os.pathsep.join([str(self.argv_bin), self.env['PATH']]))
        with patch.dict(os.environ, env, clear=True), patch.object(sandbox, 'bwrap_path',
                return_value=str(self.argv_bin / 'bwrap')), patch.object(sandbox, 'HIDDEN_DIRS',
                (Path('/home'), ancestor, Path('/root'))):
            argv = shlex.split(sandbox.selftest(**layout, product_dir=self.product,
                                               codex_home=self.private, dry_run=True))
        hidden = argv[argv.index('--', argv.index('sandbox-selftest')) + 1:]
        self.assertNotIn(str(ancestor), hidden)
        self.assertIn('/root', hidden)

    def test_worker_refuses_credential_walk_over_depth_or_match_limit(self):
        clone = self.task_layout()['clone']
        argv_log = self.fake_bwrap()
        deep = clone.joinpath(*('nested' for _ in range(13)))
        deep.mkdir(parents=True)
        result = self.worker('--task', '19-task', role='builder')
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('credential walk exceeds depth 12', result.stderr)
        shutil.rmtree(clone / 'nested')
        for index in range(2001):
            (clone / f'config-{index}.env').touch()
        result = self.worker('--task', '19-task', role='analyst')
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('credential walk exceeds 2000 matches', result.stderr)
        self.assertFalse(argv_log.exists())
        self.assertFalse((self.product / 'HANDOFF.md').exists())

    def worker(self, *args, role='analyst', bwrap=None, task_text='Test task\n',
               socket_walk_entries=None, proxy_path_log=None, setup=''):
        config_path = self.config / 'config.json'
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        if config.get('worker_sandbox') is not False and config.get('worker_network') != 'host' and '--cwd' not in args:
            self.require_unix_socket()
        task = self.product / 'TASK.md'
        task.write_text(task_text)
        if role == 'analyst' and not any(flag in args for flag in ('--cwd', '--task', '--sha')) \
                and not (self.clone / '.git').exists():
            args = ('--prompt-only', *args)
        return subprocess.run(self.launcher('run_codex.py', '--role', role, '--product', 'demo', '--prompt', str(task),
                                            '--out', 'HANDOFF.md', *args, bwrap=bwrap,
                                            socket_walk_entries=socket_walk_entries, proxy_path_log=proxy_path_log,
                                            setup=setup),
                              env=self.env, capture_output=True,
                              text=True, timeout=10)

    def test_worker_refuses_to_start_when_bwrap_selftest_fails(self):
        self.make_executable(self.binary_dir / 'bwrap', 'echo "test namespace failure" >&2\nexit 1\n')
        result = self.worker()
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('test namespace failure', result.stderr)
        self.assertIn('"worker_sandbox": false', result.stderr)
        self.assertFalse((self.product / 'HANDOFF.md').exists())

    def test_zero_exit_without_a_selftest_result_is_a_failure(self):
        self.make_executable(self.binary_dir / 'bwrap', 'exit 0\n')
        result = self.worker()
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('did not report success', result.stderr)

    def test_worker_cannot_replace_its_clone_with_the_shared_workspace(self):
        result = self.worker('--cwd', str(self.product.parents[1]))
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('outside the project clone', result.stderr)

    def test_offline_doctor_reports_the_sandbox_preflight_failure(self):
        self.require_unix_socket()
        self.make_executable(self.binary_dir / 'bwrap', 'echo "test namespace failure" >&2\nexit 1\n')
        self.make_executable(self.binary_dir / 'git', 'echo "test git"\n')
        env = dict(self.env, PATH=str(self.binary_dir))
        result = subprocess.run(self.launcher('doctor.py', '--offline'),
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('FAIL worker sandbox: test namespace failure', result.stdout)

    def fake_bwrap(self):
        argv_log = self.base / 'bwrap-argv.json'
        path = self.binary_dir / 'bwrap'
        path.write_text(
            f'#!{sys.executable}\n'
            'import json, os, stat, sys\n'
            'from pathlib import Path\n'
            'assert not {"KOROLEV_HOME", "FAKE_API_TOKEN"} & os.environ.keys()\n'
            'args = sys.argv[1:]\n'
            'cmd = args[args.index("--") + 1:]\n'
            'socket = None\n'
            'worker_env = None\n'
            'if cmd[3:4] == ["worker-egress"]:\n'
            '    index = args.index("/run/korolev-egress.sock")\n'
            '    assert args[index - 2] in ("--ro-bind", "--ro-bind-fd")\n'
            '    socket = Path(args[index - 1])\n'
            '    if args[index - 2] == "--ro-bind-fd":\n'
            '        socket = Path(os.readlink("/proc/self/fd/" + str(socket)))\n'
            '    assert socket.is_socket()\n'
            '    assert stat.S_IMODE(socket.parent.stat().st_mode) == 0o700\n'
            '    cmd = cmd[9:]\n'
            f'if cmd[:3] == [{str(Path("/usr/bin/python3").resolve())!r}, "-IS", "-c"]:\n'
            '    assert "install_socket_filter()" in cmd[3]\n'
            '    _, count, *cmd = cmd[4:]\n'
            '    count = int(count)\n'
            '    worker_env = dict(zip(cmd[:count], cmd[count:2 * count]))\n'
            '    cmd = cmd[2 * count:]\n'
            'run_mounts = {}\n'
            'for flag, fd, dest in zip(args, args[1:], args[2:]):\n'
            '    if flag in ("--bind-fd", "--ro-bind-fd"):\n'
            '        info = os.fstat(int(fd))\n'
            '        source = Path(os.readlink("/proc/self/fd/" + fd))\n'
            '        if dest != "/run/korolev-egress.sock" and source.name != ".sandbox-empty" and not source.parent.name.startswith("gitless-"):\n'
            '            assert os.path.samestat(info, os.stat(dest))\n'
            '        if Path(dest).parent.name == ".runs":\n'
            '            run_mounts[dest] = [info.st_dev, info.st_ino]\n'
            '        os.close(int(fd))\n'
            f'preflight = Path({str(self.base / "preflight.json")!r})\n'
            'if cmd[4:5] == ["sandbox-selftest"]:\n'
            '    preflight.write_text(json.dumps(run_mounts))\n'
            '    print("sandbox checks passed")\n'
            'else:\n'
            '    assert json.loads(preflight.read_text()) == run_mounts\n'
            f'    Path({str(argv_log)!r}).write_text(json.dumps(args))\n'
            '    env = worker_env or {args[i+1]: args[i+2] for i, arg in enumerate(args) if arg == "--setenv"}\n'
            '    if socket is not None:\n'
            '        env["FIXTURE_PROXY_SOCKET"] = str(socket)\n'
            '    if "--ro-bind-data" in args:\n'
            '        fd = int(args[args.index("--ro-bind-data") + 1])\n'
            '        assert fd >= 10\n'
            '        assert os.read(fd, 64) == b"{}"\n'
            '        os.close(fd)\n'
            '    os.execve(cmd[0], cmd, env)\n'
        )
        path.chmod(0o700)
        return argv_log

    def test_bwrap_worker_moves_deliverable_from_its_outbox(self):
        argv_log = self.fake_bwrap()
        self.env['FAKE_API_TOKEN'] = 'fixture'
        self.make_executable(self.binary_dir / 'codex',
                             'while [ "$1" != -o ]; do shift; done\n'
                             'test ! -e "$2" && echo deliverable > "$2"\n')
        absolute_out = self.product / 'reports' / 'HANDOFF.md'
        proxy_path_log = self.base / 'proxy-path'
        outboxes = set()
        homes = set()
        for role, target in (('analyst', 'HANDOFF.md'), ('builder', str(absolute_out)), ('analyst', 'HANDOFF.md')):
            with self.subTest(role=role):
                if role == 'builder':
                    self.git('init', '-q', '-b', 'main')
                out = self.product / target
                out.parent.mkdir(exist_ok=True)
                out.write_text('previous deliverable')
                result = self.worker('--out', target, role=role, proxy_path_log=proxy_path_log)
                if role == 'builder':
                    shutil.rmtree(self.clone / '.git')
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(argv_log.read_text())
                cmd = argv[argv.index('--') + 1:]
                self.assertEqual(cmd[3], 'worker-egress')
                cmd = cmd[13:]
                self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-s'],
                                 ['danger-full-access'])
                deliverable = Path(cmd[cmd.index('-o') + 1])
                outbox = deliverable.parent
                self.assertEqual(deliverable.name, 'deliverable.md')
                self.assertEqual(outbox.parent, self.product / '.runs')
                self.assertTrue(outbox.name.endswith('-out'))
                self.assertNotIn(outbox, outboxes)
                outboxes.add(outbox)
                self.assertIn(outbox, self.mounts(argv, '--bind-fd').values())
                socket_path = Path(proxy_path_log.read_text())
                self.assertFalse(socket_path.is_relative_to(outbox))
                self.assertFalse(any(socket_path.is_relative_to(source)
                                     for source in (*self.mounts(argv, '--bind', include_pinned=False),
                                                    *self.mounts(argv, '--bind-fd').values())))
                self.assertFalse(socket_path.parent.exists())
                bundle = outbox.with_name(outbox.name.removesuffix('-out') + '-ctx')
                self.assertIn(bundle, self.mounts(argv, '--ro-bind-fd').values())
                self.assertNotIn(str(self.product), argv)
                self.assertEqual(out.read_text(), 'deliverable\n')
                self.assertEqual(deliverable.read_text(), 'deliverable\n')
                record = json.loads(result.stdout)
                self.assertEqual(record['sandbox'], 'bwrap')
                self.assertEqual(record['network'], 'isolated')
                self.assertEqual(record['codex_sandbox'], 'danger-full-access (inside bwrap)')
                log = Path(record['egress_log'])
                self.assertEqual(log.name, outbox.name.removesuffix('-out') + '.egress.jsonl')
                self.assertTrue(log.is_file())
                self.assertFalse((outbox / 'egress.sock').exists())
                env = self.argv_worker_env(argv)
                for var in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY'):
                    self.assertEqual(env[var], 'http://127.0.0.1:18080')
                    self.assertEqual(env[var.lower()], env[var])
                self.assertEqual(env['NO_PROXY'], '')
                self.assertEqual(env['no_proxy'], '')
                home = Path(env['CODEX_HOME'])
                self.assertNotEqual(home, self.isolated)
                self.assertNotIn(home, homes)
                homes.add(home)
                self.assertFalse(home.exists())
                self.assertEqual(record['out'], str(out))
                self.assertEqual(record['out_bytes'], len(b'deliverable\n'))

    def test_worker_requires_a_new_nonempty_regular_deliverable_in_both_modes(self):
        self.fake_bwrap()
        out = self.product / 'HANDOFF.md'
        out.write_text('previous deliverable')
        for sandboxed in (False, True):
            (self.config / 'config.json').write_text(json.dumps(
                {'worker_sandbox': sandboxed, 'worker_network': 'host'}))
            for body in ('exit 0\n', ': > "$2"\n', 'ln -s "$HOME/secret/hidden.txt" "$2"\n',
                         'echo report > "$2"; ln "$2" "$2.link"\n', 'mkfifo "$2"\n'):
                with self.subTest(sandboxed=sandboxed, body=body):
                    self.make_executable(self.binary_dir / 'codex',
                                         'while [ "$1" != -o ]; do shift; done\n' + body)
                    result = self.worker()
                    self.assertEqual(result.returncode, 3, result.stderr)
                    self.assertIn('wrote no deliverable', result.stderr)
                    self.assertEqual(out.read_text(), 'previous deliverable')
                    record = json.loads(result.stdout)
                    self.assertEqual(record['out_bytes'], 0)
                    self.assertEqual(record['exit'], 3)

    def test_worker_refuses_existing_output_symlink_in_both_modes(self):
        out = self.product / 'HANDOFF.md'
        out.symlink_to(self.secret)
        for sandboxed in (False, True):
            with self.subTest(sandboxed=sandboxed):
                (self.config / 'config.json').write_text(json.dumps(
                    {'worker_sandbox': sandboxed, 'worker_network': 'host'}))
                result = self.worker()
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertIn('worker output cannot be a symlink', result.stderr)
                self.assertFalse((self.product / '.runs').exists())
                self.assertTrue(out.is_symlink())
                self.assertEqual(self.secret.read_text(), 'private')

    def swap_runs_fixture(self):
        return f'''
from pathlib import Path
runs = Path({str(self.product / '.runs')!r})
previous = runs.with_name('previous-runs')

def swap_runs():
    runs.rename(previous)
    runs.mkdir()
    for path in previous.iterdir():
        if path.is_dir():
            replacement = runs / path.name
            replacement.mkdir()
            if path.name.endswith('-ctx'):
                (replacement / 'TASK.md').write_text('replacement task')
'''

    def test_bwrap_refuses_runs_swap_before_either_phase(self):
        argv_log = self.fake_bwrap()
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        for swap_before in (True, False):
            with self.subTest(swap_before=swap_before):
                setup = self.swap_runs_fixture() + f'''
selftest = sandbox.selftest
def preflight(**kwargs):
    if {swap_before!r}:
        swap_runs()
    result = selftest(**kwargs)
    swap_runs()
    return result
sandbox.selftest = preflight
'''
                result = self.worker(setup=setup)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn('run directory changed', result.stderr)
                self.assertFalse(argv_log.exists())
                self.assertFalse((self.product / 'HANDOFF.md').exists())
                self.assertEqual((self.base / 'preflight.json').exists(), not swap_before)
                for directory in ('.runs', 'previous-runs'):
                    shutil.rmtree(self.product / directory)

    def test_worker_and_logs_use_pinned_paths_after_runs_swap(self):
        swap_fixture = self.swap_runs_fixture()
        setup = swap_fixture + '''
worker_env = sandbox.worker_env
def swap_before_launch(*args, **kwargs):
    swap_runs()
    return worker_env(*args, **kwargs)
sandbox.worker_env = swap_before_launch
'''
        fake = self.binary_dir / 'codex'
        for sandboxed in (False, True):
            with self.subTest(sandboxed=sandboxed):
                (self.config / 'config.json').write_text(json.dumps({
                    'worker_sandbox': sandboxed, 'worker_network': 'isolated' if sandboxed else 'host'}))
                if sandboxed:
                    self.fake_bwrap()
                fake.write_text(
                    f'#!{sys.executable}\n' + swap_fixture +
                    'import subprocess, sys\n'
                    'prompt = sys.stdin.read()\n'
                    'bundle = next(line.split(": ", 1)[1] for line in prompt.splitlines()\n'
                    '              if line.startswith("Run context bundle: "))\n'
                    'out = sys.argv[sys.argv.index("-o") + 1]\n'
                    'assert Path(bundle, "TASK.md").read_text() == "Test task\\n"\n'
                    'subprocess.run([sys.executable, "-c",\n'
                    '                "import sys; from pathlib import Path; "\n'
                    '                "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text())",\n'
                    '                str(Path(bundle, "TASK.md")), out], check=True, close_fds=True)\n'
                    f'if {sandboxed!r}:\n'
                    '    swap_runs()\n'
                    'for path in previous.glob("*.jsonl"):\n'
                    '    (runs / path.name).write_text("replacement log")\n'
                    'print(\'{"type":"turn.completed","usage":{"input_tokens":7}}\')\n'
                )
                fake.chmod(0o700)
                result = self.worker(setup='' if sandboxed else setup)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                self.assertIsNone(record['events'])
                self.assertIsNone(record['egress_log'])
                self.assertEqual(record['usage'], {'input_tokens': 7})
                self.assertIn('cannot verify run log path:', result.stderr)
                self.assertIn('.events.jsonl', result.stderr)
                self.assertEqual('.egress.jsonl' in result.stderr, sandboxed)
                self.assertEqual(json.loads((self.product / 'USAGE.jsonl').read_text().splitlines()[-1]), record)
                self.assertEqual((self.product / 'HANDOFF.md').read_text(), 'Test task\n')
                runs, previous = self.product / '.runs', self.product / 'previous-runs'
                self.assertFalse(list(runs.glob('*-out/*')))
                original, = previous.glob('*.events.jsonl')
                self.assertIn('"input_tokens":7', original.read_text())
                self.assertEqual((runs / original.name).read_text(), 'replacement log')
                if sandboxed:
                    original_egress, = previous.glob('*.egress.jsonl')
                    self.assertEqual((runs / original_egress.name).read_text(), 'replacement log')
                for directory in (runs, previous):
                    shutil.rmtree(directory)

    def test_worker_network_host_skips_the_bridge_and_doctor_warns(self):
        argv_log = self.fake_bwrap()
        self.make_executable(self.binary_dir / 'codex',
                             'if [ "$1" = --version ]; then exit 0; fi\n'
                             'while [ "$1" != -o ]; do shift; done\n'
                             'echo deliverable > "$2"\n')
        (self.config / 'config.json').write_text('{"worker_network": "host"}')
        (self.binary_dir / 'socat').unlink()
        result = self.worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(argv_log.read_text())
        self.assertNotIn('--unshare-net', argv)
        self.assertNotIn('HTTPS_PROXY', self.argv_env(argv))
        record = json.loads(result.stdout)
        self.assertEqual(record['network'], 'host')
        self.assertIsNone(record['egress_log'])
        self.assertIn('Host abstract sockets and local ports are reachable', record['sandbox_warnings'][0])
        self.make_executable(self.binary_dir / 'git', 'echo "test git"\n')
        result = subprocess.run(self.launcher('doctor.py', '--offline'),
                                env=dict(self.env, PATH=str(self.binary_dir)), capture_output=True, text=True, timeout=10)
        self.assertIn('WARN worker network: worker_network is set to host', result.stdout)
        self.assertIn('Host abstract sockets and local ports are reachable', result.stdout)

    def test_bridge_selects_system_socat_before_a_sibling_job_on_path(self):
        from skill.scripts import sandbox

        sibling = self.base / 'sibling-bin'
        sibling.mkdir()
        marker = self.base / 'sibling-socat-ran'
        self.make_executable(sibling / 'socat', f': > {shlex.quote(str(marker))}\nexit 95\n')
        self.env['PATH'] = f'{sibling}:{self.env["PATH"]}'
        proxy_dir = self.base / 'proxy'
        proxy_dir.mkdir(mode=0o700)
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        self.require_unix_socket()
        proxy_socket = proxy_dir / sandbox.egress_proxy.SOCKET_NAME
        with socket.socket(socket.AF_UNIX) as listener, patch.object(sandbox.egress_proxy, 'PORT', port):
            listener.bind(str(proxy_socket))
            listener.listen()
            argv = self.wrap(['/bin/true'], proxy_socket=proxy_socket)
        command = argv[argv.index('--') + 1:]
        self.assertEqual(Path(command[4]), sandbox.trusted_system_executable('socat', sandbox.SYSTEM_BIN_DIRS))
        flag, fd, destination = argv[argv.index('--chdir') - 3:argv.index('--chdir')]
        self.assertEqual(flag, '--ro-bind-fd')
        self.assertEqual(destination, command[4])
        self.assertTrue(os.path.samestat(os.fstat(int(fd)), Path(destination).stat()))
        result = subprocess.run(command, cwd='/', env=self.argv_env(argv), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_pre_filter_helpers_reject_a_mount_shadowing_their_system_view(self):
        from skill.scripts import sandbox

        for name in ('socat', 'sh', 'sleep', 'python3'):
            helper = sandbox.trusted_system_executable(name)
            alias = self.base / 'system-alias'
            alias.symlink_to(helper.parent)
            mounts = {Path('/usr'): ('--ro-bind', '/usr'), Path('/bin'): ('--ro-bind', '/bin')}
            sandbox.check_helper_mounts(helper, mounts)
            with self.subTest(helper=name), self.assertRaisesRegex(OSError, 'shadows trusted'):
                sandbox.check_helper_mounts(helper, mounts | {alias: ('--ro-bind', str(self.clone))})
            alias.unlink()

    def test_system_executable_requires_verified_backing_file_and_ancestors(self):
        import common

        bins = self.base / 'system-bin'
        bins.mkdir()
        root = Path('/')
        for command in ('git', 'socat', 'bwrap', 'sh', 'sleep', 'python3'):
            file = bins / command
            self.make_executable(file, 'exit 0\n')

            def info(path):
                mode = stat.S_IFREG if path == file else stat.S_IFDIR
                return SimpleNamespace(st_uid=owner, st_mode=mode | (0o775 if path == writable else 0o755))

            system = ('8:1', root, root, True, 'ext4')
            alias = ('8:1', bins, Path('/writable'), False, 'ext4')
            overlay = ('0:9', root, bins, False, 'overlay')
            with (patch.object(Path, 'lstat', info),
                  patch.object(common, 'effective_mount',
                               side_effect=lambda path, mounts: mounts[3] if 3 in mounts and
                               path.is_relative_to(bins) else mounts[1])):
                for owner, uid in ((0, 0), (65534, 0), (1000, 1000)):
                    for exposed, writable in (({2: alias}, None), ({3: overlay}, None), ({}, bins), ({}, file),
                                              ({}, None)):
                        with self.subTest(command=command, owner=owner, exposed=exposed, writable=writable):
                            view = ({1: system, **exposed}, [(uid, 1000, 1)], 65534)
                            if exposed or writable:
                                with self.assertRaisesRegex(OSError, 'no trusted'):
                                    common.trusted_system_executable(command, (bins,), view=view)
                            else:
                                self.assertEqual(common.trusted_system_executable(command, (bins,), view=view), file)

    def test_missing_socat_exits_78_and_names_the_host_escape_hatch(self):
        bwrap_marker = self.base / 'bwrap-ran'
        bridge_marker = self.base / 'untrusted-bridge-ran'
        self.make_executable(self.binary_dir / 'bwrap', f': > {shlex.quote(str(bwrap_marker))}\n')
        self.make_executable(self.binary_dir / 'socat', f': > {shlex.quote(str(bridge_marker))}\n')
        proxy_path_log = self.base / 'proxy-path'
        result = self.worker(proxy_path_log=proxy_path_log,
                             setup="sandbox.SYSTEM_BIN_DIRS = (Path('/missing-system-bin'),)")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('socat is required', result.stderr)
        self.assertIn('"worker_network": "host"', result.stderr)
        self.assertFalse(Path(proxy_path_log.read_text()).parent.exists())
        self.assertFalse(bwrap_marker.exists())
        self.assertFalse(bridge_marker.exists())

    def test_proxy_is_closed_and_logged_when_worker_is_killed(self):
        self.fake_bwrap()
        codex = self.binary_dir / 'codex'
        codex.write_text(
            f'#!{sys.executable}\n'
            'import os, signal, socket, sys\n'
            'with socket.socket(socket.AF_UNIX) as connection:\n'
            '    connection.connect(os.environ["FIXTURE_PROXY_SOCKET"])\n'
            '    connection.sendall(b"CONNECT denied.invalid:443 HTTP/1.1\\r\\n\\r\\n")\n'
            '    assert connection.recv(256).startswith(b"HTTP/1.1 403")\n'
            'os.kill(os.getpid(), signal.SIGKILL)\n'
        )
        codex.chmod(0o700)
        proxy_path_log = self.base / 'proxy-path'
        result = self.worker(proxy_path_log=proxy_path_log)
        self.assertEqual(result.returncode, 256 - signal.SIGKILL, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record['exit'], -signal.SIGKILL)
        audit = json.loads(Path(record['egress_log']).read_text())
        self.assertEqual((audit['host'], audit['allowed'], audit['bytes']), ('denied.invalid', False, 0))
        self.assertFalse(Path(proxy_path_log.read_text()).parent.exists())

    def test_proxy_is_closed_after_worker_timeout(self):
        self.fake_bwrap()
        self.make_executable(self.binary_dir / 'codex', 'exec /bin/sleep 60\n')
        proxy_path_log = self.base / 'proxy-path'
        result = self.worker('--timeout', '-1', proxy_path_log=proxy_path_log)
        self.assertEqual(result.returncode, 124, result.stderr)
        record = json.loads(result.stdout)
        self.assertTrue(record['timed_out'])
        self.assertTrue(Path(record['egress_log']).is_file())
        self.assertFalse(Path(proxy_path_log.read_text()).parent.exists())

    def test_terminating_launcher_stops_worker_and_proxy(self):
        self.require_unix_socket()
        self.fake_bwrap()
        self.make_executable(self.binary_dir / 'codex',
                             'while [ "$1" != -o ]; do shift; done\n'
                             'printf "%s\\n" "$$" > "$2"\nexec /bin/sleep 60\n')
        task = self.product / 'TASK.md'
        task.write_text('Test task\n')
        proxy_path_log = self.base / 'proxy-path'
        with subprocess.Popen(self.launcher('run_codex.py', '--role', 'analyst', '--product', 'demo',
                                             '--prompt-only', '--prompt', str(task), '--out', 'HANDOFF.md',
                                             proxy_path_log=proxy_path_log), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as launcher:
            try:
                deadline = time.monotonic() + 5
                runs = self.product / '.runs'
                while time.monotonic() < deadline:
                    deliverables = list(runs.glob('*-out/deliverable.md'))
                    if deliverables and deliverables[0].stat().st_size:
                        break
                    if launcher.poll() is not None:
                        self.fail(f'launcher exited before starting the worker: {launcher.stderr.read()!r}')
                    time.sleep(0.01)
                else:
                    self.fail('the worker did not start')
                pid = int(deliverables[0].read_text())
                proxy_socket = Path(proxy_path_log.read_text())
                self.assertTrue(proxy_socket.is_socket())
                launcher.terminate()
                _, stderr = launcher.communicate(timeout=5)
                self.assertEqual(launcher.returncode, 128 + signal.SIGTERM, stderr)
                self.assertFalse(proxy_socket.exists())
                self.assertFalse(Path(f'/proc/{pid}').exists())
            finally:
                if launcher.poll() is None:
                    launcher.terminate()

    def test_registered_read_worker_uses_isolated_egress(self):
        layout = self.task_layout()
        argv_log = self.fake_bwrap()
        self.make_executable(self.binary_dir / 'codex',
                             'while [ "$1" != -o ]; do shift; done\n'
                             'echo deliverable > "$2"\n')
        result = self.worker('--task', '19-task')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record['network'], 'isolated')
        self.assertTrue(Path(record['egress_log']).is_file())
        argv = json.loads(argv_log.read_text())
        self.assertIn('--unshare-net', argv)
        self.projected_git(argv, layout['git_common_dir'], include_pinned=False)

    def test_owner_can_disable_worker_sandbox(self):
        self.make_executable(self.binary_dir / 'bwrap', 'exit 1\n')
        self.make_executable(self.binary_dir / 'codex',
                             'test "$HTTPS_PROXY" = https://host.invalid && test "$https_proxy" = https://lower.invalid '
                             '&& test "$no_proxy" = localhost || exit 1\n'
                             'while [ "$1" != -s ]; do shift; done\nmode=$2\n'
                             'while [ "$1" != -o ]; do shift; done\nprintf "%s\\n" "$mode" "$2" > "$2"\n')
        self.make_executable(self.binary_dir / 'git', 'exit 1\n')
        (self.config / 'config.json').write_text('{"worker_sandbox": false, "worker_network": "host"}')
        self.env.update(HTTPS_PROXY='https://host.invalid', https_proxy='https://lower.invalid', no_proxy='localhost')
        out = self.product / 'HANDOFF.md'
        for role, mode in (('analyst', 'read-only'), ('builder', 'workspace-write')):
            with self.subTest(role=role):
                result = self.worker(role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                self.assertEqual(record['sandbox'], 'off')
                self.assertEqual(record['codex_sandbox'], mode)
                reported_mode, worker_output = out.read_text().splitlines()
                self.assertEqual(reported_mode, mode)
                self.assertRegex(worker_output, r'^/proc/\d+/fd/\d+/deliverable\.md$')
                self.assertEqual(record['out'], str(out))

    def test_disabling_worker_sandbox_requires_explicit_host_network(self):
        for setting in ({}, {'worker_network': 'isolated'}):
            with self.subTest(setting=setting):
                (self.config / 'config.json').write_text(json.dumps({'worker_sandbox': False, **setting}))
                result = self.worker()
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn('requires explicit worker_network: "host"', result.stderr)
                self.assertFalse((self.product / 'HANDOFF.md').exists())
