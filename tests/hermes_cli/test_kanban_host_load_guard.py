"""Host-load guard contracts through the embedded dispatcher's real tick."""
from dataclasses import asdict
from pathlib import Path

import pytest

from gateway.kanban_watchers_dispatcher import _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_boot_enforce_critical_load_defers_both_lanes(board, monkeypatch, lane):
    # Inject only the host observation; caps, memory, DB and lane loops are real.
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 41.0, "logicalcpu": 20, "stall_age": None, "vm": 1,
    }, raising=False)
    for title in ("a", "b", "c"):
        tid = kb.create_task(board, title=title, assignee="alice")
        if lane == "review":
            board.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    kwargs = {k: v for k, v in asdict(settings).items() if k != "interval"}
    result = kbd.dispatch_once(board, dry_run=True, **kwargs)
    assert result.spawned == []
    assert board.execute("SELECT count(*) FROM tasks WHERE status=?", (lane,)).fetchone()[0] == 3


@pytest.mark.parametrize("load,age,vm,expected", [
    (10, None, 1, (True, 5)), (25, None, 1, (True, 1)),
    (20, None, 1, (True, 1)), (40, None, 1, (False, None)),
    (10, 299, 1, (False, None)), (10, 300, 1, (True, 5)),
    (10, 301, 1, (True, 5)), (10, None, 2, (True, 1)),
    (10, None, 4, (False, None)),
])
def test_thresholds(board, monkeypatch, load, age, vm, expected):
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": load, "logicalcpu": 20, "stall_age": age, "vm": vm,
    }, raising=False)
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=5,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == expected


@pytest.mark.parametrize("mode,expected", [("shadow", (True, 5)), ("off", (True, 5))])
def test_shadow_and_off_do_not_enforce(board, monkeypatch, caplog, mode, expected):
    calls = []
    def sample():
        calls.append(True)
        return {"load1": 41, "logicalcpu": 20, "stall_age": 2, "vm": 4}
    monkeypatch.setattr(kbd, "_host_pressure_sample", sample, raising=False)
    caplog.set_level("INFO")
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": mode}}, kbd)
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=5,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == expected
    assert len(calls) == (0 if mode == "off" else 1)
    assert ("host_pressure=critical" in caplog.text) == (mode == "shadow")
    if mode == "shadow":
        assert "would_budget=0" in caplog.text


def test_sampler_exception_is_elevated_and_logged(board, monkeypatch, caplog):
    def broken():
        raise OSError("fixture sampler unavailable")
    monkeypatch.setattr(kbd, "_host_pressure_sample", broken, raising=False)
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=5,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == (True, 1)
    assert "fixture sampler unavailable" in caplog.text


@pytest.mark.parametrize("memory", ["elevated", "critical"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_host_never_weakens_memory(board, monkeypatch, memory, mode):
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: memory)
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 1, "logicalcpu": 20, "stall_age": None, "vm": 1,
    }, raising=False)
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": mode}}, kbd)
    result = kbd.DispatchResult()
    expected = (False, None) if memory == "critical" else (True, 1)
    assert kbd._tick_spawn_budget(board, result, max_spawn=None,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == expected
    assert result.memory_pressure == memory


def test_shadow_logs_even_when_concurrency_cap_is_full(board, monkeypatch, caplog):
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 41, "logicalcpu": 20, "stall_age": None, "vm": 1,
    })
    caplog.set_level("INFO")
    settings = _resolve_dispatcher_settings({}, kbd)
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=0,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == (False, None)
    assert "host_pressure=critical" in caplog.text
    assert "would_budget=0" in caplog.text


@pytest.mark.parametrize("field,value", [
    ("load1", float("nan")), ("load1", -1), ("logicalcpu", 0),
    ("logicalcpu", True), ("vm", 3), ("stall_age", float("inf")),
])
def test_invalid_observations_are_sampler_errors(board, monkeypatch, caplog, field, value):
    sample = {"load1": 10, "logicalcpu": 20, "vm": 1, "stall_age": None}
    sample[field] = value
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: sample)
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=5,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == (True, 1)
    assert "host sampler error" in caplog.text


