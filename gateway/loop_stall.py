"""Small cross-process signal shared by WS writes and kanban dispatch."""
import logging
from pathlib import Path

from hermes_constants import get_process_hermes_home

_log = logging.getLogger(__name__)


def loop_stall_path() -> Path:
    """Use the launch home, not whichever routed profile timed out."""
    return get_process_hermes_home() / "run" / "loop_stall.ts"


def note_loop_stall() -> None:
    """Record an observed timeout from its worker thread; never close the transport."""
    try:
        marker = loop_stall_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError as exc:
        _log.warning("loop stall marker write failed: %s", exc)
