"""Worker saves must not hold the owner loop or falsely advertise a quiet process."""
import asyncio
import threading
import time
from types import SimpleNamespace

from agent.review_idle_queue import ReviewIdleQueue


def test_worker_claim_io_does_not_block_concurrent_receipts(monkeypatch):
    from gateway import session_worker as worker
    import gateway.session_authorities as authorities
    monkeypatch.setattr(authorities, 'require_serving', lambda authority: None)
    owner_thread = threading.get_ident()
    threads = []

    def slow_claim(*args):
        threads.append(threading.get_ident())
        time.sleep(.08)
        return 'claim'

    def slow_verify(*args):
        threads.append(threading.get_ident())
        time.sleep(.08)

    monkeypatch.setattr(worker, '_claim', slow_claim)
    monkeypatch.setattr(worker, '_verify', slow_verify)
    repo = SimpleNamespace(persist_operation=lambda **kw: {'saved': True})
    monkeypatch.setattr(worker, 'authority_persistence', lambda authority: SimpleNamespace(worker_executions=repo))
    connection = SimpleNamespace(authority=SimpleNamespace(), actor=SimpleNamespace(capabilities={'worker:adopt'}))
    params = {key: 'value' for key in worker._SCOPE | {'epoch', 'sequence', 'operation', 'payload'}}
    params['operation'] = 'transcript.append'

    async def scenario():
        started = time.monotonic()
        save = asyncio.create_task(worker.worker_request(connection, None, params, operation='persist'))
        await asyncio.sleep(.01)
        lag = time.monotonic() - started
        assert await save == {'saved': True}
        assert lag < .08, f'owner loop blocked for {lag:.3f}s'
    asyncio.run(scenario())
    assert threads and all(t != owner_thread for t in threads)


def test_managed_worker_lifetime_suppresses_idle_full_gc(monkeypatch):
    from agent import review_idle_queue
    from gateway import session_managed_worker as managed
    import gateway.session_context_refs as refs
    queue = ReviewIdleQueue()
    clock = [1.0]
    queue._now = lambda: clock[0]
    queue.note_turn_started()
    queue.note_turn_finished()
    clock[0] = 401.0
    assert queue.quiet_period()[0] == 400
    monkeypatch.setattr(review_idle_queue, 'QUEUE', queue)

    async def text(*args):
        return '', None

    async def spawn(*args):
        assert queue.quiet_period() == (0.0, None), 'live subprocess was mistaken for idle; full GC can freeze every receipt'
        return None

    monkeypatch.setattr(refs, 'managed_turn_text', text)
    monkeypatch.setattr(managed, '_mark_spawning', lambda *args: None)
    monkeypatch.setattr(managed, '_stopped_before_spawn', lambda *args: False)
    monkeypatch.setattr(managed, '_spawn_registered_worker', spawn)
    asyncio.run(managed.execute_managed(SimpleNamespace(), None, {}, None))
    assert queue.quiet_period() == (0.0, 401.0)
