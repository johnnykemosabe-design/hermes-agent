"""Concurrent real RPC readers against a deliberately stalled asyncio owner.

CLI compares any checkout with this same harness; no gateway, profiles, or LLMs.
The controller expires the first reply window only after every save commits,
then releases the real stalled loop only when every worker retries or exits.
WebSockets, reader threads and SQLite receipts are real; no timing margin is used.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading


def reproduce(root, workers=10):
    from agent.runtime_session_store import WorkerRPC, _OwnerLink
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session
    from tests.fixtures.storage_holder import raw_holder
    from websockets.asyncio.server import serve
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    root = Path(root)
    home = root / 'owner'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text(json.dumps({'kanban': {
        'worker_reply_timeout_seconds': 20, 'worker_reply_retries': 2,
        'worker_reply_backoff_seconds': .01}}))
    with open_persistence(profile_scope(home=home)) as db:
        epoch = db.runtime.begin_epoch(instance_id='load-owner')
        scopes = []
        for n in range(workers):
            sid = f's{n}'
            seed_session(db, sid, 'cli')
            scope = dict(epoch=epoch, execution_id=f'w{n}', session_id=sid, generation=0)
            db.worker_executions.register(**scope, kind='compute', adoption_secret='local-fixture')
            scopes.append(scope)
        started = threading.Event()
        ready = {}
        committed, release = threading.Event(), threading.Event()
        arrivals = threading.Barrier(workers)
        retries = threading.Barrier(workers, action=release.set)
        failures = threading.Barrier(workers, action=release.set)

        async def owner():
            all_received = asyncio.Event()
            count = 0

            async def handle(socket):
                nonlocal count
                request = json.loads(await socket.recv())
                result = await asyncio.to_thread(db.worker_executions.persist_operation, **request['params'])
                count += 1
                if count == workers:
                    all_received.set()
                    # Deliberately emulate an event-loop/GIL pause after all saves commit.
                    committed.set()
                    assert release.wait(30), 'workers neither retried nor exited'
                await all_received.wait()
                try:
                    await socket.send(json.dumps({'id': request['id'], 'result': result}))
                except ConnectionClosed:
                    pass  # This is precisely the base regression: committed, caller gone.

            async with serve(handle, '127.0.0.1', 0) as server:
                ready.update(loop=asyncio.get_running_loop(), stop=asyncio.Event(),
                             port=next(iter(server.sockets)).getsockname()[1])
                started.set()
                await ready['stop'].wait()

        thread = threading.Thread(target=lambda: asyncio.run(owner()), daemon=True)
        thread.start()
        assert started.wait(5), 'fixture server did not start'

        class ControlledReplyClock(_OwnerLink):
            first = True

            def receive(self, timeout):
                if self.first:
                    self.first = False
                    assert committed.wait(30), 'saves did not commit'
                    arrivals.wait(timeout=30)
                    raise TimeoutError('worker_receipt_timeout')
                retries.wait(timeout=30)
                return super().receive(30)

        barrier = threading.Barrier(workers)

        def fake_worker(scope):
            rpc = WorkerRPC(home)
            socket = connect(f"ws://127.0.0.1:{ready['port']}", proxy=None,
                             open_timeout=5, close_timeout=.1, ping_interval=None)
            link = ControlledReplyClock(socket)
            rpc.link = link
            rpc._connection = lambda: link
            try:
                barrier.wait(timeout=5)
                rpc('worker.persist', **scope, sequence=1, operation='transcript.append',
                    payload={'messages': [{'role': 'user', 'content': 'once'}], 'turn_lease_holder': None})
                return 0
            except TimeoutError:
                failures.wait(timeout=30)
                return 1
            finally:
                rpc.close()

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                crashes = sum(pool.map(fake_worker, scopes))
            receipts = raw_holder(db)._read_one('SELECT COUNT(*) FROM worker_receipts')[0]
            messages = raw_holder(db)._read_one('SELECT COUNT(*) FROM messages')[0]
        finally:
            release.set()
            ready['loop'].call_soon_threadsafe(ready['stop'].set)
            thread.join(timeout=10)
            assert not thread.is_alive(), 'fixture owner leaked'
    return dict(workers=workers, crashes=crashes, receipts=receipts, messages=messages,
                stall_control='events', expired_reply_windows=workers)


def test_concurrent_committed_saves_survive_stalled_loop(tmp_path):
    result = reproduce(tmp_path)
    assert result['receipts'] == result['messages'] == result['workers']
    assert result['crashes'] == 0, result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('--expect-crashes', type=int, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    with tempfile.TemporaryDirectory(dir=args.scratch) as work:
        result = reproduce(work, args.workers)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['crashes'] == args.expect_crashes and
                     result['receipts'] == result['messages'] == args.workers else 1)
