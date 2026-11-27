#!/usr/bin/env python3
"""Attach an existing Git folder or clone a URL into the owner's projects directory."""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
from ipaddress import IPv6Address
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIG_DIR, HOME, SKILL, derive_product_id, display_name, edit_projects,  # noqa: E402
                    git_common_dir, launcher_git, metadata_parent, now, product_dir, product_meta, publish_metadata,
                    read_projects, shared_remotes,
                    tasks_root, trusted_git_executable, valid_product, work_repo, write_atomic)
from privacy_scan import SECRETS  # noqa: E402
from procs import _start_time  # noqa: E402

SPEC_WORDS = re.compile(r'(spec|plan|roadmap|design|architect|adr|prd|requirement|todo|status|handoff|'
                        r'contributing|changelog|readme|agents|claude)', re.I)
AGENT_FILES = ['AGENTS.md', 'CLAUDE.md', '.cursorrules', '.github/copilot-instructions.md', 'GEMINI.md']
ECOSYSTEM = [  # (label, path prefix or file) for agent tooling the workers should respect and use
    ('Claude skills', '.claude/skills/'), ('Claude subagents', '.claude/agents/'), ('Claude commands', '.claude/commands/'),
    ('Claude settings/hooks', '.claude/settings'), ('Claude plugin', '.claude-plugin/'), ('Codex skills', '.codex/skills/'),
    ('Codex config', '.codex/config'), ('Agent skills (.agents)', '.agents/'), ('MCP servers', '.mcp.json'),
    ('Cursor rules', '.cursor/rules/'), ('Kilo/opencode config', '.kilo'), ('Gemini', '.gemini/')]
TEXT_EXT = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java', '.kt', '.rb', '.php',
            '.cs', '.cpp', '.c', '.h', '.swift', '.scala', '.sql', '.sh', '.vue', '.svelte', '.css', '.scss',
            '.html', '.md', '.yml', '.yaml', '.toml', '.json'}


def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd[:3])} ...\n{result.stderr[-800:]}")
    return result.stdout


def derive_name(source):
    if URI_SCHEME.match(source):
        tail = unquote(urlsplit(source).path)
    else:
        scp = SCP_URL.fullmatch(source)
        tail = scp['path'] if scp else source
    return derive_product_id(Path(tail).name.removesuffix('.git'))


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
    if subprocess.run(['git', 'clone', '--quiet', source, str(dest)], capture_output=True).returncode:
        raise ValueError(f'Git could not clone into {dest}')
    return 'cloned with git'


def first_heading(path):
    try:
        for number, line in enumerate(path.read_text(errors='replace').splitlines()[:40], 1):
            if line.startswith('#'):
                return sanitize_inventory(line.lstrip('#').strip(), path.name, number)[:100]
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
                    found.append(f'npm run {key}  →  {sanitize_inventory(scripts[key])[:120]}')
        except json.JSONDecodeError:
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
           f'- Source: `{public_url(source)}` ({how})',
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
    out += ['', '## Agent tooling in the repo (workers must follow and use these)', '']
    eco = []
    for label, prefix in ECOSYSTEM:
        hits = [f for f in files if f.startswith(prefix) or ('/' + prefix) in ('/' + f)]
        if hits:
            eco.append(f'- {label}: {len(hits)} file(s), e.g. ' + ', '.join(f'`{h}`' for h in hits[:4]))
    out += eco or ['- none found']
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
    return sanitize_inventory('\n'.join(out))


def git_read(folder, *args):
    result = subprocess.run(['git', '--no-optional-locks', *args], cwd=folder,
                            capture_output=True)
    return result.stdout.decode('utf-8', 'surrogateescape').removesuffix('\n') if result.returncode == 0 else None


URL_SCHEME = re.compile(r'^(?:https?|ssh|git|file)://', re.I)
URI_SCHEME = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://')
SCP_HOST = r'(?:[\w.-]+|\[[^\s\[\]<>\x60"]+\])'
SCP_USER = r"\w.!$%&'*+,;=~\\\-"
SCP_URL = re.compile(rf'(?:(?P<user>[{SCP_USER}]+)@)?(?P<host>{SCP_HOST}):(?P<path>(?!:).+)')
SCP_USER_START = rf'(?<![{SCP_USER}:@])[{SCP_USER}:@]+@(?:{SCP_HOST}:|\[[^\s\]]*[:%])'
URL_CHAR = r'(?:(?!\]\()[^\s<>\x60"])'
SCP_PLAIN_START = r'(?:(?<![\w.@:/\[\]-])[\w.-]+|(?<![:@/\[\]])\[[^\s\[\]<>\x60"]+\])'
INVENTORY_URL = re.compile(
    rf'(?:(?<![A-Za-z0-9.+-])[0-9.+-]*(?P<uri>{URI_SCHEME.pattern})|{SCP_USER_START}'
    rf'|{SCP_PLAIN_START}:(?!//)(?={URL_CHAR}*?[?#])){URL_CHAR}*')
