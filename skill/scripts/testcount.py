#!/usr/bin/env python3
"""Read test counts from pytest JUnit XML, pytest terminal output, or Node test output."""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PYTEST_COUNT = re.compile(r'(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|warnings?|deselected|reruns?)')
PYTEST_TIME = re.compile(r' in \d+(?:\.\d+)?s(?: \([^)]*\))?$')
PYTEST_SKIP = re.compile(r'^\s*(?:SKIPPED(?: \[\d+\])?|XFAIL)\s+(.+)$')
PYTEST_ZERO_COLLECTION = re.compile(r'^collected 0 items?\b')
PYTEST_COLLECTION_ERRORS = re.compile(r'Interrupted: (\d+) errors? during collection')
NODE_COUNT = re.compile(r'[#ℹ] (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*')
NODE_TAP_SKIP = re.compile(r'^\s*(?:not )?ok \d+ - (.+?)\s+# (?:SKIP|TODO)(?:\s|$)', re.IGNORECASE)
NODE_SPEC_SKIP = re.compile(r'^\s*[﹣⚠✔] (.+?) \([\d.]+ms\) #')
EXIT_CODE = re.compile(r'(?:Error: )?(?:Process completed with )?exit(?:[ _]code| status)'
                       r'(?:\s*[:=]\s*|\s+)(-?\d+)\.?', re.IGNORECASE)


def report(collected=0, passed=0, failed=0, errors=0, skipped=0, skipped_ids=()):
    return dict(collected=collected, passed=passed, failed=failed, errors=errors, skipped=skipped,
                skipped_ids=list(dict.fromkeys(skipped_ids)))


def parse_junit(data):
    root = ET.fromstring(data)
    if root.tag not in ('testsuite', 'testsuites'):
        raise ValueError('expected JUnit XML with a testsuite or testsuites root')
    suites = [root] if root.tag == 'testsuite' else root.findall('testsuite')
    totals = Counter()
    skipped_ids = []
    collection_reports = 0
    for suite in suites:
        cases = suite.findall('.//testcase')
        totals['tests'] += int(suite.get('tests', len(cases)))
        for attribute, tag in (('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped')):
            totals[attribute] += int(suite.get(attribute, sum(case.find(tag) is not None for case in cases)))
        for case in cases:
            # Pytest emits synthetic cases for collection failures and module-level skips.
            if not case.get('classname') and (
                case.find('error[@message="collection failure"]') is not None
                or case.find('skipped[@message="collection skipped"]') is not None
            ):
                collection_reports += 1
            if case.find('skipped') is not None and case.get('name'):
                skipped_ids.append('::'.join(filter(None, (case.get('classname'), case.get('name')))))
    passed = totals['tests'] - totals['failures'] - totals['errors'] - totals['skipped']
    return report(totals['tests'] - collection_reports, passed, totals['failures'], totals['errors'],
                  totals['skipped'], skipped_ids)


def parse_pytest(lines):
    for line in reversed(lines):
        summary = PYTEST_TIME.sub('', line.strip('= \t'))
        if summary == 'no tests ran':
            return report()
        parts = [PYTEST_COUNT.fullmatch(part.strip()) for part in summary.split(',')]
        if all(parts):
            counts = Counter({part[2]: int(part[1]) for part in parts})
            break
    else:
        return report()

    passed = counts['passed'] + counts['xpassed']
    skipped = counts['skipped'] + counts['xfailed']
    failed = counts['failed']
    errors = counts['error'] + counts['errors']
    collected = passed + failed + errors + skipped
    skipped_ids = []
    for line in lines:
        if match := PYTEST_SKIP.match(line):
            skipped_ids.append(re.split(r' - |: ', match[1], maxsplit=1)[0].strip())
        if PYTEST_ZERO_COLLECTION.match(line):
            collected = 0
        elif match := PYTEST_COLLECTION_ERRORS.search(line):
            collected = max(0, collected - min(int(match[1]), errors))
    return report(collected, passed, failed, errors, skipped, skipped_ids)


