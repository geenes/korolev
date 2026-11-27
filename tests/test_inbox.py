"""Exercise the inbox CLI with an offline Telegram transport and clock."""
import http.client
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill' / 'scripts'))
import inbox  # noqa: E402

TOKEN = '123:inbox-fixture'
POLL_URL = f'https://api.telegram.org/bot{TOKEN}/getUpdates'
BODY_MARKER = 'private error body'


def http_error(status, retry_after=None):
    body = {'ok': False, 'error_code': status, 'description': f'{BODY_MARKER}\n{POLL_URL}'}
    if retry_after is not None:
        body['parameters'] = {'retry_after': retry_after}
    return urllib.error.HTTPError(POLL_URL, status, BODY_MARKER, {}, io.BytesIO(json.dumps(body).encode()))


def update(update_id, sender=42):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'date': 1, 'text': 'Owner reply',
                                               'chat': {'id': 31}, 'from': {'id': sender}}}


class FakeClock:
    def __init__(self):
        self.now = 100
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class InboxTests(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        lock = self.enterContext((state / 'inbox.lock').open('a+'))
        self.clock = FakeClock()
        self.poll = Mock()
        self.offsets = []
        self.receipts = []
        self.webhook = Mock(return_value='')
        self.env = {'KOROLEV_BOT_TOKEN': TOKEN, 'KOROLEV_CHAT_ID': '31', 'KOROLEV_OWNER_ID': '42'}
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(patch.multiple(inbox, BOT_STATE=state, INBOX=state / 'INBOX.jsonl', OFFSET=state / 'offset',
                                         LOCK=Mock(open=Mock(return_value=lock)), time=self.clock,
                                         load_env=lambda: self.env, t=lambda key, **kwargs: key))
        self.enterContext(patch.object(inbox.urllib.request, 'urlopen', self.transport))
        inbox.OFFSET.write_text('40')

    def tearDown(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        for private in (TOKEN, 'api.telegram.org', BODY_MARKER):
            self.assertNotIn(private, output)

    def transport(self, url, *, data, timeout):
        method = url.rsplit('/', 1)[-1]
        params = urllib.parse.parse_qs(data.decode())
        if method == 'getWebhookInfo':
            result = {'url': self.webhook(timeout)}
        elif method == 'getUpdates':
            self.offsets.append(int(params['offset'][0]))
            result = self.poll(int(params['timeout'][0]))
            if isinstance(result, io.BytesIO):
                return result
        elif method == 'sendMessage':
            self.receipts.append(int(params['reply_to_message_id'][0]))
            result = {}
        else:
            self.fail(f'unexpected method {method}')
        return io.BytesIO(json.dumps({'ok': True, 'result': result}).encode())

    def read(self, *args):
        with (patch.object(sys, 'argv', ['inbox.py', *args]), redirect_stdout(self.stdout),
              redirect_stderr(self.stderr)):
            inbox.main()

    def test_two_timeouts_then_exactly_one_owner_message(self):
        self.poll.side_effect = [TimeoutError(POLL_URL), TimeoutError(POLL_URL),
                                 [update(41), update(41), update(38, sender=7)]]
        self.read('--wait', '100')
        line, total = self.stdout.getvalue().splitlines()
        record = json.loads(line)
        self.assertEqual(total, 'accepted 1 message(s)')
        self.assertEqual(inbox.records(), [record])
        self.assertEqual(record['update_id'], 41)
        self.assertTrue(record['ack'])
        self.assertEqual(self.receipts, [41])
        self.assertEqual(self.offsets, [40, 40, 40])
        self.assertEqual(inbox.OFFSET.read_text(), '42')
        self.assertEqual(self.clock.delays, [5, 10])
        self.assertEqual(self.stderr.getvalue().splitlines(), [
            'telegram getUpdates transient failure (TimeoutError), retry delay 5s',
            'telegram getUpdates transient failure (TimeoutError), retry delay 10s',
        ])

    def test_transient_at_deadline_exits_normally(self):
        def timeout(_):
            self.clock.now += 7
            raise TimeoutError(POLL_URL)

        self.poll.side_effect = timeout
        self.read('--wait', '7')
        self.assertEqual(self.stdout.getvalue(), 'accepted 0 message(s)\n')
        self.assertEqual(self.offsets, [40])
        self.assertEqual(inbox.OFFSET.read_text(), '40')
        self.assertEqual(self.clock.delays, [])
        self.assertEqual(self.stderr.getvalue().splitlines(), [
            'telegram getUpdates transient failure (TimeoutError), retry delay 0s',
        ])

    def test_incomplete_body_read_retries_from_saved_offset(self):
        response = io.BytesIO()
        response.read = Mock(side_effect=http.client.IncompleteRead(BODY_MARKER.encode()))
        self.poll.side_effect = [response, [update(40)]]
        self.read('--wait', '100')
        self.assertEqual(self.offsets, [40, 40])
        self.assertEqual(self.clock.delays, [5])
        self.assertEqual([record['update_id'] for record in inbox.records()], [40])
        self.assertIn('accepted 1 message(s)', self.stdout.getvalue())

    def test_retry_sleep_ends_at_deadline(self):
        self.poll.side_effect = [http_error(429, retry_after=90)]
        self.read('--wait', '3')
        self.assertEqual(self.stdout.getvalue(), 'accepted 0 message(s)\n')
        self.assertEqual(self.offsets, [40])
        self.assertEqual(self.clock.delays, [3])
        self.assertIn('retry delay 3s', self.stderr.getvalue())

    def test_fatal_http_statuses_exit_without_retry(self):
        for status in (401, 403, 404, 409):
            with self.subTest(status=status):
                self.poll.side_effect = [http_error(status)]
                with self.assertRaises(SystemExit) as error:
                    self.read('--wait', '100')
                self.assertEqual(str(error.exception), f'telegram getUpdates failed: HTTP {status}')
        self.assertEqual(self.offsets, [40] * 4)
        self.assertEqual(self.clock.delays, [])
        self.assertEqual(self.stdout.getvalue() + self.stderr.getvalue(), '')

    def test_retry_after_controls_rate_limit_delay(self):
        self.poll.side_effect = [http_error(429, retry_after=75), [update(40)]]
        self.read('--wait', '100')
        self.assertEqual(self.clock.delays, [75])
        self.assertEqual(self.offsets, [40, 40])
        self.assertIn('accepted 1 message(s)', self.stdout.getvalue())
        self.assertIn('(HTTP 429), retry delay 75s', self.stderr.getvalue())

    def test_invalid_retry_after_uses_backoff(self):
        self.poll.side_effect = [http_error(429, retry_after=True), [update(40)]]
        self.read('--wait', '100')
        self.assertEqual(self.clock.delays, [5])

    def test_zero_retry_after_uses_backoff_before_next_poll(self):
        self.poll.side_effect = [http_error(429, retry_after=0), [update(40)]]
        self.read('--wait', '10')
        self.assertEqual(self.clock.delays, [5])
        self.assertEqual(self.poll.call_args_list, [call(10), call(5)])

    def test_backoff_caps_and_resets_with_advanced_offset(self):
        self.poll.side_effect = [urllib.error.URLError(POLL_URL), ConnectionResetError(POLL_URL),
                                 http_error(500), http_error(502), http_error(503), http_error(504),
                                 [update(55, sender=7), update(39, sender=7)], TimeoutError(POLL_URL), [update(56)]]
        self.read('--wait', '300')
        self.assertEqual(self.clock.delays, [5, 10, 20, 40, 60, 60, 5])
        self.assertEqual(self.offsets, [40] * 7 + [56, 56])
        self.assertEqual(inbox.OFFSET.read_text(), '57')
        self.assertEqual(self.receipts, [56])

    def test_webhook_timeout_retries_within_wait_deadline(self):
        self.webhook.side_effect = [TimeoutError(POLL_URL), '']

        def poll(seconds):
            self.assertEqual(self.webhook.call_args_list, [call(10), call(5)])
            self.assertEqual(seconds, 5)
            return [update(40)]

        self.poll.side_effect = poll
        self.read('--wait', '10')
        self.assertEqual(self.clock.delays, [5])
        self.assertIn('accepted 1 message(s)', self.stdout.getvalue())

    def test_webhook_is_fatal(self):
        self.webhook.return_value = 'https://example.invalid/webhook'
        with self.assertRaisesRegex(SystemExit, '^this bot has a webhook; getUpdates cannot be used$'):
            self.read('--wait', '100')
        self.poll.assert_not_called()
        self.assertEqual(self.clock.delays, [])

    def test_malformed_config_is_fatal(self):
        self.env['KOROLEV_OWNER_ID'] = 'invalid'
        with self.assertRaises(ValueError):
            self.read('--wait', '100')
        self.poll.assert_not_called()
        self.assertEqual(self.clock.delays, [])

    def test_one_shot_preserves_http_error_result(self):
        self.poll.side_effect = [http_error(429, retry_after=75)]
        self.read()
        self.assertEqual(self.stdout.getvalue(), 'accepted 0 message(s)\n')
        self.assertEqual(self.offsets, [40])
        self.assertEqual(self.clock.delays, [])
        self.assertEqual(self.stderr.getvalue(), '')

    def test_one_shot_timeout_remains_fatal(self):
        self.poll.side_effect = [TimeoutError(POLL_URL)]
        with self.assertRaises(SystemExit) as error:
            self.read()
        self.assertEqual(str(error.exception), 'telegram getUpdates failed: TimeoutError')
        self.assertEqual(self.offsets, [40])
        self.assertEqual(self.clock.delays, [])
