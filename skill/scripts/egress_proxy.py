"""Host-side CONNECT proxy for a single worker. Linux and standard library only."""
import ctypes
import errno
import fcntl
import ipaddress
import json
import os
import re
import selectors
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

DEFAULT_ALLOW = ('chatgpt.com', 'ab.chatgpt.com', 'api.openai.com', 'auth.openai.com')
SOCKET_NAME = 'egress.sock'
PORT = 18080
SANDBOX_SOCKET = '/run/korolev-egress.sock'
HEADER_LIMIT = 8 * 1024
HELLO_LIMIT = 16 * 1024
TLS_RECORD_LIMIT = 16 * 1024
# Four records cover fragmented hellos, including a split handshake header,
# without allowing tiny records to amplify reads. Only their headers add to the wire budget.
HELLO_RECORD_LIMIT = 4
HELLO_WIRE_LIMIT = HELLO_LIMIT + 5 * HELLO_RECORD_LIMIT
HEADER_TIMEOUT = HELLO_TIMEOUT = 10
DNS_TIMEOUT = CONNECT_TIMEOUT = 5
IDLE_TIMEOUT = 120
MAX_BYTES = 2 * 1024 ** 3
MAX_SECONDS = 2 * 60 * 60
MAX_CONNECTIONS = 2048
CONCURRENCY = 32
DNS_CONCURRENCY = CONCURRENCY
DNS_ANSWER_LIMIT = 128
DNS_IPC_LIMIT = 32 * 1024
AUDIT_LIMIT = 1024 * 1024
# Either terminal denial ends admission, so both can use the same reserved space.
AUDIT_TERMINAL_RESERVE = 256
AUDIT_TRUNCATION = b'{"allowed": false, "bytes": 0, "reason": "audit truncated"}\n'
AUDIT_BUDGET_TRUNCATION = AUDIT_TRUNCATION[:-2] + b', "budget_exhausted": true}\n'
AUDIT_FLUSH_INTERVAL = 1
AUDIT_DENIAL_LIMIT = 128
CONNECTION_CAP = 'per-run connection cap'
DISPATCH_FAILURE = 'handler dispatch failed'
RESOLVER_SCRIPT = '''
import json
import resource
import signal
import socket
import sys
import time

signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
signal.setitimer(signal.ITIMER_REAL, max(0.000001, float(sys.argv[2]) - time.monotonic()))
try:
    soft, _ = resource.getrlimit(resource.RLIMIT_AS)
    limit = 64 * 1024 ** 2
    if soft != resource.RLIM_INFINITY:
        limit = min(limit, soft)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    answers = socket.getaddrinfo(sys.argv[1], 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if len(answers) > int(sys.argv[3]):
        sys.exit(2)
    print(json.dumps([(family, address) for family, _, _, _, address in answers]))
except (MemoryError, OSError, ValueError):
    sys.exit(1)
'''
HOSTNAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
                      r'(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', re.ASCII)
REQUEST_LINE = re.compile(rb'CONNECT ([A-Za-z0-9.-]+):443 HTTP/1\.1')
HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
EMBEDDED_IPV4 = tuple(map(ipaddress.IPv6Network, (
    '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '::/96',
)))


class Denied(Exception):
    def __init__(self, reason, status=b'403 Forbidden'):
        super().__init__(reason)
        self.status = status


def hostname(value):
    if not value.isascii():
        return None
    name = value.lower()
    return name if len(name) <= 253 and HOSTNAME.fullmatch(name) and not re.fullmatch(r'[0-9.]+', name) else None


def allowlist(extra):
    if not isinstance(extra, list) or any(not isinstance(name, str) or not hostname(name) for name in extra):
        raise OSError('worker_egress_allow must be a list of exact hostnames')
    return (*DEFAULT_ALLOW, *(hostname(name) for name in extra))


def limits(config):
    result = {}
    for key, default in (('max_bytes', MAX_BYTES), ('max_seconds', MAX_SECONDS),
                         ('max_connections', MAX_CONNECTIONS)):
        setting = f'worker_egress_{key}'
        value = config.get(setting, default)
        if type(value) is not int or value <= 0:
            raise OSError(f'{setting} must be a positive integer')
        result[key] = value
    return result


