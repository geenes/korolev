"""Offline tests for the shared single-use approval store."""
import json
import os
import stat
import subprocess
import sys

if __package__:
    from .test_skill import SCRIPTS, Sandbox
else:
    from test_skill import SCRIPTS, Sandbox


SHA = '1' * 40
OLDER = '2' * 40
MERGED = '3' * 40
DESTINATION = {'repository_id': 'repo-fixture', 'ref': 'refs/heads/main'}


def provision_receiver(base, key=b'S' * 32):
    directory = base / '.config' / 'korolev' / 'receiver'
    base.mkdir(mode=0o700, exist_ok=True)
    base.chmod(0o700)
    for path in (base / '.config', directory.parent, directory):
        path.mkdir(mode=0o700)
    for name, data in (('approval.key', key), ('approvals.sqlite', b''),
                       ('pairing.json', b'{"bot_id":123,"chat_id":321,"owner_id":321}')):
        with os.fdopen(os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
            stream.write(data)
    return directory


def receiver_fixture(base):
    return f"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import common
patch.object(common, 'pwd', SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_dir={str(base)!r}))).start()
"""


REQUEST = f"""
import sys
import approvals
import hashlib
from approvals import ApprovalError

def delivery(text, **changes):
    return dict(sha256=hashlib.sha256(text.encode()).hexdigest(), chat_id=321, bot_id=123, message_id=10) | changes

def create(action='merge', sha={SHA!r}, destination=None, **details):
    decision = approvals.create(action, 'demo', destination or {DESTINATION!r}, sha, **details)
    text = approvals.request_text(approvals.get(decision))
    approvals.bind_message(decision, delivery(text))
    return decision, text

def issue(decision, request, sha={SHA!r}, *, chat_id=321, original_chat=321, message_id=11, reply_to=10,
          original_bot=123, bot_id=123, owner=321, kind='private'):
    original = {{'chat': dict(id=original_chat, type='private'), 'from': dict(id=original_bot, is_bot=True),
                 'message_id': reply_to, 'text': request}}
    message = {{'chat': dict(id=chat_id, type=kind), 'from': dict(id=owner, is_bot=False),
                'message_id': message_id, 'text': f'approve {{decision}} {{sha}}', 'reply_to_message': original}}
    return approvals.issue_receipt(message, bot_id=bot_id)

def claim(decision, **changes):
    args = dict(action='merge', project='demo', destination={DESTINATION!r}) | changes
    return approvals.claim(decision, {SHA!r}, **args)

def confirm_notes(record, payload):
    sys.path.insert(0, {str(SCRIPTS.parent.parent / 'tests')!r})
    import notify
    from test_notify import FakeTelegram
    from unittest.mock import patch
    telegram = FakeTelegram()
    with patch.object(notify, 'review_urlopen', telegram), patch.object(notify, 'append_line'):
        notes = notify.send_review_payload(telegram.env, record['project'], 'notes.md', payload,
                                           proposal=(record['decision_id'], record['nonce']))
    assert telegram.payload == payload
    return notes

def refuses(fn, reason):
    try:
        fn()
    except ApprovalError as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError('approval unexpectedly accepted')
"""


class ApprovalStoreTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.receiver = provision_receiver(self.base)
        self.key = self.receiver / 'approval.key'

    def py(self, code):
        return super().py(receiver_fixture(self.base) + code)

    def test_signed_claim_delivery_replay_and_finish(self):
        result = self.py(REQUEST + """
import sqlite3
decision, text = create(ticket=7)
receipt = issue(decision, text)
approvals.update_delivery(decision, status='sent')
assert issue(decision, text) == receipt
refuses(lambda: issue(decision, text, message_id=12), 'approved')
refuses(lambda: issue(decision, text, chat_id=8), 'private owner reply')
record = claim(decision)
assert issue(decision, text) == receipt
approvals.mark(decision, 'done')
assert issue(decision, text) == receipt
with sqlite3.connect(approvals.receiver_state() / 'approvals.sqlite') as db:
    mode = db.execute('PRAGMA journal_mode').fetchone()[0]