SPLIT_USERINFO = re.compile(rf'[{SCP_USER}:()\]]*@')


def public_url(url, *, for_clone=False):
    if URI_SCHEME.match(url):
        try:
            parts = urlsplit(url)
            port = parts.port
            if (url.count('://') != 1 or (not parts.hostname and parts.scheme != 'file')
                    or parts.netloc.count('@') > 1 or (port is None and parts.netloc.endswith(':'))
                    or re.search(SCP_USER_START, parts.path)):
                raise ValueError
            return parts._replace(netloc=parts.netloc.rsplit('@', 1)[-1], query='', fragment='').geturl()
        except ValueError:
            raise ValueError('ambiguous repository URL') from None
    if '://' in url:
        raise ValueError('ambiguous repository URL')
    scp = SCP_URL.fullmatch(url)
    if scp:
        path = re.split(r'[?#]', scp['path'], maxsplit=1)[0]
        host = scp['host']
        if host.startswith('['):
            try:
                IPv6Address(host[1:-1])
            except ValueError:
                raise ValueError('ambiguous repository URL') from None
            if for_clone and '%' in host:
                raise ValueError('zone-qualified SCP repository URLs are unsupported')
        if path and not re.search(SCP_USER_START, path):
            return f'{host}:{path}'
    if scp or re.match(SCP_USER_START, url):
        raise ValueError('ambiguous repository URL')
    return url


def url_extent(text, start, end):
    url = text[start:end]
    opener = text[start - 1:start]
    closer = {'[': ']', '(': ')'}.get(opener)
    if closer:
        depth = 1
        for offset, char in enumerate(url):
            depth += (char == opener) - (char == closer)
            if depth == 0:
                return '', url[:offset], closer, start + offset + 1
    trimmed = url.rstrip('.,;!:')
    for marker in ('***', '___', '**', '__', '~~', '*', '_', "'"):
        if trimmed.endswith(marker):
            if trimmed.startswith(marker):
                return marker, trimmed[len(marker):-len(marker)], marker, start + len(trimmed)
            if start >= len(marker) and text.startswith(marker, start - len(marker)):
                return '', trimmed[:-len(marker)], marker, start + len(trimmed)
    return '', url, '', end


def sanitize_inventory(text, filename='INVENTORY.md', first_line=1):
    pieces = []
    position = 0
    while match := INVENTORY_URL.search(text, position):
        start = match.start('uri') if match['uri'] else match.start()
        prefix, url, suffix, end = url_extent(text, start, match.end())
        try:
            following = text[end:end + 1]
            if ((text.startswith('](', end) and (text[start - 1:start] != '['
                    or SPLIT_USERINFO.match(text, end) and not INVENTORY_URL.match(text, end + 2)))
                    or (following in {'"', '\x60', '>'} and SPLIT_USERINFO.match(text, end + 1))
                    or following == '@'
                    or (suffix in {')', ']'} and (following in {'?', '#', '/'} or following.isalnum()
                                                  or SPLIT_USERINFO.match(text, end, match.end())))):
                raise ValueError
            pieces.extend((text[position:start], prefix, public_url(url), suffix))
        except ValueError:
            line = text.count('\n', 0, match.start()) + first_line
            raise ValueError(f'cannot sanitize URL in {filename} at line {line}') from None
        position = end
    return ''.join((*pieces, text[position:]))


def repo_entry(name, folder, *, common=None):
    branch = git_read(folder, 'symbolic-ref', '--quiet', '--short', 'HEAD') or ''
    upstream = git_read(folder, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}') or ''
    remotes = {}
    for remote in (git_read(folder, 'remote') or '').splitlines():
        urls = git_read(folder, 'config', '--null', '--get-all', f'remote.{remote}.url') or ''
        remotes[remote] = public_url(urls.partition('\0')[0])
    tracking = git_read(folder, 'config', '--get', f'branch.{branch}.remote') if branch else None
    default_branch = ''
    for remote in dict.fromkeys((tracking, 'origin', *remotes)):
        if remote not in remotes:
            continue
        prefix = f'refs/remotes/{remote}/'
        ref = git_read(folder, 'symbolic-ref', '--quiet', prefix + 'HEAD') or ''
        if ref.startswith(prefix):
            default_branch = ref.removeprefix(prefix)
            break
    return dict(name=name, folder=str(folder), git_common_dir=str(common or git_common_dir(folder)),
                branch=branch, upstream=upstream, default_branch=default_branch, remotes=remotes)


