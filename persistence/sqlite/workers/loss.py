"""Read-only finish proof, and a transactional fence before publishing loss evidence."""
from persistence.authority.vocabulary import admission_fingerprint
from persistence.core.errors import AuthorityRefusal
from ..authority._runtime import _epoch


def committed_finish(holder, *, epoch, execution_id, session_id, generation, sequence):
    digest = admission_fingerprint(canonical_target=session_id, payload={'operation': 'execution.finish', 'payload': {}})
    with holder._read_ctx() as conn:
        return conn.execute(
            "SELECT 1 FROM worker_executions w JOIN worker_receipts r ON r.execution_id=w.execution_id "
            "WHERE w.execution_id=? AND w.session_id=? AND w.generation=? AND w.owner_epoch=? "
            "AND w.status='terminal' AND w.last_sequence=? AND r.sequence=? AND r.payload_digest=?",
            (execution_id, session_id, generation, epoch, sequence, sequence, digest)).fetchone() is not None


def fence_for_loss(holder, *, epoch, execution_id, session_id, generation):
    def write(conn):
        _epoch(conn, epoch)
        row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (execution_id,)).fetchone()
        if row is None or row['generation'] != generation or row['owner_epoch'] != epoch:
            raise AuthorityRefusal('stale_generation')
        # Session rebinding preserves the guard; the reservation's original session is not the current target.
        conn.execute("UPDATE worker_executions SET status='terminal' WHERE execution_id=?", (execution_id,))
        return row['tool_work_state'] == 0
    return holder._execute_write(write)


def tool_guard(holder, conn, session_id, payload, *, execution_id, started):
    if payload != {}:
        raise AuthorityRefusal('invalid_params')
    if started:
        conn.execute('UPDATE worker_executions SET tool_work_state=1 WHERE execution_id=?', (execution_id,))
    else:
        conn.execute('UPDATE worker_executions SET tool_work_state=0 WHERE execution_id=? AND tool_work_state IS NULL', (execution_id,))
    return {'value': None}
