"""A failed finish lookup is unproven and must continue into normal loss settlement."""
import asyncio

from persistence.core.errors import StoreContention


def unavailable(*args, **kwargs):
    raise StoreContention('test authority unavailable')


def test_finish_lookup_contention_returns_unproven_not_an_escaping_error(monkeypatch):
    from gateway import session_managed_worker
    monkeypatch.setattr(session_managed_worker, '_owner_finished', unavailable)
    result = asyncio.run(session_managed_worker._reconcile_finished(
        object(), {}, {'final_response': 'already received'}, 'worker_transport_timeout'))
    assert result is False

