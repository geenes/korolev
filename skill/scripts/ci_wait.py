#!/usr/bin/env python3
"""Wait for hosted CI and verify the run for an exact commit."""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TESTCOUNT = Path(__file__).resolve().with_name('testcount.py')
LOG_PREFIX = re.compile(r'^[^\t]+\t[^\t]+\t\d{4}-\d{2}-\d{2}T\S+Z(?: |$)')
NODE_TESTS = re.compile(r'[#ℹ] tests (\d+)')
PYTEST_COUNT = r'\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed)'
PYTEST_ITEM = rf'(?:{PYTEST_COUNT}|\d+ (?:warnings?|deselected|reruns?))'
PYTEST_SUMMARY = re.compile(rf'{PYTEST_ITEM}(?:, {PYTEST_ITEM})* in \d+(?:\.\d+)?s(?: \([^)]*\))?')
PYTEST_COLLECTION = re.compile(r'collected (\d+) items?\b')


def gh(*args, timeout=None):
    return subprocess.run(['gh', *args], stdin=subprocess.DEVNULL, capture_output=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def checked_output(result, command):
    if result.returncode != 0:
        raise RuntimeError(f'{command} exited {result.returncode}')
    return result.stdout


def workflow_runs(output):
    # gh api --paginate emits one JSON object per page, without an enclosing array.
    decoder = json.JSONDecoder()
    output = output.lstrip()
    if not output:
        raise ValueError('missing workflow run response')
    while output:
        page, end = decoder.raw_decode(output)
        yield from page['workflow_runs']
        output = output[end:].lstrip()


def find_run(args):
    endpoint = f'repos/{args.repo}/actions/runs?head_sha={args.sha}'
    deadline = time.monotonic() + args.find_timeout
    attempt_timeout = args.find_timeout or 10  # Zero performs one lookup without retrying.
    while True:
        try:
            result = gh('api', endpoint, '--paginate', timeout=attempt_timeout)
        except subprocess.TimeoutExpired:
            break
        runs = workflow_runs(checked_output(result, 'gh api'))
        matches = {run['id'] for run in runs if run['head_sha'] == args.sha
                   and (args.workflow is None or run['name'] == args.workflow)}
        if len(matches) > 1:
            hint = '; select one with --workflow NAME' if args.workflow is None else ''
            raise RuntimeError(f'multiple workflow runs match the commit{hint}')
        if matches:
            return matches.pop()

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(10, remaining))
        attempt_timeout = deadline - time.monotonic()
        if attempt_timeout <= 0:
            break
    raise RuntimeError('timed out looking for a matching workflow run')


def collected_tests(log):
    lines = [LOG_PREFIX.sub('', line) for line in log.splitlines()]
    if TESTCOUNT.is_file():
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'ci.log'
            report.write_text('\n'.join(lines), encoding='utf-8')
            result = subprocess.run([sys.executable, '-I', str(TESTCOUNT), 'parse', str(report)],
                                    capture_output=True, encoding='utf-8')
            count = json.loads(checked_output(result, 'testcount.py parse'))['collected']
            if type(count) is not int or count < 0:
                raise ValueError('invalid test count')
            return count

    count = 0
    empty_collection = False
    for line in lines:
        summary = line.strip('= \t')
        if match := NODE_TESTS.fullmatch(summary):
            count = int(match[1])
        elif match := PYTEST_COLLECTION.match(summary):
            empty_collection = int(match[1]) == 0
        elif summary.startswith('no tests ran') or PYTEST_SUMMARY.fullmatch(summary):
            items = re.findall(PYTEST_COUNT, summary)
            count = 0 if empty_collection else sum(int(item.split()[0]) for item in items)
            empty_collection = False
    return count


def verify(args, verdict):
    run_id = find_run(args)
    verdict['run_id'] = run_id
    target = (str(run_id), '-R', args.repo)
    watch = gh('run', 'watch', *target, '--exit-status', '--interval', '30')
    view = gh('run', 'view', *target, '--json', 'headSha,status,conclusion,jobs')
    run = json.loads(checked_output(view, 'gh run view'))
    verdict.update(status=run['status'], conclusion=run['conclusion'])
    if run['headSha'] != args.sha:
        raise RuntimeError('workflow run headSha does not match the requested commit')
    if run['status'] != 'completed' or run['conclusion'] != 'success':
        raise RuntimeError(f"workflow run status {run['status']}, conclusion {run['conclusion']}")
    if not run['jobs']:
        raise RuntimeError('workflow run returned no jobs to verify')
    # The job list has no required-check flag, so treat all jobs as required.
    skipped = [job['name'] for job in run['jobs'] if job['conclusion'] == 'skipped']
    if skipped:
        raise RuntimeError('workflow run skipped jobs: ' + ', '.join(skipped))
    checked_output(watch, 'gh run watch')
    if args.min_tests is not None:
        log = checked_output(gh('run', 'view', *target, '--log'), 'gh run view --log')
        count = collected_tests(log)
        verdict['collected'] = count
        if count < args.min_tests:
            raise RuntimeError(f'collected {count} tests, below minimum {args.min_tests}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', metavar='owner/repo')
    parser.add_argument('sha', metavar='full-sha', type=str.lower)
    parser.add_argument('--workflow', help='exact workflow name')
    parser.add_argument('--find-timeout', type=int, default=300, help='seconds to find a run (default: 300)')
    parser.add_argument('--min-tests', type=int, help='minimum collected tests in the CI log')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('repo must be owner/repo')
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha):
        parser.error('full-sha must be a 40-character hexadecimal commit SHA')
    if args.find_timeout < 0 or (args.min_tests is not None and args.min_tests < 0):
        parser.error('--find-timeout and --min-tests must be nonnegative')

    verdict = {'ok': False, 'repo': args.repo, 'head_sha': args.sha}
    try:
        verify(args, verdict)
    except RuntimeError as exc:
        verdict['error'] = str(exc)
    except OSError as exc:
        verdict['error'] = f'could not run CI verification: {exc.strerror}'
    except (KeyError, TypeError, ValueError):
        verdict['error'] = 'invalid response from gh or testcount.py'
    else:
        verdict['ok'] = True
    print(json.dumps(verdict))
    return 0 if verdict['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
