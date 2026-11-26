"""Real receipt transport -> SQLite guard -> real ToolBarrier -> owner loss evidence."""
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from agent.worker_safety import ToolBarrier
from persistence.core.errors import AuthorityRefusal
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_session


@pytest.fixture
def execution(tmp_path):
    with open_persistence(profile_scope(home=tmp_path / 'authority')) as db:
        seed_session(db, 'conversation', 'cli')
        ids = dict(epoch=db.runtime.begin_epoch(instance_id='test-owner'), execution_id='turn',
                   session_id='conversation', generation=0)
        db.worker_executions.register(**ids, kind='kanban', adoption_secret='test-only')
        calls = []

        def persist(method, **params):
            assert method == 'worker.persist'
            calls.append(params['operation'])
            return db.worker_executions.persist_operation(**params)

        store = RuntimeSessionStore(persist, ids, tmp_path / 'journal')
        try:
            yield db, ids, store, calls
        finally:
            store._outbox_owner.close()


def state(db):
    return raw_holder(db)._read_one('SELECT tool_work_state, status FROM worker_executions')


@pytest.mark.parametrize('runtime, proven', [
    ('chat_completions', True), ('anthropic_messages', True), ('codex_responses', True),
    ('codex_app_server', False), ('claude_agent_sdk', False), ('copilot_acp', False),
    ('future-external-runtime', False), (None, False),
])
def test_runtime_must_explicitly_guarantee_hermes_tool_dispatch(execution, runtime, proven):
    db, ids, store, _ = execution
    ToolBarrier(store, runtime=runtime)
    assert state(db)['tool_work_state'] == (0 if proven else None)
    assert db.worker_executions.fence_for_loss(**ids) is proven
    assert state(db)['status'] == 'terminal'


@pytest.mark.parametrize('count', [0, 2])
def test_real_barrier_is_durable_before_middleware_and_owner_note(execution, monkeypatch, count):
    from agent import tool_executor
    from gateway import session_kanban, session_managed_worker
    from hermes_cli import middleware

    db, ids, store, calls = execution
    agent = SimpleNamespace(_worker_tool_barrier=ToolBarrier(store, runtime='chat_completions'),
                            session_id='conversation')
    observations = []
    original = middleware.apply_tool_request_middleware

    def observe_request(*args, **kwargs):
        assert state(db)['tool_work_state'] == 1
        observations.append('middleware')
        return original(*args, **kwargs)

    def inert_dispatch(*args, **kwargs):
        assert state(db)['tool_work_state'] == 1
        observations.append('dispatch')
        return 'inert-result'

    monkeypatch.setattr(middleware, 'apply_tool_request_middleware', observe_request)
    monkeypatch.setattr(tool_executor, '_dispatch_authorized_once', inert_dispatch)
    for index in range(count):
        result = tool_executor._run_agent_tool_execution_middleware(
            agent, function_name='terminal', function_args={'command': 'inert'},
            effective_task_id='conversation', tool_call_id=f'tool-{index}', execute=lambda _: pytest.fail('not inert'))
        assert result.result == 'inert-result'
    assert observations == ['middleware', 'dispatch'] * count
    assert calls == ['execution.tool_guard'] + (['execution.tool_start'] if count else [])
    notes = []
    monkeypatch.setattr(session_kanban, 'record_worker_loss', lambda *a, **kw: notes.append(kw))
    session_managed_worker._fenced_loss_note(SimpleNamespace(persistence=db), None,
                                            'worker_transport_timeout', dict(ids, pid=123))
    assert len(notes) == 1 and notes[0]['no_tool_started'] is (count == 0)
    assert state(db)['status'] == 'terminal'
    with pytest.raises(AuthorityRefusal):
        store._apply('execution.tool_start', {})


def test_real_barrier_failure_prevents_middleware_and_dispatch(execution, monkeypatch):
    from agent import tool_executor
    from hermes_cli import middleware

    db, ids, store, _ = execution
    barrier = ToolBarrier(store, runtime='chat_completions')
    # Lose authority after installation. The actual tool-start receipt must fail, not a stub barrier.
    db.worker_executions.fence_for_loss(**ids)
    monkeypatch.setattr(middleware, 'apply_tool_request_middleware', lambda *a, **kw: pytest.fail('middleware ran'))
    with pytest.raises(AuthorityRefusal):
        tool_executor._run_agent_tool_execution_middleware(
            SimpleNamespace(_worker_tool_barrier=barrier), function_name='terminal', function_args={},
            effective_task_id='conversation', tool_call_id='late-tool', execute=lambda _: pytest.fail('tool ran'))
    assert barrier.started is False


def test_reinstalling_real_guard_cannot_erase_committed_tool_start(execution):
    db, ids, store, _ = execution
    ToolBarrier(store, runtime='chat_completions')()
    ToolBarrier(store, runtime='chat_completions')
    assert state(db)['tool_work_state'] == 1
    assert db.worker_executions.fence_for_loss(**ids) is False
