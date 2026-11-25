#!/usr/bin/env python3
"""Onboard a project: clone it into Korolev's workspace, create its state folder, write INVENTORY.md.

  onboard.py init <repo-url-or-local-path> [--name NAME] [--display "Shown name"]

The original repository is never modified. A local path is cloned too, so workers
only ever touch Korolev's own copy. Re-running init on an existing project fetches
updates and rewrites INVENTORY.md; it never overwrites STATE, DECISIONS or PLAN.
"""
import argparse
import collections
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SKILL, display_name, now, product_dir, slug, work_repo, write_atomic  # noqa: E402

SPEC_WORDS = re.compile(r'(spec|plan|roadmap|design|architect|adr|prd|requirement|todo|status|handoff|'
                        r'contributing|changelog|readme|agents|claude)', re.I)
AGENT_FILES = ['AGENTS.md', 'CLAUDE.md', '.cursorrules', '.github/copilot-instructions.md', 'GEMINI.md']
TEXT_EXT = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java', '.kt', '.rb', '.php',
            '.cs', '.cpp', '.c', '.h', '.swift', '.scala', '.sql', '.sh', '.vue', '.svelte', '.css', '.scss',
            '.html', '.md', '.yml', '.yaml', '.toml', '.json'}


def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd[:3])} ...\n{result.stderr[-800:]}")
    return result.stdout


def derive_name(source):
    tail = source.rstrip('/').split('/')[-1].split(':')[-1]
    return slug(tail[:-4] if tail.endswith('.git') else tail)


