#!/usr/bin/env python3
"""Block personal or private information before Korolev pushes anything public.

  privacy_scan.py [--repo PATH] [--range BASE..HEAD] [--json]

Default range: origin/main..HEAD. It checks:
  - author and committer emails of every commit in the range: only GitHub noreply
    addresses and addresses listed in allow_emails pass;
  - commit messages and the messages of tags pointing into the range;
  - every added or changed line in the range's diff.
It looks for real email addresses, home-directory paths (/home/<user>, /Users/<user>),
secrets and tokens, and the private terms in ~/.config/korolev/privacy.json:
  {"deny": ["my-private-project", "old.name@example.org"], "allow_emails": ["bot@localhost"]}
That file stays outside the repo, so the private word list is never published.
Findings are printed redacted. Exit 1 if anything is found, 0 if clean.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG_DIR  # noqa: E402

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


def scan(repo, rng, deny, allow):
    findings = []
    commits = [c for c in git(repo, 'rev-list', rng).split() if c]
    for sha in commits:
        meta = git(repo, 'log', '-1', '--format=%ae%n%ce%n%B', sha)
        author, committer, message = meta.split('\n', 2)
        for kind, email in (('author', author), ('committer', committer)):
            if not SAFE_EMAIL.search(email) and email.lower() not in allow:
                findings.append((f'commit {sha[:7]} {kind}', 'email', redact(email)))
        check_text(message, f'commit {sha[:7]} message', deny, allow, findings)
    for line in git(repo, 'for-each-ref', 'refs/tags', '--format=%(refname:short) %(*objectname) %(taggeremail)').splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in commits:
            email = parts[2].strip('<>')
            if not SAFE_EMAIL.search(email) and email.lower() not in allow:
                findings.append((f'tag {parts[0]} tagger', 'email', redact(email)))
            check_text(git(repo, 'for-each-ref', f'refs/tags/{parts[0]}', '--format=%(contents)'),
                       f'tag {parts[0]} message', deny, allow, findings)
    if commits:
        base, _, head = rng.partition('..')
        current = None
        for line in git(repo, 'diff', '--unified=0', '--no-color', base, head or 'HEAD').splitlines():
            if line.startswith('+++ '):
                current = line[6:] if line.startswith('+++ b/') else line[4:]
            elif line.startswith('+') and current:
                check_text(line[1:], current, deny, allow, findings)
    return commits, findings


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--range', default='origin/main..HEAD')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    deny, allow = load_config()
    commits, findings = scan(Path(args.repo).resolve(), args.range, deny, allow)
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
