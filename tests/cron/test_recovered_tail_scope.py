"""Recovered tails bind the owning profile and never submit another model turn."""
from contextlib import asynccontextmanager, contextmanager
import asyncio
import json
from pathlib import Path
import threading

import pytest

from agent import secret_scope
from cron import executions, jobs, scheduler, scheduler_authority, scheduler_bookkeeping
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def recovery_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.hermes'
    alice, bob = [root / 'profiles' / name for name in ('alice', 'bob')]
    for home, room in ((root, 'host-room'), (alice, 'alice-room'), (bob, 'bob-room')):
        home.mkdir(parents=True, exist_ok=True)
        (home / '.env').write_text(f'MATRIX_HOME_ROOM={room}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('MATRIX_HOME_ROOM', 'ambient-room')
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(tmp_path / 'no-managed'))
    token = secret_scope.set_multiplex_context(True)
    try:
        yield alice, bob
    finally:
        secret_scope.reset_multiplex_context(token)


@contextmanager
def profile_store(home):
    token = set_hermes_home_override(str(home))
    try:
        with jobs.use_cron_store(home):
            yield
    finally:
        reset_hermes_home_override(token)


def prepared(job, *, ledger=True):
    fire = executions.create_execution(job['id'], source='test')['id'] if ledger else 'pruned-old-fire'
    if ledger:
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?', ('dead', 999999, fire))
    params = {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}
    journal = scheduler_authority.journal_path(job['id'], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({'params': params, 'receipt': None}), encoding='utf-8')
    return fire, journal


@contextmanager
def recovered_peer(monkeypatch, state, transport):
    calls = []
    async def call(method, params):
        calls.append(method)
        assert method in ('recover', 'cron.recover'), 'recovery must never submit model work'
        return state
    if transport == 'remote':
        class Peer:
            async def rpc(self, method, **params):
                return await call(method, params)
        @asynccontextmanager
        async def connect():
            yield Peer()
        monkeypatch.setattr('gateway.session_cron.owner_for_home', lambda home: None)
        monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', connect)
        yield calls
    else:
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run():
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()
        thread = threading.Thread(target=run)
        thread.start()
        assert ready.wait(5)
        monkeypatch.setattr('gateway.session_cron.owner_for_home', lambda home: (object(), loop))
        async def operation(owner, method, params):
            return await call(method, params)
        monkeypatch.setattr('gateway.session_cron.operation', operation)
        def forbidden_connect():
            pytest.fail('local/headless recovery must not connect or spawn a gateway')
        monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', forbidden_connect)
        try:
            yield calls
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()


@pytest.mark.parametrize('transport', ['remote', 'local'])
@pytest.mark.parametrize('caller_bound', [False, True])
def test_recovery_scopes_entire_tail_and_settles_once(recovery_home, monkeypatch, transport, caller_bound):
    alice, bob = recovery_home
    from cron import delivery_queue, scheduler_delivery
    # Restrict only platform discovery; target resolution and secret lookup stay real.
    monkeypatch.setattr(scheduler_delivery, '_iter_home_target_platforms', lambda: iter(['matrix']))
    monkeypatch.setattr(scheduler_delivery, '_resolve_home_env_var', lambda name: 'MATRIX_HOME_ROOM')
    seen, settlements, targets = [], [], []
    enqueue = delivery_queue.enqueue
    def scoped_enqueue(execution_id, job, content, **kwargs):
        targets.extend(scheduler_delivery._resolve_delivery_targets(job, **kwargs))
        assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'alice-room'
        return enqueue(execution_id, job, content, **kwargs)
    monkeypatch.setattr(delivery_queue, 'enqueue', scoped_enqueue)
    compose = scheduler._compose_run_delivery
    finish = scheduler_bookkeeping.finish_completed_run
    def scoped_compose(*args, **kwargs):
        seen.append(secret_scope.get_secret('MATRIX_HOME_ROOM'))
        return compose(*args, **kwargs)
    def scoped_finish(*args, **kwargs):
        assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'alice-room'
        settlements.append(args[2])
        return finish(*args, **kwargs)
    def scoped_lift(*args):
        assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'alice-room'
    monkeypatch.setattr(scheduler, '_compose_run_delivery', scoped_compose)
    monkeypatch.setattr(scheduler_bookkeeping, 'finish_completed_run', scoped_finish)
    monkeypatch.setattr(scheduler_authority, '_lift_pause', scoped_lift)
    caller = secret_scope.set_secret_scope({'MATRIX_HOME_ROOM': 'bob-room'}, profile_home=str(bob)) if caller_bound else None
    try:
        with profile_store(alice):
            job = jobs.create_job(prompt='report', schedule='every 1h', deliver='origin')
            fire, journal = prepared(job)
            state = {'status': 'terminal', 'job': job, 'result': [False, '', '', 'interrupted']}
            with recovered_peer(monkeypatch, state, transport) as calls:
                for _ in range(2):
                    scheduler_authority.reconcile_pending(allow_connect=transport == 'remote')
                    assert secret_scope.current_secret_scope() == ({'MATRIX_HOME_ROOM': 'bob-room'} if caller_bound else None)
            assert seen == ['alice-room']
            assert settlements == [fire]
            assert [target['chat_id'] for target in targets] == ['alice-room']
            assert delivery_queue.get_status(fire)['status'] == 'pending'
            with delivery_queue._transaction() as conn:
                assert conn.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 1
            assert len(calls) == 1
            assert not journal.exists()
            assert executions.get_execution(fire)['status'] == 'failed'
            assert jobs.get_job(job['id'])['repeat']['completed'] == 1
            assert len(list(jobs._job_output_dir(job['id']).glob('*.md'))) == 1
        assert not (bob / 'cron').exists()
    finally:
        if caller is not None:
            secret_scope.reset_secret_scope(caller)


