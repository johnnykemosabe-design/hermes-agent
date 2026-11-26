"""Worker saves must not hold the owner loop or falsely advertise a quiet process."""
import asyncio
import threading
from types import SimpleNamespace

from agent.review_idle_queue import ReviewIdleQueue


def test_worker_claim_io_does_not_block_concurrent_receipts(monkeypatch):
    from gateway import session_worker as worker
    import gateway.session_authorities as authorities
    monkeypatch.setattr(authorities, 'require_serving', lambda authority: None)
    owner_thread = threading.get_ident()
    threads = []
    entered, release = threading.Event(), threading.Event()

    def slow_claim(*args):
        threads.append(threading.get_ident())
        assert threading.get_ident() != owner_thread
        entered.set()
        assert release.wait(10), 'loop never released claim I/O'
        return 'claim'

    def slow_verify(*args):
        threads.append(threading.get_ident())
        assert threading.get_ident() != owner_thread

    monkeypatch.setattr(worker, '_claim', slow_claim)
    monkeypatch.setattr(worker, '_verify', slow_verify)
    repo = SimpleNamespace(persist_operation=lambda **kw: {'saved': True})
    monkeypatch.setattr(worker, 'authority_persistence', lambda authority: SimpleNamespace(worker_executions=repo))
    connection = SimpleNamespace(authority=SimpleNamespace(), actor=SimpleNamespace(capabilities={'worker:adopt'}))
    params = {key: 'value' for key in worker._SCOPE | {'epoch', 'sequence', 'operation', 'payload'}}
    params['operation'] = 'transcript.append'

    async def scenario():
        save = asyncio.create_task(worker.worker_request(connection, None, params, operation='persist'))
        try:
            assert await asyncio.to_thread(entered.wait, 10), 'claim did not enter'
        finally:
            release.set()
        assert await save == {'saved': True}
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

    monkeypatch.setattr(refs, 'managed_turn_text', text)
    monkeypatch.setattr(managed, '_mark_spawning', lambda *args: None)
    monkeypatch.setattr(managed, '_stopped_before_spawn', lambda *args: False)
    monkeypatch.setattr(managed, '_spawn_registered_worker', spawn)
    asyncio.run(managed.execute_managed(SimpleNamespace(), None, {}, None))
    assert queue.quiet_period() == (0.0, 401.0)
