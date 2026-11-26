"""Unavailable loss evidence is never positive proof and never drops the board note."""
import asyncio
from types import SimpleNamespace

from persistence.core.errors import StoreContention


def unavailable(*args, **kwargs):
    raise StoreContention('test authority unavailable')


def test_finish_lookup_contention_returns_unproven_not_an_escaping_error(monkeypatch):
    from gateway import session_managed_worker
    monkeypatch.setattr(session_managed_worker, '_owner_finished', unavailable)
    result = asyncio.run(session_managed_worker._reconcile_finished(
        object(), {}, {'final_response': 'already received'}, 'worker_transport_timeout'))
    assert result is False


def test_fence_contention_still_records_loss_without_strike_exemption(monkeypatch):
    from gateway import session_kanban, session_managed_worker
    import gateway.session_persistence

    monkeypatch.setattr(gateway.session_persistence, 'authority_persistence',
                        lambda _: SimpleNamespace(worker_executions=SimpleNamespace(fence_for_loss=unavailable)))
    notes = []
    monkeypatch.setattr(session_kanban, 'record_worker_loss', lambda *args, **kwargs: notes.append((args, kwargs)))
    scope = dict(epoch=1, execution_id='attempt', session_id='conversation', generation=0, pid=42)
    policy = object()
    session_managed_worker._fenced_loss_note(object(), policy, 'worker_transport_timeout', scope)
    assert notes == [((policy, 'worker_transport_timeout'),
                      dict(pid=42, bound=True, no_tool_started=False))]
