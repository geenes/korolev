#!/usr/bin/env python3
"""Get second and third opinions: run the Astra and Sol 5.6 reviewers in parallel (read-only).

  opinions.py --product NAME --prompt FILE [--tag plan] [--roles astra sol56] [--context FILE ...]

Writes OPINION-<role>[-<tag>].md in the product folder and exits when both are done.
Run it in the background. The exit code is non-zero if any reviewer failed.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--product', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--tag', default='')
    parser.add_argument('--roles', nargs='+', default=['astra', 'sol56'])
    parser.add_argument('--cwd')
    parser.add_argument('--context', nargs='*', default=[])
    parser.add_argument('--effort', help='override reasoning effort for all reviewers (testing)')
    args = parser.parse_args()
    procs = []
    for role in args.roles:
        out = f'OPINION-{role}' + (f'-{args.tag}' if args.tag else '') + '.md'
        cmd = [sys.executable, '-I', str(HERE / 'run_codex.py'), '--role', role, '--product', args.product,
               '--prompt', args.prompt, '--out', out]
        if args.cwd:
            cmd += ['--cwd', args.cwd]
        if args.context:
            cmd += ['--context', *args.context]
        if args.effort:
            cmd += ['--effort', args.effort]
        procs.append((role, out, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
    worst = 0
    for role, out, proc in procs:
        output, _ = proc.communicate()
        worst = max(worst, proc.returncode)
        print(f'{role}: exit {proc.returncode} -> {out}')
        print(output.strip().splitlines()[-1] if output.strip() else '(no output)')
    sys.exit(worst)


if __name__ == '__main__':
    main()
