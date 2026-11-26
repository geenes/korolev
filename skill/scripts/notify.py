#!/usr/bin/env python3
"""Send the owner a message through the Korolev Telegram bot.

  notify.py --project NAME "text"
  notify.py --project NAME --file QUESTIONS.md [--pending]
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
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOT_STATE, TELEGRAM_ENV, append_line, display_name, now, product_dir, touch_heartbeat  # noqa: E402

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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='product name (as in onboard.py) or any label')
    parser.add_argument('--file', help='send this file')
    parser.add_argument('--pending', action='store_true', help='also log as pending in DECISIONS.md')
    parser.add_argument('text', nargs='*')
    args = parser.parse_args()
    touch_heartbeat(args.project, 'Sending the owner a message')
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
