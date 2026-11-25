import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill' / 'scripts'


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.base / 'user'
        self.product = self.base / 'korolev' / 'products' / 'demo'
        self.clone = self.base / 'korolev' / 'work' / 'demo' / 'repo'
        self.isolated = self.home / '.local' / 'share' / 'korolev' / 'codex-home'
        self.auth = self.home / '.codex' / 'auth.json'
        self.binary_dir = self.home / '.codex' / 'packages' / 'release' / 'bin'
        self.config = self.home / '.config' / 'korolev'
        self.secret = self.home / 'secret' / 'hidden.txt'
        self.other_home = self.base / 'other-home'
        for directory in (self.product, self.clone, self.isolated, self.auth.parent,
                          self.binary_dir, self.config, self.secret.parent, self.other_home):
            directory.mkdir(parents=True, exist_ok=True)
        self.auth.write_text('{}')
        (self.isolated / 'auth.json').symlink_to(self.auth)
        self.secret.write_text('private')
        (self.clone / 'visible.txt').write_text('clone')
        (self.clone / '.git').mkdir()
        self.make_executable(self.binary_dir / 'codex', 'exit 0\n')
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=os.pathsep.join([str(self.binary_dir), os.defpath]),
                        KOROLEV_HOME=str(self.product.parents[1]), KOROLEV_CONFIG_DIR=str(self.config),
                        KOROLEV_CODEX_HOME=str(self.isolated), CODEX_HOME=str(self.auth.parent))

    def make_executable(self, path, body):
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)

    def wrap(self, cmd, *, write=False):
        from skill.scripts import sandbox

        with patch.dict(os.environ, self.env, clear=True):
            return sandbox.wrap(cmd, clone=self.clone, product_dir=self.product,
                                codex_home=self.isolated, write=write)

    def mounts(self, argv, option):
        return {Path(source): Path(dest) for arg, source, dest in zip(argv, argv[1:], argv[2:])
                if arg == option}

    def test_argv_uses_private_filesystem_and_real_codex_binary(self):
        argv = self.wrap(['codex', 'exec'])
        self.assertEqual(argv[-3:], ['--', str(self.binary_dir / 'codex'), 'exec'])
        self.assertIn('--unshare-pid', argv)
        self.assertIn('--die-with-parent', argv)
        self.assertNotIn('--unshare-net', argv)
        for option, dest in (('--proc', '/proc'), ('--dev', '/dev'), ('--tmpfs', '/tmp'),
                             ('--tmpfs', str(self.home))):
            self.assertIn((option, dest), list(zip(argv, argv[1:])))
        self.assertEqual(self.mounts(argv, '--ro-bind')[self.binary_dir], self.binary_dir)

    def test_only_auth_file_is_bound_from_the_user_codex_home(self):
        argv = self.wrap(['codex', 'exec'])
        ro = self.mounts(argv, '--ro-bind')
        rw = self.mounts(argv, '--bind')
        self.assertEqual(ro[self.auth], self.auth)
        self.assertEqual(rw[self.isolated], self.isolated)
        for hidden in (self.secret, self.other_home, self.config / 'telegram.env',
                       self.home / '.ssh', self.clone.parents[1] / 'other', Path('/var')):
            self.assertFalse(any(hidden.is_relative_to(path) for path in (*ro, *rw)), hidden)

    def test_clone_mount_respects_write_mode(self):
        read_only = self.wrap(['codex'], write=False)
        writable = self.wrap(['codex'], write=True)
        self.assertEqual(self.mounts(read_only, '--ro-bind')[self.clone], self.clone)
        self.assertEqual(self.mounts(writable, '--bind')[self.clone], self.clone)
        self.assertNotIn(self.clone, self.mounts(writable, '--ro-bind'))
        self.assertEqual(self.mounts(writable, '--ro-bind')[self.clone / '.git'], self.clone / '.git')
        for argv in (read_only, writable):
            self.assertEqual(self.mounts(argv, '--ro-bind').get(self.product), self.product)
            self.assertNotIn(self.product, self.mounts(argv, '--bind'))

    def test_path_only_exposes_toolchain_directories_outside_home(self):
        toolchain = self.base / 'toolchain' / 'bin'
        private_bin = self.home / 'bin'
        for directory in (toolchain, private_bin):
            directory.mkdir(parents=True)
            self.make_executable(directory / 'python3', 'exit 0\n')
        self.env['PATH'] += os.pathsep + os.pathsep.join(map(str, (toolchain, private_bin, self.secret.parent)))
        mounts = self.mounts(self.wrap(['codex']), '--ro-bind')
        self.assertEqual(mounts[toolchain], toolchain)
        self.assertNotIn(private_bin, mounts)
        self.assertNotIn(self.secret.parent, mounts)

    def test_real_bwrap_hides_secret_and_exposes_clone(self):
        from skill.scripts import sandbox

        if sandbox.bwrap_path() is None:
            reason = 'bwrap is missing'
            print(f'SKIP real bubblewrap test: {reason}', file=sys.stderr)
            self.skipTest(reason)
        argv = self.wrap(['/bin/sh', '-eu', '-c',
                          'test ! -e "$1"; test "$(cat "$2")" = clone; test -r "$3"',
                          'probe', str(self.secret), str(self.clone / 'visible.txt'),
                          str(self.isolated / 'auth.json')])
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def worker(self, *args, role='analyst'):
        task = self.product / 'TASK.md'
        task.write_text('Test task\n')
        return subprocess.run([sys.executable, '-I', str(SCRIPTS / 'run_codex.py'),
                               '--role', role, '--product', 'demo', '--prompt', str(task),
                               '--out', 'HANDOFF.md', *args], env=self.env, capture_output=True,
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
        self.make_executable(self.binary_dir / 'bwrap', 'echo "test namespace failure" >&2\nexit 1\n')
        self.make_executable(self.binary_dir / 'git', 'echo "test git"\n')
        env = dict(self.env, PATH=str(self.binary_dir))
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'doctor.py'), '--offline'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('FAIL worker sandbox: test namespace failure', result.stdout)

    def fake_bwrap(self):
        argv_log = self.base / 'bwrap-argv.json'
        path = self.binary_dir / 'bwrap'
        path.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'cmd = args[args.index("--") + 1:]\n'
            'if cmd[0] == "/bin/sh":\n'
            '    print("sandbox checks passed")\n'
            'else:\n'
            f'    Path({str(argv_log)!r}).write_text(json.dumps(args))\n'
            '    os.execv(cmd[0], cmd)\n'
        )
        path.chmod(0o700)
        return argv_log

    def test_bwrap_worker_moves_deliverable_from_its_outbox(self):
        argv_log = self.fake_bwrap()
        self.make_executable(self.binary_dir / 'codex',
                             'while [ "$1" != -o ]; do shift; done\n'
                             'test ! -e "$2" && echo deliverable > "$2"\n')
        self.make_executable(self.binary_dir / 'git', 'exit 1\n')
        absolute_out = self.base / 'reports' / 'HANDOFF.md'
        outboxes = set()
        for role, target in (('analyst', 'HANDOFF.md'), ('builder', str(absolute_out)), ('analyst', 'HANDOFF.md')):
            with self.subTest(role=role):
                out = self.product / target
                out.parent.mkdir(exist_ok=True)
                out.write_text('previous deliverable')
                result = self.worker('--out', target, role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(argv_log.read_text())
                cmd = argv[argv.index('--') + 1:]
                self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-s'],
                                 ['danger-full-access'])
                deliverable = Path(cmd[cmd.index('-o') + 1])
                outbox = deliverable.parent
                self.assertEqual(deliverable.name, 'deliverable.md')
                self.assertEqual(outbox.parent, self.product / '.runs')
                self.assertTrue(outbox.name.endswith('-out'))
                self.assertNotIn(outbox, outboxes)
                outboxes.add(outbox)
                self.assertEqual(self.mounts(argv, '--bind').get(outbox), outbox)
                self.assertEqual(out.read_text(), 'deliverable\n')
                self.assertFalse(deliverable.exists())
                record = json.loads(result.stdout)
                self.assertEqual(record['sandbox'], 'bwrap')
                self.assertEqual(record['codex_sandbox'], 'danger-full-access (inside bwrap)')
                self.assertEqual(record['out'], str(out))
                self.assertEqual(record['out_bytes'], len(b'deliverable\n'))

    def test_bwrap_worker_requires_a_new_nonempty_regular_deliverable(self):
        self.fake_bwrap()
        out = self.product / 'HANDOFF.md'
        out.write_text('previous deliverable')
        for body in ('exit 0\n', ': > "$2"\n', 'ln -s "$HOME/secret/hidden.txt" "$2"\n'):
            with self.subTest(body=body):
                self.make_executable(self.binary_dir / 'codex',
                                     'while [ "$1" != -o ]; do shift; done\n' + body)
                result = self.worker()
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn('wrote no deliverable', result.stderr)
                self.assertEqual(out.read_text(), 'previous deliverable')
                self.assertEqual(json.loads(result.stdout)['out_bytes'], 0)

    def test_owner_can_disable_worker_sandbox(self):
        self.make_executable(self.binary_dir / 'bwrap', 'exit 1\n')
        self.make_executable(self.binary_dir / 'codex',
                             'while [ "$1" != -s ]; do shift; done\nmode=$2\n'
                             'while [ "$1" != -o ]; do shift; done\nprintf "%s\\n" "$mode" "$2" > "$2"\n')
        self.make_executable(self.binary_dir / 'git', 'exit 1\n')
        (self.config / 'config.json').write_text('{"worker_sandbox": false}')
        out = self.product / 'HANDOFF.md'
        for role, mode in (('analyst', 'read-only'), ('builder', 'workspace-write')):
            with self.subTest(role=role):
                result = self.worker(role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                self.assertEqual(record['sandbox'], 'off')
                self.assertEqual(record['codex_sandbox'], mode)
                self.assertEqual(out.read_text().splitlines(), [mode, str(out)])