def local_interfaces():
    """Read interface addresses and their subnet masks without network probes."""
    class IfAddr(ctypes.Structure):
        pass

    head = ctypes.POINTER(IfAddr)
    IfAddr._fields_ = [('next', head), ('name', ctypes.c_char_p), ('flags', ctypes.c_uint),
                       ('addr', ctypes.c_void_p), ('netmask', ctypes.c_void_p)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getifaddrs.argtypes = [ctypes.POINTER(head)]
    libc.freeifaddrs.argtypes = [head]
    libc.freeifaddrs.restype = None
    first = head()
    if libc.getifaddrs(ctypes.byref(first)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    addresses, subnets = set(), set()
    try:
        current = first
        while current:
            interface = current.contents
            if interface.addr:
                family = ctypes.c_ushort.from_address(interface.addr).value
                if family in (socket.AF_INET, socket.AF_INET6):
                    offset, length = (4, 4) if family == socket.AF_INET else (8, 16)
                    address = ipaddress.ip_address(ctypes.string_at(interface.addr + offset, length))
                    mapped = getattr(address, 'ipv4_mapped', None)
                    addresses.add(mapped or address)
                    if interface.netmask and ctypes.c_ushort.from_address(interface.netmask).value == family:
                        mask = int.from_bytes(ctypes.string_at(interface.netmask + offset, length), 'big')
                        prefix = mask.bit_count()
                        if prefix:
                            subnets.add(ipaddress.ip_network((address, prefix), strict=False))
                        if mapped is not None and prefix > 96:
                            subnets.add(ipaddress.ip_network((mapped, prefix - 96), strict=False))
            current = interface.next
    finally:
        libc.freeifaddrs(first)
    return addresses, subnets


def public_address(value, local):
    address = ipaddress.ip_address(value.split('%', 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in prefix for prefix in EMBEDDED_IPV4):
            return False
        address = address.ipv4_mapped or address
    addresses, subnets = local
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not getattr(address, 'is_site_local', False) and address not in addresses
            and not any(address in subnet for subnet in subnets))


def finish_connect(upstream, error, deadline):
    if error in (errno.EINPROGRESS, errno.EINTR):
        with selectors.DefaultSelector() as ready:
            ready.register(upstream, selectors.EVENT_WRITE)
            if not ready.select(remaining(deadline)):
                raise TimeoutError
        error = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if error:
        raise OSError(error, os.strerror(error))


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def read_resolver_output(fd, deadline):
    output = bytearray()
    with selectors.DefaultSelector() as ready:
        ready.register(fd, selectors.EVENT_READ)
        while ready.select(remaining(deadline)):
            block = os.read(fd, min(4096, DNS_IPC_LIMIT + 1 - len(output)))
            if not block:
                return output
            output.extend(block)
            if len(output) > DNS_IPC_LIMIT:
                raise Denied('DNS result exceeds IPC limit')
    raise TimeoutError


def resolve_host(host, deadline, slots):
    if not slots.acquire(blocking=False):
        raise Denied('DNS capacity reached')
    dns_deadline = time.monotonic() + DNS_TIMEOUT
    until = min(dns_deadline, deadline)
    try:
        remaining(until)
        command = [sys.executable, '-I', '-S', '-c', RESOLVER_SCRIPT, host,
                   str(until), str(DNS_ANSWER_LIMIT)]
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as child:
            try:
                output = read_resolver_output(child.stdout.fileno(), until)
                status = child.wait(timeout=remaining(until))
            finally:
                if child.returncode is None:
                    child.kill()
        if status == -signal.SIGALRM:
            raise TimeoutError
        if status == 2:
            raise Denied('DNS answer count exceeds limit')
        return json.loads(output) if status == 0 else ()
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        if deadline <= dns_deadline or time.monotonic() >= deadline:
            raise TimeoutError from error
        raise Denied('DNS resolution deadline exceeded') from error
    finally:
        slots.release()


def collect_fixture_answers(resolver, patterns, fd, deadline):
    # The sandbox self-test supplies a local callback. Run it before admission
    # in a bounded child so connection handlers only read precomputed answers.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - time.monotonic()))
    answers = {host: resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
               for host in patterns}
    if any(len(rows) > DNS_ANSWER_LIMIT for rows in answers.values()):
        return
    with os.fdopen(fd, 'wb', closefd=False) as stream:
        stream.write(json.dumps(answers).encode('utf-8'))


def fixture_answers(resolver, patterns):
    from multiprocessing import get_context

    context = get_context('fork')  # The trusted self-test callback closes over its fixture address.
    deadline = time.monotonic() + DNS_TIMEOUT
    receive, send = context.Pipe(duplex=False)
    with receive, send:
        child = context.Process(target=collect_fixture_answers,
                                args=(resolver, tuple(host + '.' for host in patterns), send.fileno(), deadline))
        child.start()
        try:
            send.close()
            output = read_resolver_output(receive.fileno(), deadline)
            child.join(remaining(deadline))
            if child.exitcode != 0:
                raise OSError('egress resolver fixture failed')
            return json.loads(output)
        except (Denied, TimeoutError, ValueError) as error:
            raise OSError('egress resolver fixture failed') from error
        finally:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()


def read_request(client, deadline, audit):
    data = bytearray()
    try:
        while len(data) < HEADER_LIMIT:
            client.settimeout(remaining(deadline))
            block = client.recv(min(4096, HEADER_LIMIT - len(data)))
            if not block:
                raise Denied('truncated CONNECT headers', b'400 Bad Request')
            data.extend(block)
            header, separator, pending = data.partition(b'\r\n\r\n')
            if separator:
                break
        else:
            raise Denied('CONNECT headers exceed 8 KiB', b'431 Request Header Fields Too Large')
    except TimeoutError as error:
        raise Denied('CONNECT header deadline exceeded', b'408 Request Timeout') from error
    finally:
        # Keep the proposed authority for malformed and timed-out CONNECTs too.
        parts = data.partition(b'\r\n')[0].split(None, 2)
        if len(parts) >= 2 and parts[0].upper() == b'CONNECT':
            authority = parts[1].rsplit(b'@', 1)[-1].split(b'/', 1)[0][:255].decode('ascii', errors='replace')
            name, _, port = authority.rpartition(':')
            audit.update(authority=authority, host=hostname(name or authority), port=port or None)

    line, *headers = header.split(b'\r\n')
    match = REQUEST_LINE.fullmatch(line)
    if not match:
        if not line.upper().startswith(b'CONNECT'):
            raise Denied('CONNECT required', b'405 Method Not Allowed')
        raise Denied('invalid CONNECT request line')
    host = hostname(match[1].decode('ascii'))
    if host is None:
        raise Denied('invalid CONNECT hostname')
    seen_host = False
    for line in headers:
        name, separator, value = line.partition(b':')
        if not separator or not HEADER_NAME.fullmatch(name) or any(
                byte < 32 and byte != 9 or byte == 127 for byte in value):
            raise Denied('invalid CONNECT header', b'400 Bad Request')
        if name.lower() == b'host':
            if seen_host or value.strip(b' \t').lower() != match[1].lower() + b':443':
                raise Denied('Host header does not match CONNECT authority')
            seen_host = True
    return host, pending


class TLSFields:
    """Length-prefixed fields in one bounded TLS handshake."""
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, count):
        end = self.offset + count
        if end > len(self.data):
            raise Denied('malformed TLS ClientHello')
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def number(self, size):
        return int.from_bytes(self.take(size), 'big')

    def vector(self, size):
        return self.take(self.number(size))

    def __bool__(self):
        return self.offset < len(self.data)


def hello_hostname(body):
    hello = TLSFields(body)
    if hello.take(2) not in (b'\x03\x01', b'\x03\x02', b'\x03\x03'):
        raise Denied('invalid ClientHello version')
    hello.take(32)
    session_id, ciphers, compression = (hello.vector(1), hello.vector(2), hello.vector(1))
    if len(session_id) > 32 or not ciphers or len(ciphers) % 2 or not compression:
        raise Denied('malformed TLS ClientHello')
    if not hello:
        raise Denied('TLS ClientHello is missing SNI')
    extensions = TLSFields(hello.vector(2))
    if hello:
        raise Denied('malformed TLS ClientHello')
    seen, sni = set(), None
    while extensions:
        kind = extensions.number(2)
        data = extensions.vector(2)
        if kind in seen:
            raise Denied('duplicate TLS extension')
        seen.add(kind)
        if kind in (0xfe0d, 0xffce):
            raise Denied('encrypted ClientHello is unsupported')
        if kind == 0x0015 and any(data):
            raise Denied('invalid TLS padding')
        if kind == 0:
            extension = TLSFields(data)
            names = TLSFields(extension.vector(2))
            if extension or names.number(1) != 0:
                raise Denied('invalid TLS SNI')
            sni = hostname(names.vector(2).decode('ascii', errors='replace'))
            if not sni or names:
                raise Denied('invalid TLS SNI')
    if sni is None:
        raise Denied('TLS ClientHello is missing SNI')
    return sni


def read_client_hello(client, pending, deadline):
    wire, handshake = bytearray(pending), bytearray()

    def fill(size):
        if size > HELLO_WIRE_LIMIT:
            raise Denied('TLS ClientHello exceeds wire limit')
        while len(wire) < size:
            client.settimeout(remaining(deadline))
            block = client.recv(size - len(wire))
            if not block:
                raise Denied('truncated TLS ClientHello')
            wire.extend(block)

    offset = 0
    try:
        for _ in range(HELLO_RECORD_LIMIT):
            fill(offset + 1)
            if wire[offset] != 22:
                raise Denied('expected a TLS ClientHello record')
            fill(offset + 5)
            header = wire[offset:offset + 5]
            size = int.from_bytes(header[3:], 'big')
            if header[1] != 3 or header[2] > 3 or not 0 < size <= TLS_RECORD_LIMIT:
                raise Denied('invalid TLS record header')
            end = offset + 5 + size
            fill(end)
            handshake.extend(wire[offset + 5:end])
            if handshake[0] != 1:
                raise Denied('first TLS handshake is not ClientHello')
            if len(handshake) >= 4:
                total = 4 + int.from_bytes(handshake[1:4], 'big')
                if total > HELLO_LIMIT:
                    raise Denied('TLS ClientHello exceeds 16 KiB')
                if len(handshake) > total:
                    raise Denied('trailing bytes in TLS ClientHello record')
                if len(handshake) == total:
                    return hello_hostname(handshake[4:]), wire
            offset = end
        raise Denied('TLS ClientHello spans too many records')
    except TimeoutError as error:
        raise Denied('TLS ClientHello deadline exceeded') from error


def relay(client, upstream, pending, server, audit, deadline):
    idle = time.monotonic() + server.idle_timeout

    def forward(destination, data):
        nonlocal idle
        permitted = server.reserve(len(data))
        sent = 0
        try:
            while sent < permitted:
                destination.settimeout(remaining(min(idle, deadline)))
                count = destination.send(memoryview(data)[sent:permitted])
                if not count:
                    raise ConnectionError('tunnel peer closed')
                sent += count
                audit['bytes'] += count
                idle = time.monotonic() + server.idle_timeout
        finally:
            server.refund(permitted - sent)
        return permitted == len(data) and not server.byte_cap_reached()

    try:
        if not forward(upstream, pending):
            return 'per-run byte cap'
        with selectors.DefaultSelector() as ready:
            ready.register(client, selectors.EVENT_READ, upstream)
            ready.register(upstream, selectors.EVENT_READ, client)
            while ready.get_map():
                until = min(idle, deadline)
                events = ready.select(remaining(until))
                if not events:
                    raise TimeoutError
                for key, _ in events:
                    key.fileobj.settimeout(remaining(min(idle, deadline)))
                    data = key.fileobj.recv(65536)
                    if data:
                        if not forward(key.data, data):
                            return 'per-run byte cap'
                    else:
                        ready.unregister(key.fileobj)
                        key.data.shutdown(socket.SHUT_WR)
    except TimeoutError:
        return 'connection duration cap' if deadline <= idle else 'tunnel idle timeout'
    return 'tunnel closed'


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server, client = self.server, self.request
        audit = server.audit()
        reply = True
        deadline = time.monotonic() + server.max_seconds
        with server.track(client) as active:
            try:
                if not active:
                    raise Denied('proxy stopping')
                host, pending = read_request(client, min(deadline, time.monotonic() + HEADER_TIMEOUT), audit)
                if host not in server.patterns:
                    raise Denied('host is not allowlisted')
                if server.byte_cap_reached():
                    raise Denied('per-run byte cap')
                candidates = server.destinations(host, deadline)
                if not candidates:
                    raise Denied('DNS returned no public, nonlocal destinations')
                client.settimeout(remaining(deadline))
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                reply = False
                sni, pending = read_client_hello(client, pending, min(deadline, time.monotonic() + HELLO_TIMEOUT))
                audit['sni'] = sni
                if sni != host:
                    raise Denied('TLS SNI does not match CONNECT host')
                with server.connect(candidates, deadline) as upstream:
                    if upstream is None:
                        raise Denied('upstream connection failed')
                    # HTTP Host inside TLS and HTTP/2 coalescing remain possible.
                    # Egress v2 separates the model transport and worker credentials.
                    audit['allowed'] = True
                    audit['reason'] = relay(client, upstream, pending, server, audit, deadline)
            except Denied as error:
                audit['reason'] = str(error)
                if reply:
                    with suppress(OSError):
                        client.settimeout(min(1, remaining(deadline)))
                        client.sendall(b'HTTP/1.1 ' + error.status + b'\r\n\r\n')
            except TimeoutError:
                audit['reason'] = 'connection duration cap'
            except OSError:
                audit['reason'] = 'connection interrupted'
            finally:
                server.log(audit)


class Proxy(socketserver.UnixStreamServer):
    def __init__(self, path, log_path, patterns, *, resolve=None, connect=None,
                 max_bytes=MAX_BYTES, max_seconds=MAX_SECONDS, max_connections=MAX_CONNECTIONS,
                 idle_timeout=IDLE_TIMEOUT):
        self.log_path = log_path
        self.patterns = patterns
        self.answers = fixture_answers(resolve, patterns) if callable(resolve) else resolve
        self.dns_slots = threading.BoundedSemaphore(DNS_CONCURRENCY)
        self.dial = connect
        self.lock = threading.Lock()
        self.poll_gate = threading.Condition(self.lock)
        self.polling = False
        self.waiting_dials = 0
        self.connections = set()
        self.handlers = set()
        self.stopping = False
        self.listener_ready = threading.Event()
        self.listener_error = None
        self.listener_wakeup = None
        self.bytes_left = max_bytes
        self.connections_left = max_connections
        self.connection_cap_logged = False
        self.audit_truncated = False
        self.audit_error = None
        self.max_seconds = max_seconds
        self.idle_timeout = idle_timeout
        self.denials = {}
        self.audit_flush_at = time.monotonic() + AUDIT_FLUSH_INTERVAL
        super().__init__(path, ConnectHandler)

    @staticmethod
    def audit():
        return {'time': time.time(), 'host': None, 'authority': None, 'port': None,
                'allowed': False, 'bytes': 0, 'reason': 'proxy unavailable'}

    def listen(self):
        try:
            with self.socket, selectors.DefaultSelector() as ready, ExitStack() as wakeup:
                read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
                wakeup.callback(os.close, read_fd)
                wakeup.callback(os.close, write_fd)
                self.socket.setblocking(False)
                ready.register(read_fd, selectors.EVENT_READ)
                ready.register(self.socket, selectors.EVENT_READ)
                with self.lock:
                    self.listener_wakeup = write_fd
                failure = None
                try:
                    self.listener_ready.set()
                    self.accept_loop(ready)
                except BaseException as error:
                    failure = error
                with self.lock:
                    self.listener_wakeup = None
                    if failure is not None:
                        self.fail_listener(failure)
                    self.retire_listener(ready)
                    self.flush_denials()
        except BaseException as error:
            with self.lock:
                self.fail_listener(error)
        finally:
            self.listener_ready.set()

    def fail_listener(self, error):
        # Call with self.lock held. Preserve the original failure during cleanup.
        if self.listener_error is None:
            self.listener_error = error
            self.stop_connections()

    def retire_listener(self, ready):
        # Call with self.lock held.
        if self.socket.fileno() >= 0:
            try:
                ready.unregister(self.socket)
                self.server_close()
            except BaseException as error:
                self.fail_listener(error)
                raise

    def accept_loop(self, ready):
        while True:
            with self.lock:
                # Let queued dialers run before starting another bounded poll.
                self.poll_gate.wait_for(lambda: not self.waiting_dials)
                if self.stopping or self.socket.fileno() < 0:
                    return
                self.polling = True
            accepted = failure = None
            try:
                if any(key.fileobj is self.socket for key, _ in ready.select(0.05)):
                    accepted = self.accept_request()
            except BaseException as error:
                failure = error
                raise
            finally:
                with self.lock:
                    if failure is not None:
                        self.fail_listener(failure)
                    self.polling = False
                    self.poll_gate.notify_all()
            if accepted is not None:
                request, address = accepted
                try:
                    self.process_request(request, address, ready)
                except BaseException:
                    self.shutdown_request(request)
                    raise
            self.service_actions()

    def accept_request(self):
        try:
            return self.get_request()
        except OSError as error:
            if error.errno in (errno.EMFILE, errno.ENFILE):
                raise OSError(error.errno, 'egress accept descriptor exhaustion') from error
            if error.errno in (errno.EAGAIN, errno.EINTR, errno.ECONNABORTED):
                return
            raise

    def process_request(self, request, address, ready):
        with self.lock:
            if self.stopping:
                reason = 'proxy stopping'
            elif self.connections_left:
                self.connections_left -= 1
                self.handlers = {thread for thread in self.handlers if thread.is_alive()}
                if len(self.handlers) < CONCURRENCY:
                    try:
                        thread = threading.Thread(target=self.handle_request, args=(request, address), daemon=True)
                        thread.start()
                    except BaseException as error:
                        self.connections_left += 1
                        self.fail_listener(error)
                        self.write_audit(dict(self.audit(), reason=DISPATCH_FAILURE))
                        raise
                    self.handlers.add(thread)
                    return
                reason = 'tunnel capacity reached'
            else:
                reason = CONNECTION_CAP
                with suppress(OSError):
                    request.setblocking(False)
                    request.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                self.write_audit(dict(self.audit(), reason=reason))
                self.retire_listener(ready)
        if reason != CONNECTION_CAP:
            self.log(dict(self.audit(), reason=reason))
        self.shutdown_request(request)

    def handle_request(self, request, address):
        try:
            self.finish_request(request, address)
        finally:
            self.shutdown_request(request)

    @contextmanager
    def track(self, connection):
        with self.lock:
            active = not self.stopping
            if active:
                self.connections.add(connection)
        try:
            yield active
        finally:
            with self.lock:
                self.connections.discard(connection)

    def reserve(self, requested):
        with self.lock:
            permitted = min(requested, self.bytes_left)
            self.bytes_left -= permitted
        return permitted

    def refund(self, count):
        with self.lock:
            self.bytes_left += count

    def byte_cap_reached(self):
        with self.lock:
            return self.bytes_left == 0

    def stop_connections(self):
        # Call with self.lock held.
        self.stopping = True
        if self.listener_wakeup is not None:
            with suppress(BlockingIOError):
                os.write(self.listener_wakeup, b'\0')
        for connection in self.connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def disconnect(self):
        with self.lock:
            self.stop_connections()
            handlers = list(self.handlers)
        deadline = time.monotonic() + max(DNS_TIMEOUT, CONNECT_TIMEOUT) + 1
        for thread in handlers:
            thread.join(max(0, deadline - time.monotonic()))
        with self.lock:
            self.flush_denials()
        return not any(thread.is_alive() for thread in handlers)

    def destinations(self, host, deadline):
        try:
            if self.answers is None:
                answers = resolve_host(host + '.', deadline, self.dns_slots)
            else:
                answers = self.answers.get(host + '.', ())
            if len(answers) > DNS_ANSWER_LIMIT:
                raise Denied('DNS answer count exceeds limit')
            if self.answers is not None:
                answers = [(family, address) for family, _, _, _, address in answers]
            with self.lock:
                if self.stopping:
                    raise Denied('proxy stopping')
            candidates = [(family, tuple(address)) for family, address in answers]
            local = local_interfaces()
            if all(family in (socket.AF_INET, socket.AF_INET6) and public_address(address[0], local)
                   for family, address in candidates):
                return list(dict.fromkeys(candidates))
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass
        return []

    @contextmanager
    def connect(self, candidates, deadline):
        deadline = min(deadline, time.monotonic() + CONNECT_TIMEOUT)
        for family, address in candidates:
            if not public_address(address[0], local_interfaces()):
                raise Denied('upstream destination is no longer public and nonlocal')
            upstream = None
            try:
                if self.dial is None:
                    upstream = socket.socket(family, socket.SOCK_STREAM)
                    upstream.setblocking(False)
                with self.lock:
                    if self.polling:
                        self.waiting_dials += 1
                        try:
                            self.poll_gate.wait_for(lambda: not self.polling)
                        finally:
                            self.waiting_dials -= 1
                            self.poll_gate.notify_all()
                    if self.stopping:
                        raise Denied('proxy stopping')
                    timeout = remaining(deadline)
                    if upstream is None:  # Trusted self-test supplies an already connected socket.
                        upstream = self.dial(family, address, timeout)
                        self.connections.add(upstream)
                        error = 0
                    else:
                        self.connections.add(upstream)
                        # Use the checked numeric sockaddr without another DNS lookup.
                        error = upstream.connect_ex(address)
                finish_connect(upstream, error, deadline)
                with self.lock:
                    if self.stopping:
                        raise Denied('proxy stopping')
            except OSError as error:
                with self.lock:
                    if self.stopping:
                        raise Denied('proxy stopping') from error
                if isinstance(error, TimeoutError):
                    break
            else:
                yield upstream
                return
            finally:
                if upstream is not None:
                    with self.lock:
                        self.connections.discard(upstream)
                    upstream.close()
        yield None

    def log(self, audit):
        with self.lock:
            if self.audit_error is not None:
                return
            if audit['allowed'] or audit['reason'] == CONNECTION_CAP or self.stopping or self.socket.fileno() < 0:
                self.write_audit(audit)
            else:
                key = audit['host'], audit['reason']
                if key in self.denials:
                    self.denials[key]['count'] += 1
                else:
                    if len(self.denials) == AUDIT_DENIAL_LIMIT:
                        self.flush_denials()
                    if self.audit_error is None:
                        self.denials[key] = dict(audit, count=1)

    def service_actions(self):
        with self.lock:
            if time.monotonic() >= self.audit_flush_at:
                self.flush_denials()

    def flush_denials(self):
        # Call with self.lock held. Retain the first authority and time in each batch.
        for audit in self.denials.values():
            self.write_audit(audit)
        self.denials.clear()
        self.audit_flush_at = time.monotonic() + AUDIT_FLUSH_INTERVAL

    def write_audit(self, audit):
        budget = audit['reason'] == CONNECTION_CAP
        dispatch = audit['reason'] == DISPATCH_FAILURE
        if self.audit_error is not None or (budget and self.connection_cap_logged):
            return
        replacing = self.audit_truncated
        if replacing and not (budget or dispatch):
            return
        line = (json.dumps(audit) + '\n').encode('utf-8')
        truncated = replacing
        reserve = len(AUDIT_TRUNCATION)
        if not self.connection_cap_logged:
            reserve += AUDIT_TERMINAL_RESERVE
        try:
            with self.log_path.open('rb' if replacing else 'ab', buffering=0) as stream:
                if replacing:
                    stream.seek(-len(AUDIT_TRUNCATION), os.SEEK_END)
                size = stream.tell()
                if budget and size + len(line) + len(AUDIT_TRUNCATION) > AUDIT_LIMIT:
                    line = AUDIT_BUDGET_TRUNCATION
                    truncated = True
                elif replacing or (not budget and size + len(line) + reserve > AUDIT_LIMIT):
                    line = (line if budget or dispatch else b'') + AUDIT_TRUNCATION
                    truncated = True
                if size + len(line) > AUDIT_LIMIT:
                    raise OSError('egress audit record could not be written in full')
                if replacing:
                    stream.seek(0)
                    prefix = stream.read(size)
                    if len(prefix) != size:
                        raise OSError('egress audit prefix could not be read in full')
                    self.replace_audit(prefix + line)
                elif stream.write(line) != len(line):
                    raise OSError('egress audit record could not be written in full')
            if budget:
                self.connection_cap_logged = True
            self.audit_truncated = truncated
        except OSError as error:
            self.audit_error = error
            self.stop_connections()

    def replace_audit(self, content):
        sibling = tempfile.NamedTemporaryFile(dir=self.log_path.parent, buffering=0, delete=False)
        try:
            with sibling:
                os.fchmod(sibling.fileno(), 0o600)
                if sibling.write(content) != len(content):
                    raise OSError('egress audit record could not be written in full')
            os.replace(sibling.name, self.log_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(sibling.name)


@contextmanager
def blocked_cleanup_signals():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def lock_outbox(outbox):
    fd = os.open(outbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise OSError('worker outbox must be owned by the launcher user with mode 0700')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def unlink_stale_socket(fd):
    with suppress(FileNotFoundError):
        info = os.stat(SOCKET_NAME, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISSOCK(info.st_mode):
            os.unlink(SOCKET_NAME, dir_fd=fd)


def prune_stale_sockets(runs):
    for outbox in Path(runs).glob('*-out'):
        try:
            fd = lock_outbox(outbox)
        except OSError:
            continue
        try:
            unlink_stale_socket(fd)
        finally:
            os.close(fd)


@contextmanager
def serve(outbox, log_path, patterns=DEFAULT_ALLOW, **transport):
    """Run the listener in the launcher; its descriptors die with the launcher."""
    socket_path = Path(outbox) / SOCKET_NAME
    log_path = Path(log_path)
    fd = lock_outbox(outbox)
    server = listener = None
    try:
        unlink_stale_socket(fd)
        # Linux limits sockaddr_un length. The directory FD also anchors cleanup.
        short_path = f'/proc/self/fd/{fd}/{SOCKET_NAME}'
        with blocked_cleanup_signals():
            log_path.touch(mode=0o600)
            with log_path.open('ab', buffering=0):
                pass
            old_umask = os.umask(0o177)
            try:
                server = Proxy(short_path, log_path, patterns, **transport)
            finally:
                os.umask(old_umask)
            os.chmod(SOCKET_NAME, 0o600, dir_fd=fd)
            listener = threading.Thread(target=server.listen, daemon=True)
            listener.start()
        server.listener_ready.wait()
        if server.listener_error is not None:
            raise OSError(f'egress listener failed: {server.listener_error}') from server.listener_error
        yield socket_path
    finally:
        with blocked_cleanup_signals():
            try:
                if server is not None:
                    if listener is None or listener.ident is None:
                        server.server_close()
                    drained = server.disconnect()
                    if listener is not None and listener.ident is not None:
                        listener.join(1)
                    if server.audit_error is not None:
                        raise OSError(f'egress audit failed: {server.audit_error}') from server.audit_error
                    if server.listener_error is not None:
                        raise OSError(f'egress listener failed: {server.listener_error}') from server.listener_error
                    if not drained or (listener is not None and listener.is_alive()):
                        raise OSError('egress proxy shutdown deadline exceeded')
            finally:
                try:
                    unlink_stale_socket(fd)
                finally:
                    os.close(fd)
