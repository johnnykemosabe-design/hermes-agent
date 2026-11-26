"""Reply replay is opt-in even through delegated/off-loop RPC adapters."""
from functools import partial
from types import SimpleNamespace

import pytest

from agent.runtime_session_child import ChildSessionStore
from agent.runtime_session_store import RuntimeSessionStore, WorkerReplyBudget, WorkerRPC, call_off_event_loop
from tests.agent.test_worker_rpc_timeout import Link


@pytest.mark.parametrize('store_kind', ['parent', 'wrapped_parent', 'child'])
def test_unconfigured_store_has_one_send_and_one_base_window(tmp_path, monkeypatch, store_kind):
    rpc = WorkerRPC(tmp_path)
    connections = []
    pauses = []

    def connection():
        link = Link([TimeoutError('worker_receipt_timeout')] * 10)
        connections.append(link)
        return link

    monkeypatch.setattr(rpc, '_connection', connection)
    monkeypatch.setattr('agent.runtime_session_store.time.sleep', pauses.append)
    store_type = ChildSessionStore if store_kind == 'child' else RuntimeSessionStore
    transport = rpc if store_kind == 'parent' else partial(call_off_event_loop, rpc)
    store = store_type(transport, dict(epoch=1, execution_id='attempt', session_id='session', generation=0),
                       tmp_path / 'journal')
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store._apply('transcript.append', {'messages': [], 'turn_lease_holder': None})
        assert [window for link in connections for window in link.waits] == [20.0]
        assert sum(len(link.sent) for link in connections) == 1
        assert pauses == []
        assert len(store.journal['pending']) == 1
    finally:
        store._outbox_owner.close()


def test_callable_without_a_budget_does_not_implicitly_enable_replay(tmp_path):
    requests = []

    def rpc(method, **params):
        requests.append((method, params))
        raise TimeoutError('lost_ack')

    store = RuntimeSessionStore(rpc, dict(epoch=1, execution_id='attempt', session_id='session', generation=0),
                                tmp_path / 'journal')
    try:
        with pytest.raises(TimeoutError, match='lost_ack'):
            store._apply('execution.finish', {})
        assert len(requests) == 1
    finally:
        store._outbox_owner.close()


def test_reserved_child_inherits_parent_opt_in_budget(tmp_path):
    from agent.runtime_session_child import reserve_child_store
    budget = WorkerReplyBudget(retries=2)
    parent = SimpleNamespace(
        scope=dict(epoch=1, execution_id='parent', session_id='s', generation=0),
        path=tmp_path / 'journal' / 'pending.json', reply_budget=budget,
        rpc=lambda *a, **kw: None,
        _apply=lambda operation, payload: {'child': dict(execution_id='parent:child', session_id='child', generation=0)})
    child = reserve_child_store(parent)
    try:
        assert child.reply_budget is budget
    finally:
        child._outbox_owner.close()


def test_missing_config_is_a_silent_default_not_an_operational_warning(tmp_path, caplog):
    assert WorkerReplyBudget.for_home(tmp_path) == WorkerReplyBudget()
    assert not [record for record in caplog.records if record.levelno >= 30]
    assert not (tmp_path / 'config.yaml').exists()


def test_selected_config_backend_unavailable_does_not_terminate_worker(tmp_path, monkeypatch, caplog):
    from hermes_cli import config_backend

    def unavailable(_home):
        raise config_backend.ConfigBackendUnavailable('private backend detail')

    monkeypatch.setattr(config_backend, 'get_config_backend',
                        lambda: SimpleNamespace(read_user_doc=unavailable))
    assert WorkerReplyBudget.for_home(tmp_path) == WorkerReplyBudget()
    assert 'private backend detail' not in caplog.text
    assert 'using defaults' in caplog.text