print(approvals.json.dumps([record, approvals.get(decision)['state'], mode]))
""")
        record, state, mode = json.loads(result)
        self.assertRegex(record['decision_id'], r'^R-[2-9A-Z]{5}$')
        self.assertEqual(record['state'], 'consumed')
        self.assertEqual(record['approved_sha'], SHA)
        self.assertEqual(record['source_message_id'], '11')
        self.assertEqual(record['details'], {'ticket': 7})
        self.assertEqual(record['receipt']['digest'], self.py(
            f'import approvals; print(approvals.decision_digest(approvals.get({record["decision_id"]!r})))'))
        self.assertEqual(record['receipt']['nonce'], record['nonce'])
        self.assertEqual(record['receipt']['expires_at'], record['expires_at'])
        self.assertEqual((state, mode), ('done', 'wal'))
        self.assertEqual(stat.S_IMODE((self.receiver / 'approvals.sqlite').stat().st_mode), 0o600)

    def test_exact_request_newest_sha_and_separate_actual_merge(self):
        self.py(REQUEST + f"""
decision, text = create(actions=[['adopt', 'main', 'absent', {OLDER!r}],
                                 ['land', 'main', {OLDER!r}, {SHA!r}]])
refuses(lambda: issue(decision, text, {OLDER!r}), 'displayed decision')
refuses(lambda: issue(decision, text, '9' * 40), 'displayed decision')
refuses(lambda: issue(decision, text, reply_to=12), 'request message')
refuses(lambda: issue(decision, 'Different actions\\n' + text), 'displayed decision')
issue(decision, text)
refuses(lambda: claim(decision, destination={{'repository_id': 'other'}}), 'destination')
claim(decision)
local = {{'folder': '/live', 'ref': 'refs/heads/main'}}
update, text = create('local-update', {MERGED!r}, destination=local,
                      actions=[['update', '/live', {OLDER!r}, {MERGED!r}]])
refuses(lambda: issue(update, text), 'displayed decision')
issue(update, text, {MERGED!r})
assert approvals.claim(update, {MERGED!r}, action='local-update', project='demo',
                       destination=local)['state'] == 'consumed'
""")

    def test_expiry_and_stale_nonce_at_issue_claim_and_resume(self):
        self.py(REQUEST + """
from unittest.mock import patch
decision, text = create()
issue(decision, text)
record = approvals.get(decision)
another, request = create()
with patch('approvals.time.time', return_value=record['expires_at'] + 100):
    refuses(lambda: claim(decision), 'expired')
    refuses(lambda: issue(another, request), 'expired')
decision, text = create()
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET nonce = 'changed' WHERE decision_id = ?", (decision,))
refuses(lambda: issue(decision, text), 'request message')
decision, text = create()
issue(decision, text)
record = claim(decision)
verify = lambda: approvals.verify(record, action='merge', project='demo', destination=record['destination'])
with patch('approvals.time.time', return_value=record['expires_at']):
    refuses(verify, 'expired')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET nonce = 'changed' WHERE decision_id = ?", (decision,))
refuses(verify, 'current nonce')
""")

    def test_provisioning_modes_and_missing_key_fail_closed(self):
        self.key.chmod(0o644)
        self.py(REQUEST + """
decision, text = create()
refuses(lambda: issue(decision, text), '0600')
assert approvals.get(decision)['state'] == 'pending'
""")
        self.key.chmod(0o600)
        self.key.parent.chmod(0o755)
        self.py(REQUEST + """
refuses(lambda: approvals.get('R-23456'), '0700')
""")
        self.key.parent.chmod(0o700)
        self.key.unlink()
        self.py(REQUEST + """
decision, text = create()
refuses(lambda: issue(decision, text), 'trusted receiver key unavailable')
""")

    def test_atomic_claim_has_one_winner(self):
        decision = self.py(REQUEST + """
