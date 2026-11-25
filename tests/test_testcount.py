"""Offline CLI tests with inline test-run reports."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skill' / 'scripts' / 'testcount.py'
PYTEST_REPORT = '''
=========================== short test summary info ============================
SKIPPED [2] tests/test_api.py:12: service unavailable
FAILED tests/test_api.py::test_fail - AssertionError
ERROR tests/test_api.py::test_error - RuntimeError
============== 4 passed, 1 failed, 2 skipped, 1 error, 3 warnings in 0.10s ==============
'''
JUNIT_REPORT = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
    <testcase classname="tests.test_api" name="test_pass" />
    <testcase classname="tests.test_api" name="test_fail"><failure /></testcase>
    <testcase classname="tests.test_api" name="test_error"><error /></testcase>
    <testcase classname="tests.test_api" name="test_skip[one]"><skipped /></testcase>
  </testsuite>
</testsuites>
'''


class TestCountTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def fixture(self, name, contents):
        path = self.directory / name
        path.write_text(contents, encoding='utf-8')
        return str(path)

    def run_script(self, *args):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), *args],
                              capture_output=True, text=True)

    def assert_parse(self, report, **expected):
        result = self.run_script('parse', self.fixture('report', report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def assert_verdict(self, code, *options, report=PYTEST_REPORT, reasons=()):
        result = self.run_script('check', self.fixture('report', report), *options)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertTrue(result.stdout.startswith('FAIL' if code else 'PASS'), result.stdout)
        for reason in reasons:
            self.assertIn(reason, result.stdout)

    def test_parse_pytest_junit_xml(self):
        self.assert_parse(JUNIT_REPORT, collected=4, passed=1, failed=1, errors=1,
                          skipped=1, skipped_ids=['tests.test_api::test_skip[one]'])

    def test_parse_pytest_terminal_summary_and_grouped_skip(self):
        self.assert_parse(PYTEST_REPORT, collected=8, passed=4, failed=1, errors=1,
                          skipped=2, skipped_ids=['tests/test_api.py:12'])

    def test_parse_pytest_expected_failure_and_unexpected_pass(self):
        self.assert_parse('XFAIL test_api.py::test_expected - reason\n'
                          '\x1b[32m1 passed, 1 xfailed, 1 xpassed, 3 deselected in 0.12s\x1b[0m\n',
                          collected=3, passed=2, failed=0, errors=0,
                          skipped=1, skipped_ids=['test_api.py::test_expected'])

    def test_parse_node_tap_with_skip_todo_and_cancellation(self):
        self.assert_parse('''TAP version 13
ok 1 - a pass
not ok 2 - a failure
ok 3 - a skip # SKIP absent
not ok 4 - later # TODO fix me
not ok 5 - cancelled
1..5
# tests 5
# suites 0
# pass 1
# fail 1
# cancelled 1
# skipped 1
# todo 1
# duration_ms 9.81
''', collected=5, passed=1, failed=1, errors=1, skipped=2, skipped_ids=['a skip', 'later'])

    def test_parse_node_spec(self):
        self.assert_parse('✔ a pass (0.22ms)\n﹣ a skip (0.12ms) # absent\n✔ later (0.11ms) # fix me\n'
                          'ℹ tests 3\nℹ suites 0\nℹ pass 1\nℹ fail 0\nℹ skipped 1\nℹ todo 1\n',
                          collected=3, passed=1, failed=0, errors=0, skipped=2, skipped_ids=['a skip', 'later'])

    def test_check_rejects_zero_collected(self):
        for report in ('', '==== no tests ran in 0.01s ====\n', '2 tests collected in 0.01s\n',
                       '# tests 0\n# pass 0\n# fail 0\n# skipped 0\n'):
            with self.subTest(report=report):
                self.assert_verdict(1, report=report, reasons=('nothing ran',))

    def test_check_rejects_pytest_collection_failure_without_tests(self):
        report = ('==== ERROR collecting test_api.py ====\n'
                  '!!!! Interrupted: 1 error during collection !!!!\n'
                  '==== 1 error in 0.02s ====\n')
        xml = '''<testsuite tests="1" errors="1" failures="0" skipped="0">
          <testcase classname="" name="test_api"><error message="collection failure" /></testcase>
        </testsuite>'''
        for fixture in (report, xml):
            with self.subTest(report=fixture):
                self.assert_verdict(1, report=fixture, reasons=('nothing ran',))

    def test_check_rejects_pytest_module_skip_without_tests(self):
        report = ('collected 0 items / 1 skipped\n'
                  'SKIPPED [1] test_api.py:2: optional module\n'
                  '==== 1 skipped in 0.01s ====\n')
        xml = '''<testsuite tests="1" errors="0" failures="0" skipped="1">
          <testcase classname="" name="test_api"><skipped message="collection skipped" /></testcase>
        </testsuite>'''
        for fixture in (report, xml):
            with self.subTest(report=fixture):
                self.assert_verdict(1, report=fixture, reasons=('nothing ran',))

    def test_check_rejects_skip_growth(self):
        base = self.fixture('base', '7 passed, 1 skipped in 0.03s\n')
        self.assert_verdict(1, '--base', base, reasons=('skipped grew from 1 to 2',))

    def test_check_rejects_collected_drop(self):
        base = self.fixture('base', '7 passed, 2 skipped in 0.03s\n')
        self.assert_verdict(1, '--base', base, reasons=('collected dropped from 9 to 8',))

    def test_check_accepts_allowlisted_skip(self):
        allow = self.fixture('allow', '\n tests.test_api::test_skip[one] \n')
        self.assert_verdict(0, '--skips-allow', allow, '--max-skips', '1', '--min', '4',
                            report=JUNIT_REPORT)

    def test_check_rejects_unlisted_skip(self):
        allow = self.fixture('allow', 'tests.test_api::test_other\n')
        self.assert_verdict(1, '--skips-allow', allow, report=JUNIT_REPORT,
                            reasons=('not allowlisted', 'tests.test_api::test_skip[one]'))

    def test_check_rejects_counts_outside_limits(self):
        self.assert_verdict(1, '--min', '9', '--max-skips', '1',
                            reasons=('collected 8 below minimum 9', 'skipped 2 exceeds maximum 1'))
