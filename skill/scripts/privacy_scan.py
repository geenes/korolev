#!/usr/bin/env python3
"""Block personal or private information before Korolev pushes anything public.

  privacy_scan.py [--repo PATH] [--range BASE..HEAD] [--json]
  privacy_scan.py --text [--json] < rendered-payload

Default range: origin/main..HEAD. Scans newly reachable commits, annotated tags,
paths and complete blobs. Use --text to scan rendered UTF-8 bytes from stdin.
It looks for real email addresses, home-directory paths (/home/<user>, /Users/<user>),
secrets and tokens, and the private terms in ~/.config/korolev/privacy.json:
  {"deny": ["my-private-project", "old.name@example.org"], "allow_emails": ["bot@localhost"]}
That file stays outside the repo, so the private word list is never published.
Findings are printed redacted. Exit 1 if anything is found, 0 if clean.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG_DIR, trusted_git_executable  # noqa: E402

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
SAFE_EMAIL = re.compile(r'(@users\.noreply\.github\.com$|@example\.(com|org|net|test)$|@[a-z0-9.-]+\.test$|^noreply@anthropic\.com$|^korolev@localhost$|^git@github\.com$)', re.I)
HOME_PATH = re.compile(r'(/home/(?!runner\b|user\b|<)[a-z_][a-z0-9_-]*|/Users/(?!<)[A-Za-z][A-Za-z0-9_-]*)')
SECRETS = [
    ('openai key', re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{20,}')),
    ('github token', re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}')),
    ('aws key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('private key', re.compile(r'BEGIN [A-Z ]*PRIVATE KEY')),
    ('telegram bot token', re.compile(r'\b\d{8,10}:AA[A-Za-z0-9_-]{30,}')),
    ('assigned secret', re.compile(r'(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*["\'][A-Za-z0-9_./+=-]{16,}["\']')),
]


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def redact(text):
    text = text.strip()
    return (text[:3] + '…' + text[-2:]) if len(text) > 8 else '…'


def load_config():
    path = CONFIG_DIR / 'privacy.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    deny = [t.lower() for t in data.get('deny', []) if t.strip()]
    allow = {e.lower() for e in data.get('allow_emails', [])}
    return deny, allow


def check_text(text, where, deny, allow, findings):
    for match in EMAIL.findall(text):
        if not SAFE_EMAIL.search(match) and match.lower() not in allow:
            findings.append((where, 'email', redact(match)))
    for match in HOME_PATH.findall(text):
        findings.append((where, 'home path', redact(match)))
    for name, pattern in SECRETS:
        for match in pattern.findall(text):
            findings.append((where, name, redact(match if isinstance(match, str) else match[0])))
    low = text.lower()
    for term in deny:
        if term in low:
            findings.append((where, 'private term', redact(term)))


OBJECT_ENV = {'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_GRAFT_FILE': os.devnull, 'GIT_NO_LAZY_FETCH': '1'}
OBJECT_LIMIT = 20 * 1024 * 1024
TOTAL_LIMIT = 500 * 1024 * 1024
OPAQUE_BLOB_PREFIXES = (b'PK\x03\x04', b'PK\x05\x06', b'\x1f\x8b', b'%PDF-', b'SQLite format 3\0',
                        b'BZh', b'\xfd7zXZ\0', b'\x28\xb5\x2f\xfd', b'7z\xbc\xaf\x27\x1c')
OPAQUE_TEXT = re.compile(r'^[ \t\ufeff]*(?:-{4,} ?BEGIN (?:[A-Z0-9][A-Z0-9 -]*|'
                         r'PGP MESSAGE, PART [0-9]+(?:/[0-9]+)?)-{4,}|'
                         r'begin(?:-base64)? [0-7]{3} [^\r\n]+|GIT binary patch)[ \t]*\r?$', re.M)


class ScanBudgetError(ValueError):
    pass


def scan_git_env():
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')} | OBJECT_ENV | {
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ALLOW_PROTOCOL': '',
    }


def object_git(repo, executable, *args):
    return subprocess.run([executable, *args], cwd=repo, env=scan_git_env(),
                          capture_output=True, check=True).stdout


def git_records(repo, executable, *args, delimiter=b'\n', stdin=None):
    args = [executable, *args]
    with subprocess.Popen(args, cwd=repo, env=scan_git_env(),
                          stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            if delimiter == b'\n':
                for line in process.stdout:
                    yield line.rstrip(b'\n')
            else:
                tail = b''
                while chunk := process.stdout.read(65536):
                    *records, tail = (tail + chunk).split(delimiter)
                    yield from records
                    if len(tail) > OBJECT_LIMIT:
                        raise ScanBudgetError('outgoing path exceeds the 20 MiB per-object budget')
                if tail:
                    raise ValueError('incomplete outgoing Git path list')
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, args)
        finally:
            if process.poll() is None:
                process.kill()


def read_objects(repo, executable, oids):
    args = [executable, 'cat-file', '--batch']
    with subprocess.Popen(args, cwd=repo, env=scan_git_env(),
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        total = 0
        try:
            for oid in oids:
                process.stdin.write(oid + b'\n')
                process.stdin.flush()
                header = process.stdout.readline().split()
                if len(header) != 3 or header[0] != oid:
                    raise ValueError('could not read an outgoing Git object')
                size = int(header[2])
                if size > OBJECT_LIMIT:
                    raise ScanBudgetError(f'object {oid[:12].decode()} exceeds the 20 MiB per-object budget')
                total += size
                if total > TOTAL_LIMIT:
                    raise ScanBudgetError('outgoing objects exceed the 500 MiB total budget')
                body = process.stdout.read(size)
                if len(body) != size or process.stdout.read(1) != b'\n':
                    raise ValueError('incomplete outgoing Git object')
                yield oid.decode('ascii'), header[1], body
            process.stdin.close()
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, args)
        finally:
            if process.poll() is None:
                process.kill()
            process.stdin.close()


def unicode_encoding(body):
    if body.startswith((b'\xff\xfe\0\0', b'\0\0\xfe\xff')):
        return 'utf-32'
    if body.startswith((b'\xff\xfe', b'\xfe\xff')):
        return 'utf-16'
    # ASCII-heavy Unicode text has NULs in its high-byte lanes. Test UTF-32 first.
    sample = body[:4096]
    for width, codec in ((4, 'utf-32'), (2, 'utf-16')):
        if len(sample) < width * 4:
            continue
        lanes = [sample[offset::width] for offset in range(width)]
        rates = [lane.count(0) / len(lane) for lane in lanes]
        if rates[0] < 0.25 and min(rates[1:]) > 0.75:
            return codec + '-le'
        if rates[-1] < 0.25 and min(rates[:-1]) > 0.75:
            return codec + '-be'
    return None


def inspect_text(body, where, deny, allow, findings, *, alternate=False):
    try:
        if alternate and (body.startswith(OPAQUE_BLOB_PREFIXES) or body[257:263] in (b'ustar\0', b'ustar ')):
            raise ValueError
        codec = (unicode_encoding(body) if alternate else None) or 'utf-8'
        text = body.decode(codec)
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', text):
            raise ValueError
    except ValueError:
        findings.append((where, 'opaque content', 'cannot inspect as text'))
        return
    if OPAQUE_TEXT.search(text):
        findings.append((where, 'opaque content', 'cannot inspect as text'))
    check_text(text, where, deny, allow, findings)


def scan_text(body):
    """Scan the exact UTF-8 bytes of a rendered payload before delivery."""
    if len(body) > OBJECT_LIMIT:
        raise ScanBudgetError('rendered text exceeds the 20 MiB budget')
    findings = []
    inspect_text(body, 'rendered text', *load_config(), findings)
    return sorted(set(findings))


def scan_commit_paths(repo, executable, commits, deny, allow, findings):
    blobs = {}
    for where, kind, sample in findings:
        if where.startswith('blob '):
            blobs.setdefault(where[5:], []).append((kind, sample))
    located = set()
    for sha in commits:
        records = git_records(repo, executable, 'diff-tree', '--root', '-m', '-r', '-t', '--no-commit-id',
                              '--no-renames', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none',
                              '--diff-filter=ACMT', '--raw', '--no-abbrev', '-z', sha, delimiter=b'\0')
        with closing(records):
            for header, path in zip(records, records, strict=True):
                fingerprint = hashlib.sha256(path).hexdigest()[:12]
                where = f'commit {sha[:12]} path sha256:{fingerprint}'
                inspect_text(b'/' + path, where, deny, allow, findings)
                oid = header.split()[3].decode('ascii')
                for kind, sample in blobs.get(oid, ()):
                    located.add(f'blob {oid}')
                    findings.append((f'{where} blob {oid[:12]}', kind, sample))
    return [row for row in findings if row[0] not in located]


def add_range_tags(repo, executable, tips):
    tags = {}
    with closing(git_records(repo, executable, 'for-each-ref',
                             '--format=%(objecttype) %(objectname) %(*objecttype) %(*objectname)',
                             'refs/tags')) as refs:
        for ref in refs:
            kind, oid, *target = ref.split()
            if kind == b'tag':
                target_kind, peeled = target
                if target_kind == b'tag':
                    peeled = object_git(repo, executable, 'rev-parse', '--verify', oid.decode() + '^{}').strip()
                tags.setdefault(peeled, []).append(oid)
    if tags:
        tips.seek(0)
        records = git_records(repo, executable, 'rev-list', '--objects', '--no-object-names', '--stdin', stdin=tips)
        with closing(records):
            selected = [tag for oid in records for tag in tags.pop(oid, ())]
        tips.seek(0, os.SEEK_END)
        tips.writelines(tag + b'\n' for tag in selected)


def scan_new_objects(repo, old_oid_or_empty, new_ref, *, executable=None, range_tags=False):
    """Return (commits, findings) for history newly reachable from new_ref."""
    deny, allow = load_config()
    findings, commits = [], []
    executable = executable or trusted_git_executable()
    if object_git(repo, executable, 'rev-parse', '--is-shallow-repository').strip() != b'false':
        raise ValueError('cannot scan outgoing history from a shallow repository')
    new_oid = object_git(repo, executable, 'rev-parse', '--verify', '--end-of-options', new_ref).strip()
    with TemporaryFile() as tips:
        tips.write(new_oid + b'\n')
        if old_oid_or_empty:
            old = object_git(repo, executable, 'rev-parse', '--verify', '--end-of-options', old_oid_or_empty).strip()
            tips.write(b'^' + old + b'\n')
        if range_tags:
            add_range_tags(repo, executable, tips)
        tips.seek(0)
        records = git_records(repo, executable, 'rev-list', '--objects', '--no-object-names', '--stdin', stdin=tips)
        with closing(records), closing(read_objects(repo, executable, records)) as objects:
            for oid, kind, body in objects:
                scan_object(repo, oid, kind, body, deny, allow, commits, findings)
    findings = scan_commit_paths(repo, executable, commits, deny, allow, findings)
    return commits, sorted(set(findings))


def scan_object(repo, oid, kind, body, deny, allow, commits, findings):
    where = f'{kind.decode()} {oid if kind == b"blob" else oid[:12]}'
    if kind == b'commit':
        commits.append(oid)
    if kind in (b'commit', b'tag'):
        header, _, _ = body.partition(b'\n\n')
        for line in header.splitlines():
            field, _, value = line.partition(b' ')
            if field in (b'author', b'committer', b'tagger'):
                match = re.search(rb'<([^<>]*)> [0-9]+ [+-][0-9]{4}$', value)
                email = match[1].decode('utf-8', 'replace') if match else ''
                if not SAFE_EMAIL.search(email) and email.lower() not in allow:
                    findings.append((f'{where} {field.decode()}', 'email', redact(email)))
    if kind == b'tree':
        start = 0
        while start < len(body):
            end = body.index(b'\0', start)
            name = body[start:end].split(b' ', 1)[1]
            inspect_text(name, f'{where} entry', deny, allow, findings)
            start = end + 1 + len(oid) // 2
    else:
        inspect_text(body, where, deny, allow, findings, alternate=kind == b'blob')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--range', default='origin/main..HEAD')
    parser.add_argument('--text', action='store_true', help='scan exact UTF-8 bytes from stdin')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        if args.text:
            commits, findings = [], scan_text(sys.stdin.buffer.read(OBJECT_LIMIT + 1))
        else:
            base, separator, head = args.range.partition('..')
            if not separator:
                parser.error('--range expects BASE..HEAD')
            commits, findings = scan_new_objects(Path(args.repo).resolve(), base, head or 'HEAD', range_tags=True)
    except ScanBudgetError as error:
        sys.exit(str(error))
    except (OSError, ValueError, subprocess.CalledProcessError):
        sys.exit('privacy scan could not inspect the input')
    unique = sorted(set(findings))
    if args.json:
        print(json.dumps({'commits': len(commits), 'clean': not unique,
                          'findings': [{'where': w, 'kind': k, 'sample': s} for w, k, s in unique]}, ensure_ascii=False))
    else:
        for where, kind, sample in unique:
            print(f'BLOCK {kind:18} {where}: {sample}')
        print(f"{'clean' if not unique else 'NOT clean'}: {len(commits)} commit(s) scanned, {len(unique)} finding(s)")
    sys.exit(1 if unique else 0)


if __name__ == '__main__':
    main()
