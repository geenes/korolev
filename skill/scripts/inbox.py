#!/usr/bin/env python3
"""Receive the owner's messages to the Korolev bot (the only reader of that bot).

  inbox.py                    fetch new messages once, record them, exit
  inbox.py --wait SECONDS     long-poll; exit as soon as one owner message arrives
  inbox.py --show [--project NAME] [--last N]   print recorded messages (no network)

Only messages from KOROLEV_OWNER_ID in KOROLEV_CHAT_ID are accepted. A reply is
routed to the project of the question it answers. Free text can be addressed as
"project: instruction". An exclusive lock guarantees a single reader. Each
accepted message is fsync'ed to INBOX.jsonl before the offset advances, so a
crash re-delivers rather than loses it, and duplicates are dropped by update_id.
The bot acknowledges each message. Never point this at a bot that another
service reads: getUpdates consumes updates for everyone.
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOT_STATE, HOME, append_line, display_name, touch_heartbeat, write_atomic  # noqa: E402
from i18n import t  # noqa: E402
from notify import load_env  # noqa: E402

INBOX = BOT_STATE / 'INBOX.jsonl'
OFFSET = BOT_STATE / 'offset'
SENT = BOT_STATE / 'SENT.jsonl'
LOCK = BOT_STATE / 'inbox.lock'
RELEASE_APPROVAL_REPLY = re.compile(r'^(a|а|да|yes|ok|ок|approve|approved|confirmed|опубликовать|✅)\b')
RELEASE_REJECTION_REPLY = re.compile(r'^(b|б|нет|no|reject|отклонить|не публиковать)\b')


def call(token, method, params, timeout=70):
    data = urllib.parse.urlencode(params).encode()
    try:
        return json.load(urllib.request.urlopen('https://api.telegram.org/bot' + token + '/' + method,
                                                data=data, timeout=timeout))
    except urllib.error.HTTPError as error:  # Telegram explains rejections in the JSON body
        try:
            return json.load(error)
        except ValueError:
            raise SystemExit(f'telegram {method} failed: HTTP {error.code}') from None
    except Exception as error:  # never echo the URL: it contains the token
        raise SystemExit(f'telegram {method} failed: {type(error).__name__}') from None


def acknowledge(token, chat, message_id, text):
    """Send the receipt; check Telegram's answer, retry once, never fail silently. Returns True if delivered."""
    for params in ({'chat_id': chat, 'reply_to_message_id': message_id, 'text': text}, {'chat_id': chat, 'text': text}):
        for attempt in range(2):
            try:
                result = call(token, 'sendMessage', params, timeout=20)
            except SystemExit as error:
                result = {'ok': False, 'description': str(error)}
            if result.get('ok'):
                return True
            retry = (result.get('parameters') or {}).get('retry_after', 2)
            time.sleep(min(int(retry), 10))
    print(f'WARNING: receipt for message {message_id} was NOT delivered: {result.get("description", "unknown")}',
          file=sys.stderr)
    return False


def known_projects():
    names = {}
    products = HOME / 'products'
    if products.exists():
        for path in products.iterdir():
            if path.is_dir():
                names[path.name.lower()] = path.name
                names[display_name(path.name).lower()] = path.name
    return names


def project_for(reply_to, text):
    if reply_to and SENT.exists():
        for line in SENT.read_text().splitlines():
            row = json.loads(line)
            if row['message_id'] == reply_to:
                return row['project']
    if ':' in text:
        head = text.split(':', 1)[0].strip().lower()
        return known_projects().get(head) or (head if 0 < len(head) <= 40 and '\n' not in head else None)
    return None


def records():
    if not INBOX.exists():
        return []
    return [json.loads(line) for line in INBOX.read_text().splitlines() if line.strip()]


def show(args):
    rows = [r for r in records() if not args.project or r.get('project') == args.project]
    for row in rows[-args.last:]:
        print(json.dumps(row, ensure_ascii=False))
    print(f'{len(rows)} message(s) recorded' + (f' for {args.project}' if args.project else ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--wait', type=int, default=0)
    parser.add_argument('--show', action='store_true')
    parser.add_argument('--project')
    parser.add_argument('--last', type=int, default=20)
    args = parser.parse_args()
    if args.show:
        touch_heartbeat(args.project, 'Reading the owner inbox')
        return show(args)
    BOT_STATE.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('another inbox reader is already running; its messages land in INBOX.jsonl (use --show)')
        sys.exit(75)
    env = load_env()
    if not env.get('KOROLEV_OWNER_ID'):
        sys.exit('KOROLEV_OWNER_ID missing: run setup_telegram.py')
    token, chat, owner = env['KOROLEV_BOT_TOKEN'], int(env['KOROLEV_CHAT_ID']), int(env['KOROLEV_OWNER_ID'])
    if call(token, 'getWebhookInfo', {}).get('result', {}).get('url'):
        sys.exit('this bot has a webhook; getUpdates cannot be used')
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    seen = {r['update_id'] for r in records()}
    deadline = time.time() + args.wait
    accepted = []
    while True:
        poll = max(0, min(50, int(deadline - time.time()))) if args.wait else 0
        updates = call(token, 'getUpdates', {'offset': offset, 'timeout': poll,
                                             'allowed_updates': json.dumps(['message'])}, timeout=poll + 20).get('result', [])
        for update in updates:
            message = update.get('message') or {}
            text = message.get('text', '')
            if (message.get('chat', {}).get('id') == chat and message.get('from', {}).get('id') == owner and text
                    and not text.startswith('/start') and update['update_id'] not in seen):
                reply_to = (message.get('reply_to_message') or {}).get('message_id')
                record = {'update_id': update['update_id'], 'date': message['date'], 'text': text,
                          'message_id': message['message_id'], 'reply_to': reply_to,
                          'project': project_for(reply_to, text)}
                receipt = (t('inbox_received', project=display_name(record['project'])) if record['project']
                           else t('inbox_unknown_project'))
                record['ack'] = acknowledge(token, chat, message['message_id'], receipt)
                append_line(INBOX, record)
                seen.add(update['update_id'])
                accepted.append(record)
            offset = max(offset, update['update_id'] + 1)
        if updates:
            write_atomic(OFFSET, str(offset))
        if accepted or not args.wait or time.time() >= deadline:
            break
    for record in accepted:
        print(json.dumps(record, ensure_ascii=False))
    print(f'accepted {len(accepted)} message(s)')


if __name__ == '__main__':
    main()