@pytest.mark.parametrize('phase', ['compose', 'finish', 'pause-lift'])
def test_failed_tail_retries_reuse_output_and_restore_scope(recovery_home, monkeypatch, phase):
    alice, bob = recovery_home
    module, name = {
        'compose': (scheduler, '_compose_run_delivery'),
        'finish': (scheduler_bookkeeping, 'finish_completed_run'),
        'pause-lift': (scheduler_authority, '_lift_pause'),
    }[phase]
    original_call = getattr(module, name)
    calls = []
    def crash_once(*args, **kwargs):
        assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'alice-room'
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('fixture crash after output publication')
        return original_call(*args, **kwargs)
    monkeypatch.setattr(module, name, crash_once)
    caller = secret_scope.set_secret_scope({'MATRIX_HOME_ROOM': 'bob-room'}, profile_home=str(bob))
    try:
        with profile_store(alice):
            job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
            monkeypatch.setattr(jobs, '_cron_output_keep', lambda: 2)
            history = jobs.save_job_output(job['id'], 'historical real report')
            fire, journal = prepared(job)
            state = {'status': 'terminal', 'job': job, 'result': [False, '', '', 'interrupted']}
            with recovered_peer(monkeypatch, state, 'remote'):
                scheduler_authority.reconcile_pending()
                assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'bob-room'
                assert journal.exists() == (phase != 'pause-lift')
                files = sorted(jobs._job_output_dir(job['id']).glob('*.md'))
                assert len(files) == 2
                recovered = next(path for path in files if path != history)
                original = recovered.stat()
                # Simulate a different recovery tick, without waiting on the real clock.
                from datetime import timedelta
                now = jobs._hermes_now()
                monkeypatch.setattr(jobs, '_hermes_now', lambda: now + timedelta(minutes=1))
                scheduler_authority.reconcile_pending()
            assert secret_scope.get_secret('MATRIX_HOME_ROOM') == 'bob-room'
            assert sorted(jobs._job_output_dir(job['id']).glob('*.md')) == files
            assert recovered.stat().st_mtime_ns == original.st_mtime_ns
            assert 'interrupted' in recovered.read_text(encoding='utf-8')
            assert history.read_text(encoding='utf-8') == 'historical real report'
            assert not journal.exists()
            assert jobs.get_job(job['id'])['repeat']['completed'] == 1
            assert executions.get_execution(fire)['status'] == 'failed'
    finally:
        secret_scope.reset_secret_scope(caller)


@pytest.mark.parametrize('ledger', [True, False], ids=['retained-row', 'pruned-row'])
@pytest.mark.parametrize('receipt', ['terminal', 'missing', 'booked-before-newer-legacy-success'])
def test_old_receipt_never_replaces_newer_success(recovery_home, monkeypatch, ledger, receipt):
    from datetime import datetime, UTC
    alice, _ = recovery_home
    clock = [datetime(2026, 11, 16, 15, tzinfo=UTC)]
    monkeypatch.setattr(jobs, '_hermes_now', lambda: clock[0])
    monkeypatch.setattr(executions, '_hermes_now', lambda: clock[0])
    with profile_store(alice):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='origin')
        fire, journal = prepared(job, ledger=ledger)
        state = {'status': 'missing'} if receipt == 'missing' else {
            'status': 'terminal', 'job': job, 'result': [False, '', '', 'interrupted']}
        if receipt == 'booked-before-newer-legacy-success':
            assert jobs.mark_job_run(job['id'], False, 'interrupted', execution_id=fire)
        clock[0] = datetime(2026, 11, 23, 15, tzinfo=UTC)
        newer = executions.create_execution(job['id'], source='test')['id']
        executions.finish_execution(newer, success=True)
        assert jobs.mark_job_run(job['id'], True, **(
            {} if receipt == 'booked-before-newer-legacy-success' else {'execution_id': newer}))
        before = jobs.get_job(job['id'])
        queue_calls = []
        def enqueue(*args, **kwargs):
            queue_calls.append((args, kwargs))
            return {'status': 'queued'}
        monkeypatch.setattr('cron.delivery_queue.enqueue', enqueue)
        with recovered_peer(monkeypatch, state, 'remote'):
            scheduler_authority.reconcile_pending()
            scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id']) == before, 'old settlement must not mutate current status, counters, schedule or queued delivery'
        assert queue_calls == [], 'stale failure must never be queued to the user'
        assert not journal.exists()
        if ledger:
            assert executions.get_execution(fire)['status'] == 'failed'
            assert executions.get_execution(fire)['delivery_outcome'] == 'suppressed'
        assert executions.get_execution(newer)['status'] == 'completed'


