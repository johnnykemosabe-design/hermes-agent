"""Opt-in disk-space admission for new kanban workers, before claiming."""

from __future__ import annotations

import logging
import math
import shutil
import sqlite3
from typing import TYPE_CHECKING

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult


_log = logging.getLogger(__name__)
_GIB = 2**30


def preclaim(
    conn: sqlite3.Connection, task_id: str, result: DispatchResult, *, dry_run: bool,
) -> bool:
    """Defer below the configured floor; never claim, fail or kill a worker."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import load_config_readonly

    raw = (load_config_readonly() or {}).get("kanban", {}).get("min_free_disk_gb", 0)
    try:
        floor = float(raw)
        if isinstance(raw, bool) or not math.isfinite(floor) or floor < 0:
            raise ValueError("expected a finite non-negative number")
    except (TypeError, ValueError):
        _log.warning("kanban dispatch: invalid kanban.min_free_disk_gb; disk admission disabled (use 0 or a positive GiB value)")
        return True
    if floor == 0:
        return True
    try:
        free = shutil.disk_usage(get_hermes_home()).free
    except OSError as exc:
        _log.warning("kanban dispatch: free disk probe failed (%s); fail-open, allowing new workers", exc)
        return True
    if free >= floor * _GIB:
        return True
    result.respawn_guarded.append((task_id, "disk_floor"))
    _log.warning(
        "kanban dispatch: deferring new worker %s: free disk %.2f GiB below "
        "kanban.min_free_disk_gb=%.2f; retry next tick (running workers unchanged)",
        task_id, free / _GIB, floor,
    )
    if not dry_run:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "respawn_guarded", {
                "reason": "disk_floor", "free_bytes": free, "min_free_disk_gb": floor,
            })
    return False
