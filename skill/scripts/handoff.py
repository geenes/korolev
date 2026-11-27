#!/usr/bin/env python3
"""Update a product's session handoff from coordinator notes and local state."""
import argparse
import json
import re
import subprocess
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import now, product_dir, project_folder, write_atomic  # noqa: E402
from inbox import records as inbox_records  # noqa: E402
from lesson import lessons  # noqa: E402
from privacy_scan import EMAIL, HOME_PATH, SAFE_EMAIL, SECRETS, check_text, git  # noqa: E402

TITLE = 'Session handoff'
MAX_LINES = 60
HEADING = re.compile(r'^ {0,3}#{1,2}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
PENDING = re.compile(r'Pending(?:[ \t]+\(.*\))?', re.I)
QUESTION = re.compile(r'^\d+[.)]\s+')
PRIVATE_KEY_BLOCK = re.compile(
    r'-----' + dict(SECRETS)['private key'].pattern + r'-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S,
)


def read_optional(path):
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''


def sections(text):
    """Yield the title, start, body start and end of each top-level section."""
    current = fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip('\r\n')
        if match := FENCE.match(stripped):
            marker, rest = match.groups()
            if fence is None:
                fence = marker
            elif marker.startswith(fence) and not rest.strip():
                fence = None
        elif fence is None and (match := HEADING.match(stripped)):
            if current is not None:
                yield (*current, offset)
            current = (match[1], offset, offset + len(line))
        offset += len(line)
    if current is not None:
        yield (*current, offset)


def replace_handoff(state, handoff):
    for title, start, _, end in sections(state):
        if title == TITLE:
            suffix = '\n' + state[end:] if end < len(state) else ''
            return state[:start] + handoff + suffix
    if state and not state.endswith('\n\n'):
        state += '\n' if state.endswith('\n') else '\n\n'
    return state + handoff


def redact_text(text, findings):
    check_text(text, 'handoff', [], set(), findings)
    text = PRIVATE_KEY_BLOCK.sub('[redacted]', text)
    for _, pattern in reversed(SECRETS):
        text = pattern.sub('[redacted]', text)
    text = HOME_PATH.sub('[redacted]', text)
    return EMAIL.sub(lambda match: match[0] if SAFE_EMAIL.search(match[0]) else '[redacted]', text)


def clone_summary(repo):
    try:
        head = git(repo, 'rev-parse', 'HEAD').strip()
        branch = git(repo, 'rev-parse', '--abbrev-ref', 'HEAD').strip()
        status = git(repo, '--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal')
    except (OSError, subprocess.CalledProcessError):
        return '- Clone: unverified (Git snapshot unavailable).'
    if branch == 'HEAD':
        branch = 'detached HEAD'
    return f"- Clone: HEAD {head}, branch {branch}, working tree {'dirty' if status else 'clean'}."


def recent_runs(path):
    try:
        with path.open(encoding='utf-8') as stream:
            rows = deque((json.loads(line) for line in stream if line.strip()), maxlen=5)
    except FileNotFoundError:
        return []
    summaries = []
    for row in rows:
        line = f"- {row.get('role', 'unknown')} | exit {row.get('exit', 'unknown')} | commit {row.get('commit') or 'none'}"
        if row.get('uncached_input_tokens') is not None:
            line += f" | uncached input tokens {row['uncached_input_tokens']}"
        summaries.append(line)
    return summaries


def preview(text):
    text = ' '.join(text.split())
    return text if len(text) <= 240 else text[:240].rstrip() + '… [truncated]'


def format_handoff(product, folder, notes, findings):
    runs = recent_runs(folder / 'USAGE.jsonl') or ['- No usage runs recorded.']
    pending = sum(item['status'] == 'pending' for item in lessons().values())
    summary = [f'## {TITLE}', '', f'- UTC: {now()}', clone_summary(project_folder(product)),
               f'- Pending lessons: {pending}', '', '### Recent runs', *runs]
    lines = redact_text('\n'.join(summary), findings).splitlines()

    decisions = read_optional(folder / 'DECISIONS.md')
    questions = []
    for title, _, body, end in sections(decisions):
        if PENDING.fullmatch(title):
            text = redact_text(decisions[body:end], findings)
            entries = [line.strip() for line in text.splitlines() if line.strip()]
            prompts = [line for line in entries if QUESTION.match(line)]
            questions.extend(f'- {preview(line)}' for line in (prompts or entries))
    lines += ['', '### Pending owner questions (DECISIONS.md)']
    lines += questions[:12] or ['- None listed.']
    if len(questions) > 12:
        lines.append('- Pending questions truncated. See DECISIONS.md.')

    messages = [row for row in inbox_records() if row.get('project') == product][-3:]
    lines += ['', '### Recent inbox messages']
    for row in messages:
        text = redact_text(f"Message {row.get('message_id', '?')}: {row.get('text', '')}", findings)
        lines.append(f'- {preview(text)}')
    if not messages:
        lines.append('- No inbox messages recorded.')

    notes = redact_text(notes, findings).strip()
    # Keep note headings and fenced blocks inside this section on the next update.
    lines += ['', '### Coordinator notes']
    lines.extend(f'> {line}' if line else '>' for line in notes.splitlines())
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + ['> [Coordinator notes truncated.]']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product', required=True, help='product directory name')
    parser.add_argument('--file', required=True, type=Path, help='coordinator notes in Markdown')
    args = parser.parse_args()
    if not args.product or Path(args.product).name != args.product or args.product in {'.', '..'}:
        parser.error('invalid product directory name')
    folder = product_dir(args.product)
    if not folder.is_dir():
        parser.error('unknown product; run onboard.py init first')

    state_path = folder / 'STATE.md'
    findings = []
    try:
        notes = args.file.expanduser().read_text(encoding='utf-8')
        handoff = format_handoff(args.product, folder, notes, findings)
        write_atomic(state_path, replace_handoff(read_optional(state_path), handoff))
    except (OSError, ValueError) as error:
        sys.exit(f'Could not update the session handoff ({type(error).__name__}).')
    if findings:
        print('WARNING: Redacted sensitive text in the session handoff.', file=sys.stderr)
    print('Updated the session handoff in STATE.md.')


if __name__ == '__main__':
    main()
