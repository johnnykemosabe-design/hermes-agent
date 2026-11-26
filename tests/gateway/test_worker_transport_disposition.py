import pytest

from agent.runtime_session_store import WorkerPersistenceError


@pytest.mark.parametrize(('error', 'expected'), [
    (WorkerPersistenceError('worker_receipt_timeout'), 'worker_transport_timeout'),
    (TimeoutError('worker_receipt_timeout'), 'worker_transport_timeout'),
    (WorkerPersistenceError('disk_full'), 'managed_worker_failed'),
    (RuntimeError('worker_receipt_timeout'), 'managed_worker_failed'),
    (ValueError('task failed'), 'managed_worker_failed'),
])
def test_only_owner_reply_timeouts_get_infrastructure_disposition(error, expected):
    from agent import managed_worker
    assert managed_worker._failure_reason(error) == expected


def test_finished_owner_state_wins_over_lost_finished_ack(tmp_path, monkeypatch):
    from gateway import session_managed_worker as managed
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session
    import gateway.session_persistence as persistence
    db = open_persistence(profile_scope(home=tmp_path / 'owner'))
    try:
        seed_session(db, 's', 'cli')
        epoch = db.runtime.begin_epoch(instance_id='owner')
        secret = 'local-test-assignment'
        scope = dict(execution_id='worker', session_id='s', generation=0, epoch=epoch, secret=secret,
                     profile_id='fixture', pid=1234, birth=1.0)
        # Managed reservations store the assignment fingerprint, not its raw launch secret.
        from persistence.authority.vocabulary import admission_fingerprint
        claim = admission_fingerprint(canonical_target='s', payload={k: v for k, v in scope.items() if k != 'epoch'} |
                                      {'principal': managed.owner_worker_subject()})
        db.worker_executions.register(epoch=epoch, execution_id='worker', session_id='s',
                                      generation=0, kind='kanban', adoption_secret=claim)
        db.worker_executions.adopt(epoch=epoch, execution_id='worker', session_id='s',
                                   generation=0, adoption_secret=claim)
        monkeypatch.setattr(persistence, 'authority_persistence', lambda _: db)
        assert not managed._owner_finished(None, scope)
        from tests.fixtures.storage_holder import raw_holder
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE worker_executions SET status='terminal' WHERE execution_id='worker'"))
        assert not managed._owner_finished(None, scope), 'revocation is not a committed finish receipt'
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE worker_executions SET status='running' WHERE execution_id='worker'"))
        db.worker_executions.persist_operation(**{k: scope[k] for k in ('epoch', 'execution_id', 'session_id', 'generation')},
                                              sequence=1, operation='execution.finish', payload={})
        monkeypatch.setattr(raw_holder(db), '_execute_write', lambda *a, **k: pytest.fail('finish proof must be read-only'))
        assert managed._owner_finished(None, scope)
        assert not managed._owner_finished(None, dict(scope, execution_id='other-worker'))
    finally:
        db.close()


@pytest.mark.platforms('posix')
@pytest.mark.usefixtures('authority_cwd_in_tmp')
def test_owner_reconciles_terminal_receipt_when_finished_pipe_frame_is_lost(tmp_path, monkeypatch):
    from gateway import session_managed_worker as managed
    from tests.gateway import test_managed_worker_finished_exit as fixture
    # Exercise the real owner pipeline, private reservation and child pipe. The
    # child reports the actual timeout marker instead of its final finished ACK.
    monkeypatch.setattr(fixture, 'CHILD', fixture.CHILD.replace(
        "send({'type': 'finished'})",
        "send({'type': 'error', 'reason': 'worker_transport_timeout'})"))
    reserve = managed.reserve_admission_worker

    def commit_finish(authority, **kwargs):
        scope = reserve(authority, **kwargs)
        authority.persistence.worker_executions.finish(
            **{key: scope[key] for key in ('epoch', 'execution_id', 'session_id', 'generation')}, sequence=1)
        return scope

    monkeypatch.setattr(managed, 'reserve_admission_worker', commit_finish)
    fixture.exercise_finished_worker(tmp_path, monkeypatch, 'nonzero', False)


@pytest.mark.usefixtures('authority_cwd_in_tmp')
def test_uncommitted_timeout_is_recorded_before_unknown_for_board_retry(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace
    from gateway import session_managed_worker as managed, session_kanban
    from tests.gateway import test_managed_worker_unknown_status as fixture
    monkeypatch.setattr(fixture, 'CHILD', fixture.CHILD.replace(
        '"type": "bogus"', '"type": "error", "reason": "worker_transport_timeout"'))
    authority, ref, policy, claim, _, db = fixture._authority_with_a_lost_worker(tmp_path, monkeypatch)
    reasons = []
    monkeypatch.setattr(session_kanban, 'record_worker_loss', lambda policy, reason, **kw: reasons.append(reason))
    policy = replace(policy, kanban_json='{"task_id": "fixture"}')

    async def scenario():
        row = await claim()
        assert row is not None
        authority._stamp_claim(ref, authority.sessions['s'], row)
        with pytest.raises(managed.ManagedExecutionUnknown):
            await managed.execute_managed(authority, ref, row, policy)
        assert db.admissions.get(admission_id=row['admission_id']).status == 'unknown'
        assert reasons == ['worker_transport_timeout']

    with db, authority.persistence:
        asyncio.run(scenario())
