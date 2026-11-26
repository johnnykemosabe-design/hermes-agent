"""Reviewer probes (not part of the candidate): transport-timeout requeue bounds and guards."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from tests.fixtures.storage_seed import seed_admission
from tests.hermes_cli.test_kanban_managed_worker_loss import (  # noqa: F401  (fixture import)
    FINGERPRINT, HOST, NOW, _absent_pid, board as _board_fixture,
)

board = _board_fixture


def _age_runs(b, tid, seconds):
    b.conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE task_id=? AND ended_at IS NOT NULL",
                   (seconds, tid))
    b.conn.commit()


@pytest.mark.parametrize('reason', ['managed_worker_failed', 'worker_transport_timeout'])
def test_probe_active_pr_guard_after_lost_worker(board, reason):
    """A worker that already opened a PR and was then lost must not be respawned against it."""
    board.running('t_pr', worker_lost=reason)
    board.conn.execute("INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
                       ('t_pr', 'fan', 'opened https://github.com/o/r/pull/7', NOW - 60))
    board.conn.commit()
    board.tick(failure_limit=5)
    _age_runs(board, 't_pr', 3 * 3600)
    row = board.state('t_pr')
    guard = kbd.check_respawn_guard(board.conn, 't_pr')
    print(f'PROBE_PR reason={reason} status={row["status"]} strikes={row["consecutive_failures"]} guard={guard}')
    assert row['status'] == 'ready'
    assert guard == 'active_pr', f'{reason}: duplicate-PR guard bypassed (guard={guard})'


def _rearm(b, tid, attempt, reason):
    """Put the same card back into a fresh running attempt whose managed worker is lost again."""
    worker_pid = _absent_pid()
    lock = f"{HOST}:{_absent_pid(base=3_900_000)}"
    started = NOW - 120
    run_id = int(b.conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'fan', 'running', ?, ?, ?, ?)", (tid, lock, NOW + 900, worker_pid, started)).lastrowid)
    b.conn.execute("UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, worker_pid=?, "
                   "worker_started_at=?, current_run_id=?, started_at=? WHERE id=?",
                   (lock, NOW + 900, worker_pid, FINGERPRINT, run_id, started, tid))
    b.event(tid, run_id, 'claimed', {'lock': lock, 'expires': NOW + 900}, started)
    b.event(tid, run_id, 'spawned', {'pid': worker_pid, 'started_at': FINGERPRINT}, started)
    request_id = f'kanban:["default","{tid}",{run_id}]'
    b.event(tid, run_id, 'owner_admitted', {'db': str(b.ledger), 'session_id': 'sess-1',
            'pid': _absent_pid(base=3_950_000), 'request_id': request_id}, started + 3)
    seed_admission(b.store, 'sess-1', request_id, 'terminal', 'interrupted')
    b.event(tid, run_id, 'worker_lost', {'reason': reason, 'claim_lock': lock, 'run_id': run_id,
            'pid': worker_pid, 'bound': False}, started + 24)
    b.conn.commit()
    return run_id


@pytest.mark.parametrize('reason', ['managed_worker_failed', 'worker_transport_timeout'])
def test_probe_repeated_loss_is_eventually_bounded(board, reason):
    """The SAME card losing its worker attempt after attempt must eventually stop being respawned."""
    tid = 't_loop'
    board.running(tid, worker_lost=reason)
    board.conn.execute('UPDATE tasks SET max_retries=3 WHERE id=?', (tid,))
    board.conn.commit()
    history = []
    for attempt in range(12):
        board.tick(failure_limit=3)
        row = board.state(tid)
        _age_runs(board, tid, 3 * 3600)
        guard = kbd.check_respawn_guard(board.conn, tid)
        history.append((row['status'], row['consecutive_failures'], guard))
        if row['status'] != 'ready' or guard is not None:
            break
        _rearm(board, tid, attempt, reason)
    gave_up = board.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()[0]
    print(f'PROBE_LOOP reason={reason} attempts={len(history)} gave_up={gave_up} history={json.dumps(history)}')
    assert len(history) < 12, f'{reason}: 12 consecutive lost attempts, still respawnable, no strike: {history}'
