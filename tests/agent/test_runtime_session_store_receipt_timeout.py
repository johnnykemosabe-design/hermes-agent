"""Owner receipt stalls get a longer budget without masking closes or resending."""
import json
import queue
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed

from agent import runtime_session_store as store


class _OwnerSocket:
    """A local transport double; the real _OwnerLink reader and queue still run."""

    def __init__(self, delay, *, closes=False):
        self.delay = delay
        self.closes = closes
        self.frames = queue.SimpleQueue()
        self.sent = []
        self.timer = None

    def send(self, message):
        request = json.loads(message)
        self.sent.append(request)
        reply = (ConnectionClosed(None, None) if self.closes else
                 json.dumps({'id': request['id'], 'result': {'committed': True}}))
        if self.delay is not None:
            self.timer = threading.Timer(self.delay, self.frames.put, args=(reply,))
            self.timer.start()

    def recv(self):
        frame = self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
        self.frames.put(ConnectionClosed(None, None))


@pytest.fixture
def owner_rpc(monkeypatch, tmp_path):
    sockets, links = [], []

    def create(delay, *, closes=False):
        socket = _OwnerSocket(delay, closes=closes)
        link = store._OwnerLink(socket)
        rpc = store.WorkerRPC(tmp_path)
        rpc.link = link
        # Discovery/authentication aren't under test and must never dial a live owner.
        monkeypatch.setattr(rpc, '_connection', lambda: link)
        sockets.append(socket)
        links.append(link)
        return rpc, socket

    yield create
    for socket, link in zip(sockets, links):
        socket.close()
        if socket.timer is not None:
            socket.timer.join(timeout=2)
        link.reader.join(timeout=2)
        assert not link.reader.is_alive()


def test_receipt_waits_past_owner_stall(owner_rpc):
    """A 25-second stall exceeds the old 20-second receipt budget."""
    rpc, socket = owner_rpc(25)
    try:
        assert rpc('worker.persist') == {'committed': True}
        assert len(socket.sent) == 1
    finally:
        rpc.close()


def test_closed_owner_still_fails_fast(owner_rpc):
    rpc, socket = owner_rpc(0.1, closes=True)
    started = time.monotonic()
    with pytest.raises(ConnectionClosed):
        rpc('worker.persist')
    elapsed = time.monotonic() - started
    assert elapsed < 1
    assert len(socket.sent) == 1
    assert rpc.link is None


def test_receipt_timeout_still_bounded(monkeypatch, owner_rpc):
    # raising=False exercises the old literal as a behavioral RED, not a missing-name error.
    monkeypatch.setattr(store, '_OWNER_RECEIPT_TIMEOUT_S', 0.2, raising=False)
    rpc, socket = owner_rpc(None)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='^worker_receipt_timeout$'):
        rpc('worker.persist')
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 2
    assert len(socket.sent) == 1
    assert rpc.link is None
