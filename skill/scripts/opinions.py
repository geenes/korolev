#!/usr/bin/env python3
"""Get second and third opinions: run the Astra and Sol 5.6 reviewers in parallel (read-only).

  opinions.py --product NAME --prompt FILE [--sha SHA] [--tag plan] [--roles astra sol56] [--context FILE ...]

Prints the path to a round manifest after all requested reviewers succeed.
Run it in the background. The exit code is non-zero if any reviewer failed.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import product_dir, project_folder, valid_product  # noqa: E402
from run_codex import (code_review_prompt, fd_path, open_context_file, open_directory,  # noqa: E402
                       report_chunks, resolve_review_sha, verify_directory)


def report_version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_report(files, round_fd, out, record):
    stream, info = files.enter_context(open_context_file(round_fd, Path(out.name)))
    size = record['out_bytes']
    if type(size) is not int or not size or size != info.st_size:
        raise ValueError('missing or mismatched report size')
    digest = hashlib.sha256()
    for data in report_chunks(stream, size):
        digest.update(data)
    if (digest.hexdigest() != record['out_sha256']
            or report_version(os.fstat(stream.fileno())) != report_version(info)):
        raise ValueError('mismatched or changed report')
    return digest.hexdigest(), info


def publish_manifest(round_dir, round_fd, verified, payload):
    def check_reports():
        verify_directory(round_dir, round_fd)
        for name, info in verified.items():
            current = os.stat(name, dir_fd=round_fd, follow_symlinks=False)
            if report_version(current) != report_version(info):
                raise OSError('a verified report changed before manifest publication')

    fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=fd_path(round_fd))
    temporary = Path(temporary).name
    manifest = 'manifest.json'
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as sink:
            sink.write(json.dumps(payload, indent=2) + '\n')
            sink.flush()
            check_reports()
            os.replace(temporary, manifest, src_dir_fd=round_fd, dst_dir_fd=round_fd)
            check_reports()
            if not os.path.samestat(os.fstat(sink.fileno()), (round_dir / manifest).lstat()):
                raise OSError('manifest changed during publication')
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=round_fd)
    return round_dir / manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--product', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--tag', default='')
    parser.add_argument('--roles', nargs='+', default=['astra', 'sol56'])
    parser.add_argument('--cwd')
    revision = parser.add_mutually_exclusive_group()
    revision.add_argument('--sha', help='commit for all reviewers (default: HEAD)')
    revision.add_argument('--prompt-only', action='store_true', help='analyze context without a Git checkout')
    parser.add_argument('--context', nargs='*', default=[])
    parser.add_argument('--effort', help='override reasoning effort for all reviewers (testing)')
    args = parser.parse_args()
    if not valid_product(args.product):
        parser.error('invalid product name')
    if code_review_prompt(args.prompt) and args.sha is None:
        parser.error('the code-review template requires --sha')
    if len(set(args.roles)) != len(args.roles) or any(Path(role).name != role for role in args.roles):
        parser.error('review roles must be distinct names without path components')
    pdir = product_dir('').resolve() / args.product
    if pdir.is_symlink() or not pdir.is_dir():
        parser.error('product must be a directory at its canonical name under products/')
    sha = None
    if not args.prompt_only:
        source = Path(args.cwd).expanduser() if args.cwd else project_folder(args.product)
        try:
            sha = resolve_review_sha(source.resolve(), args.sha)
        except (OSError, RuntimeError) as error:
            parser.error(str(error))
    try:
        with ExitStack() as files:
            product_fd = files.enter_context(open_directory(pdir))
            name = Path(tempfile.mkdtemp(prefix='review-round-', dir=fd_path(product_fd))).name
            round_dir = pdir / name
            round_fd = files.enter_context(open_directory(Path(name), parent=product_fd))
            verify_directory(round_dir, round_fd)
            return run_round(args, sha, round_dir, round_fd, files)
    except OSError as error:
        print(f'Cannot publish review round: {error}', file=sys.stderr)
        return 1


def run_round(args, sha, round_dir, round_fd, files):
    procs, reports, digests, verified = [], {}, {}, {}
    for role in args.roles:
        out = round_dir / f'OPINION-{role}.md'
        cmd = [sys.executable, '-I', str(HERE / 'run_codex.py'), '--role', role, '--product', args.product,
               '--prompt', args.prompt, '--out', str(out)]
        if args.cwd:
            cmd += ['--cwd', args.cwd]
        cmd += ['--prompt-only'] if args.prompt_only else ['--sha', sha]
        if args.context:
            cmd += ['--context', *args.context]
        if args.effort:
            cmd += ['--effort', args.effort]
        procs.append((role, out, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
    worst = 0
    for role, out, proc in procs:
        output, errors = proc.communicate()
        code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
        if code == 0:
            try:
                record = json.loads(output.splitlines()[-1])
                if (record['role'], record['reviewed_sha'], record['out']) != (role, sha, str(out)):
                    raise ValueError('unexpected review record')
                digests[role], verified[out.name] = verify_report(files, round_fd, out, record)
                reports[role] = str(out)
            except (OSError, ValueError, IndexError, KeyError, TypeError):
                print(f'{role}: missing or mismatched role, reviewed_sha or report for this round', file=sys.stderr)
                code = 1
        worst = max(worst, code)
        print(f'{role}: exit {code} -> {out}')
        print(output.strip() or '(no output)')
        if errors:
            print(errors.rstrip(), file=sys.stderr)
    if worst == 0:
        manifest = publish_manifest(round_dir, round_fd, verified, dict(
            round_id=round_dir.name, tag=args.tag, reviewed_sha=sha, reports=reports, report_sha256=digests))
        print(manifest)
    return worst


if __name__ == '__main__':
    sys.exit(main())