decision, text = create()
issue(decision, text)
print(decision)
""")
        code = f'import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n' + receiver_fixture(self.base) + REQUEST
        code += f'print(claim({decision!r})["state"])'
        attempts = [
            subprocess.Popen([sys.executable, '-c', code], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        results = [attempt.communicate(timeout=10) for attempt in attempts]
        self.assertEqual(sorted(attempt.returncode for attempt in attempts), [0, 1])
        self.assertCountEqual([out.strip() for out, _ in results], ['consumed', ''])
        self.assertIn('approval is consumed', ''.join(error for _, error in results))

    def test_revocation_survives_restored_row_and_blocks_idempotent_issue(self):
        self.py(REQUEST + """
decision, text = create()
receipt = issue(decision, text)
assert issue(decision, text) == receipt
approvals.mark(decision, 'stale')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='approved' WHERE decision_id=?", (decision,))
refuses(lambda: issue(decision, text), 'nonce is revoked')
refuses(lambda: claim(decision), 'nonce is revoked')
""")

    def test_concurrent_claim_and_revocation_leave_no_resumable_execution(self):
        self.py(REQUEST + """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
decision, text = create()
issue(decision, text)
barrier = Barrier(2)
def race():
    barrier.wait()
    try:
        return claim(decision)
    except ApprovalError as error:
        assert 'nonce is revoked' in str(error)
        return None
with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(race)
    barrier.wait()
    approvals.mark(decision, 'stale')
    execution = future.result(timeout=10)
with approvals.transaction() as db:
    assert db.execute('SELECT COUNT(*) FROM revocations').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM executions WHERE active=1').fetchone()[0] == 0
    db.execute('UPDATE approvals SET state=? WHERE decision_id=?',
               ('consumed' if execution else 'approved', decision))
if execution:
    refuses(lambda: approvals.verify(execution, action='merge', project='demo',
                                     destination=execution['destination']), 'nonce is revoked')
else:
    refuses(lambda: claim(decision), 'nonce is revoked')
""")

    def test_notes_metadata_cannot_substitute_for_sender_delivery_or_cross_nonces(self):
        self.py(REQUEST + f"""
payload = b'Notes with exact whitespace.\\r\\n'
digest = hashlib.sha256(payload).hexdigest()
def proposal():
    decision = approvals.create('release', 'demo', {DESTINATION!r}, {SHA!r},
                                action_set={{}}, action_set_sha256='b' * 64, notes_sha256=digest)
    return approvals.get(decision)
def bind(record, notes):
    text = approvals.request_text(record)
    approvals.bind_message(record['decision_id'], delivery(text), notes_delivery=notes)
    return text
first = proposal()
refuses(lambda: bind(first, None), 'no verified delivery')
synthetic = dict(sha256=digest, mode='inline', verification='message-echo', bot_id=123, chat_id=321, message_id=99)
approvals.update_delivery(first['decision_id'], notes_delivery=synthetic)
refuses(lambda: bind(first, synthetic), 'no verified delivery')
notes = confirm_notes(first, payload)
assert notes == synthetic
text = bind(first, notes)
receipt = issue(first['decision_id'], text)
assert receipt['delivery']['payloads']['notes'] == notes
second = proposal()
approvals.update_delivery(second['decision_id'], notes_delivery=notes)
refuses(lambda: bind(second, notes), 'no verified delivery')
with approvals.transaction() as db:
    db.execute('DELETE FROM notes_deliveries WHERE nonce=?', (first['nonce'],))
refuses(lambda: issue(first['decision_id'], text), 'no verified delivery')
assert approvals.get(second['decision_id'])['receipt'] is None
""")

    def test_unsigned_legacy_and_recomputed_rows_are_refused(self):
        self.py(REQUEST + f"""
import json
for legacy in (True, False):
    decision, text = create(actions=[['land', 'main', 'absent', {SHA!r}]])
    record = approvals.get(decision)
    if legacy:
        receipt = nonce = None
    else:
        nonce = record['nonce']
        receipt = json.dumps({{'digest': approvals.decision_digest(record), 'mac': 'a' * 64}})
    with approvals.transaction() as db:
        db.execute("UPDATE approvals SET state='approved', approved_sha=?, source_message_id='11', "
                   "nonce=?, receipt=? WHERE decision_id=?", ({SHA!r}, nonce, receipt, decision))
    refuses(lambda: claim(decision), 'receipt')
