"""A slow owner's durable receipt must outlive its write patience, without blind replay."""
import contextlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC, _OwnerLink


@pytest.fixture
def receipt_owner(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    from websockets.sync.server import serve

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', 'test')
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='worker', session_id='s', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='test-secret')
    owner = SimpleNamespace(delay=0, release=threading.Event(), handled=threading.Event(),
                            requests=[], db=db, scope=scope)

    def handle(ws):
        try:
            with contextlib.suppress(ConnectionClosed):
                for raw in ws:
                    request = json.loads(raw)
                    owner.requests.append(request)
                    # Neither a broadcast nor an unrelated reply may acknowledge this mutation.
                    ws.send(json.dumps({'method': 'event', 'params': {}}))
                    ws.send(json.dumps({'id': request['id'] + 100, 'result': {'wrong': True}}))
                    owner.release.wait(owner.delay)
                    result = mutate_worker_execution(db, **request['params'])
                    ws.send(json.dumps({'id': request['id'], 'result': result}))
        finally:
            owner.handled.set()

    rpc = WorkerRPC(tmp_path)
    with serve(handle, '127.0.0.1', 0, ping_interval=None, close_timeout=1) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        rpc.link = _OwnerLink(connect(f'ws://127.0.0.1:{server.socket.getsockname()[1]}',
                                     ping_interval=None, proxy=None, close_timeout=1))
        owner.rpc = rpc
        try:
            yield owner
        finally:
            owner.release.set()
            rpc.close()
            server.shutdown()
            server_thread.join(5)
            assert owner.handled.wait(5), 'owner handler did not retire before closing its DB'
            db.close()


@pytest.mark.parametrize('delay', [25, 60])
def test_slow_owner_receipt_commits_once_and_clears_pending(receipt_owner, tmp_path, delay):
    owner = receipt_owner
    owner.delay = delay
    store = RuntimeSessionStore(owner.rpc, owner.scope, tmp_path / 'outbox')
    messages = [{'role': 'user', 'content': 'retained until durable receipt'}]
    try:
        assert store.append_messages_batch('s', messages) == 1
        assert messages[0]['_row_id'] > 0
        assert store.failure is None
        assert json.loads(store.path.read_text())['pending'] == []
        assert len(owner.requests) == 1
        assert owner.requests[0]['method'] == 'worker.persist'
        assert owner.requests[0]['params']['sequence'] == 1
        assert [row['content'] for row in owner.db.get_messages('s')] == [messages[0]['content']]
        assert store.flush_token_counts()
    finally:
        store._outbox_owner.close()


def test_silent_owner_exhausts_derived_budget_without_replay(receipt_owner, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from agent.runtime_session_store import WorkerPersistenceError
    # Scale the real queue deadline through its source constants, not a fake receive().
    monkeypatch.setattr(SessionDB, '_TRANSCRIPT_WRITE_PATIENCE_S', 0.1)
    monkeypatch.setattr(SessionDB, '_WRITE_PATIENCE_S', 0.1)
    owner = receipt_owner
    owner.delay = 60
    store = RuntimeSessionStore(owner.rpc, owner.scope, tmp_path / 'outbox')
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'unacknowledged'}])
        assert time.monotonic() - started < 5
        assert owner.rpc.link is None
        pending = json.loads(store.path.read_text())['pending']
        assert len(pending) == 1 and pending[0]['sequence'] == 1
        assert store.failure == 'worker_receipt_timeout'
        assert owner.db.get_messages('s') == []
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.flush_token_counts()
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'never sent'}])
        assert len(owner.requests) == 1
        assert json.loads(store.path.read_text())['pending'] == pending
    finally:
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.close()
