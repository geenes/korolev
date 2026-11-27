"""Single-use owner approvals for actions bound to immutable objects."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from pathlib import Path

from common import receiver_state

DECISION_ID = r'R-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}'
FULL_SHA = r'(?:[0-9a-f]{40}|[0-9a-f]{64})'
APPROVAL_REPLY = re.compile(rf'(?i:approve)\s+((?i:{DECISION_ID}))\s+({FULL_SHA})')
ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
SCHEMA = 'korolev-approval-v2'


class ApprovalError(ValueError):
    pass


class ReceiptExpired(ApprovalError):
    pass


def _root():
    root = receiver_state()
    uid = os.geteuid()
    if not root.is_absolute() or '..' in root.parts:
        raise ApprovalError('ambiguous receiver state path')
    info = root.lstat()
    if info.st_uid != uid or info.st_mode != stat.S_IFDIR | 0o700:
        raise ApprovalError('receiver state must be owner-only (0700)')
    for parent in root.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
                or info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
            raise ApprovalError('untrusted receiver state ancestry')
    return root


def _private(info):
    if info.st_mode != stat.S_IFREG | 0o600 or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ApprovalError('receiver files must be owner-only regular files (0600)')


def _read_private(name, limit):
    with os.fdopen(os.open(_root() / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        _private(os.fstat(stream.fileno()))
        return stream.read(limit + 1)


def _receiver_key():
    try:
        key = _read_private('approval.key', 32)
    except OSError as error:
        raise ApprovalError('trusted receiver key unavailable') from error
    if len(key) != 32:
        raise ApprovalError('receiver key must be 32 bytes')
    return key


def pairing():
    try:
        pairs = json.loads(_read_private('pairing.json', 256), object_pairs_hook=tuple)
        pair = dict(pairs)
        if (not isinstance(pairs, tuple) or len(pairs) != 3 or set(pair) != {'bot_id', 'chat_id', 'owner_id'}
                or any(type(value) is not int or value <= 0 for value in pair.values())):
            raise ValueError
    except (OSError, ValueError, TypeError) as error:
        raise ApprovalError('verified receiver pairing unavailable') from error
    return pair


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decision_digest(record):
    fields = ('decision_id', 'project', 'action', 'sha', 'destination', 'details')
    return hashlib.sha256(canonical([SCHEMA, {field: record[field] for field in fields}])).hexdigest()


def approval_prompt(record):
    request = hashlib.sha256(canonical([decision_digest(record), record['expires_at'], record['nonce']])).hexdigest()
    return (f'Decision commitment: {request}\nReply to this message within 24 hours with\n'
            f'approve {record["decision_id"]} {record["sha"]}')


def request_text(record):
    details = record['details']
    plan = details['action_set'] if record['action'] == 'release' else details
    context = details.get('request_context', '')
    lines = [f'📁 {record["project"]}', '', f'Action: {record["action"]}', f'Commit: {record["sha"]}',
             f'Destination: {json.dumps(record["destination"])}']
    shown = plan
    if record['action'] == 'release' and 'Separate local approval' in plan:
        folder, *binding = plan['Separate local approval']
        owner_home = receiver_state().parents[2].resolve()
        if Path(folder).is_relative_to(owner_home):
            relative = Path(folder).relative_to(owner_home)
            shown = plan | {'Separate local approval': [f'~/{relative}', *binding]}
    lines.extend(f'{key}: {json.dumps(value)}' for key, value in shown.items() if key != 'request_context')
    if record['action'] == 'release':
        lines.extend((f'Action set SHA-256: {details["action_set_sha256"]}',
                      f'Full release notes SHA-256: {details["notes_sha256"]}'))
    return '\n'.join([*lines, '', context, approval_prompt(record)])


@contextmanager
def transaction():
    try:
        path = _root() / 'approvals.sqlite'
        _private(path.lstat())
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                _private(path.with_name(path.name + suffix).lstat())
            except FileNotFoundError:
                pass
    except OSError as error:
        raise ApprovalError('trusted receiver store unavailable') from error
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS approvals (
                decision_id TEXT PRIMARY KEY, action TEXT NOT NULL, project TEXT NOT NULL,
                destination TEXT NOT NULL, sha TEXT NOT NULL, details TEXT NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL, request_message_id TEXT,
                source_message_id TEXT, approved_sha TEXT
            )''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(approvals)')}
            for name in ('nonce', 'request_sha256', 'receipt', 'claimed', 'delivery'):
                if name not in columns:
                    db.execute(f'ALTER TABLE approvals ADD COLUMN {name} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                nonce TEXT PRIMARY KEY, delivery TEXT NOT NULL, receipt TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS executions (
                nonce TEXT PRIMARY KEY, decision_id TEXT NOT NULL, attempt TEXT NOT NULL, active INTEGER NOT NULL,
                child_id TEXT UNIQUE, child TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS revocations (nonce TEXT PRIMARY KEY)')
            db.execute("""INSERT OR IGNORE INTO revocations
                          SELECT nonce FROM approvals WHERE state = 'stale' AND nonce IS NOT NULL""")
            db.execute('''CREATE TABLE IF NOT EXISTS notes_deliveries (
                nonce TEXT PRIMARY KEY, delivery TEXT NOT NULL)''')
            # Persist the expiry even when the requested operation is refused.
            db.execute("""UPDATE approvals SET state = 'expired'
                          WHERE state IN ('pending', 'approved') AND expires_at <= ?""", (time.time(),))
            db.execute('SAVEPOINT operation')
            try:
                yield db
            except ApprovalError as error:
                db.execute('ROLLBACK TO operation')
                refusal = error
            else:
                refusal = None
        if refusal:
            raise refusal


def create(action, project, destination, sha, *, expires_in=24 * 60 * 60,
           version_authorization=None, **details):
    """Use the newest displayed landing SHA, or actual M for its separate local update."""
    if not re.fullmatch(FULL_SHA, sha):
        raise ApprovalError('approval requires a full SHA')
    with transaction() as db:
        if version_authorization:
            owner_id, base, version_details = version_authorization
            parent = _record(db, owner_id)
            _verify(db, parent, base, 'release-version', project, destination, ('approved', 'consumed'))
            if (action != 'release' or details.get('base') != base or details.get('version') != version_details.get('version')
                    or details.get('action_set', {}).get('Published boundary') != version_details.get('published_boundary')
                    or {key: value for key, value in parent['details'].items() if key != 'request_context'} != version_details):
                raise ApprovalError('approval does not match the requested transition')
            if parent['state'] == 'approved':
                _consume(db, parent)
            details['version_decision'] = owner_id
        while True:
            decision_id = 'R-' + ''.join(secrets.choice(ALPHABET) for _ in range(5))
            result = db.execute('''INSERT INTO approvals
                (decision_id, action, project, destination, sha, details, expires_at, nonce, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT(decision_id) DO NOTHING''',
                (decision_id, action, project, canonical(destination).decode(), sha, canonical(details).decode(),
                 time.time() + expires_in, secrets.token_hex(32)))
            if result.rowcount:
                if version_authorization:
                    child = _record(db, decision_id)
                    linked = db.execute('''UPDATE executions SET child_id = ?, child = ?
                                           WHERE nonce = ? AND child_id IS NULL''',
                                        (decision_id, _child_commitment(child), parent['nonce']))
                    if not linked.rowcount:
                        raise ApprovalError('version authorization already has a child')
                return decision_id


def _record(db, decision_id):
    row = db.execute('SELECT * FROM approvals WHERE decision_id = ?', (decision_id,)).fetchone()
    if row is None:
        raise ApprovalError('unknown approval decision ID')
    record = dict(row)
    for key in ('destination', 'details'):
        record[key] = json.loads(record[key])
    record['delivery'] = json.loads(record['delivery'] or '{}')
    record['receipt'] = json.loads(record['receipt'] or 'null')
    return record


def get(decision_id):
    with transaction() as db:
        return _record(db, decision_id)


def verified_payload(receipt, digest, pair):
    return (_paired_message(receipt, pair) and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
            and receipt.get('sha256') == digest and (receipt.get('mode'), receipt.get('verification')) in
            (('inline', 'message-echo'), ('document', 'file-sha256')))


def _save_notes_delivery(decision_id, nonce, receipt):
    """Trusted sender only, after confirming the echoed message or downloaded bytes."""
    with transaction() as db:
        record = _record(db, decision_id)
        _require_unrevoked(db, record)
        if (record['state'] != 'pending' or not nonce or record['nonce'] != nonce
                or record['action'] != 'release' or _request(db, record)
                or not verified_payload(receipt, record['details'].get('notes_sha256'), pairing())):
            raise ApprovalError('confirmed notes delivery does not match the pending release')
        saved = db.execute('INSERT INTO notes_deliveries VALUES (?, ?) ON CONFLICT(nonce) DO NOTHING',
                           (nonce, canonical(receipt).decode()))
        if not saved.rowcount:
            raise ApprovalError('notes delivery was already recorded for this release')


def _payloads(db, record, pair):
    if record['action'] != 'release':
        return {}
    receipt = record['delivery'].get('notes_delivery')
    saved = db.execute('SELECT delivery FROM notes_deliveries WHERE nonce = ?', (record['nonce'],)).fetchone()
    if not saved or not verified_payload(receipt, record['details'].get('notes_sha256'), pair):
        raise ApprovalError('no verified delivery of the exact release notes to the paired destination')
    if canonical(receipt).decode() != saved['delivery']:
        raise ApprovalError('bound review payload differs from confirmed delivery')
    return {'notes': receipt}


def _paired_message(receipt, pair):
    return (isinstance(receipt, dict)
            and all(type(receipt.get(key)) is int and receipt[key] == pair[key] for key in ('chat_id', 'bot_id'))
            and type(receipt.get('message_id')) is int and receipt['message_id'] > 0)


def bind_message(decision_id, delivery, *, notes_delivery=None):
    """Record the trusted sender's confirmed Telegram echo."""
    with transaction() as db:
        record = _record(db, decision_id)
        _require_unrevoked(db, record)
        pair = pairing()
        digest = hashlib.sha256(request_text(record).encode()).hexdigest()
        if (not _paired_message(delivery, pair) or set(delivery) != {'message_id', 'chat_id', 'bot_id', 'sha256'}
                or delivery['sha256'] != digest):
            raise ApprovalError('approval request does not match the displayed decision or pairing')
        link = _version_link(db, record)
        payloads = _payloads(db, record, pair)
        if (payloads.get('notes') != notes_delivery
                or notes_delivery and notes_delivery['message_id'] == delivery['message_id']):
            raise ApprovalError('bound review payload differs from confirmed delivery')
        binding = canonical({'message': delivery, 'pairing': pair, 'payloads': payloads}).decode()
        saved = _request(db, record)
        if saved and saved['delivery'] == binding:
            return
        if record['state'] != 'pending' or not record['nonce'] or record['request_message_id'] or saved:
            raise ApprovalError('cannot bind a message to this decision')
        if link:
            parent = _record(db, link['decision_id'])
            if parent['nonce'] != link['nonce']:
                raise ApprovalError('version authorization nonce changed')
            _verify(db, parent, parent['sha'], 'release-version', record['project'], record['destination'], ('consumed',))
            _finish(db, parent, 'done')
        db.execute('INSERT INTO requests (nonce, delivery) VALUES (?, ?)', (record['nonce'], binding))
        db.execute('UPDATE approvals SET request_message_id = ?, request_sha256 = ? WHERE decision_id = ?',
                   (str(delivery['message_id']), digest, decision_id))