def dirty_count(folder):
    try:
        status = git_read(folder, '-c', 'core.fsmonitor=false', 'status',
                          '--porcelain=v1', '-z', '--untracked-files=all')
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return None
    if status is None:
        return None
    paths = iter(status.split('\0'))
    count = 0
    for item in paths:
        if item:
            count += 1
            if 'R' in item[:2] or 'C' in item[:2]:
                next(paths, None)
    return count


def processes_in(folders):
    processes = []
    for path in Path('/proc').glob('[0-9]*'):
        if not path.name.isdecimal():
            continue
        pid = int(path.name)
        try:
            cwd = Path((path / 'cwd').readlink())
            if not any(cwd.is_relative_to(folder) for folder in folders):
                continue
            start_time = _start_time(pid)
            command = (path / 'comm').read_text(errors='replace').strip()
            if cwd == (path / 'cwd').readlink() and start_time == _start_time(pid):
                processes.append(dict(pid=pid, command=command, start_time=start_time))
        except (OSError, ValueError, IndexError):
            # Processes may exit, change cwd, or be owned by an inaccessible user.
            continue
    return sorted(processes, key=lambda record: record['pid'])


def supervision_hints(folder):
    hints = []
    try:
        with (folder / 'AGENTS.md').open(errors='replace') as instructions:
            for line in instructions:
                if re.search(r'(?<![A-Za-z])(?:supervision|controller|dispatch|lock)', line, re.I):
                    for _, pattern in SECRETS:
                        line = pattern.sub('[redacted]', line)
                    hints.append(line.strip()[:160])
                    if len(hints) == 5:
                        break
    except OSError:
        pass
    return hints


def other_controllers(folder):
    """Inspect linked worktrees, process working directories and AGENTS.md."""
    folder = Path(folder).resolve()
    worktrees = []
    listing = git_read(folder, 'worktree', 'list', '--porcelain', '-z') or ''
    for record in listing.split('\0\0'):
        fields = dict(field.split(' ', 1) for field in record.split('\0') if ' ' in field)
        if 'worktree' not in fields:
            continue
        path = Path(fields['worktree'])
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            pass  # Keep reporting worktrees whose path can no longer be traversed.
        if path == folder:
            continue
        worktrees.append(dict(path=str(path), head=fields.get('HEAD', ''),
                              branch=fields.get('branch', '').removeprefix('refs/heads/'),
                              dirty_count=dirty_count(path),
                              other_tool=bool({'.kilo', '.codex', '.claude'} & set(path.parts[:-1]))))
    folders = [folder, *(Path(entry['path']) for entry in worktrees)]
    return dict(worktrees=worktrees, processes=processes_in(folders),
                supervision_hints=supervision_hints(folder))


def clone_destination(source, dest_option):
    if URL_SCHEME.match(source):
        repo_path = unquote(urlsplit(source).path)
    else:
        repo_path = SCP_URL.fullmatch(source)['path']
    repo_name = Path(repo_path).name.removesuffix('.git')
    if repo_name in ('', '.', '..'):
        raise ValueError('cannot determine repository name from the URL')
    if dest_option:
        return Path(dest_option).expanduser().resolve(), repo_name

    try:
        config = json.loads((CONFIG_DIR / 'config.json').read_text())
    except FileNotFoundError:
        config = {}
    root = Path(config.get('projects_root', '~/projects')).expanduser()
    return (root / repo_name).resolve(), repo_name


def check_state_paths(folder, name):
    paths = (HOME, HOME / 'projects.json', HOME / 'projects.json.lock',
             product_dir(name), product_dir(name) / 'product.json')
    if any(path.resolve().is_relative_to(folder) for path in paths):
        raise ValueError('project state and registry must be outside the attached folder')


def product_slots(name, projects, *, new=False, refresh=False):
    slots = (product_dir(name), work_repo(name).parent, tasks_root(name))
    if ((name in projects and not refresh)
            or any(path.is_symlink() for path in (*slots, work_repo(name), slots[0] / 'product.json'))
            or (new and any(path.exists() for path in slots))):
        raise ValueError(f'product ID {name!r} is already in use')
    return slots


