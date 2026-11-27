"""Real-sandbox proof of the checklist: a worker really tries each forbidden thing.

Every test needs real bubblewrap (skipped unless KOROLEV_REQUIRE_HOST_SANDBOX=1 or bwrap works,
failing when that variable is set and bubblewrap cannot start). Fixtures come from test_sandbox.
"""
import hashlib
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

if __package__:
    from . import test_sandbox as ts
else:
    import test_sandbox as ts

SH = '''
no() { if ("$@") >/dev/null 2>&1; then echo "UNEXPECTED SUCCESS: $*" >&2; exit 1; fi; }
'''


def tree(*roots):
    """Path -> content hash (or link target) for everything under the roots."""
    seen = {}
    for root in roots:
        for path in [Path(root), *sorted(Path(root).rglob('*'))]:
            if path.is_symlink():
                seen[str(path)] = os.readlink(path)
            elif path.is_file():
                seen[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                seen[str(path)] = 'dir'
    return seen


class ChecklistTests(unittest.TestCase):
    # Reuse the sandbox fixture (fixture home/product/clone, git env, wrap, real_bwrap) without its tests.
    setUp = ts.SandboxTests.setUp
    make_executable = ts.SandboxTests.make_executable
    wrap = ts.SandboxTests.wrap
    task_layout = ts.SandboxTests.task_layout
    real_bwrap = ts.SandboxTests.real_bwrap
    skip_real_bwrap = ts.SandboxTests.skip_real_bwrap
    checkout_bind_fds = ts.SandboxTests.checkout_bind_fds

    def run_worker(self, script, *args, write=True, timeout=30):
        bwrap = self.real_bwrap()
        self.layout = self.task_layout()
        argv = self.wrap(['/bin/sh', '-eu', '-c', SH + script, 'probe', *map(str, args)],
                         bwrap_dir=bwrap.parent, write=write, **self.layout)
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout,
                              pass_fds=self.checkout_bind_fds(argv))

    def temp_dir(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(prefix='korolev-checklist-')))

    def test_item1_worker_cannot_read_or_list_secrets(self):
        other = self.product.parent / 'other-project'
        planted = [self.home / '.config' / 'korolev' / 'telegram.env', self.home / '.ssh' / 'id_test',
                   self.home / '.config' / 'gh' / 'hosts.yml', other / 'secret.txt']
        for path in planted:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('FAKE-SECRET')
        result = self.run_worker('''
            for f in "$@"; do
                test ! -e "$f"; no cat "$f"; no test -r "$f"
                if ls -A "$(dirname "$f")" 2>/dev/null | grep -qxF "$(basename "$f")"; then echo "LISTED: $f" >&2; exit 1; fi
            done
        ''', *planted)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_item3_worker_cannot_write_outside_the_task_folder(self):
        outside = self.temp_dir()
        bwrap = self.real_bwrap()
        layout = self.task_layout()
        targets = [self.home / 'new-file', self.product / 'new-file', layout['owner_folder'] / 'new-file']
        argv = self.wrap(['/bin/sh', '-eu', '-c', SH + '''
            outbox=$1; outside=$2; shift 2
            # HOME, product and owner folders are private tmpfs mounts: a write there may succeed inside the
            # sandbox but must vanish; the host-side comparison below is the proof. A host temp folder is absent.
            for f in "$@"; do sh -c 'echo x > "$1"' sh "$f" || true; done
            no sh -c 'echo x > "$1"' sh "$outside/new-file"
            echo ok > "$outbox/control"
        ''', 'probe', str(layout['outbox']), str(outside), *map(str, targets[:3])], bwrap_dir=bwrap.parent, write=True, **layout)
        before = tree(self.home, self.product, layout['owner_folder'], outside)  # after wrap: it adds its mask file
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=30,
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((layout['outbox'] / 'control').read_text(), 'ok\n')
        after = tree(self.home, self.product, layout['owner_folder'], outside)
        # Only the outbox control file may be new on the host side.
        self.assertEqual(set(after) - set(before), {str(layout['outbox'] / 'control')})
        self.assertEqual({k: v for k, v in before.items() if k in after and after[k] != v}, {})

    def git_state(self):
        return tree(self.layout['git_common_dir'])

    def test_item4_worker_cannot_change_git_history(self):
        bwrap = self.real_bwrap()
        self.layout = self.task_layout()
        before = self.git_state()
        argv = self.wrap(['/bin/sh', '-eu', '-c', SH + '''
            common=$(git rev-parse --path-format=absolute --git-common-dir)
            gitdir=$(git rev-parse --absolute-git-dir)
            test -n "$common" && test -n "$gitdir" && test -d "$common/objects" && test -d "$gitdir"
            echo new > new.txt
            no git update-ref refs/heads/forged HEAD
            no git update-ref HEAD HEAD
            no sh -c 'echo x > "$1"' sh "$common/objects/forged"
            no sh -c 'echo x > "$1"' sh "$gitdir/index"
            no git add new.txt
            no git -c user.name=T -c user.email=t@example.test commit --allow-empty -m forged
        ''', 'probe'], bwrap_dir=bwrap.parent, write=True, **self.layout)
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=30,
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git_state(), before)

    def test_item5_worker_cannot_push(self):
        bare = self.temp_dir() / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], env=self.env, check=True,
                       capture_output=True, timeout=10)
        before = tree(bare)
        # A host listener: if network isolation were missing, the push would connect to it.
        listener = self.enterContext(socket.socket())
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        result = self.run_worker('''
            command -v timeout >/dev/null
            git rev-parse --verify HEAD >/dev/null
            no timeout 10 git push "https://127.0.0.1:$2/repo.git" HEAD:refs/heads/pushed
            no timeout 10 git push "file://$1" HEAD:refs/heads/pushed
            no timeout 10 git push "$1" HEAD:refs/heads/pushed
        ''', bare, listener.getsockname()[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        listener.setblocking(False)
        with self.assertRaises(BlockingIOError, msg='the sandbox reached the host listener'):
            listener.accept()
        self.assertEqual(tree(bare), before)

    def test_item6_no_host_processes_or_app_sockets(self):
        result = self.run_worker('''
            no kill -0 "$1"
            ls /run > /dev/null 2>&1 || true
            if ls /run 2>/dev/null | grep -Ex 'docker.sock|dbus'; then exit 1; fi
            for p in /run/user/*/bus /var/run/docker.sock /run/docker.sock /run/dbus/system_bus_socket; do
                test ! -e "$p"
            done
        ''', os.getpid())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_item6_background_process_does_not_outlive_the_sandbox(self):
        bwrap = self.real_bwrap()
        self.layout = self.task_layout()
        log = self.layout['clone'] / 'stray.log'
        argv = self.wrap(['/bin/sh', '-eu', '-c', '''
            setsid sh -c 'for n in $(seq 100); do echo tick >> "$1"; sleep 0.2; done' sh "$1" >/dev/null 2>&1 </dev/null &
            for i in $(seq 40); do test -s "$1" && exit 0; sleep 0.05; done
            exit 1
        ''', 'probe', str(log)], bwrap_dir=bwrap.parent, write=True, **self.layout)
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=30,
                                pass_fds=self.checkout_bind_fds(argv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(log.stat().st_size, 0)
        time.sleep(3)
        size = log.stat().st_size
        time.sleep(1)
        self.assertEqual(log.stat().st_size, size, 'the background writer survived the sandbox')


if __name__ == '__main__':
    unittest.main()
