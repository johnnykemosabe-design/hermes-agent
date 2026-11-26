"""Reviewer probe (not part of the candidate): a store failure inside the new reconciliation read."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest


@pytest.mark.usefixtures('authority_cwd_in_tmp')
def test_probe_reconciliation_store_error_still_settles_unknown(tmp_path, monkeypatch):
    from gateway import session_kanban, session_managed_worker as managed
    from persistence.core.errors import StoreContention
    from tests.gateway import test_managed_worker_finished_exit as finished
    from tests.gateway import test_managed_worker_unknown_status as fixture
    # Child: hello -> result -> (on finish) transport-timeout error instead of 'finished'.
    child = finished.CHILD.replace("send({'type': 'finished'})",
                                   "send({'type': 'error', 'reason': 'worker_transport_timeout'})")
    monkeypatch.setattr(fixture, 'CHILD', child.replace("if sys.argv[1] == 'nonzero':", "if True:"))
    authority, ref, policy, claim, _, db = fixture._authority_with_a_lost_worker(tmp_path, monkeypatch)
    reasons = []
    monkeypatch.setattr(session_kanban, 'record_worker_loss', lambda policy, reason, **kw: reasons.append(reason))
    policy = replace(policy, kanban_json='{"task_id": "fixture"}')

    def busy(*args):
        raise StoreContention('database is locked')  # BEGIN IMMEDIATE of the finish replay under contention
    monkeypatch.setattr(managed, '_owner_finished', busy)
    outcome = {}

    async def scenario():
        row = await claim()
        authority._stamp_claim(ref, authority.sessions['s'], row)
        try:
            await managed.execute_managed(authority, ref, row, policy)
            outcome['raised'] = None
        except BaseException as exc:  # noqa: BLE001 - the probe records what escapes
            outcome['raised'] = type(exc).__name__
        outcome['admission'] = db.admissions.get(admission_id=row['admission_id']).status
        outcome['worker_rows'] = [tuple(r) for r in db.worker_executions_debug()] if hasattr(
            db, 'worker_executions_debug') else None

    with db, authority.persistence:
        asyncio.run(scenario())
        from tests.fixtures.storage_holder import raw_holder
        outcome['worker_status'] = [tuple(r) for r in raw_holder(db)._read_all(
            'SELECT execution_id, status FROM worker_executions')]
    print(f'PROBE_STORE_ERR raised={outcome["raised"]} admission={outcome["admission"]} '
          f'worker={outcome["worker_status"]} loss_notes={reasons}')
    assert outcome['raised'] == 'ManagedExecutionUnknown', outcome
    assert outcome['admission'] == 'unknown', outcome
