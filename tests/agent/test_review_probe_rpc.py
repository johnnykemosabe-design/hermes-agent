"""Reviewer probes (not part of the candidate): default reply budget and committed-finish reconciliation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.runtime_session_store as rss
from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC


class _Link:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.waits, self.sent = [], []
        self.reader = SimpleNamespace(is_alive=lambda: True)
        self.ws = SimpleNamespace(close=lambda: None)

    def send(self, request_id, message):
        self.sent.append(message)

    def receive(self, timeout):
        self.waits.append(timeout)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_probe_default_persist_worst_case_wait(tmp_path, monkeypatch):
    """No config keys set: how long does one stalled worker.persist wait before it fails?"""
    sleeps, links = [], []
    monkeypatch.setattr(rss.time, 'sleep', sleeps.append)

    def connection():
        link = _Link([TimeoutError('worker_receipt_timeout')] * 10)
        links.append(link)
        return link

    rpc = WorkerRPC(tmp_path)  # no config.yaml: defaults
    monkeypatch.setattr(rpc, '_connection', connection)
    scope = dict(epoch=1, execution_id='w', session_id='s', generation=0)
    store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
    try:
        with pytest.raises(TimeoutError):
            store._apply('transcript.append', {'messages': [], 'turn_lease_holder': None})
        waits = [w for link in links for w in link.waits]
        total = sum(waits) + sum(sleeps)
        print(f'PROBE_WAIT connections={len(links)} sends={[len(l.sent) for l in links]} waits={waits} '
              f'sleeps={sleeps} worst_case_s={total} failure={store.failure!r}')
        assert total == 20, f'default persist worst case is {total}s, base was 20s'
    finally:
        store._outbox_owner.close()


def test_probe_revoked_after_committed_append_is_not_a_finish(tmp_path, monkeypatch):
    """Terminal + last_sequence>=1 because the owner REVOKED after an append must not read as finished."""
    from gateway import session_managed_worker as managed
    from persistence_assembly import open_persistence, profile_scope
    from persistence.authority.vocabulary import admission_fingerprint
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_seed import seed_session
    import gateway.session_persistence as persistence
    db = open_persistence(profile_scope(home=tmp_path / 'owner'))
    try:
        seed_session(db, 's', 'cli')
        epoch = db.runtime.begin_epoch(instance_id='owner')
        scope = dict(execution_id='worker', session_id='s', generation=0, epoch=epoch,
                     secret='local-test-assignment', profile_id='fixture', pid=1234, birth=1.0)
        claim = admission_fingerprint(canonical_target='s', payload={k: v for k, v in scope.items() if k != 'epoch'}
                                      | {'principal': managed.owner_worker_subject()})
        db.worker_executions.register(epoch=epoch, execution_id='worker', session_id='s', generation=0,
                                      kind='kanban', adoption_secret=claim)
        db.worker_executions.adopt(epoch=epoch, execution_id='worker', session_id='s', generation=0,
                                   adoption_secret=claim)
        monkeypatch.setattr(persistence, 'authority_persistence', lambda _: db)
        ids = {k: scope[k] for k in ('epoch', 'execution_id', 'session_id', 'generation')}
        db.worker_executions.persist_operation(**ids, sequence=1, operation='transcript.append',
                                               payload={'messages': [{'role': 'user', 'content': 'x'}],
                                                        'turn_lease_holder': None})
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE worker_executions SET status='terminal' WHERE execution_id='worker'"))
        finished = managed._owner_finished(None, scope)
        receipts = raw_holder(db)._read_one('SELECT COUNT(*) FROM worker_receipts')[0]
        print(f'PROBE_REVOKED finished={finished} receipts={receipts}')
        assert finished is False
        assert receipts == 1, 'reconciliation must never write a new receipt'
    finally:
        db.close()