def _request(db, record):
    return db.execute('SELECT * FROM requests WHERE nonce = ?', (record['nonce'],)).fetchone()


def _require_unrevoked(db, record):
    if db.execute('SELECT 1 FROM revocations WHERE nonce = ?', (record['nonce'],)).fetchone():
        raise ApprovalError('approval is stale because its nonce is revoked')


def _mac(key, purpose, value):
    return hmac.digest(key, purpose + canonical(value), 'sha256').hex()


def _signed_receipt(key, record, binding, source_message_id):
    fields = dict(schema=SCHEMA, project=record['project'], digest=decision_digest(record),
                  expires_at=record['expires_at'], nonce=record['nonce'],
                  delivery=json.loads(binding), source_message_id=source_message_id)
    return fields | {'mac': _mac(key, b'receipt\0', fields)}


def issue_receipt(message, *, bot_id):
    """Receiver only. Supply the authenticated Telegram message from the supervised bot channel."""
    with transaction() as db:
        pair = pairing()
        try:
            original = message['reply_to_message']
            match = APPROVAL_REPLY.fullmatch(message['text'].strip())
            source = message['message_id']
            identity = dict(bot_id=original['from']['id'], chat_id=original['chat']['id'],
                            message_id=original['message_id'])
            if (not match or type(bot_id) is not int or bot_id != pair['bot_id'] or not _paired_message(identity, pair)
                    or type(source) is not int or source <= 0 or source == identity['message_id']
                    or type(message['from']['id']) is not int or message['from']['id'] != pair['owner_id']
                    or message['from']['is_bot'] is not False
                    or original['from']['is_bot'] is not True
                    or any(type(msg['chat']['id']) is not int or msg['chat']['id'] != pair['chat_id']
                           or msg['chat']['type'] != 'private' for msg in (message, original))):
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ApprovalError('approval requires a paired private owner reply to its request message') from error
        decision_id, sha = match[1].upper(), match[2]
        record = _record(db, decision_id)
        _require_unrevoked(db, record)
        saved = _request(db, record)
        if not saved or str(identity['message_id']) != record['request_message_id']:
            raise ApprovalError('approval must reply to its request message')
        binding = json.loads(saved['delivery'])
        shown = original.get('text')
        if (sha != record['sha'] or shown != request_text(record) or binding['pairing'] != pair
                or binding['message'] != identity | {'sha256': hashlib.sha256(shown.encode()).hexdigest()}
                or record['request_sha256'] != binding['message']['sha256']):
            raise ApprovalError('approval does not match the displayed decision')
        if binding['payloads'] != _payloads(db, record, pair):
            raise ApprovalError('bound review payload delivery changed')
        receipt = _signed_receipt(_receiver_key(), record, saved['delivery'], str(source))
        encoded = canonical(receipt).decode()
        if saved['receipt'] == encoded:
            return receipt
        if record['state'] != 'pending' or saved['receipt'] or time.time() >= record['expires_at']:
            raise ApprovalError(f"approval is {record['state']}")
        db.execute('UPDATE requests SET receipt = ? WHERE nonce = ?', (encoded, record['nonce']))
        db.execute("""UPDATE approvals SET state = 'approved', source_message_id = ?, approved_sha = ?, receipt = ?
                      WHERE decision_id = ?""", (str(source), sha, encoded, decision_id))
        return receipt