""")

    def test_signed_approved_row_cannot_pose_as_a_consumed_claim(self):
        self.py(REQUEST + """
decision, text = create()
issue(decision, text)
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='consumed' WHERE decision_id=?", (decision,))
record = approvals.get(decision)
refuses(lambda: approvals.verify(record, action='merge', project='demo', destination=record['destination']),
        'claim state')
""")

    def test_suffix_only_and_foreign_reply_identities_are_refused(self):
        self.py(REQUEST + """
decision = approvals.create('merge', 'demo', {'ref': 'main'}, '1' * 40,
                            actions=[['land', 'main', 'absent', '1' * 40]])
record = approvals.get(decision)
prompt = approvals.approval_prompt(record)
refuses(lambda: approvals.bind_message(decision, delivery(prompt)), 'displayed decision')
text = approvals.request_text(record)
approvals.bind_message(decision, delivery(text))
refuses(lambda: issue(decision, prompt), 'displayed decision')
for changes in ({'chat_id': 322}, {'original_chat': 322}, {'original_bot': 124}, {'bot_id': 124},
                {'owner': 322}, {'kind': 'group'}):
    refuses(lambda: issue(decision, text, **changes), 'paired private owner reply')
assert approvals.get(decision)['state'] == 'pending'
first = issue(decision, text)
assert first == issue(decision, text)
refuses(lambda: issue(decision, text, message_id=12), 'approved')
""")

    def test_proposal_row_rollback_cannot_reclaim_or_resume_another_execution(self):
        self.py(REQUEST + """
decision, text = create()
issue(decision, text)
execution = claim(decision)
def verify(snapshot):
    approvals.verify(snapshot, action='merge', project='demo', destination=execution['destination'],
                     states=('consumed',))
verify(execution)
refuses(lambda: verify(execution | {'claimed': 'different-attempt'}), 'publication snapshot')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='approved', claimed=NULL WHERE decision_id=?", (decision,))
refuses(lambda: claim(decision), 'nonce already consumed')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='consumed', claimed=? WHERE decision_id=?",
               (execution['claimed'], decision))
verify(execution)
approvals.mark(decision, 'done')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='consumed' WHERE decision_id=?", (decision,))
refuses(lambda: verify(execution), 'claim state')
""")

    def test_expiry_is_checked_after_waiting_for_the_write_lock(self):
        self.py(REQUEST + """
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
decision, text = create()
issue(decision, text)
expires = approvals.get(decision)['expires_at']
clock = [expires - 1]
waiting = Event()
connect = sqlite3.connect
def trace(*args, **kwargs):
    db = connect(*args, **kwargs)
    db.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
    return db
with connect(approvals.receiver_state() / 'approvals.sqlite') as lock, ThreadPoolExecutor() as pool:
    lock.execute('BEGIN IMMEDIATE')
    with patch('approvals.sqlite3.connect', trace), patch('approvals.time.time', lambda: clock[0]):
        future = pool.submit(refuses, lambda: claim(decision), 'expired')
        try:
            assert waiting.wait(5)
            clock[0] = expires
        finally:
            lock.commit()
        future.result(timeout=5)
with approvals.transaction() as db:
    assert db.execute('SELECT COUNT(*) FROM executions').fetchone()[0] == 0
assert approvals.get(decision)['state'] == 'expired'
""")

    def test_environment_cannot_select_a_substitute_key_and_store_at_claim(self):
        alternate = self.base / 'substitute'
        directory = provision_receiver(alternate, b'F' * 32)
        self.py(REQUEST + f"""
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
trusted = approvals.receiver_state()
decision, text = create()
with sqlite3.connect(trusted / 'approvals.sqlite') as source, sqlite3.connect({str(directory / 'approvals.sqlite')!r}) as target:
    source.backup(target)
with patch.object(common.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir={str(alternate)!r})):
    issue(decision, text)
    record = approvals.get(decision)
    approvals.verify(record, action='merge', project='demo', destination=record['destination'])
