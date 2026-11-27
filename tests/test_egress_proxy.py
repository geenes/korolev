import ctypes
import errno
import ipaddress
import io
import json
import os
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest.mock import Mock, patch

from skill.scripts import egress_proxy

REQUEST = b'CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n'
OK = b'HTTP/1.1 200 Connection Established\r\n\r\n'


def client_hello(host='api.openai.com'):
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_bio(incoming, outgoing, server_hostname=host)
    with suppress(ssl.SSLWantReadError):
        client.do_handshake()
    return outgoing.read()


def tls_vector(data, size=2):
    return len(data).to_bytes(size, 'big') + data


def hello_message(*extensions):
    sni = tls_vector(b'\x00' + tls_vector(b'api.openai.com'))
    extensions = b''.join(kind.to_bytes(2, 'big') + tls_vector(data)
                          for kind, data in [(0, sni), *extensions])
    body = b'\x03\x03' + bytes(32) + b'\x00\x00\x02\x13\x01\x01\x00' + tls_vector(extensions)
    return b'\x01' + tls_vector(body, 3)


def tls_record(payload):
    return b'\x16\x03\x03' + tls_vector(payload)


def addrinfo(*values):
    return [(socket.AF_INET6 if ':' in value else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, '', (value, 443, 0, 0) if ':' in value else (value, 443))
            for value in values]


def resolver_script(fixture):
    return egress_proxy.RESOLVER_SCRIPT.replace('\ntry:\n', f'\n{fixture}\ntry:\n', 1)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not (result := predicate()):
        if time.monotonic() >= deadline:
            raise AssertionError('fixture did not become ready')
        time.sleep(0.01)
    return result


class ClientHelloTests(unittest.TestCase):
    def parse(self, wire, chunk_size=65536):
        data = io.BytesIO(wire)
        client = Mock()
        client.recv.side_effect = lambda size: data.read(min(size, chunk_size))
        return egress_proxy.read_client_hello(client, b'', time.monotonic() + 2)

    def test_encrypted_name_extensions_are_denied(self):
        for kind in (0xfe0d, 0xffce):
            with self.subTest(kind=hex(kind)), self.assertRaisesRegex(egress_proxy.Denied, 'encrypted ClientHello'):
                self.parse(tls_record(hello_message((kind, b'\x00'))))

    def test_nonzero_padding_is_denied(self):
        with self.assertRaisesRegex(egress_proxy.Denied, 'invalid TLS padding'):
            self.parse(tls_record(hello_message((0x0015, b'\x00\x00\x01\x00'))))

    def test_full_16_kib_record_is_accepted(self):
        padding_size = egress_proxy.HELLO_LIMIT - len(hello_message((0x0015, b'')))
        message = hello_message((0x0015, bytes(padding_size)))
        self.assertEqual(len(message), 16384)
        wire = tls_record(message)
        self.assertEqual(self.parse(wire), ('api.openai.com', wire))

    def test_trailing_bytes_in_final_record_are_denied(self):
        message = hello_message()
        for wire in (tls_record(message + b'\x00'), tls_record(message[:2]) + tls_record(message[2:] + b'\x00')):
            with self.subTest(wire=wire), self.assertRaisesRegex(egress_proxy.Denied, 'trailing bytes'):
                self.parse(wire)

    def test_split_records_with_one_byte_reads(self):
        message = hello_message()
        wire = tls_record(message[:2]) + tls_record(message[2:])
        self.assertEqual(self.parse(wire, chunk_size=1), ('api.openai.com', wire))

    def test_handshake_length_and_record_count_are_bounded(self):
        advertised_size = (egress_proxy.HELLO_LIMIT - 3).to_bytes(3, 'big')
        with self.assertRaisesRegex(egress_proxy.Denied, 'TLS ClientHello exceeds 16 KiB'):
            self.parse(tls_record(b'\x01' + advertised_size))
        message = hello_message()
        split = egress_proxy.HELLO_RECORD_LIMIT
        wire = b''.join(tls_record(bytes([byte])) for byte in message[:split]) + tls_record(message[split:])
        with self.assertRaisesRegex(egress_proxy.Denied, 'too many records'):
            self.parse(wire)

    def test_wire_limit_is_enforced_across_two_valid_record_headers(self):
        header = b'\x01' + (egress_proxy.HELLO_LIMIT - 4).to_bytes(3, 'big')
        wire = tls_record(header + bytes(96)) + tls_record(bytes(egress_proxy.TLS_RECORD_LIMIT))
        with self.assertRaisesRegex(egress_proxy.Denied, 'exceeds wire limit'):
            self.parse(wire)


class ResolverTests(unittest.TestCase):
    def resolve(self, host):
        return egress_proxy.resolve_host(host, time.monotonic() + 4, threading.BoundedSemaphore(1))

    def test_real_resolver_script_uses_only_numeric_loopback(self):
        answers = self.resolve('127.0.0.1')
        self.assertEqual(answers, [[socket.AF_INET, ['127.0.0.1', 443]]])

    def test_resolver_omits_unused_answer_fields(self):
        script = resolver_script('socket.getaddrinfo = lambda *a, **kw: [(2, 1, 6, "x" * 32768, ("127.0.0.1", 443))]')
        with patch.object(egress_proxy, 'RESOLVER_SCRIPT', script):
            self.assertEqual(self.resolve('127.0.0.1'), [[socket.AF_INET, ['127.0.0.1', 443]]])

    def test_resolver_applies_memory_limit_before_lookup_and_denies_allocation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'limit'
            script = resolver_script(f'''
from pathlib import Path

def allocate(*args, **kwargs):
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    Path({str(ready)!r}).write_text(f'{{soft}} {{hard}}')
    bytearray(64 * 1024 ** 2)
    return [(2, 1, 6, '', ('127.0.0.1', 443))]

socket.getaddrinfo = allocate
''')
            with patch.object(egress_proxy, 'RESOLVER_SCRIPT', script):
                self.assertEqual(self.resolve('127.0.0.1'), ())
            soft, hard = map(int, ready.read_text().split())
            self.assertEqual(soft, hard)
            self.assertGreater(soft, 0)
            self.assertLessEqual(soft, 64 * 1024 ** 2)

    def test_resolver_rejects_whole_result_on_answer_or_pipe_overflow(self):
        for fixture, reason in (
                ('[(2, 1, 6, "", ("93.184.215.14", 443))] * 129', 'answer count exceeds limit'),
                ('[(2, 1, 6, "", ("x" * 32768, 443))]', 'result exceeds IPC limit')):
            script = resolver_script(f'socket.getaddrinfo = lambda *args, **kwargs: {fixture}')
            with (self.subTest(reason=reason), patch.object(egress_proxy, 'RESOLVER_SCRIPT', script),
                  self.assertRaisesRegex(egress_proxy.Denied, reason)):
                self.resolve('127.0.0.1')

    def test_resolver_exits_on_its_own_deadline_after_launcher_is_killed(self):
        # Adopt the orphan in this test so we can check its exit status and reap it.
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)  # PR_GET_CHILD_SUBREAPER
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)  # PR_SET_CHILD_SUBREAPER
        self.addCleanup(libc.prctl, 36, previous.value, 0, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'resolver.pid'
            fixture = f'''
import os
from pathlib import Path

def stalled(*args, **kwargs):
    Path({str(pid_file)!r}).write_text(str(os.getpid()))
    signal.pause()

socket.getaddrinfo = stalled
'''
            script = resolver_script(fixture)
            launcher = f'''
import subprocess, sys, time
subprocess.run([sys.executable, '-I', '-S', '-c', {script!r}, '127.0.0.1',
                str(time.monotonic() + 3), '128'], stdout=subprocess.DEVNULL, check=True)
'''
            with subprocess.Popen([sys.executable, '-B', '-c', launcher], stderr=subprocess.DEVNULL) as process:
                child_pid = None
                try:
                    child_pid = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
                    process.kill()
                    self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)

                    def child_status():
                        pid, status = os.waitpid(child_pid, os.WNOHANG)
                        return (status,) if pid else None

                    status, = wait_for(child_status)
                    child_pid = None
                    self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGALRM)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    if child_pid is not None:
                        with suppress(ProcessLookupError):
                            os.kill(child_pid, signal.SIGKILL)
                        os.waitpid(child_pid, 0)