def update_delivery(decision_id, **details):
    with transaction() as db:
        record = _record(db, decision_id)
        db.execute('UPDATE approvals SET delivery = ? WHERE decision_id = ?',
                   (json.dumps(record['delivery'] | details), decision_id))


def cancel_unbound_requests(project):
    """Cancel requests whose delivery may have outlived the sending process."""
    with transaction() as db:
        rows = db.execute("""SELECT decision_id FROM approvals WHERE project = ?
                             AND state IN ('pending', 'stale', 'expired')
                             AND request_message_id IS NULL
                             AND action IN ('release', 'release-version')""", (project,))
        records = [_record(db, row['decision_id']) for row in rows]
        records = [record for record in records
                   if record['state'] == 'pending' or record['delivery'].get('delivery') == 'sending']
        for record in records:
            if record['state'] != 'stale':
                _restore_unbound_version(db, record)
                _finish(db, record, 'stale')
        return records


def terminal_releases(project):
    with transaction() as db:
        rows = db.execute("""SELECT decision_id FROM approvals WHERE project = ? AND action = 'release'
                             AND state IN ('done', 'expired', 'failed', 'stale')""", (project,))
        return [_record(db, row['decision_id']) for row in rows]


def _verify(db, record, sha, action, project, destination, states):
    _require_unrevoked(db, record)
    if record['state'] == 'expired' or time.time() >= record['expires_at']:
        raise ReceiptExpired('approval is expired')
    if record['state'] not in states:
        raise ApprovalError(f"approval is {record['state']}")
    if record['action'] != action or record['project'] != project:
        raise ApprovalError('approval belongs to a different action or project')
    if (sha != record['sha'] or sha != record['approved_sha']
            or not record['source_message_id'] or not record['request_message_id']):
        raise ApprovalError('owner approval requires the exact SHA and a bound reply')
    if canonical(record['destination']) != canonical(destination):
        raise ApprovalError('approval does not match the destination')
    receipt = record['receipt']
    if not isinstance(receipt, dict) or not record['nonce'] or not record['request_sha256']:
        raise ApprovalError('receiver-signed receipt required')
    saved = _request(db, record)
    if not saved or not saved['receipt']:
        raise ApprovalError('receiver receipt does not match the decision or current nonce')
    binding = json.loads(saved['delivery'])
    message = binding['message']
    expected = _signed_receipt(_receiver_key(), record, saved['delivery'], record['source_message_id'])
    if (not hmac.compare_digest(canonical(receipt), canonical(expected)) or receipt != json.loads(saved['receipt'])
            or str(message['message_id']) != record['request_message_id']
            or message['sha256'] != record['request_sha256']):
        raise ApprovalError('receiver receipt does not match the decision or current nonce')
    pair = pairing()
    if binding['pairing'] != pair or binding['payloads'] != _payloads(db, record, pair):
        raise ApprovalError('receiver receipt delivery or pairing changed')
    execution = db.execute('SELECT * FROM executions WHERE nonce = ?', (record['nonce'],)).fetchone()
    if record['state'] == 'approved' and execution:
        raise ApprovalError('approval nonce already consumed')
    if record['state'] == 'consumed' and (
            not execution or not execution['active'] or execution['decision_id'] != record['decision_id']
            or execution['attempt'] != record['claimed']):
        raise ApprovalError('approval claim state does not match the receipt')


