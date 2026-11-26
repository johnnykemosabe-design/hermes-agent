"""Loss exemption needs a durable no-tools fence and a finite card-wide budget."""
import json
import pytest
from tests.hermes_cli.test_kanban_managed_worker_loss import board as _board_fixture

board = _board_fixture
from tests.hermes_cli.test_review_probe_transport import _rearm


def proof(b, tid, no_tools):
    row = b.conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='worker_lost' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    payload = dict(json.loads(row['payload']), no_tool_started=no_tools)
    b.conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    b.conn.commit()


@pytest.mark.parametrize('evidence', [False, None])
def test_timeout_with_tools_or_missing_proof_takes_strike(board, evidence):
    board.running('unsafe', worker_lost='worker_transport_timeout')
    board.conn.execute("UPDATE tasks SET max_retries=1 WHERE id='unsafe'")
    board.conn.commit()
    if evidence is not None:
        proof(board, 'unsafe', evidence)
    board.tick(failure_limit=1)
    assert board.state('unsafe')['status'] == 'blocked'
    assert board.state('unsafe')['consecutive_failures'] == 1


def test_exactly_three_proven_no_tool_losses_are_free_then_normal_breaker(board):
    tid = 'bounded'
    board.running(tid, worker_lost='worker_transport_timeout')
    board.conn.execute('UPDATE tasks SET max_retries=1 WHERE id=?', (tid,))
    board.conn.commit()
    for attempt in range(4):
        if attempt:
            _rearm(board, tid, attempt, 'worker_transport_timeout')
        proof(board, tid, True)
        board.tick(failure_limit=1)
        row = board.state(tid)
        assert row['consecutive_failures'] == (0 if attempt < 3 else 1)
        assert row['status'] == ('ready' if attempt < 3 else 'blocked')
    assert board.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='transport_timeout'", (tid,)).fetchone()[0] == 3


def test_no_tool_transport_retry_retains_recent_success_guard(board):
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    from tests.hermes_cli.test_kanban_managed_worker_loss import NOW
    board.running('success', worker_lost='worker_transport_timeout')
    proof(board, 'success', True)
    board.tick(failure_limit=1)
    board.conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id='success'", (NOW - 301,))
    board.conn.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at) VALUES ('success','fan','completed','completed',?,?)", (NOW - 900, NOW - 350))
    board.conn.commit()
    assert check_respawn_guard(board.conn, 'success') == 'recent_success'


@pytest.mark.parametrize('value, expected', [(-1, 0), (1000, 10), (0, 0), (None, 3), (True, 3), (float('inf'), 3)])
def test_transport_budget_clamped(monkeypatch, value, expected):
    from types import SimpleNamespace
    import hermes_cli.config_backend as backend
    from hermes_cli.kanban_owner_recovery import transport_retry_budget
    monkeypatch.setattr(backend, 'get_config_backend', lambda: SimpleNamespace(read_user_doc=lambda _: {'kanban': {'worker_transport_retry_budget': value}}))
    assert transport_retry_budget() == expected


def test_review_lane_transport_event_preserves_resume_status(board):
    from hermes_cli import kanban_db as kb
    run = board.running('review', worker_lost='worker_transport_timeout')
    board.event('review', run, 'transport_timeout', {'retry_status': 'review'}, 9999999999)
    assert kb._resume_status_from_events(board.conn, 'review') == 'review'


def test_clean_exit_transport_error_is_not_protocol_violation(board):
    from hermes_cli import kanban_db as kb
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    board.running('clean', worker_lost='worker_transport_timeout')
    proof(board, 'clean', True)
    log = kb.worker_log_path('clean')
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"saved output\n{KANBAN_WORKER_EXIT_TRAILER}0\n")
    board.tick(failure_limit=1)
    error = board.state('clean')['last_failure_error']
    assert 'without kanban_complete' not in error
    assert 'before tool work started' in error