def legacy_product(name, source, projects):
    slots = product_slots(name, projects)
    meta = product_meta(name)
    if any(path.exists() for path in slots) and not (
            meta.get('name') == name and 'folder' not in meta and meta.get('clone') == str(work_repo(name))
            and public_url(meta.get('source', '')) == source):
        raise ValueError(f'product ID {name!r} is already in use')
    return meta


def local_git_root(folder, git):
    if folder.is_dir():
        result = launcher_git(folder, 'rev-parse', '--show-toplevel', executable=git)
        if result.returncode == 0:
            return Path(os.fsdecode(result.stdout.removesuffix(b'\n'))).resolve()


def git_identity(common):
    info = common.stat()
    return [info.st_dev, info.st_ino]


def local_attach_common(name, folder, projects, git):
    slots = product_slots(name, projects, refresh=True)
    meta_path = slots[0] / 'product.json'
    clone = work_repo(name)
    common = git_common_dir(folder, executable=git)
    if (any(path.exists() and not path.is_dir() for path in (*slots, clone))
            or (meta_path.exists() and not meta_path.is_file())):
        raise ValueError(f'product ID {name!r} has an invalid state slot')
    previous = projects.get(name)
    if previous:
        if previous['folder'] != str(folder) or not common.samefile(previous['git_common_dir']):
            raise ValueError(f"project {name!r} is already attached to {previous['folder']}")
    elif any(path.exists() for path in slots):
        meta = product_meta(name)
        if 'folder' in meta:
            if (meta.get('name'), meta['folder'], meta.get('git_common_dir'), meta.get('git_common_identity')) != (
                    name, str(folder), str(common), git_identity(common)):
                raise ValueError(f'product ID {name!r} has unrelated attachment state')
            return common
        if (meta.get('name') != name or meta.get('clone') != str(clone)
                or local_git_root(clone, git) != clone.absolute()):
            raise ValueError(f'product ID {name!r} has no verified legacy repository')
        if not common.samefile(git_common_dir(clone, executable=git)):
            origin = launcher_git(clone, 'config', '--local', '--no-includes', '--get', 'remote.origin.url', executable=git)
            tip = launcher_git(clone, 'rev-parse', '--verify', 'refs/remotes/origin/HEAD^{commit}', executable=git)
            if (origin.returncode or Path(os.fsdecode(origin.stdout.removesuffix(b'\n'))).resolve() != folder
                    or meta.get('source') != str(folder) or tip.returncode):
                raise ValueError(f'product ID {name!r} has unrelated legacy state')
            branches = launcher_git(folder, 'branch', '--all', '--contains', tip.stdout.strip(), executable=git)
            if branches.returncode or not branches.stdout.strip():
                raise ValueError(f'product ID {name!r} has unrelated legacy state')
    return common


def register_product(projects, name, folder, source, *, common=None):
    entry = repo_entry(name, folder, common=common)
    previous = projects.get(name)
    if previous and previous['folder'] != entry['folder']:
        raise ValueError(f"project {name!r} is already attached to {previous['folder']}")
    warnings = []
    for other_name, other in projects.items():
        if other_name == name:
            continue
        if other['folder'] == entry['folder']:
            raise ValueError(f"folder {folder} is already attached as {other_name!r}")
        if other['git_common_dir'] == entry['git_common_dir']:
            raise ValueError(f"same Git common directory as project {other_name!r} ({other['folder']})")
        for url in sorted(shared_remotes(entry, other)):
            warnings.append(f"WARN shares remote {url} with project {other_name!r}")

    entry.update(attached=previous['attached'] if previous else now(), warnings=warnings)
    pdir = product_dir(name)
    path = pdir / 'product.json'
    with metadata_parent(path) as parent:
        existing_meta = product_meta(name, parent)
        meta = dict(existing_meta)
        meta.setdefault('created', now())
        meta['source'] = public_url(meta.get('source', source))
        meta.update(name=name, folder=str(folder), display=meta.get('display') or name)
        if common is not None:
            meta.update(git_common_dir=str(common), git_common_identity=git_identity(common))
        if meta != existing_meta:
            publish_metadata(path, json.dumps(meta, indent=2) + '\n', parent)
    projects[name] = entry
    return dict(entry, display=meta['display'], product_dir=str(pdir),
                attach_status='resumed' if previous else 'attached')