os.environ.update(HOME={str(alternate)!r}, KOROLEV_HOME={str(directory)!r},
                  KOROLEV_CONFIG_DIR={str(directory.parent)!r}, KOROLEV_BOT_STATE={str(directory)!r})
assert approvals.receiver_state() == trusted
refuses(lambda: claim(decision), 'approval is pending')
fake = approvals.canonical(record['receipt']).decode()
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET state='approved', approved_sha=?, source_message_id=?, receipt=? WHERE decision_id=?",
               (record['sha'], record['source_message_id'], fake, decision))
    db.execute("UPDATE requests SET receipt=? WHERE nonce=?", (fake, record['nonce']))
refuses(lambda: claim(decision), 'receipt does not match the decision')
""")

    def test_pairing_with_duplicate_identity_and_linked_store_are_refused(self):
        (self.receiver / 'pairing.json').write_text('{"bot_id":1,"bot_id":123,"chat_id":321,"owner_id":321}')
        self.py(REQUEST + """
refuses(approvals.pairing, 'verified receiver pairing unavailable')
""")
        store = self.receiver / 'approvals.sqlite'
        target = store.with_name('other.sqlite')
        store.rename(target)
        store.symlink_to(target)
        self.py(REQUEST + """
refuses(lambda: approvals.get('R-23456'), 'owner-only regular files')
""")

    def test_version_reservation_checks_child_identity_action_and_project(self):
        self.py(REQUEST + f"""
import json
transition = dict(released_version='1.0.0', version='1.1.0', published_boundary={{'commit': {OLDER!r}}})
payload = b'Version notes.'
parent, text = create('release-version', **transition)
issue(parent, text)
def reserve(**changes):
    return approvals.create(**(dict(action='release', project='demo', destination={DESTINATION!r},
                             sha={MERGED!r}, base={SHA!r}, version='1.1.0',
                             notes_sha256=hashlib.sha256(payload).hexdigest(),
                             action_set={{'Published boundary': transition['published_boundary']}},
                             action_set_sha256='b' * 64, version_authorization=(parent, {SHA!r}, transition)) | changes))
def bind(child, confirmed=False):
    record = approvals.get(child)
    notes = confirm_notes(record, payload) if confirmed else None
    approvals.update_delivery(child, notes_delivery=notes)
    approvals.bind_message(child, delivery(approvals.request_text(record)), notes_delivery=notes)
refuses(lambda: claim(parent, action='release-version'), 'must reserve')
refuses(lambda: reserve(version='2.0.0'), 'requested transition')
child = reserve()
refuses(lambda: approvals.mark(parent, 'done'), 'only when binding its reserved child')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET action='merge' WHERE decision_id=?", (parent,))
refuses(lambda: approvals.mark(parent, 'done'), 'does not match the receiver execution')
with approvals.transaction() as db:
    db.execute("UPDATE approvals SET action='release-version' WHERE decision_id=?", (parent,))
parent_claim = approvals.get(parent)['claimed']
refuses(reserve, 'already has a child')
record = approvals.get(child)
for field, changed, untouched in (('project', 'other', 'demo'), ('action', 'merge', 'release'),
                                  ('decision_id', 'R-ZZZZZ', child)):
    with approvals.transaction() as db:
        db.execute('UPDATE approvals SET ' + field + '=? WHERE decision_id=?', (changed, child))
    current = changed if field == 'decision_id' else child
    refuses(lambda: bind(current), 'reserved child')
    refuses(lambda: approvals.mark(current, 'stale'), 'reserved child')
    with approvals.transaction() as db:
        db.execute('UPDATE approvals SET ' + field + '=? WHERE decision_id=?', (untouched, current))
assert approvals.get(parent)['state'] == 'consumed'
approvals.mark(child, 'stale')
assert approvals.get(parent)['state'] == 'consumed'
replacement = reserve()
assert replacement != child and approvals.get(parent)['claimed'] == parent_claim
bind(replacement, confirmed=True)
assert approvals.get(parent)['state'] == 'done'
assert approvals.get(replacement)['state'] == 'pending'
with approvals.transaction() as db:
    assert db.execute('SELECT COUNT(*) FROM executions').fetchone()[0] == 1
""")
