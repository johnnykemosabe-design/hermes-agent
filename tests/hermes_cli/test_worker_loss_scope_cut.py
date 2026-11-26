"""Reply recovery must not introduce a schema or a strike-free board loss."""
import json

import pytest

from persistence.core.errors import AuthorityRefusal
from tests.fixtures.storage_holder import raw_holder
from tests.hermes_cli import test_kanban_managed_worker_loss as fixtures

board = fixtures.board


def test_worker_execution_schema_and_contract_have_no_tool_fence(board):
    columns = raw_holder(board.store)._read_all('PRAGMA table_info(worker_executions)')
    assert 'tool_work_state' not in {row['name'] for row in columns}
    assert not hasattr(board.store.worker_executions, 'fence_for_loss')


@pytest.mark.parametrize('operation', ['execution.tool_guard', 'execution.tool_start'])
def test_pre_tool_operations_are_not_receipted(board, operation):
    repo = board.store.worker_executions
    epoch = board.store.runtime.begin_epoch(instance_id='scope-cut')
    scope = dict(epoch=epoch, execution_id='scope-cut-worker', session_id='sess-1', generation=0)
    repo.register(**scope, kind='compute', adoption_secret='fixture-only')
    with pytest.raises(AuthorityRefusal, match='invalid_params'):
        repo.persist_operation(**scope, sequence=1, operation=operation, payload={})
    assert raw_holder(board.store)._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 0


@pytest.mark.parametrize('reason', ['worker_transport_timeout', 'managed_worker_failed'])
def test_every_owner_loss_uses_base_first_strike_even_with_legacy_proof(board, reason):
    run_id = board.running('scope-cut-card', heartbeat_age=30, worker_lost=reason)
    row = board.conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='worker_lost'",
                             ('scope-cut-card',)).fetchone()
    payload = json.loads(row['payload']) | {'no_tool_started': True}
    board.conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    board.conn.execute('UPDATE tasks SET max_retries=1 WHERE id=?', ('scope-cut-card',))
    board.conn.commit()
    board.tick(failure_limit=1)
    assert board.run(run_id)['outcome'] == 'crashed'
    state = board.state('scope-cut-card')
    assert state['consecutive_failures'] == 1
    assert state['status'] == 'blocked'
    assert not board.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='transport_timeout'",
                                  ('scope-cut-card',)).fetchone()