def attach(args):
    local = Path(args.source).expanduser()
    local_exists = local.exists()
    source = args.source if local_exists else public_url(args.source, for_clone=True)
    is_url = not local_exists and (URL_SCHEME.match(args.source) or SCP_URL.fullmatch(args.source))
    if not local_exists and not is_url and URI_SCHEME.match(args.source):
        raise ValueError('unsupported repository URL scheme')
    if is_url:
        folder, repo_name = clone_destination(source, args.dest)
        name = args.name if args.name is not None else derive_product_id(repo_name)
        product_slots(name, read_projects(), new=True)
        print(f'Planned clone destination: {folder}', file=sys.stderr, flush=True)
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise ValueError(f'clone destination is not empty: {folder}')
        if not args.yes:
            raise ValueError('pass --yes to confirm cloning into the planned destination')
    else:
        if args.dest:
            raise ValueError('--dest requires a Git URL')
        git = trusted_git_executable()
        folder = local_git_root(local, git)
        if folder is None:
            raise ValueError(f'path must be inside a Git work tree: {local}')
        name = args.name if args.name is not None else derive_product_id(folder.name)
        source = str(folder)

    check_state_paths(folder, name)
    with edit_projects() as projects:
        if is_url:
            product_slots(name, projects, new=True)
            if any(entry['folder'] == str(folder) for entry in projects.values()):
                raise ValueError(f'folder is already registered: {folder}')
            result = subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--',
                                     args.source, str(folder)], capture_output=True)
            if result.returncode:
                raise ValueError(f'Git could not clone into {folder} (exit {result.returncode})')
        elif not args.name:
            name = next((key for key, entry in projects.items() if entry['folder'] == str(folder)), name)
            if not valid_product(name):
                raise ValueError('invalid registered product name; migrate the product ID first')
            check_state_paths(folder, name)
        common = None if is_url else local_attach_common(name, folder, projects, git)
        summary = register_product(projects, name, folder, source, common=common)
    summary['other_controllers'] = other_controllers(folder)
    print(json.dumps(summary, indent=2))


def init(args):
    local = Path(args.source).expanduser()
    source = str(local.resolve()) if local.exists() else public_url(args.source, for_clone=True)
    name = args.name if args.name is not None else derive_name(source)
    legacy_product(name, source, read_projects())
    with edit_projects() as projects:
        init_legacy(args, name, source, legacy_product(name, source, projects))


def init_legacy(args, name, source, meta):
    repo = work_repo(name)
    how = clone(args.source, repo)
    pdir = product_dir(name)
    meta_path = pdir / 'product.json'
    meta.setdefault('created', now())
    meta.update({'name': name, 'source': source, 'clone': str(repo),
                 'display': args.display or meta.get('display') or name})
    with metadata_parent(meta_path) as parent:
        publish_metadata(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + '\n', parent)
    (pdir / 'packages').mkdir(parents=True, exist_ok=True)
    for template in ('STATE.md', 'DECISIONS.md', 'PLAN.md', 'QUESTIONS.md'):
        target = pdir / template
        if not target.exists():
            text = (SKILL / 'templates' / template).read_text()
            write_atomic(target, text.replace('{{PROJECT}}', meta['display']).replace('{{DATE}}', now()[:10]))
    write_atomic(pdir / 'INVENTORY.md', inventory(repo, name, source, how))
    print(json.dumps({'name': name, 'display': meta['display'], 'clone': str(repo), 'product_dir': str(pdir),
                      'inventory': str(pdir / 'INVENTORY.md'), 'clone_status': how}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init', help='deprecated: use attach',
                       description='Deprecated: use attach to register an owner folder.')
    p.add_argument('source', help='git URL (https/ssh) or local path')
    p.add_argument('--name', help='short project name (default: derived from the repo)')
    p.add_argument('--display', help='name shown in Telegram messages and reports')
    p = sub.add_parser('attach', help='register a Git work tree or clone a URL into a new owner folder')
    p.add_argument('source', help='path inside a Git work tree, or Git URL')
    p.add_argument('--name', help='short project name (default: derived from the folder or URL)')
    p.add_argument('--dest', help='clone destination for a Git URL')
    p.add_argument('--yes', action='store_true', help='confirm cloning into the planned destination')
    args = parser.parse_args()
    if args.name is not None and not valid_product(args.name):
        parser.error('invalid product name')
    try:
        (init if args.command == 'init' else attach)(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{parser.prog}: {error}\n')


if __name__ == '__main__':
    main()