class LocalInterfacesTests(unittest.TestCase):
    def test_mismatched_zero_and_mapped_zero_masks_do_not_add_subnets(self):
        libc = Mock()
        allocations = []

        def sockaddr(value):
            address = ipaddress.ip_address(value)
            family = socket.AF_INET if address.version == 4 else socket.AF_INET6
            # A nonzero IPv6 flow field would look like mask bits at the IPv4 address offset.
            fields = b'\x00\x00' if address.version == 4 else b'\x00\x00\x00\x0f\xff\xff'
            raw = family.to_bytes(2, sys.byteorder) + fields + address.packed
            memory = ctypes.create_string_buffer(raw.ljust(28, b'\0'))
            allocations.append(memory)
            return ctypes.addressof(memory)

        def getifaddrs(target):
            pointer_type, = libc.getifaddrs.argtypes
            first = ctypes.cast(target, pointer_type)
            node_type = pointer_type._type_._type_
            for address, netmask in (('93.184.215.14', '0.0.0.0'),
                                    ('1.1.1.1', '2001:4860::'),
                                    ('2001:4860::1', '::'),
                                    ('::ffff:1.1.1.2', 'ffff:ffff:ffff:ffff:ffff:ffff::'),
                                    ('8.8.8.8', '255.255.255.0'),
                                    ('2606:4700::1', 'ffff:ffff:ffff:ffff::')):
                node = node_type(first[0], None, 0, sockaddr(address), sockaddr(netmask))
                allocations.append(node)
                first[0] = ctypes.pointer(node)
            return 0

        libc.getifaddrs.side_effect = getifaddrs
        with patch.object(egress_proxy.ctypes, 'CDLL', return_value=libc):
            addresses, subnets = egress_proxy.local_interfaces()
        self.assertEqual(addresses, set(map(ipaddress.ip_address, ('93.184.215.14', '1.1.1.1', '2001:4860::1',
                                                                   '1.1.1.2', '8.8.8.8', '2606:4700::1'))))
        self.assertEqual(subnets, set(map(ipaddress.ip_network, ('::ffff:0:0/96', '8.8.8.0/24', '2606:4700::/64'))))
        self.assertTrue(egress_proxy.public_address('1.1.1.3', (addresses, subnets)))
        libc.freeifaddrs.assert_called_once()


class EgressProxyTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.outbox = self.base / 'outbox'
        self.outbox.mkdir(mode=0o700)
        self.log = self.base / 'worker.egress.jsonl'
        self.resolved = []
        self.dialed = []
        self.answers = {host: addrinfo('93.184.215.14') for host in ('ab.chatgpt.com.', 'api.openai.com.')}
        self.interfaces = self.enterContext(patch.object(egress_proxy, 'local_interfaces', return_value=(set(), set())))
        destinations = egress_proxy.Proxy.destinations

        def observe_resolution(server, host, deadline):
            self.resolved.append(host + '.')
            return destinations(server, host, deadline)

        self.enterContext(patch.object(egress_proxy.Proxy, 'destinations', observe_resolution))

    def connect(self, family, address, _timeout):
        self.dialed.append((family, address))
        upstream, fake = socket.socketpair()

        def echo():
            with fake, suppress(OSError):
                while data := fake.recv(4096):
                    fake.sendall(data.upper())

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        return upstream

    def serve(self, patterns=egress_proxy.DEFAULT_ALLOW, **kwargs):
        settings = {'resolve': self.answers, 'connect': self.connect, **kwargs}
        return egress_proxy.serve(self.outbox, self.log, patterns, **settings)

    @contextmanager
    def client(self, path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            yield client

    def receive(self, client):
        chunks = []
        with suppress(ConnectionResetError):
            while data := client.recv(4096):
                chunks.append(data)
        return b''.join(chunks)

    def request(self, path, message):
        with self.client(path) as client:
            client.sendall(message)
            client.shutdown(socket.SHUT_WR)
            return self.receive(client)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_allowed_connect_relays_bytes_and_only_logs_metadata(self):
        hello = client_hello('AB.ChatGPT.Com')
        message = (b'CONNECT Ab.ChatGPT.Com:443 HTTP/1.1\r\nHost: ab.chatgpt.com:443\r\n'
                   b'Proxy-Authorization: private-header\r\n\r\n' + hello)
        with self.serve() as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(self.request(path, message), OK + hello.upper())
        self.assertEqual(self.resolved, ['ab.chatgpt.com.'])
        self.assertEqual(self.dialed, [(socket.AF_INET, ('93.184.215.14', 443))])
        row, = self.records()
        self.assertEqual((row['host'], row['sni'], row['allowed'], row['bytes']),
                         ('ab.chatgpt.com', 'ab.chatgpt.com', True, 2 * len(hello)))
        self.assertNotIn('private', self.log.read_text().lower())

    def test_request_line_and_host_header_are_checked_before_dns(self):
        lines = [
            b'CONNECT child.chatgpt.com:443 HTTP/1.1',
            b'CONNECT chatgpt.com.example.test:443 HTTP/1.1',
            b'CONNECT api.openai.com:444 HTTP/1.1',
            b'CONNECT api.openai.com:0443 HTTP/1.1',
            b'CONNECT api.openai.com:443 HTTP/1.0',
            b'CONNECT  api.openai.com:443 HTTP/1.1',
            b'connect api.openai.com:443 HTTP/1.1',
            b'CONNECT api.openai.com:443 HTTP/1.1\r\nHost: outside.invalid:443',
            b'CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com',
            b'CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\nHost: api.openai.com:443',
        ]
        with self.serve() as path:
            for line in lines:
                with self.subTest(line=line):
                    self.assertTrue(self.request(path, line + b'\r\n\r\n').startswith(b'HTTP/1.1 403'))
            self.assertTrue(self.request(path, b'GET / HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 405'))
        self.assertFalse(self.resolved)
        self.assertFalse(self.dialed)
        self.assertEqual(sum(row['count'] for row in self.records()), len(lines) + 1)
        self.assertTrue(all(not row['allowed'] and row['bytes'] == 0 for row in self.records()))

    def test_header_size_truncation_and_total_deadline_are_audited(self):
        prefix = REQUEST[:-2] + b'X-Fill: '
        with patch.object(egress_proxy, 'HEADER_TIMEOUT', 0.15), self.serve() as path:
            self.assertTrue(self.request(path, prefix + b'x' * 8192).startswith(b'HTTP/1.1 431'))
            self.assertTrue(self.request(path, prefix).startswith(b'HTTP/1.1 400'))
            with self.client(path) as client:
                client.sendall(prefix)
                self.assertTrue(self.receive(client).startswith(b'HTTP/1.1 408'))
        self.assertEqual([row['host'] for row in self.records()], ['api.openai.com'] * 3)
        self.assertFalse(self.dialed)

    def test_sni_mismatch_absent_and_non_tls_are_denied_without_dialing(self):
        payloads = [
            (client_hello('different.invalid'), 'TLS SNI does not match CONNECT host'),
            (client_hello(None), 'TLS ClientHello is missing SNI'),
            (b'GET / HTTP/1.1\r\n\r\n', 'expected a TLS ClientHello record'),
        ]
        with self.serve() as path:
            for payload, _ in payloads:
                self.assertEqual(self.request(path, REQUEST + payload), OK)
        self.assertFalse(self.dialed)
        self.assertEqual([row['reason'] for row in self.records()], [reason for _, reason in payloads])

    def test_ech_and_trailing_bytes_are_rejected_end_to_end(self):
        with self.serve() as path:
            for message in (hello_message((0xfe0d, b'\x00')), hello_message() + b'\x00'):
                self.assertEqual(self.request(path, REQUEST + tls_record(message)), OK)
        self.assertFalse(self.dialed)
        self.assertEqual([row['reason'] for row in self.records()],
                         ['encrypted ClientHello is unsupported', 'trailing bytes in TLS ClientHello record'])

    def test_split_client_hello_across_reads_and_tls_records(self):
        original = client_hello()
        body = original[5:]
        split = (original[:3] + b'\x00\x02' + body[:2]
                 + original[:3] + (len(body) - 2).to_bytes(2, 'big') + body[2:])
        with self.serve() as path, self.client(path) as client, client.makefile('rb') as response:
            client.sendall(REQUEST)
            self.assertEqual(response.read(len(OK)), OK)
            client.sendall(split[:2])
            time.sleep(0.03)
            self.assertFalse(self.dialed)
            client.sendall(split[2:7])
            client.sendall(split[7:])
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(response.read(), split.upper())

    def test_tls_handshake_size_and_deadline(self):
        with patch.object(egress_proxy, 'HELLO_TIMEOUT', 0.15), self.serve() as path:
            self.assertEqual(self.request(path, REQUEST + b'\x16\x03\x03\x40\x01'), OK)
            with self.client(path) as client:
                client.sendall(REQUEST + b'\x16')
                self.assertEqual(self.receive(client), OK)
        self.assertEqual([row['reason'] for row in self.records()],
                         ['invalid TLS record header', 'TLS ClientHello deadline exceeded'])

    def test_resolution_denies_private_transition_and_mixed_answers(self):
        blocked = ('127.0.0.1', '10.1.2.3', '169.254.169.254', '100.64.0.1', '224.0.0.1',
                   '::1', '::ffff:127.0.0.1', 'fc00::1', 'fe80::1', '64:ff9b::7f00:1',
                   '64:ff9b:1::a00:1', '2002:7f00:1::', '::127.0.0.1')
        with self.serve() as path:
            for address in blocked:
                with self.subTest(address=address):
                    self.answers['api.openai.com.'] = addrinfo('93.184.215.14', address)
                    self.assertTrue(self.request(path, REQUEST).startswith(b'HTTP/1.1 403'))
        self.assertFalse(self.dialed)
        row, = self.records()
        self.assertEqual((row['host'], row['count']), ('api.openai.com', len(blocked)))

    def test_resolution_denies_host_public_address_and_its_subnet(self):
        addresses = ('93.184.215.14', '2606:4700::1')
        self.interfaces.return_value = set(map(ipaddress.ip_address, addresses)), set()
        with self.serve() as path:
            for address in addresses:
                with self.subTest(address=address):
                    self.answers['api.openai.com.'] = addrinfo(address)
                    self.assertTrue(self.request(path, REQUEST).startswith(b'HTTP/1.1 403'))
            self.interfaces.return_value = set(), {ipaddress.ip_network('93.184.215.0/24')}
            self.answers['api.openai.com.'] = addrinfo('93.184.215.15')
            self.assertTrue(self.request(path, REQUEST).startswith(b'HTTP/1.1 403'))
        self.assertFalse(self.dialed)

    def test_addresses_are_rechecked_after_resolution_and_between_dials(self):
        first, second = '93.184.215.14', '8.8.8.8'
        self.answers['api.openai.com.'] = addrinfo(first, second)

        def failed_dial(family, address, timeout):
            self.dialed.append(address)
            self.interfaces.return_value = {ipaddress.ip_address(second)}, set()
            raise OSError('first dial failed')

        with self.serve(connect=failed_dial) as path:
            with self.client(path) as client, client.makefile('rb') as response:
                client.sendall(REQUEST)
                self.assertEqual(response.read(len(OK)), OK)
                self.interfaces.return_value = {ipaddress.ip_address(first)}, set()
                client.sendall(client_hello())
                self.assertEqual(response.read(), b'')
            self.assertEqual(self.dialed, [])
            self.interfaces.return_value = set(), set()
            self.assertEqual(self.request(path, REQUEST + client_hello()), OK)
        self.assertEqual(self.dialed, [(first, 443)])
        row, = self.records()
        self.assertEqual((row['reason'], row['count']), ('upstream destination is no longer public and nonlocal', 2))

    def test_stop_between_address_check_and_connect_closes_socket_without_dialing(self):
        created, release = threading.Event(), threading.Event()
        sockets = socket.socket
        upstream = Mock(spec=sockets)
        servers = []
        accept_loop = egress_proxy.Proxy.accept_loop

        def observe_loop(server, ready):
            servers.append(server)
            accept_loop(server, ready)

        def allocate(family, *args, **kwargs):
            return upstream if family == socket.AF_INET else sockets(family, *args, **kwargs)

        def pause_before_connect(blocking):
            self.assertFalse(blocking)
            created.set()
            self.assertTrue(release.wait(4))

        upstream.setblocking.side_effect = pause_before_connect
        with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_loop),
              patch.object(socket, 'socket', allocate), self.serve(connect=None) as path,
              self.client(path) as client):
            client.sendall(REQUEST + client_hello())
            try:
                self.assertTrue(created.wait(3))
                server, = servers
                with server.lock:
                    server.stop_connections()
            finally:
                release.set()
            self.assertEqual(self.receive(client), OK)
        upstream.connect_ex.assert_not_called()
        upstream.close.assert_called_once()
        row, = self.records()
        self.assertEqual((row['allowed'], row['reason']), (False, 'proxy stopping'))

    def test_stop_can_interrupt_a_tracked_nonblocking_connect(self):
        waiting = threading.Event()
        sockets = socket.socket
        connection, peer = socket.socketpair()
        self.addCleanup(connection.close)
        self.addCleanup(peer.close)
        connection.setblocking(False)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        with self.assertRaises(BlockingIOError):
            while True:
                connection.send(bytes(4096))
        upstream = Mock(wraps=connection)
        servers = []
        selected = []
        accept_loop = egress_proxy.Proxy.accept_loop
        selector = egress_proxy.selectors.DefaultSelector
        select = selector.select

        def observe_loop(server, ready):
            servers.append(server)
            accept_loop(server, ready)

        def allocate(family, *args, **kwargs):
            return upstream if family == socket.AF_INET else sockets(family, *args, **kwargs)

        def start_connect(address):
            server, = servers
            self.assertTrue(server.lock.locked())
            self.assertIn(upstream, server.connections)
            self.assertEqual(address, ('93.184.215.14', 443))
            return errno.EINPROGRESS

        def observe_select(ready, timeout=None):
            if any(key.fileobj is upstream for key in ready.get_map().values()):
                self.assertEqual(select(ready, 0), [])
                waiting.set()
                events = select(ready, timeout)
                selected.extend((key.fileobj, mask) for key, mask in events)
                return events
            return select(ready, timeout)

        upstream.connect_ex.side_effect = start_connect
        with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_loop),
              patch.object(socket, 'socket', allocate),
              patch.object(selector, 'select', observe_select), self.serve(connect=None) as path,
              self.client(path) as client):
            client.sendall(REQUEST + client_hello())
            self.assertTrue(waiting.wait(3))
            server, = servers
            self.assertTrue(server.lock.acquire(timeout=1))
            try:
                server.stop_connections()
                upstream.shutdown.assert_called_once_with(socket.SHUT_RDWR)
                handler, = server.handlers
            finally:
                server.lock.release()
            handler.join(2)
            self.assertFalse(handler.is_alive())
            self.assertEqual(self.receive(client), OK)
        self.assertEqual(selected, [(upstream, egress_proxy.selectors.EVENT_WRITE)])
        upstream.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_ERROR)
        upstream.close.assert_called_once()
        row, = self.records()
        self.assertEqual((row['allowed'], row['reason']), (False, 'proxy stopping'))

    def test_nonblocking_connect_checks_socket_error_and_retries(self):
        self.answers['api.openai.com.'] = addrinfo('93.184.215.14', '8.8.8.8')
        sockets = socket.socket
        failed, peer = socket.socketpair()
        with failed, peer, self.connect(socket.AF_INET, ('8.8.8.8', 443), 2) as connected:
            refused = Mock(wraps=failed)
            refused.connect_ex.return_value = errno.EINPROGRESS
            refused.getsockopt.return_value = errno.ECONNREFUSED
            accepted = Mock(wraps=connected)
            accepted.connect_ex.return_value = errno.EINPROGRESS
            upstreams = iter((refused, accepted))

            def allocate(family, *args, **kwargs):
                return next(upstreams) if family == socket.AF_INET else sockets(family, *args, **kwargs)

            hello = client_hello()
            with patch.object(socket, 'socket', allocate), self.serve(connect=None) as path:
                self.assertEqual(self.request(path, REQUEST + hello), OK + hello.upper())
        refused.connect_ex.assert_called_once_with(('93.184.215.14', 443))
        accepted.connect_ex.assert_called_once_with(('8.8.8.8', 443))
        for upstream in (refused, accepted):
            upstream.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_ERROR)
            upstream.close.assert_called_once()
        row, = self.records()
        self.assertTrue(row['allowed'])

    def test_ready_dns_child_holds_one_slot_until_released(self):
        ready, release = self.base / 'resolver.ready', self.base / 'resolver.release'
        script = resolver_script(f'''
import os
from pathlib import Path

def gated(*args, **kwargs):
    Path({str(ready)!r}).write_text(str(os.getpid()))
    while not Path({str(release)!r}).exists():
        time.sleep(0.01)
    return [(2, 1, 6, '', ('93.184.215.14', 443))]

socket.getaddrinfo = gated
''')
        hello = client_hello()
        with (patch.object(egress_proxy, 'DNS_TIMEOUT', 30),
              patch.object(egress_proxy, 'DNS_CONCURRENCY', 1),
              patch.object(egress_proxy, 'RESOLVER_SCRIPT', script), self.serve(resolve=None) as path,
              self.client(path) as first):
            first.sendall(REQUEST + hello)
            first.shutdown(socket.SHUT_WR)
            try:
                pid = wait_for(lambda: ready.exists() and ready.read_text())
                self.assertTrue(Path(f'/proc/{pid}').exists())
                self.assertEqual(self.request(path, REQUEST), b'HTTP/1.1 403 Forbidden\r\n\r\n')
            finally:
                release.touch()
            self.assertEqual(self.receive(first), OK + hello.upper())
            self.assertEqual(self.request(path, REQUEST + hello), OK + hello.upper())
        rows = self.records()
        denial, = [row for row in rows if not row['allowed']]
        self.assertEqual((denial['reason'], denial['count']), ('DNS capacity reached', 1))
        self.assertEqual(sum(row['allowed'] for row in rows), 2)
        self.assertEqual(self.dialed, [(socket.AF_INET, ('93.184.215.14', 443))] * 2)

    def test_dns_timeout_reaps_children_and_releases_slot(self):
        pid_path = self.base / 'resolver.pid'
        script = resolver_script(f'''
import os
from pathlib import Path

def offline(host, *args, **kwargs):
    if host == 'api.openai.com.':
        Path({str(pid_path)!r}).write_text(str(os.getpid()))
        signal.pause()
    return [(2, 1, 6, '', ('93.184.215.14', 443))]

socket.getaddrinfo = offline
''')
        with (patch.object(egress_proxy, 'DNS_TIMEOUT', 1.5),
              patch.object(egress_proxy, 'DNS_CONCURRENCY', 1),
              patch.object(egress_proxy, 'RESOLVER_SCRIPT', script), self.serve(resolve=None) as path):
            for _ in range(2):
                with self.client(path) as stalled:
                    stalled.sendall(REQUEST)
                    pid = wait_for(lambda: pid_path.exists() and pid_path.read_text())
                    self.assertTrue(self.receive(stalled).startswith(b'HTTP/1.1 403'))
                self.assertFalse(Path(f'/proc/{pid}').exists())
                pid_path.unlink()
            hello = client_hello('ab.chatgpt.com')
            allowed = REQUEST.replace(b'api.openai.com', b'ab.chatgpt.com') + hello
            self.assertEqual(self.request(path, allowed), OK + hello.upper())
        denials = [row for row in self.records() if not row['allowed']]
        counts = {reason: sum(row['count'] for row in denials if row['reason'] == reason)
                  for reason in {row['reason'] for row in denials}}
        self.assertEqual(counts, {'DNS resolution deadline exceeded': 2})
        self.assertEqual(self.dialed, [(socket.AF_INET, ('93.184.215.14', 443))])

    def test_run_duration_expires_during_dns(self):
        pid_path = self.base / 'resolver.ready'
        script = resolver_script(f'''
from pathlib import Path
def stalled(*args, **kwargs):
    Path({str(pid_path)!r}).touch()
    signal.pause()
socket.getaddrinfo = stalled
''')
        with (patch.object(egress_proxy, 'RESOLVER_SCRIPT', script),
              self.serve(resolve=None, max_seconds=1) as path):
            self.assertEqual(self.request(path, REQUEST), b'')
        self.assertTrue(pid_path.exists())
        row, = self.records()
        self.assertEqual(row['reason'], 'connection duration cap')
        self.assertFalse(self.dialed)

    def test_sandbox_resolver_callback_is_precomputed_and_stalls_fail_startup(self):
        def resolve(host, port, **options):
            return addrinfo('93.184.215.14') if host == 'api.openai.com.' else addrinfo('127.0.0.1')

        hello = client_hello()
        with self.serve(resolve=resolve) as path:
            self.assertEqual(self.request(path, REQUEST + hello), OK + hello.upper())

        def stalled(*args, **kwargs):
            signal.pause()

        with (patch.object(egress_proxy, 'DNS_TIMEOUT', 0.25),
              self.assertRaisesRegex(OSError, 'resolver fixture failed'), self.serve(resolve=stalled)):
            self.fail('stalling resolver opened admission')
        self.assertFalse((self.outbox / 'egress.sock').exists())

    def test_allowlist_accepts_exact_hosts_and_refuses_wildcards(self):
        self.answers['models.example.test.'] = addrinfo('93.184.215.14')
        with self.serve(egress_proxy.allowlist(['Models.Example.Test'])) as path:
            request = b'CONNECT models.example.test:443 HTTP/1.1\r\n\r\n'
            self.assertTrue(self.request(path, request + client_hello('models.example.test')).startswith(OK))
        for malformed in ('str', ['*'], ['*.com'], ['*.extra.test'], ['api.example.test:443'], ['www.test/path'], [1]):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(OSError, 'exact hostnames'):
                egress_proxy.allowlist(malformed)

    def test_idle_and_absolute_duration_close_tunnels(self):
        hello = client_hello()
        for settings, reason in (({'idle_timeout': 0.15}, 'tunnel idle timeout'),
                                 ({'max_seconds': 0.2}, 'connection duration cap')):
            with self.subTest(reason=reason), self.serve(**settings) as path, self.client(path) as client:
                client.sendall(REQUEST + hello)
                self.assertEqual(self.receive(client), OK + hello.upper())
        self.assertEqual([row['reason'] for row in self.records()],
                         ['tunnel idle timeout', 'connection duration cap'])

    def test_byte_cap_is_shared_across_connections_and_directions(self):
        hello = client_hello()
        with self.serve(max_bytes=3 * len(hello) + 10) as path:
            self.assertEqual(self.request(path, REQUEST + hello), OK + hello.upper())
            self.assertEqual(self.request(path, REQUEST + hello), OK + hello[:10].upper())
            self.assertTrue(self.request(path, REQUEST).startswith(b'HTTP/1.1 403'))
        rows = self.records()
        self.assertEqual(sum(row['bytes'] for row in rows), 3 * len(hello) + 10)
        self.assertEqual([row['reason'] for row in rows[1:]], ['per-run byte cap'] * 2)

    def test_connection_budget_counts_allowed_and_refused_and_logs_exhaustion_once(self):
        for invalid in (0, -1, '2', True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(OSError, 'positive integer'):
                egress_proxy.limits({'worker_egress_max_connections': invalid})

        repeated = 4
        denials_ready = threading.Barrier(repeated)
        pause, paused, release = threading.Event(), threading.Event(), threading.Event()
        log = egress_proxy.Proxy.log
        service_actions = egress_proxy.Proxy.service_actions
        denied = REQUEST.replace(b'api.openai.com', b'outside.invalid')
        hello = client_hello()
        settings = egress_proxy.limits({'worker_egress_max_connections': repeated + 2,
                                        'worker_egress_max_bytes': 4 * len(hello)})

        def synchronized_log(server, audit):
            if audit['reason'] == 'host is not allowlisted':
                denials_ready.wait(timeout=3)
            log(server, audit)

        def pause_at_boundary(server):
            if pause.is_set():
                paused.set()
                if not release.wait(4):
                    raise RuntimeError('test did not release the accept loop')
            service_actions(server)

        cap = 1024
        refusal = b'HTTP/1.1 403 Forbidden\r\n\r\n'
        with (patch.object(egress_proxy.Proxy, 'log', synchronized_log),
              patch.object(egress_proxy.Proxy, 'service_actions', pause_at_boundary),
              patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
              patch.object(egress_proxy, 'AUDIT_FLUSH_INTERVAL', 60),
              self.serve(**settings) as path,
              self.client(path) as admitted, admitted.makefile('rb') as response,
              ThreadPoolExecutor(max_workers=repeated) as pool):
            admitted.sendall(REQUEST)
            self.assertEqual(response.read(len(OK)), OK)
            requests = [pool.submit(self.request, path, denied) for _ in range(repeated)]
            self.assertEqual([request.result(timeout=4) for request in requests], [refusal] * repeated)
            pause.set()
            try:
                self.assertTrue(paused.wait(3))
                with self.client(path) as first, self.client(path) as second:
                    for contender in (first, second):
                        contender.sendall(REQUEST + hello)
                        contender.shutdown(socket.SHUT_WR)
                    release.set()
                    self.assertCountEqual([self.receive(first), self.receive(second)], [OK + hello.upper(), refusal])
            finally:
                release.set()
            with self.assertRaises(ConnectionRefusedError), self.client(path):
                self.fail('listener admitted a connection after the budget refusal')
            admitted.sendall(hello)
            admitted.shutdown(socket.SHUT_WR)
            self.assertEqual(response.read(), hello.upper())
        self.assertEqual(self.resolved, ['api.openai.com.'] * 2)
        rows = self.records()
        self.assertEqual(sum(row['allowed'] for row in rows), 2)
        self.assertEqual(sum(row['bytes'] for row in rows), 4 * len(hello))
        refused, = [row for row in rows if row['reason'] == 'host is not allowlisted']
        self.assertEqual(refused['count'], repeated)
        self.assertEqual(sum(row['reason'] == egress_proxy.CONNECTION_CAP for row in rows), 1)
        self.assertEqual(len(rows), 4)
        self.assertLessEqual(self.log.stat().st_size, cap)

    def test_repeated_denials_flush_periodically_and_on_shutdown(self):
        denied = REQUEST.replace(b'api.openai.com', b'outside.invalid')
        with self.serve() as path:
            for _ in range(3):
                self.assertTrue(self.request(path, denied).startswith(b'HTTP/1.1 403'))
            deadline = time.monotonic() + 2 * egress_proxy.AUDIT_FLUSH_INTERVAL
            while not self.log.stat().st_size and time.monotonic() < deadline:
                time.sleep(0.01)
            row, = self.records()
            self.assertEqual((row['host'], row['reason'], row['count']),
                             ('outside.invalid', 'host is not allowlisted', 3))
            self.assertTrue(self.request(path, denied).startswith(b'HTTP/1.1 403'))
        self.assertEqual([row['count'] for row in self.records()], [3, 1])

    def test_denial_audit_cap_reserves_truncation_and_budget_records(self):
        cap = 512
        with (patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
              patch.object(egress_proxy, 'AUDIT_DENIAL_LIMIT', 1),
              self.serve(max_connections=4) as path):
            for host in (b'one.invalid', b'two.invalid', b'three.invalid', b'four.invalid'):
                self.assertTrue(self.request(path, REQUEST.replace(b'api.openai.com', host)).startswith(b'HTTP/1.1 403'))
            with self.client(path) as client:
                self.assertTrue(self.receive(client).startswith(b'HTTP/1.1 403'))
        self.assertEqual([row['reason'] for row in self.records()],
                         ['host is not allowlisted', egress_proxy.CONNECTION_CAP, 'audit truncated'])
        self.assertLessEqual(self.log.stat().st_size, cap)

    def test_allowed_audit_cap_preserves_truncation_and_budget_records(self):
        cap = 600
        hello = client_hello()
        with patch.object(egress_proxy, 'AUDIT_LIMIT', cap), self.serve(max_connections=3) as path:
            for _ in range(3):
                self.assertEqual(self.request(path, REQUEST + hello), OK + hello.upper())
            with self.client(path) as client:
                self.assertTrue(self.receive(client).startswith(b'HTTP/1.1 403'))
        allowed, budget, marker = self.records()
        self.assertTrue(allowed['allowed'])
        self.assertEqual(allowed['bytes'], 2 * len(hello))
        self.assertEqual(marker['reason'], 'audit truncated')
        self.assertEqual(budget['reason'], egress_proxy.CONNECTION_CAP)
        self.assertLessEqual(self.log.stat().st_size, cap)

    def test_audit_truncation_drops_smaller_rows_and_preserves_the_budget_record(self):
        cap = 600
        path = self.outbox / egress_proxy.SOCKET_NAME
        with (patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
              egress_proxy.Proxy(str(path), self.log, egress_proxy.DEFAULT_ALLOW, resolve=self.answers) as server):
            server.log(dict(server.audit(), allowed=True, reason='x' * cap))
            self.assertEqual(self.log.read_bytes(), egress_proxy.AUDIT_TRUNCATION)
            server.log(dict(server.audit(), allowed=True, reason='tunnel closed'))
            server.log(dict(server.audit(), reason='host is not allowlisted'))
            with server.lock:
                server.flush_denials()
            self.assertEqual(self.log.read_bytes(), egress_proxy.AUDIT_TRUNCATION)
            server.log(dict(server.audit(), reason=egress_proxy.CONNECTION_CAP))
            server.log(dict(server.audit(), allowed=True, reason='tunnel closed'))
        self.assertEqual([row['reason'] for row in self.records()], [egress_proxy.CONNECTION_CAP, 'audit truncated'])
        self.assertLessEqual(self.log.stat().st_size, cap)

    def test_audit_truncation_marker_records_budget_when_a_separate_row_will_not_fit(self):
        cap = len(egress_proxy.AUDIT_BUDGET_TRUNCATION)
        path = self.outbox / egress_proxy.SOCKET_NAME
        for truncated in (False, True):
            with (self.subTest(truncated=truncated), patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
                  egress_proxy.Proxy(str(path), self.log, egress_proxy.DEFAULT_ALLOW, resolve=self.answers) as server):
                self.log.write_bytes(b'')
                if truncated:
                    server.log(dict(server.audit(), allowed=True, reason='x' * cap))
                    self.assertEqual(self.log.read_bytes(), egress_proxy.AUDIT_TRUNCATION)
                for _ in range(2):
                    server.log(dict(server.audit(), reason=egress_proxy.CONNECTION_CAP))
                server.log(dict(server.audit(), allowed=True, reason='tunnel closed'))
                self.assertTrue(server.connection_cap_logged)
                self.assertIsNone(server.audit_error)
                marker, = self.records()
                self.assertEqual(marker, {'allowed': False, 'bytes': 0, 'reason': 'audit truncated',
                                          'budget_exhausted': True})
                self.assertLessEqual(self.log.stat().st_size, cap)
            path.unlink()

    def test_short_late_budget_write_preserves_the_original_audit(self):
        cap = 600
        path = self.outbox / egress_proxy.SOCKET_NAME
        temporary_file = tempfile.NamedTemporaryFile
        writes = []

        def open_short_sibling(*args, **kwargs):
            sibling = temporary_file(*args, **kwargs)
            write = sibling.write

            def short_write(data):
                target = Path(sibling.name)
                writes.append((target, stat.S_IMODE(target.stat().st_mode)))
                return write(data[:-1])

            sibling.write = short_write
            return sibling

        with (patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
              egress_proxy.Proxy(str(path), self.log, egress_proxy.DEFAULT_ALLOW, resolve=self.answers) as server):
            server.log(dict(server.audit(), allowed=True, reason='tunnel closed'))
            server.log(dict(server.audit(), allowed=True, reason='x' * cap))
            before = self.log.read_bytes()
            self.assertEqual([row['reason'] for row in self.records()], ['tunnel closed', 'audit truncated'])
            with patch.object(tempfile, 'NamedTemporaryFile', open_short_sibling):
                server.log(dict(server.audit(), reason=egress_proxy.CONNECTION_CAP))
            self.assertTrue(server.stopping)
            self.assertFalse(server.connection_cap_logged)
            self.assertRegex(str(server.audit_error), 'egress audit record could not be written in full')
            self.assertEqual(self.log.read_bytes(), before)
        (sibling, mode), = writes
        self.assertEqual((sibling.parent, mode), (self.log.parent, 0o600))
        self.assertFalse(sibling.exists())

    def test_late_shutdown_denials_are_written_after_the_final_flush(self):
        entered, release = threading.Event(), threading.Event()
        log = egress_proxy.Proxy.log

        def delayed_log(server, audit):
            entered.set()
            release.wait(timeout=4)
            log(server, audit)

        denied = REQUEST.replace(b'api.openai.com', b'outside.invalid')
        with (patch.object(egress_proxy.Proxy, 'log', delayed_log),
              patch.object(egress_proxy, 'DNS_TIMEOUT', 0.01),
              patch.object(egress_proxy, 'CONNECT_TIMEOUT', 0.01)):
            try:
                with (self.assertRaisesRegex(OSError, 'egress proxy shutdown deadline exceeded'),
                      self.serve() as path, self.client(path) as client):
                    client.sendall(denied)
                    self.assertTrue(entered.wait(2))
                self.assertEqual(self.log.stat().st_size, 0)
            finally:
                release.set()
            wait_for(lambda: self.log.stat().st_size)
        row, = self.records()
        self.assertEqual((row['host'], row['reason']), ('outside.invalid', 'host is not allowlisted'))

    def test_runtime_audit_io_errors_close_admission_and_fail_the_launcher(self):
        real_open = Path.open
        original_proxy = egress_proxy.Proxy
        denied = REQUEST.replace(b'api.openai.com', b'outside.invalid')
        for failure in ('periodic open', 'periodic write', 'budget write'):
            with self.subTest(failure=failure):
                servers = []

                class ObservedProxy(original_proxy):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        servers.append(self)

                @contextmanager
                def broken_write(target, *args, **kwargs):
                    with real_open(target, *args, **kwargs) as stream:
                        failing = Mock(wraps=stream)
                        failing.write.side_effect = OSError('injected audit write error')
                        yield failing

                def open_with_failure(target, *args, **kwargs):
                    if target != self.log:
                        return real_open(target, *args, **kwargs)
                    if failure == 'periodic open':
                        raise OSError('injected audit open error')
                    return broken_write(target, *args, **kwargs)

                budget = failure == 'budget write'
                with (patch.object(egress_proxy, 'Proxy', ObservedProxy),
                      patch.object(egress_proxy, 'AUDIT_FLUSH_INTERVAL', 60 if budget else 0.05),
                      self.assertRaisesRegex(OSError, 'egress audit failed: injected audit'),
                      self.serve(max_connections=1 if budget else 4) as path):
                    server, = servers
                    with patch.object(Path, 'open', open_with_failure):
                        self.assertTrue(self.request(path, denied).startswith(b'HTTP/1.1 403'))
                        if budget:
                            with self.client(path) as client:
                                self.assertTrue(self.receive(client).startswith(b'HTTP/1.1 403'))
                        wait_for(lambda: server.audit_error is not None and server.socket.fileno() < 0)
                        self.assertFalse(server.connection_cap_logged)
                        with self.assertRaises(ConnectionRefusedError), self.client(path):
                            self.fail('listener remained open after an audit I/O error')
                self.assertFalse((self.outbox / 'egress.sock').exists())

    def test_selector_setup_failure_prevents_serve_from_yielding(self):
        selector = egress_proxy.selectors.DefaultSelector
        unavailable = OSError('injected selector creation failure')
        with (patch.object(egress_proxy.selectors, 'DefaultSelector', side_effect=unavailable),
              self.assertRaisesRegex(OSError, 'egress listener failed: injected selector creation failure')
              as raised, self.serve()):
            self.fail('serve yielded after selector creation failed')
        self.assertIs(raised.exception.__cause__, unavailable)
        self.assertFalse((self.outbox / egress_proxy.SOCKET_NAME).exists())

        register = selector.register

        def fail_listener_registration(ready, fileobj, events):
            if isinstance(fileobj, socket.socket):
                raise OSError('injected listener registration failure')
            return register(ready, fileobj, events)

        with (patch.object(selector, 'register', fail_listener_registration),
              self.assertRaisesRegex(OSError, 'egress listener failed: injected listener registration failure'),
              self.serve()):
            self.fail('serve yielded after listener registration failed')
        self.assertFalse((self.outbox / egress_proxy.SOCKET_NAME).exists())

    def test_runtime_listener_failure_retires_listener_and_fails_the_run(self):
        dialing, release, waiting, retired = (threading.Event() for _ in range(4))
        dialers, listeners, retirement, dispatch_closure = [], [], [], []
        sockets = socket.socket
        upstream = Mock(spec=sockets)
        upstream.connect_ex.return_value = errno.ECONNREFUSED
        accept_loop = egress_proxy.Proxy.accept_loop
        shutdown_request = egress_proxy.Proxy.shutdown_request
        server_close = egress_proxy.Proxy.server_close
        unavailable = RuntimeError('injected handler start failure')

        def observe_loop(server, ready):
            listeners.append(threading.current_thread())
            lock = server.lock

            class ObservedLock:
                def __enter__(observed):
                    if threading.current_thread() in dialers and not waiting.is_set():
                        self.assertTrue(lock.locked())
                        waiting.set()
                    return lock.__enter__()

                def __exit__(self, *args):
                    return lock.__exit__(*args)

            server.lock = ObservedLock()
            accept_loop(server, ready)

        def allocate(family, *args, **kwargs):
            return upstream if family == socket.AF_INET else sockets(family, *args, **kwargs)

        def pause_before_connect(blocking):
            self.assertFalse(blocking)
            dialers.append(threading.current_thread())
            dialing.set()
            self.assertTrue(release.wait(4))

        def fail_start():
            release.set()
            self.assertTrue(waiting.wait(3))
            raise unavailable

        def close_request(server, request):
            if threading.current_thread() in listeners:
                rows = [row for row in self.records() if row['reason'] == 'handler dispatch failed']
                dispatch_closure.append((server.connections_left, rows))
            shutdown_request(server, request)

        def close_listener(server):
            retirement.append((server.stopping, server.listener_error))
            server_close(server)
            retired.set()

        upstream.setblocking.side_effect = pause_before_connect
        with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_loop),
              patch.object(egress_proxy.Proxy, 'shutdown_request', close_request),
              patch.object(egress_proxy.Proxy, 'server_close', close_listener), patch.object(socket, 'socket', allocate),
              self.assertRaisesRegex(OSError, 'egress listener failed: injected handler start failure') as raised,
              self.serve(connect=None, max_connections=2) as path):
            with self.client(path) as admitted:
                admitted.sendall(REQUEST + client_hello())
                try:
                    self.assertTrue(dialing.wait(3))
                    with patch.object(threading.Thread, 'start', side_effect=fail_start), self.client(path) as pending:
                        self.assertEqual(self.receive(pending), b'')
                        self.assertTrue(retired.wait(2))
                finally:
                    release.set()
                self.assertEqual(self.receive(admitted), OK)
            with self.assertRaises(ConnectionRefusedError), self.client(path):
                self.fail('listener remained open after its thread failed')
        self.assertIs(raised.exception.__cause__, unavailable)
        self.assertEqual(retirement, [(True, unavailable)])
        upstream.connect_ex.assert_not_called()
        upstream.close.assert_called_once()
        (slots_left, (dispatch,)), = dispatch_closure
        self.assertEqual(slots_left, 1)
        self.assertEqual((dispatch['allowed'], dispatch['reason']), (False, 'handler dispatch failed'))
        self.assertCountEqual([(row['allowed'], row['reason']) for row in self.records()],
                              [(False, 'handler dispatch failed'), (False, 'proxy stopping')])
        self.assertFalse(path.exists())

    def test_dispatch_failure_is_audited_before_close_when_audit_truncates(self):
        cap = 512
        accept_loop = egress_proxy.Proxy.accept_loop
        shutdown_request = egress_proxy.Proxy.shutdown_request
        unavailable = RuntimeError('injected handler start failure')
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                servers, at_close = [], []
                self.log.write_bytes(b'')

                def observe_loop(server, ready):
                    servers.append(server)
                    accept_loop(server, ready)

                def close_request(server, request):
                    at_close.append((request.fileno(), self.records()))
                    shutdown_request(server, request)

                with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_loop),
                      patch.object(egress_proxy.Proxy, 'shutdown_request', close_request),
                      patch.object(egress_proxy, 'AUDIT_LIMIT', cap),
                      self.assertRaisesRegex(OSError, 'egress listener failed: injected handler start failure'),
                      self.serve() as path):
                    server, = wait_for(lambda: servers)
                    server.log(dict(server.audit(), allowed=True, reason='tunnel closed'))
                    if truncated:
                        server.log(dict(server.audit(), allowed=True, reason='x' * cap))
                    self.assertEqual(server.audit_truncated, truncated)
                    with patch.object(threading.Thread, 'start', side_effect=unavailable), self.client(path) as pending:
                        self.assertEqual(self.receive(pending), b'')
                (descriptor, rows), = at_close
                self.assertGreaterEqual(descriptor, 0)
                self.assertEqual([row['reason'] for row in rows],
                                 ['tunnel closed', 'handler dispatch failed', 'audit truncated'])
                self.assertFalse(rows[1]['allowed'])
                self.assertEqual(self.records(), rows)
                self.assertLessEqual(self.log.stat().st_size, cap)

    def test_fatal_poll_or_accept_stops_a_handler_waiting_to_dial(self):
        sockets = socket.socket
        accept_loop = egress_proxy.Proxy.accept_loop
        get_request = egress_proxy.Proxy.get_request
        failures = (('select', OSError('injected selector failure'), 'injected selector failure'),
                    ('accept', OSError(errno.EMFILE, os.strerror(errno.EMFILE)), 'egress accept descriptor exhaustion'))
        for operation, failure, message in failures:
            with self.subTest(operation=operation):
                dialing, release, waiting, inject = (threading.Event() for _ in range(4))
                dialers, gate_releases = [], []
                upstream = Mock(spec=sockets)
                upstream.connect_ex.return_value = errno.ECONNREFUSED

                def allocate(family, *args, **kwargs):
                    return upstream if family == socket.AF_INET else sockets(family, *args, **kwargs)

                def pause_before_connect(blocking):
                    self.assertFalse(blocking)
                    dialers.append(threading.current_thread())
                    dialing.set()
                    self.assertTrue(release.wait(4))

                def fail():
                    release.set()
                    self.assertTrue(waiting.wait(2))
                    raise failure

                def observe_loop(server, ready):
                    listener = threading.current_thread()
                    select, wait, notify = ready.select, server.poll_gate.wait, server.poll_gate.notify_all

                    def select_or_fail(timeout):
                        events = select(timeout)
                        if operation == 'select' and inject.is_set() and events:
                            fail()
                        return events

                    def observe_wait(timeout=None):
                        if threading.current_thread() in dialers:
                            waiting.set()
                        return wait(timeout)

                    def observe_notify():
                        if threading.current_thread() is listener and waiting.is_set():
                            gate_releases.append((server.stopping, server.listener_error))
                        notify()

                    ready.select = select_or_fail
                    server.poll_gate.wait = observe_wait
                    server.poll_gate.notify_all = observe_notify
                    accept_loop(server, ready)

                def accept_or_fail(server):
                    if operation == 'accept' and inject.is_set():
                        fail()
                    return get_request(server)

                upstream.setblocking.side_effect = pause_before_connect
                with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_loop),
                      patch.object(egress_proxy.Proxy, 'get_request', accept_or_fail),
                      patch.object(socket, 'socket', allocate),
                      self.assertRaisesRegex(OSError, f'egress listener failed: .*{message}') as raised,
                      self.serve(connect=None) as path, self.client(path) as admitted):
                    admitted.sendall(REQUEST + client_hello())
                    try:
                        self.assertTrue(dialing.wait(2))
                        inject.set()
                        with self.client(path) as pending:
                            self.assertEqual(self.receive(pending), b'')
                    finally:
                        release.set()
                    self.assertEqual(self.receive(admitted), OK)
                cause = raised.exception.__cause__
                self.assertIs(cause if operation == 'select' else cause.__cause__, failure)
                self.assertEqual(gate_releases, [(True, cause)])
                upstream.connect_ex.assert_not_called()
                upstream.close.assert_called_once()
                self.assertFalse(path.exists())

    def test_accept_descriptor_exhaustion_retires_listener_without_retry(self):
        for code in (errno.EMFILE, errno.ENFILE):
            with self.subTest(code=code):
                retirement = []
                server_close = egress_proxy.Proxy.server_close

                def close_listener(server):
                    retirement.append((server.lock.locked(), server.stopping, server.listener_error))
                    server_close(server)

                with (patch.object(egress_proxy.Proxy, 'server_close', close_listener),
                      patch.object(egress_proxy.Proxy, 'get_request', side_effect=OSError(code, os.strerror(code)))
                      as accept, self.assertRaisesRegex(OSError, 'egress accept descriptor exhaustion') as raised,
                      self.serve() as path, self.client(path) as pending):
                    self.assertEqual(self.receive(pending), b'')
                    with self.assertRaises(ConnectionRefusedError), self.client(path):
                        self.fail('listener stayed open after descriptor exhaustion')
                accept.assert_called_once_with()
                self.assertEqual(raised.exception.__cause__.errno, code)
                self.assertEqual(retirement, [(True, True, raised.exception.__cause__)])
                self.assertFalse(path.exists())
        self.assertEqual(self.records(), [])

    def test_shutdown_wakes_selector_and_listener_thread_closes_socket(self):
        polling = threading.Event()
        selected, closed = [], []
        accept_loop = egress_proxy.Proxy.accept_loop
        server_close = egress_proxy.Proxy.server_close

        def observe_polling(server, ready):
            select = ready.select

            def blocking_select(_timeout):
                polling.set()
                events = select(10)
                selected.extend(key.fileobj for key, _ in events)
                return events

            ready.select = blocking_select
            accept_loop(server, ready)

        def close_on_listener_thread(server):
            closed.append(threading.current_thread())
            server_close(server)

        with (patch.object(egress_proxy.Proxy, 'accept_loop', observe_polling),
              patch.object(egress_proxy.Proxy, 'server_close', close_on_listener_thread), self.serve() as path):
            self.assertTrue(polling.wait(2))
        wakeup_fd, = selected
        self.assertIsInstance(wakeup_fd, int)
        thread, = closed
        self.assertIsNot(thread, threading.current_thread())
        self.assertFalse(path.exists())

    def test_shutdown_between_accept_and_dispatch_audits_immediately(self):
        accepted, release, dispatched = threading.Event(), threading.Event(), threading.Event()
        servers = []
        process_request = egress_proxy.Proxy.process_request

        def pause_dispatch(server, request, address, ready):
            servers.append(server)
            accepted.set()
            self.assertTrue(release.wait(4))
            process_request(server, request, address, ready)
            # Still inside dispatch, before the listener can flush the denial batch.
            row, = self.records()
            self.assertEqual((row['allowed'], row['reason']), (False, 'proxy stopping'))
            dispatched.set()

        with (patch.object(egress_proxy.Proxy, 'process_request', pause_dispatch),
              patch.object(egress_proxy, 'AUDIT_FLUSH_INTERVAL', 60),
              self.serve() as path, self.client(path) as client):
            try:
                self.assertTrue(accepted.wait(2))
                server, = servers
                with server.lock:
                    server.stop_connections()
                    self.assertGreaterEqual(server.socket.fileno(), 0)
            finally:
                release.set()
            self.assertEqual(self.receive(client), b'')
            self.assertTrue(dispatched.wait(2))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.dialed, [])

    def test_proxy_shutdown_closes_idle_clients_and_unlinks_the_socket(self):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            with self.serve() as path:
                client.connect(str(path))
                client.sendall(REQUEST)
                self.assertEqual(client.recv(256), OK)
            self.assertFalse(path.exists())
            self.assertEqual(client.recv(256), b'')
        self.assertEqual(len(self.records()), 1)

    def test_outbox_mode_and_init_failure_cleanup(self):
        self.outbox.chmod(0o755)
        with self.assertRaisesRegex(OSError, 'mode 0700'), self.serve():
            self.fail('insecure outbox accepted')
        self.outbox.chmod(0o700)
        with patch.object(Path, 'touch', side_effect=OSError('log unavailable')):
            with self.assertRaisesRegex(OSError, 'log unavailable'), self.serve():
                self.fail('failed proxy returned a socket')
        with patch.object(Path, 'open', side_effect=OSError('audit open failed')):
            with self.assertRaisesRegex(OSError, 'audit open failed'), self.serve():
                self.fail('failed audit open returned a socket')
        self.assertFalse((self.outbox / 'egress.sock').exists())

    def test_stale_cleanup_preserves_active_sockets(self):
        stale = self.base / 'stale-out'
        stale.mkdir(mode=0o700)
        orphan = stale / 'egress.sock'
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(orphan))
        self.outbox = self.outbox.rename(self.base / 'active-out')
        with self.serve() as active:
            egress_proxy.prune_stale_sockets(self.base)
            self.assertFalse(orphan.exists())
            self.assertTrue(active.is_socket())

    def test_socket_bind_handles_a_long_outbox_path(self):
        self.outbox = self.outbox / ('project-' + 'x' * 110)
        self.outbox.mkdir(mode=0o700)
        with self.serve() as path:
            self.assertTrue(path.is_socket())
            fd = os.open(self.outbox, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            response = self.request(f'/proc/self/fd/{fd}/egress.sock',
                                    b'CONNECT outside.invalid:443 HTTP/1.1\r\n\r\n')
            self.assertTrue(response.startswith(b'HTTP/1.1 403'))
