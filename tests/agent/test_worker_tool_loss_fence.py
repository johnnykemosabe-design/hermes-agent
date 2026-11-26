"""The pre-dispatch barrier and loss fence are authoritative, not UI tool frames."""
from types import SimpleNamespace
import pytest
from persistence_assembly import open_persistence, profile_scope
from persistence.core.errors import AuthorityRefusal
from tests.fixtures.storage_seed import seed_session


@pytest.mark.parametrize('started', [None, False, True])
def test_atomic_loss_fence_proves_no_tools_only_with_active_guard(tmp_path, started):
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, 's', 'cli')
        epoch = db.runtime.begin_epoch(instance_id='fixture')
        ids = dict(epoch=epoch, execution_id='w', session_id='s', generation=0)
        db.worker_executions.register(**ids, kind='kanban', adoption_secret='local-test')
        seq = 0
        if started is not None:
            seq += 1
            db.worker_executions.persist_operation(**ids, sequence=seq, operation='execution.tool_guard', payload={})
        if started:
            seq += 1
            db.worker_executions.persist_operation(**ids, sequence=seq, operation='execution.tool_start', payload={})
        assert db.worker_executions.fence_for_loss(**ids) is (started is False)
        with pytest.raises(AuthorityRefusal):
            db.worker_executions.persist_operation(**ids, sequence=seq + 1, operation='execution.tool_start', payload={})


def test_tool_barrier_failure_prevents_every_dispatch():
    from agent.tool_executor import _run_agent_tool_execution_middleware
    seen = []
    def barrier():
        raise TimeoutError('worker_receipt_timeout')
    agent = SimpleNamespace(_worker_tool_barrier=barrier)
    with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
        _run_agent_tool_execution_middleware(agent, function_name='terminal', function_args={}, effective_task_id='s', tool_call_id='t', execute=lambda args: seen.append(args))
    assert seen == []
