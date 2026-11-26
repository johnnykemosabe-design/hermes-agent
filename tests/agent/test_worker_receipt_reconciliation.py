"""Lost ACK recovery re-reads an exact durable receipt, never a new write identity."""
import pytest

from agent.runtime_session_store import RuntimeSessionStore
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session
from tests.fixtures.storage_holder import raw_holder


@pytest.mark.parametrize('operation,payload', [
    ('transcript.append', {'messages': [{'role': 'user', 'content': 'once'}], 'turn_lease_holder': None}),
    ('execution.finish', {}),
])
def test_committed_save_with_lost_ack_reconciles_before_sticky_failure(tmp_path, operation, payload):
    with open_persistence(profile_scope(home=tmp_path / 'owner')) as db:
        seed_session(db, 's', 'cli')
        epoch = db.runtime.begin_epoch(instance_id='owner')
        scope = dict(epoch=epoch, execution_id='worker', session_id='s', generation=0)
        db.worker_executions.register(**scope, kind='compute', adoption_secret='secret')
        calls = []

        def rpc(method, **params):
            assert method == 'worker.persist'
            calls.append(params)
            result = db.worker_executions.persist_operation(**params)
            if len(calls) == 1:
                raise TimeoutError('worker_receipt_timeout')
            return result

        store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
        try:
            result = store._apply(operation, payload)
            assert result is not None
            assert calls[0] == calls[1]
            assert store.failure is None
            assert store.journal['pending'] == []
            assert raw_holder(db)._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
            if operation == 'transcript.append':
                assert raw_holder(db)._read_one('SELECT COUNT(*) FROM messages')[0] == 1
        finally:
            store._outbox_owner.close()