def test_published_recovery_is_not_recreated_after_retention(recovery_home, monkeypatch):
    from datetime import timedelta
    alice, _ = recovery_home
    with profile_store(alice):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        _fire, journal = prepared(job)
        state = {'status': 'terminal', 'job': job, 'result': [False, 'old output', '', 'interrupted']}
        compose = scheduler._compose_run_delivery
        def fail(*args, **kwargs):
            raise RuntimeError('fixture composition failure')
        monkeypatch.setattr(scheduler, '_compose_run_delivery', fail)
        with recovered_peer(monkeypatch, state, 'remote'):
            scheduler_authority.reconcile_pending()
            first = list(jobs._job_output_dir(job['id']).glob('*.md'))
            assert len(first) == 1
            monkeypatch.setattr(jobs, '_cron_output_keep', lambda: 1)
            later = jobs._hermes_now() + timedelta(minutes=1)
            monkeypatch.setattr(jobs, '_hermes_now', lambda: later)
            latest = jobs.save_job_output(job['id'], 'newer report')
            assert not first[0].exists()
            monkeypatch.setattr(scheduler, '_compose_run_delivery', compose)
            scheduler_authority.reconcile_pending()
        assert list(jobs._job_output_dir(job['id']).glob('*.md')) == [latest]
        assert latest.read_text(encoding='utf-8') == 'newer report'
        assert not journal.exists()


def test_ordinary_tail_publication_is_reused_by_recovery(recovery_home, monkeypatch):
    from datetime import timedelta
    alice, _ = recovery_home
    with profile_store(alice):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = prepared(job)
        job['execution_id'] = fire
        compose = scheduler._compose_run_delivery
        def fail(*args, **kwargs):
            raise RuntimeError('fixture ordinary tail crash')
        monkeypatch.setattr(scheduler, '_compose_run_delivery', fail)
        with scheduler._fire_secret_scope(), pytest.raises(RuntimeError, match='ordinary tail crash'):
            scheduler._save_compose_deliver(
                scheduler._RunDelivery(job, True, None), scheduler._FireOwnership(job),
                'answer', 'report', adapters=None, loop=None, verbose=False, execution_token=None)
        files = list(jobs._job_output_dir(job['id']).glob('*.md'))
        later = jobs._hermes_now() + timedelta(minutes=1)
        monkeypatch.setattr(jobs, '_hermes_now', lambda: later)
        monkeypatch.setattr(scheduler, '_compose_run_delivery', compose)
        state = {'status': 'terminal', 'job': job, 'result': [True, 'report', 'answer', None]}
        with recovered_peer(monkeypatch, state, 'remote'):
            scheduler_authority.reconcile_pending()
        assert list(jobs._job_output_dir(job['id']).glob('*.md')) == files
        assert not journal.exists()


def test_scope_install_failure_never_uses_ambient_credentials(recovery_home, monkeypatch):
    alice, _ = recovery_home
    def denied(*args, **kwargs):
        raise secret_scope.UnscopedSecretError('fixture-source-refused')
    monkeypatch.setattr('hermes_cli.env_loader.hydrate_profile_secret_sources', denied)
    with profile_store(alice):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='origin')
        fire, journal = prepared(job)
        state = {'status': 'terminal', 'job': job, 'result': [False, '', '', 'interrupted']}
        with recovered_peer(monkeypatch, state, 'remote') as calls:
            for _ in range(2):
                scheduler_authority.reconcile_pending()
                assert secret_scope.current_secret_scope() is None
        assert len(calls) == 2
        assert journal.exists()
        assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
        assert jobs.get_job(job['id'])['last_status'] is None
        assert executions.get_execution(fire)['status'] == 'claimed'


def test_recovered_output_identity_is_serialized_across_writers(recovery_home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    alice, _ = recovery_home
    barrier = threading.Barrier(2)
    writes = []
    write = jobs.atomic_write_text
    def record_write(path, *args, **kwargs):
        writes.append(path)
        return write(path, *args, **kwargs)
    with profile_store(alice):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
    monkeypatch.setattr(jobs, 'atomic_write_text', record_write)
    def publish():
        with profile_store(alice):
            barrier.wait(timeout=5)
            return jobs.save_job_output(job['id'], 'report', execution_id='same-fire')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        paths = [future.result(timeout=10) for future in futures]
    assert paths[0] == paths[1]
    assert writes == [paths[0]]
    assert paths[0].read_text(encoding='utf-8') == 'report'
