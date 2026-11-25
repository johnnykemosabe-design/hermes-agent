"""Boot-frozen host-load policy for the shared kanban spawn budget.

Direct library callers retain the old behavior when no policy is supplied;
gateway/CLI/daemon entry points explicitly resolve a policy at startup.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

_DEFAULTS = {"mode": "shadow", "load_elevated_per_cpu": 1.0,
             "load_critical_per_cpu": 2.0, "stall_window_s": 300.0}
_RANK = {"unknown": 0, "ok": 0, "elevated": 1, "critical": 2}
_log = logging.getLogger(__name__)


def _nonnegative(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def resolve_load_guard_config(raw=None) -> dict:
    """Copy valid settings at boot; missing/invalid policy never opts into enforcement."""
    if raw is None:
        return dict(_DEFAULTS)
    try:
        if not isinstance(raw, dict):
            raise ValueError("expected a mapping")
        config = {key: raw.get(key, value) for key, value in _DEFAULTS.items()}
        if config["mode"] not in ("shadow", "enforce", "off"):
            raise ValueError("mode must be shadow, enforce or off")
        for key in ("load_elevated_per_cpu", "load_critical_per_cpu", "stall_window_s"):
            if not _nonnegative(config[key]) or config[key] == 0:
                raise ValueError(f"{key} must be a finite positive number")
        if config["load_critical_per_cpu"] <= config["load_elevated_per_cpu"]:
            raise ValueError("critical threshold must exceed elevated threshold")
        return config
    except (ValueError, TypeError) as exc:
        _log.warning("invalid kanban.load_guard: %s; using shadow defaults", exc)
        return dict(_DEFAULTS)


def configured_load_guard() -> dict:
    """Standalone daemon reads this once, before entering its tick loop."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        raw = (load_config_readonly() or {}).get("kanban", {}).get("load_guard")
    except (OSError, ValueError, TypeError, AttributeError, ConfigBackendUnavailable) as exc:
        _log.warning("kanban.load_guard read failed: %s; using shadow defaults", exc, exc_info=True)
        raw = None
    return resolve_load_guard_config(raw)


def _classify(sample: dict, config: dict) -> tuple[str, float]:
    load, cpus, age, vm = (sample[k] for k in ("load1", "logicalcpu", "stall_age", "vm"))
    if (not _nonnegative(load) or not _nonnegative(cpus) or cpus <= 0
            or cpus != int(cpus) or (age is not None and not _nonnegative(age))
            or isinstance(vm, bool) or vm not in (None, 1, 2, 4)):
        raise ValueError("invalid host pressure observation")
    per_cpu = load / cpus
    if per_cpu >= config["load_critical_per_cpu"]:
        level = "critical"
    elif per_cpu >= config["load_elevated_per_cpu"]:
        level = "elevated"
    else:
        level = "ok"
    if (age is not None and age < config["stall_window_s"]) or vm == 4:
        level = "critical"
    elif vm == 2 and level != "critical":
        level = "elevated"
    return level, per_cpu


def host_pressure(memory: str, budget: int | None, config: dict | None,
                  sampler: Callable[[], dict], log: logging.Logger) -> str:
    """Return the worse pressure in enforce mode; shadow never changes the memory guard."""
    if not config or config["mode"] == "off":
        return memory
    sample = {}
    try:
        sample = sampler()
        level, per_cpu = _classify(sample, config)
    except Exception as exc:
        sample, per_cpu, level = {}, None, "elevated"
        log.warning("kanban dispatch: host sampler error: %s", exc, exc_info=True)
    combined = max((memory, level), key=_RANK.__getitem__)
    would_budget = 0 if combined == "critical" else (
        1 if combined == "elevated" and (budget is None or budget > 1) else budget)
    log.info("host_pressure=%s load1=%s per_cpu=%s stall_age=%s vm=%s would_budget=%s",
             level, sample.get("load1"), per_cpu, sample.get("stall_age"), sample.get("vm"), would_budget)
    return combined if config["mode"] == "enforce" else memory
