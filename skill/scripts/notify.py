#!/usr/bin/env python3
"""Send the owner a message through the Korolev Telegram bot.

  notify.py --project NAME "text"
  notify.py --project NAME --file QUESTIONS.md [--pending]
  notify.py --project NAME --review-payload FILENAME < rendered-text
  echo "text" | notify.py --project NAME

Every message starts with "📁 <project display name>". Long texts are split into
parts. Sent message IDs are logged with their project, so inbox.py can route the
owner's replies. With --pending, the text is also appended to the product's
DECISIONS.md under a "Pending" heading. The default is on when --file is named
QUESTIONS.md. Credentials come from ~/.config/korolev/telegram.env
(KOROLEV_BOT_TOKEN, KOROLEV_CHAT_ID, KOROLEV_OWNER_ID; mode 0600) and are never printed.
Never include customer personal data.
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOT_STATE, TELEGRAM_ENV, append_line, display_name, now, product_dir, touch_heartbeat  # noqa: E402
from privacy_scan import OBJECT_LIMIT, scan_text  # noqa: E402
from approvals import ApprovalError, _save_notes_delivery, pairing  # noqa: E402

LIMIT = 3800


def load_env(path=TELEGRAM_ENV):
    if not path.exists():
        sys.exit(f'Telegram is not configured ({path} missing). Ask the owner in chat instead.')
    env = {k.strip(): v.strip() for k, v in (line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)}
    for key in ('KOROLEV_BOT_TOKEN', 'KOROLEV_CHAT_ID'):
        if not env.get(key):
            sys.exit(f'{key} missing in {path}')
    return env


def chunks(text, limit=LIMIT):
    parts, current = [], ''
    for block in text.split('\n\n'):
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        if current and len(current) + 2 + len(block) > limit:
            parts.append(current)
            current = block
        else:
            current = f'{current}\n\n{block}' if current else block
    if current:
        parts.append(current)
    return parts or ['']


def send(env, text):
    data = urllib.parse.urlencode({'chat_id': env['KOROLEV_CHAT_ID'], 'text': text,
                                   'disable_web_page_preview': 'true'}).encode()
    url = 'https://api.telegram.org/bot' + env['KOROLEV_BOT_TOKEN'] + '/sendMessage'
    try:
        result = json.load(urllib.request.urlopen(url, data=data, timeout=20))
    except Exception as error:  # never echo the URL: it contains the token
        sys.exit('send failed: ' + type(error).__name__)
    if not result.get('ok'):
        sys.exit('send failed: ' + str(result.get('description', 'unknown'))[:200])
    return result['result']['message_id']


def send_document(env, filename, payload, caption):
    boundary = uuid4().hex
    body = b''.join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8')
        for name, value in (('chat_id', env['KOROLEV_CHAT_ID']), ('caption', caption))
    )
    body += (f'--{boundary}\r\n{document_disposition(filename)}\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode('ascii')
    body += payload + f'\r\n--{boundary}--\r\n'.encode('ascii')
    return payload_result(env, 'sendDocument', body, f'multipart/form-data; boundary={boundary}')


def document_disposition(filename):
    return f'Content-Disposition: form-data; name="document"; filename="{filename}"'


class NoReviewRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


review_urlopen = urllib.request.build_opener(NoReviewRedirect()).open


def review_bytes(request, limit):
    with review_urlopen(request, timeout=60) as response:
        length = getattr(response, 'length', None)
        data = response.read(limit + 1)
    if len(data) > limit or (length is not None and length != len(data)):
        raise ValueError('invalid review response length')
    return data


def payload_result(env, method, body, content_type='application/x-www-form-urlencoded'):
    url = 'https://api.telegram.org/bot' + env['KOROLEV_BOT_TOKEN'] + '/' + method
    request = urllib.request.Request(url, data=body, headers={'Content-Type': content_type})
    answer = json.loads(review_bytes(request, 64 * 1024))
    if answer['ok'] is not True or not isinstance(answer['result'], dict):
        raise ValueError('unexpected Telegram result')
    return answer['result']


def document_matches(env, document, filename, size, digest):
    if (not isinstance(document, dict) or document.get('file_name') != filename
            or type(document.get('file_size')) is not int or document['file_size'] != size
            or not isinstance(document.get('file_id'), str) or not document['file_id']):
        return False
    location = payload_result(env, 'getFile', urllib.parse.urlencode({'file_id': document['file_id']}).encode())
    path = location['file_path']
    if (location.get('file_id') != document['file_id']
            or ('file_size' in location and (type(location['file_size']) is not int or location['file_size'] != size))
            or not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9._-]*/)*[A-Za-z0-9_][A-Za-z0-9._-]*', path)):
        return False
    url = 'https://api.telegram.org/file/bot' + env['KOROLEV_BOT_TOKEN'] + '/' + path
    data = review_bytes(urllib.request.Request(url), size)
    return len(data) == size and hashlib.sha256(data).hexdigest() == digest


def review_identity(env, message):
    chat, sender, message_id = message['chat'], message['from'], message['message_id']
    if (type(chat['id']) is not int or chat['id'] <= 0 or chat['type'] != 'private'
            or str(chat['id']) != env['KOROLEV_CHAT_ID']
            or type(sender['id']) is not int or sender['id'] <= 0 or sender['is_bot'] is not True
            or str(sender['id']) != env['KOROLEV_BOT_TOKEN'].partition(':')[0]
            or type(message_id) is not int or message_id <= 0):
        raise ValueError('unexpected review message identity')
    return {'chat_id': chat['id'], 'bot_id': sender['id'], 'message_id': message_id}


def paired_review_destination():
    env = load_env()
    pair = pairing()
    chat, bot = env.get('KOROLEV_CHAT_ID'), env.get('KOROLEV_BOT_TOKEN', '')
    bot_id, _, token = bot.partition(':')
    if (chat != str(pair['chat_id']) or chat != str(pair['owner_id'])
            or chat != env.get('KOROLEV_OWNER_ID') or bot_id != str(pair['bot_id']) or not token):
        raise ApprovalError('approval delivery requires the paired bot and private owner chat')
    return MappingProxyType({'KOROLEV_CHAT_ID': chat, 'KOROLEV_BOT_TOKEN': bot}), pair


def preflight_approval_request(text):
    if not text or len(text.encode('utf-16-le')) > 8192:
        sys.exit('owner approval request must fit in one message')
    require_clean_review(text.encode())


def send_approval_request(env, project, text):
    preflight_approval_request(text)
    try:
        data = urllib.parse.urlencode({'chat_id': env['KOROLEV_CHAT_ID'], 'text': text,
                                       'disable_web_page_preview': 'true'}).encode()
        message = payload_result(env, 'sendMessage', data)
        identity = review_identity(env, message)
        if message.get('text') != text:
            raise ValueError('Telegram returned different content')
    except (HTTPException, OSError, ValueError, KeyError, TypeError):
        sys.exit('approval request delivery could not be confirmed')
    append_line(BOT_STATE / 'SENT.jsonl', {'message_id': identity['message_id'], 'project': project, 'time': now()})
    return {'sha256': hashlib.sha256(text.encode()).hexdigest(), **identity}


def require_clean_review(*parts):
    try:
        blocked = any(scan_text(part) for part in parts)
    except (OSError, ValueError, TypeError):
        sys.exit('privacy scan could not inspect the review payload')
    if blocked:
        sys.exit('privacy scan blocked the review payload')


def prepare_review_payload(filename, payload):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', filename) or len(payload) > OBJECT_LIMIT:
        sys.exit('invalid review payload filename or size')
    require_clean_review(filename.encode(), payload)
    digest = hashlib.sha256(payload).hexdigest()
    caption = f'{filename}\nSHA-256 {digest}'
    # Framing keeps leading and trailing whitespace inside the message body.
    text = f'{caption}\n\n{payload.decode("utf-8")}\n\nEnd of {filename}'
    inline = len(text.encode('utf-16-le')) <= 2 * 4096
    if inline:
        require_clean_review(text.encode())
    else:
        require_clean_review(caption.encode(), document_disposition(filename).encode())
    return digest, caption, text, inline


def send_review_payload(env, project, filename, payload, *, proposal=None):
    digest, caption, text, inline = prepare_review_payload(filename, payload)
    try:
        if inline:
            data = urllib.parse.urlencode({'chat_id': env['KOROLEV_CHAT_ID'], 'text': text,
                                           'disable_web_page_preview': 'true'}).encode()
            message = payload_result(env, 'sendMessage', data)
            identity = review_identity(env, message)
            confirmed, mode, proof = message.get('text') == text, 'inline', 'message-echo'
        else:
            message = send_document(env, filename, payload, caption)
            identity = review_identity(env, message)
            confirmed = message.get('caption') == caption and document_matches(
                env, message.get('document'), filename, len(payload), digest)
            mode, proof = 'document', 'file-sha256'
        if not confirmed:
            raise ValueError('Telegram returned different content')
    except (HTTPException, OSError, ValueError, KeyError, TypeError):
        sys.exit('review payload delivery could not be confirmed')
    receipt = {'sha256': digest, 'mode': mode, 'verification': proof, **identity}
    if proposal is not None:
        _save_notes_delivery(*proposal, receipt)
    append_line(BOT_STATE / 'SENT.jsonl', {'message_id': identity['message_id'], 'project': project, 'time': now()})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='product name (as in onboard.py) or any label')
    parser.add_argument('--file', help='send this file')
    parser.add_argument('--review-payload', metavar='FILENAME', help='deliver exact review text from stdin')
    parser.add_argument('--approval', action='store_true', help='deliver confirmed approval text or payload from stdin')
    parser.add_argument('--pending', action='store_true', help='also log as pending in DECISIONS.md')
    parser.add_argument('text', nargs='*')
    args = parser.parse_args()
    touch_heartbeat(args.project, 'Sending the owner a message')
    if args.review_payload is not None or args.approval:
        if args.file or args.pending or args.text:
            parser.error('confirmed delivery takes its body from stdin')
        try:
            env = paired_review_destination()[0] if args.approval else load_env()
        except ApprovalError as error:
            sys.exit(str(error))
        payload = sys.stdin.buffer.read(OBJECT_LIMIT + 1)
        receipt = (send_review_payload(env, args.project, args.review_payload, payload)
                   if args.review_payload is not None else send_approval_request(env, args.project, payload.decode()))
        print(json.dumps(receipt))
        return
    body = Path(args.file).read_text() if args.file else (' '.join(args.text) if args.text else sys.stdin.read())
    body = body.strip()
    if not body:
        sys.exit('empty message')
    env = load_env()
    shown = display_name(args.project)
    parts = chunks(body)
    ids = []
    for index, part in enumerate(parts, 1):
        head = f'📁 {shown}' + (f' ({index}/{len(parts)})' if len(parts) > 1 else '')
        message_id = send(env, f'{head}\n\n{part}')
        ids.append(message_id)
        append_line(BOT_STATE / 'SENT.jsonl', {'message_id': message_id, 'project': args.project, 'time': now()})
    pending = args.pending or (args.file and Path(args.file).name.upper() == 'QUESTIONS.MD')
    decisions = product_dir(args.project) / 'DECISIONS.md'
    if pending and decisions.exists():
        with decisions.open('a') as stream:
            stream.write(f'\n## Pending (asked {now()[:16]} UTC via Telegram, message {ids[0]})\n\n{body}\n')
    print(json.dumps({'sent': ids, 'project': args.project, 'pending_logged': bool(pending and decisions.exists())}))


if __name__ == '__main__':
    main()
