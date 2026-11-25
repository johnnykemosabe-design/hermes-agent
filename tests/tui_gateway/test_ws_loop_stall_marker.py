"""A WS worker timeout publishes a cross-process, launch-home stall signal."""
import asyncio
import concurrent.futures
from pathlib import Path

import pytest

from tui_gateway import ws


def test_loop_timeout_touches_marker_without_closing_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import async_utils

    def stalled_send(coro, loop):
        coro.close()
        fut = concurrent.futures.Future()
        fut.set_exception(concurrent.futures.TimeoutError())
        return fut

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", stalled_send)
    loop = asyncio.new_event_loop()
    try:
        transport = ws.WSTransport(object(), loop)
        assert transport.write({"jsonrpc": "2.0", "id": "fixture", "result": {}}) is True
        assert not transport.closed
        marker = tmp_path / "run" / "loop_stall.ts"
        assert marker.is_file()
        before = marker.stat().st_mtime_ns
        assert transport.write({"id": "again"}) is True
        assert marker.stat().st_mtime_ns >= before
    finally:
        loop.close()


def test_marker_write_error_keeps_transport_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "run").write_text("not a directory")
    from agent import async_utils

    def stalled_send(coro, loop):
        coro.close()
        fut = concurrent.futures.Future()
        fut.set_exception(concurrent.futures.TimeoutError())
        return fut

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", stalled_send)
    loop = asyncio.new_event_loop()
    try:
        transport = ws.WSTransport(object(), loop)
        assert transport.write({"id": "fixture"}) is True
        assert not transport.closed
        assert "loop stall marker" in caplog.text
    finally:
        loop.close()