def parse_node(lines):
    counts = Counter()
    summary_index = None
    for index, line in enumerate(lines):
        if match := NODE_COUNT.fullmatch(line):
            if match[1] == 'tests':
                counts.clear()
                summary_index = index
            if summary_index is not None:
                counts[match[1]] = int(match[2])
    if summary_index is None:
        return None

    skip_pattern = NODE_TAP_SKIP if lines[summary_index].startswith('#') else NODE_SPEC_SKIP
    skipped_ids = [match[1] for line in lines[:summary_index] if (match := skip_pattern.match(line))]
    return report(counts['tests'], counts['pass'], counts['fail'], counts['cancelled'],
                  counts['skipped'] + counts['todo'], skipped_ids)


def parse(path, *, include_exit_code=False):
    data = path.read_bytes()
    if data.lstrip().startswith((b'<', b'\xef\xbb\xbf<', b'\xff\xfe', b'\xfe\xff')):
        return parse_junit(data)
    lines = ANSI.sub('', data.decode('utf-8-sig', errors='replace')).splitlines()
    result = parse_node(lines) or parse_pytest(lines)
    if include_exit_code:
        for line in reversed(lines):
            if match := EXIT_CODE.fullmatch(line.strip()):
                result['exit_code'] = int(match[1])
                break
    return result


def check(args, result):
    collected = result['collected']
    passed = result['passed']
    skipped = result['skipped']
    reasons = []
    if collected == 0:
        reasons.append('nothing ran (collected 0)')
    elif passed + result['failed'] == 0:
        if args.allow_all_skipped and skipped == collected and result['errors'] == 0:
            print(f'WARNING: all {collected} collected tests were skipped (--allow-all-skipped)',
                  file=sys.stderr)
        else:
            reasons.append('no tests executed (0 passed, 0 failed)')
    elif passed == 0:
        reasons.append('no tests passed')
    exit_code = result.get('exit_code') or args.exit_code
    if exit_code:
        reasons.append(f'runner exit code {exit_code}')
    if args.min is not None and collected < args.min:
        reasons.append(f'collected {collected} below minimum {args.min}')
    if args.max_skips is not None and skipped > args.max_skips:
        reasons.append(f'skipped {skipped} exceeds maximum {args.max_skips}')
    if args.skips_allow is not None:
        allowed = {line.strip() for line in args.skips_allow.read_text(encoding='utf-8-sig').splitlines()}
        unlisted = sorted(set(result['skipped_ids']) - allowed)
        if unlisted:
            ids = ', '.join(json.dumps(test_id, ensure_ascii=True) for test_id in unlisted)
            reasons.append('skipped IDs not allowlisted: ' + ids)
    if args.base is not None:
        base = parse(args.base)
        if collected < base['collected']:
            reasons.append(f"collected dropped from {base['collected']} to {collected}")
        if skipped > base['skipped']:
            reasons.append(f"skipped grew from {base['skipped']} to {skipped}")
    if reasons:
        print('FAIL: ' + ' | '.join(reasons))
        return 1
    print(f'PASS: collected {collected}, skipped {skipped}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    read = sub.add_parser('parse', help='print the report as JSON')
    read.add_argument('file', type=Path)
    verify = sub.add_parser('check', help='verify test and skip counts')
    verify.add_argument('file', type=Path)
    verify.add_argument('--min', type=int, help='minimum collected count')
    verify.add_argument('--max-skips', type=int, help='maximum skipped count')
    verify.add_argument('--skips-allow', type=Path, help='file with one reported skipped ID per line')
    verify.add_argument('--base', type=Path, help='baseline report for count and skip comparisons')
    verify.add_argument('--allow-all-skipped', action='store_true',
                        help='allow a fully skipped run and print a warning')
    verify.add_argument('--exit-code', type=int,
                        help='runner exit code (also read from an "exit code: N" report line)')
    args = parser.parse_args()

    try:
        result = parse(args.file, include_exit_code=args.command == 'check')
        if args.command == 'check':
            return check(args, result)
        print(json.dumps(result, ensure_ascii=False))
    except (ET.ParseError, OSError, ValueError) as exc:
        message = ' '.join(str(exc).splitlines())
        if args.command == 'check':
            print(f'FAIL: {message}')
        else:
            print(f'testcount: {message}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