def test_policy_is_read_through_real_loader_and_copied_at_boot(board, tmp_path, monkeypatch):
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text(
        "kanban:\n  load_guard:\n    mode: enforce\n    load_elevated_per_cpu: 2.5\n"
        "    load_critical_per_cpu: 3.5\n    stall_window_s: 7\n")
    cfg = load_config_readonly()["kanban"]
    settings = _resolve_dispatcher_settings(cfg, kbd)
    cfg["load_guard"]["mode"] = "off"
    assert settings.load_guard["mode"] == "enforce"
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 60, "logicalcpu": 20, "vm": 1, "stall_age": 8,
    })
    assert kbd._tick_spawn_budget(board, kbd.DispatchResult(), max_spawn=5,
        max_in_progress=None, board=None, load_guard=settings.load_guard) == (True, 1)


@pytest.mark.parametrize("raw", [
    {"mode": "typo"}, {"load_elevated_per_cpu": float("nan")},
    {"load_elevated_per_cpu": 4}, {"stall_window_s": -1},
    {"load_critical_per_cpu": True}, "not a map",
])
def test_invalid_config_falls_back_to_shadow_defaults_with_warning(raw, caplog):
    settings = _resolve_dispatcher_settings({"load_guard": raw}, kbd)
    assert settings.load_guard == {"mode": "shadow", "load_elevated_per_cpu": 1.0,
                                  "load_critical_per_cpu": 2.0, "stall_window_s": 300.0}
    assert "invalid kanban.load_guard" in caplog.text


def test_cli_dispatch_resolves_policy(board, monkeypatch, capsys):
    import argparse
    from hermes_cli import config, kanban_ops
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"load_guard": {"mode": "enforce"}}})
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 41, "logicalcpu": 20, "vm": 1, "stall_age": None,
    })
    kb.create_task(board, title="fixture", assignee="alice")
    assert kanban_ops._cmd_dispatch(argparse.Namespace(dry_run=True, json=True, max=5)) == 0
    import json
    assert json.loads(capsys.readouterr().out)["spawned"] == []


def test_daemon_freezes_guard_at_boot(board, monkeypatch):
    import threading
    from hermes_cli import config
    raw = {"kanban": {"load_guard": {"mode": "enforce", "stall_window_s": 7}}}
    monkeypatch.setattr(config, "load_config_readonly", lambda: raw)
    seen = []
    stop = threading.Event()
    def tick(conn, **kwargs):
        seen.append(kwargs.get("load_guard"))
        if len(seen) == 1:
            raw["kanban"]["load_guard"]["mode"] = "off"
        else:
            stop.set()
        return kbd.DispatchResult()
    monkeypatch.setattr(kbd, "dispatch_once", tick)
    kbd.run_daemon(interval=0, stop_event=stop)
    assert len(seen) == 2
    assert all(p and p["mode"] == "enforce" and p["stall_window_s"] == 7 for p in seen)


def test_elevated_budget_is_shared_across_ready_and_review(board, monkeypatch):
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 25, "logicalcpu": 20, "vm": 1, "stall_age": None,
    })
    for lane in ("ready", "review"):
        for title in ("a", "b", "c"):
            tid = kb.create_task(board, title=title, assignee="alice")
            board.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    kwargs = {k: v for k, v in asdict(settings).items() if k != "interval"}
    result = kbd.dispatch_once(board, dry_run=True, **kwargs)
    assert len(result.spawned) == 1


def test_host_deferral_recovers_without_dropping_work(board, monkeypatch):
    sample = {"load1": 41, "logicalcpu": 20, "vm": 1, "stall_age": None}
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: sample)
    ids = [kb.create_task(board, title=lane, assignee="alice") for lane in ("ready", "review")]
    board.execute("UPDATE tasks SET status='review' WHERE id=?", (ids[1],))
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    kwargs = {k: v for k, v in asdict(settings).items() if k != "interval"}
    calls = []
    def spawn(task, workspace, board=None):
        calls.append(task.id)
    assert not kbd.dispatch_once(board, spawn_fn=spawn, **kwargs).spawned
    assert [kb.get_task(board, tid).status for tid in ids] == ["ready", "review"]
    sample["load1"] = 10
    assert len(kbd.dispatch_once(board, spawn_fn=spawn, **kwargs).spawned) == 2
    assert set(calls) == set(ids)


def test_gateway_tick_forwards_boot_policy(board, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    monkeypatch.setattr(kbd, "_host_pressure_sample", lambda: {
        "load1": 41, "logicalcpu": 20, "vm": 1, "stall_age": None,
    })
    kb.create_task(board, title="fixture", assignee="alice")
    calls = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **kw: calls.append(True))
    settings = _resolve_dispatcher_settings({"load_guard": {"mode": "enforce"}}, kbd)
    dispatcher = _KanbanDispatcher(kb, settings)
    assert not dispatcher.tick_once_for_board("default").spawned
    assert not calls
