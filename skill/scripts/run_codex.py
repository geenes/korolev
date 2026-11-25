#!/usr/bin/env python3
"""Run one fresh Codex session for a Korolev role and save its final answer.

  run_codex.py --role analyst --product NAME --prompt FILE --out ANALYSIS.md
  run_codex.py --role builder --product NAME --prompt TASK.md --out packages/01-x/HANDOFF.md --write

Each run is a new ephemeral session: no history, a small context. Read-only roles
cannot change files. --write (or a role with write=true) allows workspace writes
in the clone only. The final message goes to OUT, relative to the product folder
unless absolute. Events and usage are kept in the product's .runs/ folder and
summarized in USAGE.jsonl. Exit code: Codex's own; 124 on timeout.
Run it in the background: the coordinator wakes when it exits.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import append_line, display_name, now, product_dir, roles, work_repo  # noqa: E402

HEADER = """You are the {role} for the project "{display}" in a Korolev-coordinated team.
Repository (your working directory): {cwd}
Project state folder (read STATE.md, DECISIONS.md and PLAN.md there when relevant): {pdir}
Follow the repository's agent instructions (AGENTS.md, CLAUDE.md, nested ones too) and use its own agent
tooling when relevant: skills (.codex/skills, .claude/skills, .agents), subagent definitions, commands,
plugins and MCP configs listed in INVENTORY.md in the state folder. Never print secrets, never use
credentials you find, never push, deploy or contact external services unless the task says so.
{write_rule}
Your final message is saved as the deliverable: make it complete, structured and bounded as asked.

----- TASK -----
"""


def usage_from(events_path):
    total = {}
    try:
        for line in events_path.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                for key, value in event['usage'].items():
                    if isinstance(value, (int, float)):
                        total[key] = total.get(key, 0) + value
    except OSError:
        pass
    return total or None


def commit_changes(cwd, task, model):
    """Commit the builder's working-tree changes (Codex cannot write .git). Returns the SHA or None."""
    git = lambda *a: subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True)  # noqa: E731
    if git('rev-parse', '--is-inside-work-tree').returncode != 0 or not git('status', '--porcelain').stdout.strip():
        return None
    title = next((line.lstrip('# ').strip() for line in task.splitlines() if line.startswith('#')), 'Korolev builder change')
    git('add', '-A')
    result = git('-c', 'user.name=Korolev builder', '-c', 'user.email=korolev@localhost', 'commit', '-q', '-m',
                 f'{title[:72]}\n\nBuilt by Codex {model} via Korolev run_codex.py.')
    if result.returncode != 0:
        print('WARNING: auto-commit failed: ' + result.stderr.strip()[-300:], file=sys.stderr)
        return None
    return git('rev-parse', 'HEAD').stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--role', required=True)
    parser.add_argument('--product', required=True)
    parser.add_argument('--prompt', required=True, help='prompt or task file')
    parser.add_argument('--out', required=True, help='deliverable file (relative to the product folder)')
    parser.add_argument('--cwd', help='working directory (default: the product clone)')
    parser.add_argument('--write', action='store_true', help='allow workspace writes in the clone')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--timeout', type=int, help='minutes')
    parser.add_argument('--context', nargs='*', default=[], help='extra files whose paths are listed for the worker')
    args = parser.parse_args()

    role = roles().get(args.role)
    if not role:
        sys.exit(f'unknown role {args.role}; known: {", ".join(roles())}')
    pdir = product_dir(args.product)
    if not pdir.exists():
        sys.exit(f'no product folder {pdir}; run onboard.py init first')
    cwd = Path(args.cwd or work_repo(args.product)).expanduser().resolve()
    write = args.write or role.get('write', False)
    model = args.model or role['model']
    effort = args.effort or role['effort']
    timeout = 60 * (args.timeout or role.get('timeout_minutes', 60))
    out = Path(args.out) if os.path.isabs(args.out) else pdir / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    runs = pdir / '.runs'
    runs.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{args.role}'
    events = runs / f'{stamp}.events.jsonl'

    task = Path(args.prompt).expanduser().read_text()
    if args.context:
        task += '\n\nRelevant files:\n' + '\n'.join(f'- {Path(c).expanduser().resolve()}' for c in args.context)
    prompt = HEADER.format(role=args.role, display=display_name(args.product), cwd=cwd, pdir=pdir,
                           write_rule=('You may edit files in the repository. Do NOT run git commit, add, reset or push: '
                                       '.git is read-only in your sandbox. Leave finished changes in the working tree; '
                                       'the Korolev launcher commits them after you exit.' if write
                                       else 'READ-ONLY: do not modify any file.')) + task

    cmd = ['codex', 'exec', '-C', str(cwd), '-m', model, '-c', f'model_reasoning_effort={effort}',
           '-s', 'workspace-write' if write else 'read-only', '--ephemeral', '--skip-git-repo-check',
           '--json', '-o', str(out), '-']
    started = now()
    begin = time.time()
    timed_out = False
    with events.open('w') as sink:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=sink, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    code = 124 if timed_out else proc.returncode
    commit = None
    if write and code == 0:
        commit = commit_changes(cwd, task, model)
    record = {'role': args.role, 'product': args.product, 'model': model, 'effort': effort,
              'sandbox': 'workspace-write' if write else 'read-only', 'started': started, 'ended': now(),
              'seconds': round(time.time() - begin), 'exit': code, 'timed_out': timed_out,
              'out': str(out), 'out_bytes': out.stat().st_size if out.exists() else 0,
              'events': str(events), 'usage': usage_from(events), 'commit': commit}
    append_line(pdir / 'USAGE.jsonl', record)
    print(json.dumps(record, ensure_ascii=False))
    if code == 0 and record['out_bytes'] == 0:
        print('WARNING: Codex exited 0 but wrote no deliverable', file=sys.stderr)
        code = 3
    sys.exit(code)


if __name__ == '__main__':
    main()
