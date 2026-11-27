import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skill.scripts import ci_wait


REPO = 'owner/project'
SHA = 'a' * 40
OTHER_SHA = 'b' * 40
API = ('api', f'repos/{REPO}/actions/runs?head_sha={SHA}', '--paginate')
WATCH = ('run', 'watch', '123', '-R', REPO, '--exit-status', '--interval', '30')
VIEW = ('run', 'view', '123', '-R', REPO, '--json', 'headSha,status,conclusion,jobs')
LOG = ('run', 'view', '123', '-R', REPO, '--log')


def response(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, '')


def discovered(*runs):
    return response(json.dumps({'total_count': len(runs), 'workflow_runs': runs}))


def run(run_id=123, name='tests', sha=SHA):
    return {'id': run_id, 'name': name, 'head_sha': sha}


def viewed(sha=SHA, conclusion='success', status='completed', job='success'):
    return response(json.dumps({'headSha': sha, 'conclusion': conclusion, 'status': status,
                                'jobs': [{'name': 'test', 'conclusion': job}]}))


class CIWaitTests(unittest.TestCase):
    def invoke(self, *args, responses):
        output = io.StringIO()
        with patch.object(ci_wait, 'gh', side_effect=responses) as gh, redirect_stdout(output):
            exit_code = ci_wait.main([REPO, SHA, *args])
        return exit_code, json.loads(output.getvalue()), [call.args for call in gh.call_args_list]

    def assert_failure(self, result, message):
        exit_code, verdict, _ = result
        self.assertEqual(exit_code, 1)
        self.assertFalse(verdict['ok'])
        self.assertIn(message, verdict['error'])

    def test_gh_uses_verified_executable_and_allowlisted_environment(self):
        executable = Path('/verified/bin/gh')
        ambient = {'HOME': '/launcher', 'PATH': '/shadow', 'GH_TOKEN': 'fixture',
                   'GH_HOST': 'shadow.invalid', 'GH_CONFIG_DIR': '/shadow', 'LD_PRELOAD': '/shadow/lib.so'}
        with (patch.dict(os.environ, ambient, clear=True),
              patch.object(ci_wait, 'trusted_system_executable', return_value=executable) as select,
              patch.object(ci_wait.subprocess, 'run', return_value=response()) as spawn):
            ci_wait.gh(*API, timeout=5)
        select.assert_called_once_with('gh')
        self.assertEqual(spawn.call_args.args[0], [executable, *API])
        self.assertEqual(spawn.call_args.kwargs['env'],
                         {'HOME': '/launcher', 'PATH': '/usr/bin:/bin', 'GH_TOKEN': 'fixture',
                          'GH_HOST': 'github.com', 'GH_PROMPT_DISABLED': '1', 'LC_ALL': 'C'})

    def test_run_not_found_waits_until_timeout(self):
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(ci_wait.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(ci_wait.time, 'sleep', side_effect=sleep) as wait:
            result = self.invoke('--find-timeout', '25', responses=[discovered()] * 3)
        self.assert_failure(result, 'timed out')
        self.assertEqual(result[2], [API, API, API])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [10, 10, 5])

    def test_lookup_retries_and_ignores_wrong_sha(self):
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(ci_wait.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(ci_wait.time, 'sleep', side_effect=sleep):
            code, verdict, calls = self.invoke(
                responses=[discovered(run(456, sha=OTHER_SHA)), discovered(run()), response(), viewed()])
        self.assertEqual(code, 0)
        self.assertTrue(verdict['ok'])
        self.assertEqual(clock[0], 10)
        self.assertEqual(calls, [API, API, WATCH, VIEW])

    def test_wrong_sha_after_watch_fails(self):
        result = self.invoke(responses=[discovered(run()), response(), viewed(sha=OTHER_SHA)])
        self.assert_failure(result, 'headSha')

    def test_failed_run_is_verified_after_unsuccessful_watch(self):
        result = self.invoke(responses=[discovered(run()), response(returncode=1), viewed(conclusion='failure')])
        self.assert_failure(result, 'failure')
        self.assertEqual(result[2], [API, WATCH, VIEW])

    def test_success(self):
        code, verdict, calls = self.invoke(responses=[discovered(run()), response(), viewed()])
        self.assertEqual(code, 0)
        self.assertTrue(verdict['ok'])
        self.assertEqual(verdict['run_id'], 123)
        self.assertEqual(verdict['head_sha'], SHA)
        self.assertEqual(calls, [API, WATCH, VIEW])

    def test_matching_runs_on_different_pages_are_ambiguous(self):
        pages = discovered(run()).stdout + discovered(run(456, 'lint')).stdout
        result = self.invoke(responses=[response(pages)])
        self.assert_failure(result, 'multiple')
        self.assertEqual(result[2], [API])

    def test_workflow_filters_runs_by_exact_name(self):
        code, verdict, calls = self.invoke(
            '--workflow', 'tests', responses=[discovered(run(456, 'other tests'), run()), response(), viewed()])
        self.assertEqual(code, 0)
        self.assertEqual(verdict['run_id'], 123)
        self.assertEqual(calls, [API, WATCH, VIEW])

    def test_workflow_name_can_still_be_ambiguous(self):
        result = self.invoke('--workflow', 'tests', responses=[discovered(run(), run(456))])
        self.assert_failure(result, 'multiple')

    def test_branch_filter_disambiguates_branch_and_tag_runs(self):
        runs = discovered(
            {**run(456), 'head_branch': 'v1.2.3'},
            {**run(), 'head_branch': 'main'},
        )
        result = self.invoke(responses=[runs])
        self.assert_failure(result, 'multiple')
        self.assertEqual(result[2], [API])

        code, verdict, calls = self.invoke(
            '--branch', 'main', responses=[runs, response(), viewed()])
        self.assertEqual(code, 0)
        self.assertTrue(verdict['ok'])
        self.assertEqual(verdict['run_id'], 123)
        self.assertEqual(calls, [API, WATCH, VIEW])

    def test_skipped_job_fails_even_when_run_succeeds(self):
        result = self.invoke(responses=[discovered(run()), response(), viewed(job='skipped')])
        self.assert_failure(result, 'skipped')

    def test_incomplete_run_fails(self):
        result = self.invoke(responses=[discovered(run()), response(), viewed(status='in_progress')])
        self.assert_failure(result, 'in_progress')

    def test_watch_error_fails_even_when_view_reports_success(self):
        result = self.invoke(responses=[discovered(run()), response(returncode=1), viewed()])
        self.assert_failure(result, 'watch')

    def with_log(self, log, minimum):
        prefixed = ''.join(f'test\tRun tests\t2026-11-25T12:34:56.0000000Z {line}\n'
                           for line in log.splitlines())
        return self.invoke('--min-tests', str(minimum), responses=[
            discovered(run()), response(), viewed(), response(prefixed),
        ])

    def test_min_tests_rejects_zero_tests(self):
        result = self.with_log('collected 0 items\n===== no tests ran in 0.01s =====', 1)
        self.assert_failure(result, 'collected 0')
        self.assertEqual(result[1]['collected'], 0)
        self.assertEqual(result[2], [API, WATCH, VIEW, LOG])

    def test_min_tests_parses_prefixed_logs_with_testcount(self):
        code, verdict, _ = self.with_log('===== 2 passed, 1 xfailed in 0.04s =====', 3)
        self.assertEqual(code, 0)
        self.assertEqual(verdict['collected'], 3)

    def test_min_tests_uses_fallback_if_testcount_is_absent(self):
        missing = ci_wait.TESTCOUNT.with_name('nonexistent_testcount.py')
        with patch.object(ci_wait, 'TESTCOUNT', missing):
            for log in ('===== 3 passed, 1 warning in 0.02s =====', 'TAP version 13\n# tests 3\n# pass 3'):
                with self.subTest(log=log):
                    code, verdict, _ = self.with_log(log, 3)
                    self.assertEqual(code, 0)
                    self.assertEqual(verdict['collected'], 3)

    def test_fallback_does_not_count_collection_only_skips(self):
        missing = ci_wait.TESTCOUNT.with_name('nonexistent_testcount.py')
        with patch.object(ci_wait, 'TESTCOUNT', missing):
            result = self.with_log('collected 0 items / 1 skipped\n===== 1 skipped in 0.01s =====', 1)
        self.assert_failure(result, 'collected 0')
