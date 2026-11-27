"""Exercise byte-exact payload delivery with a local Telegram transport."""
import hashlib
import io
import json
import sys
import urllib.parse
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

if __package__:
    from .test_skill import SCRIPTS, Sandbox
else:
    from test_skill import SCRIPTS, Sandbox

sys.path.insert(0, str(SCRIPTS))
import notify  # noqa: E402


class FakeTelegram:
    env = {'KOROLEV_BOT_TOKEN': '123:fixture', 'KOROLEV_CHAT_ID': '321', 'KOROLEV_OWNER_ID': '321'}
    identity = {'chat_id': 321, 'bot_id': 123, 'message_id': 99}

    def __init__(self, missing_document=False, download=None, message_override=None, approval=False):
        self.missing_document = missing_document
        self.download = download
        self.message_override = message_override or {}
        self.approval = approval
        self.calls = []

    def __call__(self, request, *, timeout):
        message = {'message_id': 99, 'chat': {'id': 321, 'type': 'private'},
                   'from': {'id': 123, 'is_bot': True}}
        method = request.full_url.rsplit('/', 1)[-1]
        self.calls.append(method)
        prefix = 'file/bot' if method == 'review.md' else 'bot'
        assert request.full_url.startswith(f'https://api.telegram.org/{prefix}123:fixture/')
        if method == 'sendMessage':
            fields = urllib.parse.parse_qs(request.data.decode())
            assert fields['chat_id'] == ['321']
            message['text'] = self.text = fields['text'][0]
            if self.approval:
                message['message_id'] = 100
            else:
                self.payload = self.text.split('\n\n', 1)[1].rsplit('\n\n', 1)[0].encode()
        elif method == 'sendDocument':
            header = f'Content-Type: {request.headers["Content-type"]}\r\n\r\n'.encode()
            parts = BytesParser(policy=policy.default).parsebytes(header + request.data).iter_parts()
            fields = {part.get_param('name', header='content-disposition'): part for part in parts}
            assert fields['chat_id'].get_payload(decode=True) == b'321'
            document = fields['document']
            self.payload = document.get_payload(decode=True)
            message['caption'] = self.text = fields['caption'].get_payload(decode=True).decode()
            if not self.missing_document:
                message['document'] = {'file_name': document.get_filename(), 'file_size': len(self.payload),
                                       'file_id': 'fixture-file'}
        elif method == 'getFile':
            assert urllib.parse.parse_qs(request.data.decode()) == {'file_id': ['fixture-file']}
            message = {'file_id': 'fixture-file', 'file_size': len(self.payload), 'file_path': 'documents/review.md'}
        elif method == 'review.md':
            assert request.full_url.endswith('/documents/review.md')
            return io.BytesIO(self.payload if self.download is None else self.download)
        else:
            raise AssertionError('unexpected Telegram operation')
        if method.startswith('send'):
            message.update(self.message_override)
        return io.BytesIO(json.dumps({'ok': True, 'result': message}).encode())


