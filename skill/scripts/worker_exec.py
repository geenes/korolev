"""Install the worker's Linux socket filter, then replace this process."""
import ctypes
import errno
import os
import platform
import socket
import sys


# Audit arch, socket, socketpair, ptrace, process_vm_readv, process_vm_writev.
# io_uring and pidfd_getfd share syscall numbers on both ABIs.
SYSCALLS = {
    'x86_64': (0xC000003E, 41, 53, 101, 310, 311),
    'aarch64': (0xC00000B7, 198, 199, 117, 270, 271),
}
IO_URING_CALLS = (425, 426, 427)  # setup, enter, register
PIDFD_GETFD = 438
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
LD_WORD_ABS = 0x20
JUMP_EQUAL = 0x15
JUMP_GREATER = 0x25
JUMP_AT_LEAST = 0x35
JUMP_BITS_SET = 0x45
AND_CONSTANT = 0x54
RETURN = 0x06
KILL_PROCESS = 0x80000000
ALLOW = 0x7FFF0000
DENY = 0x00050000 | errno.EACCES
SOCKET_TYPE_MASK = 0xffffffff ^ (socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)


class Instruction(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]


class Filter(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(Instruction))]


def socket_filter(machine):
    arch, socket_nr, socketpair_nr, *process_calls = SYSCALLS[machine]
    rules = [
        (LD_WORD_ABS, 0, 0, 4),               # seccomp_data.arch
        (JUMP_EQUAL, 1, 0, arch),
        (RETURN, 0, 0, KILL_PROCESS),
        (LD_WORD_ABS, 0, 0, 0),               # seccomp_data.nr
        (JUMP_BITS_SET, 0, 1, 0x40000000),   # x32 syscall bit
        (RETURN, 0, 0, KILL_PROCESS),
    ]
    if machine == 'x86_64':
        # Older kernels also accept untagged x32 syscall numbers in this range.
        rules.extend(((JUMP_AT_LEAST, 0, 2, 512), (JUMP_GREATER, 1, 0, 547),
                      (RETURN, 0, 0, DENY)))
    # The bridge stays unfiltered. Block direct access to other processes'
    # memory and descriptors, alongside the read-only procfs in its sandbox.
    for number in (*IO_URING_CALLS, PIDFD_GETFD, *process_calls):
        rules.extend(((JUMP_EQUAL, 0, 1, number), (RETURN, 0, 0, DENY)))
    # INET/INET6: stream/TCP and datagram/UDP, including protocol zero.
    inet_rules = (
        (LD_WORD_ABS, 0, 0, 24),              # socket type, args[1]
        (AND_CONSTANT, 0, 0, SOCKET_TYPE_MASK),
        (JUMP_EQUAL, 0, 5, socket.SOCK_STREAM),
        (LD_WORD_ABS, 0, 0, 32),              # socket protocol, args[2]
        (JUMP_EQUAL, 1, 0, socket.IPPROTO_TCP),
        (JUMP_EQUAL, 0, 1, 0),
        (RETURN, 0, 0, ALLOW),
        (RETURN, 0, 0, DENY),
        (JUMP_EQUAL, 0, 4, socket.SOCK_DGRAM),
        (LD_WORD_ABS, 0, 0, 32),
        (JUMP_EQUAL, 1, 0, socket.IPPROTO_UDP),
        (JUMP_EQUAL, 0, 1, 0),
        (RETURN, 0, 0, ALLOW),
        (RETURN, 0, 0, DENY),
    )
    socket_rules = (
        (LD_WORD_ABS, 0, 0, 16),              # low word of seccomp_data.args[0]
        (JUMP_EQUAL, 1, 0, socket.AF_INET),
        (JUMP_EQUAL, 0, len(inet_rules), socket.AF_INET6),
        *inet_rules,
        (JUMP_EQUAL, 0, 3, socket.AF_NETLINK),
        (LD_WORD_ABS, 0, 0, 32),
        (JUMP_EQUAL, 0, 1, socket.NETLINK_ROUTE),
        (RETURN, 0, 0, ALLOW),
        (RETURN, 0, 0, DENY),
    )
    # Keep unnamed stream pairs. A datagram pair can address pathname sockets.
    pair_rules = (
        (LD_WORD_ABS, 0, 0, 16),              # socketpair domain
        (JUMP_EQUAL, 0, 3, socket.AF_UNIX),
        (LD_WORD_ABS, 0, 0, 24),              # socketpair type
        (AND_CONSTANT, 0, 0, SOCKET_TYPE_MASK),
        (JUMP_EQUAL, 1, 0, socket.SOCK_STREAM),
        (RETURN, 0, 0, DENY),
    )
    return [*rules, (JUMP_EQUAL, 0, len(socket_rules), socket_nr), *socket_rules,
            (JUMP_EQUAL, 0, len(pair_rules), socketpair_nr), *pair_rules, (RETURN, 0, 0, ALLOW)]


def install_socket_filter():
    machine = platform.machine()
    bits = 8 * ctypes.sizeof(ctypes.c_void_p)
    if machine not in SYSCALLS or bits != 64 or sys.byteorder != 'little':
        raise OSError(f'unsupported syscall architecture for worker socket isolation: {machine}, {bits}-bit')
    rules = socket_filter(machine)
    instructions = (Instruction * len(rules))(*rules)
    program = Filter(len(instructions), instructions)
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) or prctl(
            PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(program), 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def main():
    try:
        install_socket_filter()
        cwd, count, *args = sys.argv[1:]
        count = int(count)
        env = dict(zip(args[:count], args[count:2 * count]))
        command = args[2 * count:]
        os.chdir(cwd)
        os.execvpe(command[0], command, env)
    except OSError as error:
        print(f'worker socket isolation could not start the command: {error}', file=sys.stderr)
        return 78


if __name__ == '__main__':
    sys.exit(main())
