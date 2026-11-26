"""Loss exemption needs a durable no-tools fence and a finite card-wide budget."""
import json
import pytest
from tests.hermes_cli.test_kanban_managed_worker_loss import board as _board_fixture

board = _board_fixture
from tests.fixtures.storage_seed import seed_admission
from tests.hermes_cli.test_kanban_managed_worker_loss import NOW


def _next_attempt(b, tid):
    """Create another attempt on the same card without clearing its cumulative budget."""
    previous = b.conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
    run_id = b.conn.execute(
        "INSERT INTO task_runs(task_id,profile,status,claim_lock,claim_expires,worker_pid,started_at) "
        "SELECT task_id,profile,'running',claim_lock,?,worker_pid,? FROM task_runs WHERE id=?",
        (NOW + 900, NOW - 120, previous['id'])).lastrowid
    b.conn.execute(
        "UPDATE tasks SET status='running',current_run_id=?,claim_lock=?,claim_expires=?,worker_pid=?,started_at=? WHERE id=?",
        (run_id, previous['claim_lock'], NOW + 900, previous['worker_pid'], NOW - 120, tid))
    events = b.conn.execute(
        "SELECT kind,payload FROM task_events WHERE run_id=? AND kind IN ('claimed','spawned','owner_admitted','worker_lost') ORDER BY id",
        (previous['id'],)).fetchall()
    for event in events:
        payload = json.loads(event['payload'])
        if event['kind'] == 'owner_admitted':
            payload['request_id'] = f'kanban:["default","{tid}",{run_id}]'
            seed_admission(b.store, payload['session_id'], payload['request_id'], 'terminal', 'interrupted')
        if event['kind'] == 'worker_lost':
            payload['run_id'] = run_id
        b.event(tid, run_id, event['kind'], payload, NOW - 120)
    b.conn.commit()


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


@pytest.mark.parametrize('config_text', [None, 'kanban: {}', 'terminal: {}'])
def test_exactly_three_proven_no_tool_losses_are_free_then_normal_breaker(board, config_text):
    from hermes_constants import get_hermes_home
    if config_text is not None:
        (get_hermes_home() / 'config.yaml').write_text(config_text)
    tid = 'bounded'
    board.running(tid, worker_lost='worker_transport_timeout')
    board.conn.execute('UPDATE tasks SET max_retries=1 WHERE id=?', (tid,))
    board.conn.commit()
    for attempt in range(4):
        if attempt:
            _next_attempt(board, tid)
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


def test_config_backend_unavailable_cannot_exit_dispatcher_tick(monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import config_backend
    from hermes_cli.kanban_owner_recovery import transport_retry_budget

    def unavailable(_home):
        raise config_backend.ConfigBackendUnavailable('test backend offline')

    monkeypatch.setattr(config_backend, 'get_config_backend',
                        lambda: SimpleNamespace(read_user_doc=unavailable))
    assert transport_retry_budget() == 3


def test_no_tool_transport_loss_does_not_bypass_open_pr_guard(board):
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    board.running('open-pr', worker_lost='worker_transport_timeout')
    proof(board, 'open-pr', True)
    board.conn.execute(
        "INSERT INTO task_comments(task_id,author,body,created_at) VALUES('open-pr','fan',?,?)",
        ('Work published: https://github.com/test/project/pull/123', NOW - 60))
    board.conn.commit()
    board.tick(failure_limit=1)
    assert board.state('open-pr')['consecutive_failures'] == 0
    assert check_respawn_guard(board.conn, 'open-pr') == 'infrastructure_cooldown'
    board.conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id='open-pr'", (NOW - 3 * 3600,))
    board.conn.commit()
    assert check_respawn_guard(board.conn, 'open-pr') == 'active_pr'