class NotifyTests(Sandbox):
    def test_approval_request_requires_exact_unsplit_echo_from_the_private_bot(self):
        text = 'Display every action\n\napprove R-23456 ' + 'a' * 40
        for override in ({}, {'text': text.splitlines()[-1]}, {'chat': {'id': 321, 'type': 'group'}},
                         {'from': {'id': 124, 'is_bot': True}}):
            with self.subTest(override=override):
                transport = FakeTelegram(message_override=override, approval=True)
                with patch.object(notify, 'review_urlopen', transport), patch.object(notify, 'append_line') as log:
                    if override:
                        with self.assertRaisesRegex(SystemExit, 'could not be confirmed'):
                            notify.send_approval_request(transport.env, 'fixture', text)
                        log.assert_not_called()
                    else:
                        receipt = notify.send_approval_request(transport.env, 'fixture', text)
                        self.assertEqual(receipt, {'sha256': hashlib.sha256(text.encode()).hexdigest(),
                                                   **transport.identity, 'message_id': 100})
                        log.assert_called_once()
                    self.assertEqual(transport.calls, ['sendMessage'])
        with patch.object(notify, 'review_urlopen') as request:
            with self.assertRaisesRegex(SystemExit, 'one message'):
                notify.send_approval_request(transport.env, 'fixture', '🚀' * 2049)
            request.assert_not_called()

    def test_exact_review_payload_preserves_bytes_and_digest_in_both_modes(self):
        for payload, mode, method in (
                ('  Before\r\n\r\nAfter ☀\n\n'.encode(), 'inline', 'sendMessage'),
                (('  Before\r\n' + '🚀' * 2200 + '\r\n\n').encode(), 'document', 'sendDocument')):
            with self.subTest(mode=mode):
                transport = FakeTelegram()
                with patch.object(notify, 'review_urlopen', transport), patch.object(notify, 'append_line'):
                    receipt = notify.send_review_payload(transport.env, 'fixture', 'preview.md', payload)
                digest = hashlib.sha256(payload).hexdigest()
                self.assertEqual(transport.payload, payload)
                self.assertEqual(transport.calls, [method] if mode == 'inline' else [method, 'getFile', 'review.md'])
                self.assertIn(digest, transport.text)
                proof = 'message-echo' if mode == 'inline' else 'file-sha256'
                self.assertEqual(receipt, {'sha256': digest, 'mode': mode, 'verification': proof, **transport.identity})

    def test_message_without_document_cannot_confirm_attachment(self):
        transport = FakeTelegram(missing_document=True)
        with patch.object(notify, 'review_urlopen', transport), patch.object(notify, 'append_line') as log:
            with self.assertRaisesRegex(SystemExit, 'delivery could not be confirmed'):
                notify.send_review_payload(transport.env, 'fixture', 'preview.md', b'a' * 5000)
        self.assertEqual(transport.calls, ['sendDocument'])
        log.assert_not_called()

    def test_direct_cli_refuses_a_privacy_hit_without_any_send(self):
        fixture = ('ghp_' + 'TemporaryFixture' * 3).encode()
        for payload in (fixture, b'a' * 5000 + fixture):
            with self.subTest(size=len(payload)):
                transport = FakeTelegram()
                argv = ['notify.py', '--project', 'fixture', '--review-payload', 'preview.md']
                with (patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(payload))),
                      patch.object(notify, 'load_env', return_value=transport.env),
                      patch.object(notify, 'touch_heartbeat'), patch.object(notify, 'review_urlopen', transport),
                      patch.object(notify, 'append_line') as log):
                    with self.assertRaisesRegex(SystemExit, 'privacy scan blocked') as error:
                        notify.main()
                self.assertNotIn(fixture.decode(), str(error.exception))
                self.assertEqual(transport.calls, [])
                log.assert_not_called()

    def test_direct_approval_cli_scans_before_transport(self):
        transport = FakeTelegram(approval=True)
        argv = ['notify.py', '--project', 'fixture', '--approval']
        with (patch.object(sys, 'argv', argv), patch.object(notify, 'load_env', return_value=transport.env),
              patch.object(notify, 'pairing', return_value={'bot_id': 123, 'chat_id': 321, 'owner_id': 321}),
              patch.object(notify, 'touch_heartbeat'), patch.object(notify, 'review_urlopen', transport),
              patch.object(notify, 'append_line') as log):
            with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'/home/' + b'private-owner/live'))):
                with self.assertRaisesRegex(SystemExit, 'privacy scan blocked'):
                    notify.main()
            self.assertEqual(transport.calls, [])
            log.assert_not_called()
            text = 'Exact fixture approval text'
            with (patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(text.encode()))),
                  patch.object(sys, 'stdout', new_callable=io.StringIO) as output):
                notify.main()
            self.assertEqual(transport.calls, ['sendMessage'])
            self.assertEqual(transport.text, text)
            self.assertEqual(json.loads(output.getvalue())['sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_direct_cli_scans_filename_and_exact_framing_in_both_modes(self):
        import privacy_scan
        filename = 'private-fixture.md'
        for size, term in ((5, filename), (5000, filename), (5, 'End of ' + filename),
                           (5000, f'filename="{filename}"'), (5000, filename + '\nSHA-256')):
            with self.subTest(size=size, term=term):
                transport = FakeTelegram()
                payload = b'a' * size
                argv = ['notify.py', '--project', 'fixture', '--review-payload', filename]
                config = self.base / 'privacy.json'
                config.write_text(json.dumps({'deny': [term]}))
                with (patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(payload))),
                      patch.object(privacy_scan, 'CONFIG_DIR', self.base),
                      patch.object(notify, 'load_env', return_value=transport.env),
                      patch.object(notify, 'touch_heartbeat'), patch.object(notify, 'review_urlopen', transport),
                      patch.object(notify, 'append_line') as log):
                    with self.assertRaisesRegex(SystemExit, 'privacy scan blocked') as error:
                        notify.main()
                self.assertNotIn(filename, str(error.exception))
                self.assertEqual(transport.calls, [])
                log.assert_not_called()

    def test_truncated_oversized_and_same_length_substitutions_are_unverified(self):
        for size in (4999, 5000, 5001):
            with self.subTest(size=size):
                transport = FakeTelegram(download=b'b' * size)
                with patch.object(notify, 'review_urlopen', transport), patch.object(notify, 'append_line') as log:
                    with self.assertRaisesRegex(SystemExit, 'delivery could not be confirmed'):
                        notify.send_review_payload(transport.env, 'fixture', 'preview.md', b'a' * 5000)
                self.assertEqual(transport.calls, ['sendDocument', 'getFile', 'review.md'])
                log.assert_not_called()

    def test_inline_requires_exact_echo_and_message_identity(self):
        for override in ({'text': 'changed'}, {'message_id': True},
                         {'chat': {'id': 999, 'type': 'private'}}, {'from': {'id': 999, 'is_bot': True}}):
            with self.subTest(override=override):
                transport = FakeTelegram(message_override=override)
                with patch.object(notify, 'review_urlopen', transport), patch.object(notify, 'append_line') as log:
                    with self.assertRaisesRegex(SystemExit, 'delivery could not be confirmed'):
                        notify.send_review_payload(transport.env, 'fixture', 'preview.md', b'ok')
                log.assert_not_called()
