"""Free-disk admission defers new workers without spending task retries."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.config import atomic_config_write


GIB = 2**30


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_low_disk_defers_before_claim_and_retries_next_tick(
    kanban_home, monkeypatch, caplog,
):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": 2}},
    )
    free = [2 * GIB - 1]
    probes = []

    def disk_usage(path):
        probes.append(Path(path))
        return SimpleNamespace(free=free[0])

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    spawns = []

    def spawn(task, workspace, board=None):
        spawns.append(task.id)

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Queued work", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.current_run_id is None
        assert task.workspace_path is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()[0] == 0
        assert not spawns
        assert not result.spawned
        assert (task_id, "disk_floor") in result.respawn_guarded
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded'",
            (task_id,),
        ).fetchall()
        assert any(json.loads(row[0]) == {
            "reason": "disk_floor", "free_bytes": free[0], "min_free_disk_gb": 2,
        } for row in events)
        assert "free disk" in caplog.text.lower()
        assert probes == [kanban_home]

        free[0] = 2 * GIB
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert spawns == [task_id]
        assert len(result.spawned) == 1
        assert probes == [kanban_home, kanban_home]


def test_disk_probe_error_fails_open_with_warning(kanban_home, monkeypatch, caplog):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": 2}},
    )

    def unavailable(path):
        raise OSError("disk probe unavailable")

    monkeypatch.setattr(shutil, "disk_usage", unavailable)
    spawns = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Queued work", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace: spawns.append(task.id))
        assert spawns == [task_id]
        assert len(result.spawned) == 1
        assert not result.respawn_guarded
    assert "disk probe unavailable" in caplog.text
    assert "fail-open" in caplog.text


@pytest.mark.parametrize("floor", [None, 0])
def test_disabled_floor_does_not_probe(kanban_home, monkeypatch, floor):
    if floor is not None:
        atomic_config_write(
            kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": floor}},
        )

    def unexpected_probe(path):
        pytest.fail("disabled floor must not query disk usage")

    monkeypatch.setattr(shutil, "disk_usage", unexpected_probe)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Queued work", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        assert len(result.spawned) == 1
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert not result.respawn_guarded


@pytest.mark.parametrize("free", [2 * GIB, 3 * GIB])
def test_sufficient_disk_claims(kanban_home, monkeypatch, free):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": 2}},
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Queued work", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert len(result.spawned) == 1
        assert not result.respawn_guarded


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_both_lanes_defer_and_dry_run_does_not_write(
    kanban_home, monkeypatch, lane, dry_run,
):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": 0.5}},
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=GIB // 4))

    def no_spawn(task, workspace):
        pytest.fail("low disk must defer both dispatch lanes")

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Queued work", assignee="alice")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, task_id))
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        result = kbd.dispatch_once(conn, spawn_fn=no_spawn, dry_run=dry_run)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == lane
        assert not result.spawned
        assert (task_id, "disk_floor") in result.respawn_guarded
        after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        assert after == before + (0 if dry_run else 1)


@pytest.mark.parametrize("floor", [None, True, -1, "invalid", "nan", "inf", []])
def test_invalid_floor_warns_and_disables_guard(kanban_home, monkeypatch, caplog, floor):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": floor}},
    )

    def unexpected_probe(path):
        pytest.fail("invalid floor must not query disk usage")

    monkeypatch.setattr(shutil, "disk_usage", unexpected_probe)
    with kbc.connect() as conn:
        kb.create_task(conn, title="Queued work", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        assert len(result.spawned) == 1
    assert "kanban.min_free_disk_gb" in caplog.text
    assert "invalid" in caplog.text.lower()


def test_disk_is_rechecked_between_spawns_without_touching_running_work(
    kanban_home, monkeypatch,
):
    atomic_config_write(
        kanban_home / "config.yaml", {"kanban": {"min_free_disk_gb": 1}},
    )
    free = [2 * GIB]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=free[0]))
    spawns = []

    def spawn(task, workspace):
        spawns.append(task.id)
        free[0] = 0

    with kbc.connect() as conn:
        first = kb.create_task(conn, title="First work", assignee="alice", priority=100)
        second = kb.create_task(conn, title="Later work", assignee="alice", priority=50)
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert spawns == [first]
        assert result.respawn_guarded == [(second, "disk_floor")]
        before = tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (first,)).fetchone())
        kbd.dispatch_once(conn, spawn_fn=spawn)
        after = tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (first,)).fetchone())
        assert after == before
        assert spawns == [first]