def _consume(db, record):
    if time.time() >= record['expires_at']:
        raise ReceiptExpired('approval is expired')
    attempt = secrets.token_hex(32)
    db.execute('INSERT INTO executions (nonce, decision_id, attempt, active) VALUES (?, ?, ?, 1)',
               (record['nonce'], record['decision_id'], attempt))
    db.execute("UPDATE approvals SET state = 'consumed', claimed = ? WHERE decision_id = ?",
               (attempt, record['decision_id']))


def claim(decision_id, sha, *, action, project, destination, details=None, expected=None):
    """Atomically authorize the first outward step exactly once."""
    if action == 'release-version':
        raise ApprovalError('version approval must reserve a release child when consumed')
    with transaction() as db:
        record = _record(db, decision_id)
        _verify(db, record, sha, action, project, destination, ('approved',))
        if expected is not None and record['receipt'] != expected['receipt']:
            raise ApprovalError('receiver receipt does not match the publication snapshot')
        if details and any(record['details'].get(key) != value for key, value in details.items()):
            raise ApprovalError('approval does not match the requested transition')
        _consume(db, record)
        return _record(db, decision_id)


def verify(decision, *, action, project, destination, states=('approved', 'consumed')):
    """Verify a publisher's snapshot against current authority, including on resume."""
    with transaction() as db:
        current = _record(db, decision['decision_id'])
        _verify(db, current, decision['sha'], action, project, destination, states)
        if (decision_digest(current) != decision_digest(decision) or current['receipt'] != decision['receipt']
                or current['claimed'] != decision.get('claimed')):
            raise ApprovalError('receiver receipt does not match the publication snapshot')
        if time.time() >= current['expires_at']:
            raise ReceiptExpired('approval is expired')