def clone(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        run(['git', 'fetch', '--all', '--prune', '--quiet'], cwd=dest, check=False)
        return 'existing clone, fetched'
    local = Path(source).expanduser()
    if local.exists():
        run(['git', 'clone', '--quiet', '--no-hardlinks', str(local.resolve()), str(dest)])
        return 'cloned from local path (original untouched)'
    github = re.match(r'^(?:https?://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$', source)
    if github and shutil.which('gh') and subprocess.run(['gh', 'auth', 'status'], capture_output=True).returncode == 0:
        run(['gh', 'repo', 'clone', github.group(1), str(dest), '--', '--quiet'])
        return 'cloned with gh'
    run(['git', 'clone', '--quiet', source, str(dest)])
    return 'cloned with git'


def first_heading(path):
    try:
        for line in path.read_text(errors='replace').splitlines()[:40]:
            if line.startswith('#'):
                return line.lstrip('#').strip()[:100]
    except OSError:
        pass
    return ''


def detect_commands(repo):
    found = []
    pkg = repo / 'package.json'
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get('scripts', {})
            for key in ('test', 'build', 'lint', 'typecheck', 'check', 'e2e'):
                if key in scripts:
                    found.append(f'npm run {key}  →  {scripts[key][:120]}')
        except ValueError:
            found.append('package.json present (unparsable)')
    py = repo / 'pyproject.toml'
    if py.exists():
        text = py.read_text(errors='replace')
        if 'pytest' in text:
            found.append('pytest (configured in pyproject.toml)')
        for tool in ('ruff', 'mypy', 'black', 'poetry', 'hatch', 'uv'):
            if f'[tool.{tool}' in text:
                found.append(f'{tool} configured in pyproject.toml')
    for name, hint in (('pytest.ini', 'pytest'), ('tox.ini', 'tox'), ('setup.cfg', 'setup.cfg (check [tool:pytest])'),
                       ('requirements.txt', 'pip install -r requirements.txt'), ('go.mod', 'go test ./...'),
                       ('Cargo.toml', 'cargo test'), ('pom.xml', 'mvn test'), ('build.gradle', 'gradle test'),
                       ('Gemfile', 'bundle exec rspec / rake'), ('composer.json', 'composer test'),
                       ('docker-compose.yml', 'docker compose'), ('compose.yaml', 'docker compose'),
                       ('Dockerfile', 'docker build')):
        if (repo / name).exists():
            found.append(hint)
    make = repo / 'Makefile'
    if make.exists():
        targets = re.findall(r'^([A-Za-z][\w.-]*):', make.read_text(errors='replace'), re.M)
        found.append('make targets: ' + ', '.join(sorted(set(targets))[:20]))
    return found


def inventory(repo, name, source, how):
    git = lambda *a: run(['git', *a], cwd=repo, check=False).strip()  # noqa: E731
    files = [f for f in git('ls-files').splitlines() if f]
    by_ext = collections.Counter(Path(f).suffix.lower() or '(none)' for f in files)
    top = collections.Counter(f.split('/')[0] if '/' in f else '(root)' for f in files)
    lines = 0
    for f in files:
        path = repo / f
        if path.suffix.lower() in TEXT_EXT and path.is_file() and path.stat().st_size < 2_000_000:
            try:
                lines += sum(1 for _ in path.open(errors='replace'))
            except OSError:
                pass
    docs = [f for f in files if f.lower().endswith(('.md', '.rst', '.adoc', '.txt'))
            and (f.count('/') == 0 or f.lower().startswith(('docs/', 'doc/', 'spec', 'plans/', '.github/'))
                 or SPEC_WORDS.search(Path(f).name))]
    tests = [f for f in files if re.search(r'(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|'
                                           r'\.(test|spec)\.[jt]sx?$', f)]
    workflows = [f for f in files if f.startswith('.github/workflows/')]
    out = [f'# Inventory: {display_name(name)}', '',
           f'- Generated: {now()} by `onboard.py`',
           f'- Source: `{source}` ({how})',
           f'- Korolev clone: `{repo}`',
           f"- Default branch / HEAD: `{git('rev-parse', '--abbrev-ref', 'HEAD')}` @ `{git('rev-parse', '--short', 'HEAD')}`",
           f"- Branches (local + remote): {len(git('branch', '-a').splitlines())}; commits: {git('rev-list', '--count', 'HEAD')}; "
           f"contributors: {len(set(git('log', '--format=%ae').splitlines()))}",
           f'- Tracked files: {len(files)}; approx. text lines: {lines:,}', '',
           '## Languages / file types (top 12)', '']
    out += [f'- `{ext}`: {n}' for ext, n in by_ext.most_common(12)]
    out += ['', '## Top-level layout (files per entry, top 20)', '']
    out += [f'- `{d}`: {n}' for d, n in top.most_common(20)]
    out += ['', '## Agent instructions', '']
    names = {Path(a).name for a in AGENT_FILES}
    present = [a for a in AGENT_FILES if (repo / a).exists()]
    present += [f for f in files if Path(f).name in names and f not in present][:20]
    out += [f'- `{a}` ({(repo / a).stat().st_size} bytes)' for a in present] or ['- none found']
    out += ['', '## Docs, specs and plans (read the authoritative ones; never everything)', '']
    for f in sorted(docs, key=lambda x: (x.count('/'), x))[:60]:
        out.append(f"- `{f}` ({(repo / f).stat().st_size} B) {first_heading(repo / f)}")
    if len(docs) > 60:
        out.append(f'- … {len(docs) - 60} more')
    out += ['', '## Build / test commands detected (unverified)', '']
    out += [f'- {c}' for c in detect_commands(repo)] or ['- none detected']
    out += ['', f'## Tests: {len(tests)} test-like files', '']
    out += [f'- `{t}`' for t in tests[:15]] + ([f'- … {len(tests) - 15} more'] if len(tests) > 15 else [])
    out += ['', '## CI workflows', '']
    out += [f'- `{w}`' for w in workflows] or ['- none']
    out += ['', '## Recent commits', '', '```', git('log', '-15', '--format=%h %ad %s', '--date=short'), '```', '']
    return '\n'.join(out)


def init(args):
    name = slug(args.name) if args.name else derive_name(args.source)
    repo = work_repo(name)
    how = clone(args.source, repo)
    pdir = product_dir(name)
    (pdir / 'packages').mkdir(parents=True, exist_ok=True)
    meta_path = pdir / 'product.json'
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {'created': now()}
    meta.update({'name': name, 'source': args.source, 'clone': str(repo),
                 'display': args.display or meta.get('display') or name})
    write_atomic(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
    for template in ('STATE.md', 'DECISIONS.md', 'PLAN.md', 'QUESTIONS.md'):
        target = pdir / template
        if not target.exists():
            text = (SKILL / 'templates' / template).read_text()
            write_atomic(target, text.replace('{{PROJECT}}', meta['display']).replace('{{DATE}}', now()[:10]))
    write_atomic(pdir / 'INVENTORY.md', inventory(repo, name, args.source, how))
    print(json.dumps({'name': name, 'display': meta['display'], 'clone': str(repo), 'product_dir': str(pdir),
                      'inventory': str(pdir / 'INVENTORY.md'), 'clone_status': how}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init', help='clone, create the state folder and write INVENTORY.md')
    p.add_argument('source', help='git URL (https/ssh) or local path')
    p.add_argument('--name', help='short project name (default: derived from the repo)')
    p.add_argument('--display', help='name shown in Telegram messages and reports')
    args = parser.parse_args()
    if args.command == 'init':
        init(args)


if __name__ == '__main__':
    main()
