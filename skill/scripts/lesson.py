#!/usr/bin/env python3
"""Korolev learns from its mistakes: record lessons, propose skill changes, get owner approval.

  lesson.py add --title "…" --change "feature title" --detail "what happened" --proposal "concrete change to Korolev" \
                [--kind mistake|improvement|tooling|cost] [--project NAME] [--safety]
  lesson.py retitle ID --change "feature title"
  lesson.py list [--status pending|proposed|approved|rejected|applied]
  lesson.py propose            send all pending lessons to the owner via Telegram (project "korolev")
  lesson.py resolve ID approved|rejected|applied [--note "…"]

Lessons must be generic: describe the pattern, not customer data, secrets or private
details. Approved lessons are implemented in Korolev's own repo through the normal
build loop (product "korolev"). --safety marks changes to Korolev's golden rules;
those always need the owner's explicit approval and are never applied silently.
Storage: $KOROLEV_HOME/lessons/lessons.jsonl (append-only event log).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME, append_line, now  # noqa: E402
from i18n import t  # noqa: E402

LOG = HOME / 'lessons' / 'lessons.jsonl'


def events():
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]


def lessons():
    state = {}
    for event in events():
        if event['event'] == 'add':
            state[event['id']] = dict(event, status='pending')
        elif event['id'] in state:
            item = state[event['id']]
            if event['event'] == 'retitle':
                item['change'] = event['change']
            else:
                item['status'] = event['status']
            item.setdefault('history', []).append(event)
    return state


def next_id():
    day = now()[:10].replace('-', '')
    count = sum(1 for e in events() if e['event'] == 'add' and e['id'].startswith(f'L-{day}-'))
    return f'L-{day}-{count + 1}'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('add')
    a.add_argument('--title', required=True)
    a.add_argument('--change', required=True, help='short feature title in plain language')
    a.add_argument('--detail', required=True)
    a.add_argument('--proposal', required=True)
    a.add_argument('--kind', default='mistake', choices=['mistake', 'improvement', 'tooling', 'cost'])
    a.add_argument('--project', default='')
    a.add_argument('--safety', action='store_true')
    a.add_argument('--status', default='pending', choices=['pending', 'applied'],
                   help='"applied" when the owner already asked for this change directly')
    retitle = sub.add_parser('retitle')
    retitle.add_argument('id')
    retitle.add_argument('--change', required=True, help='short feature title in plain language')
    lst = sub.add_parser('list')
    lst.add_argument('--status')
    sub.add_parser('propose')
    r = sub.add_parser('resolve')
    r.add_argument('id')
    r.add_argument('status', choices=['approved', 'rejected', 'applied'])
    r.add_argument('--note', default='')
    args = parser.parse_args()

    if args.command == 'add':
        lesson_id = next_id()
        append_line(LOG, {'event': 'add', 'id': lesson_id, 'time': now(), 'kind': args.kind, 'title': args.title,
                          'change': args.change, 'detail': args.detail, 'proposal': args.proposal, 'project': args.project,
                          'safety': args.safety})
        if args.status != 'pending':
            append_line(LOG, {'event': 'status', 'id': lesson_id, 'time': now(), 'status': args.status,
                              'note': 'requested directly by the owner'})
        print(lesson_id)
    elif args.command == 'retitle':
        if args.id not in lessons():
            sys.exit(f'unknown lesson {args.id}')
        append_line(LOG, {'event': 'retitle', 'id': args.id, 'time': now(), 'change': args.change})
        print(f'{args.id} -> {args.change}')
    elif args.command == 'list':
        for item in lessons().values():
            if not args.status or item['status'] == args.status:
                flag = ' [SAFETY]' if item.get('safety') else ''
                title = item.get('change') or item['title']
                print(f"{item['id']} {item['status']:9} {item['kind']:11}{flag} {title}")
    elif args.command == 'propose':
        pending = [l for l in lessons().values() if l['status'] == 'pending']
        if not pending:
            print('no pending lessons')
            return
        lines = [t('lesson_header'), '']
        for number, item in enumerate(pending, 1):
            flag = ' ' + t('lesson_safety') if item.get('safety') else ''
            lines += [
                f"{number}. {item['title']}{flag}",
                '   ' + t('lesson_detail', detail=item['detail']),
                '   ' + t('lesson_proposal', proposal=item['proposal']),
                '   ' + t('lesson_options'),
                '',
            ]
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('notify.py')),
                                 '--project', 'korolev', '\n'.join(lines)], capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(result.stderr.strip() or 'notify failed')
        message = json.loads(result.stdout)['sent'][0]
        for number, item in enumerate(pending, 1):
            append_line(LOG, {'event': 'status', 'id': item['id'], 'time': now(), 'status': 'proposed',
                              'message_id': message, 'number': number})
        print(f'proposed {len(pending)} lesson(s) in message {message}')
    elif args.command == 'resolve':
        if args.id not in lessons():
            sys.exit(f'unknown lesson {args.id}')
        append_line(LOG, {'event': 'status', 'id': args.id, 'time': now(), 'status': args.status, 'note': args.note})
        print(f'{args.id} -> {args.status}')


if __name__ == '__main__':
    main()