@contextmanager
def active_execution(decision_id, execution_id, action):
    """Verify the current signed execution and fence revocation through dispatch."""
    if not isinstance(execution_id, str) or not re.fullmatch(r'[0-9a-f]{64}', execution_id):
        raise ApprovalError('active receiver execution ID required')
    with transaction() as db:
        record = _record(db, decision_id)
        _verify(db, record, record['sha'], action, record['project'], record['destination'], ('consumed',))
        if record['claimed'] != execution_id:
            raise ApprovalError('approval does not match the requested execution ID')
        if time.time() >= record['expires_at']:
            raise ReceiptExpired('approval is expired')
        yield record


def _child_commitment(record):
    return canonical([record['nonce'], decision_digest(record)]).decode()


def _version_link(db, record):
    link = db.execute('SELECT * FROM executions WHERE child_id = ?', (record['decision_id'],)).fetchone()
    parent = record['details'].get('version_decision')
    if (parent or link) and (not link or link['decision_id'] != parent or link['child'] != _child_commitment(record)):
        raise ApprovalError('version authorization does not match the reserved child')
    return link


def _restore_unbound_version(db, record):
    link = _version_link(db, record)
    if link and not _request(db, record):
        db.execute('UPDATE executions SET child_id = NULL, child = NULL WHERE nonce = ?', (link['nonce'],))


def _finish(db, record, state):
    if record['state'] == 'consumed':
        updated = db.execute('UPDATE executions SET active = 0 WHERE nonce = ? AND attempt = ? AND active = 1',
                             (record['nonce'], record['claimed']))
        if not updated.rowcount:
            raise ApprovalError('approval execution does not match the active claim')
    if state == 'stale' and record['nonce']:
        db.execute('INSERT OR IGNORE INTO revocations VALUES (?)', (record['nonce'],))
    db.execute('UPDATE approvals SET state = ? WHERE decision_id = ?', (state, record['decision_id']))


def mark(decision_id, state):
    allowed = {'pending': {'stale'}, 'approved': {'stale'}, 'consumed': {'done', 'failed', 'stale', 'expired'}}
    with transaction() as db:
        record = _record(db, decision_id)
        if state not in allowed.get(record['state'], ()):
            raise ApprovalError(f"cannot change approval from {record['state']} to {state}")
        if state == 'done':
            if record['action'] == 'release-version':
                raise ApprovalError('version approval can finish only when binding its reserved child')
            # The proposal row cannot disguise a reserved version execution as another action.
            saved = _request(db, record)
            if not saved or not saved['receipt'] or json.loads(saved['receipt'])['digest'] != decision_digest(record):
                raise ApprovalError('approval does not match the receiver execution')
        if state == 'stale':
            _restore_unbound_version(db, record)
        _finish(db, record, state)
