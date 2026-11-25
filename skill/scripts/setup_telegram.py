#!/usr/bin/env python3
"""Connect Korolev to your own Telegram bot (one time per machine).

  1. Create a bot with @BotFather and copy its token.
  2. echo "<token>" | setup_telegram.py      (reads the token from stdin; it is never printed)
  3. Within 5 minutes, send /start to your bot from your own Telegram account.

The wizard records your chat ID and user ID, sends a test message, and writes
~/.config/korolev/telegram.env (mode 0600). Use a bot dedicated to Korolev: the
inbox consumes the bot's updates, so a bot used by another service would lose messages.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG_DIR, TELEGRAM_ENV  # noqa: E402


def call(token, method, params=None, timeout=70):
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        return json.load(urllib.request.urlopen(f'https://api.telegram.org/bot{token}/{method}', data=data,
                                                timeout=timeout))
    except Exception as error:
        raise SystemExit(f'telegram {method} failed: {type(error).__name__}') from None


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        return
    token = sys.stdin.readline().strip()
    if ':' not in token:
        sys.exit('pipe the BotFather token on stdin')
    bot = call(token, 'getMe')['result']
    if call(token, 'getWebhookInfo')['result'].get('url'):
        sys.exit('this bot has a webhook (used by another service?); use a dedicated bot')
    print(f'Bot @{bot["username"]} OK. Now send /start to @{bot["username"]} from your Telegram account (5 min)…')
    offset, deadline = 0, time.time() + 300
    while time.time() < deadline:
        for update in call(token, 'getUpdates', {'offset': offset, 'timeout': 50}, timeout=70).get('result', []):
            offset = update['update_id'] + 1
            message = update.get('message') or {}
            if message.get('text', '').startswith('/start') and message.get('chat', {}).get('type') == 'private':
                user = message['from']['id']
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                old = os.umask(0o077)
                try:
                    TELEGRAM_ENV.write_text(f'KOROLEV_BOT_TOKEN={token}\nKOROLEV_CHAT_ID={message["chat"]["id"]}\n'
                                            f'KOROLEV_OWNER_ID={user}\n')
                finally:
                    os.umask(old)
                TELEGRAM_ENV.chmod(0o600)
                call(token, 'getUpdates', {'offset': offset, 'timeout': 0})
                call(token, 'sendMessage', {'chat_id': message['chat']['id'],
                                            'text': 'Korolev подключён. Здесь будут вопросы по проектам; '
                                                    'отвечайте реплаем или пишите «проект: указание».'})
                print(f'Connected. Config written to {TELEGRAM_ENV} (0600).')
                return
    sys.exit('no /start received within 5 minutes; run again')


if __name__ == '__main__':
    main()
