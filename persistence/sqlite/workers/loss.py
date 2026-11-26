"""Read-only exact finish-receipt proof; no worker or schema mutations."""
from persistence.authority.vocabulary import admission_fingerprint


def committed_finish(holder, *, epoch, execution_id, session_id, generation, sequence):
    digest = admission_fingerprint(canonical_target=session_id, payload={'operation': 'execution.finish', 'payload': {}})
    with holder._read_ctx() as conn:
        return conn.execute(
            "SELECT 1 FROM worker_executions w JOIN worker_receipts r ON r.execution_id=w.execution_id "
            "WHERE w.execution_id=? AND w.session_id=? AND w.generation=? AND w.owner_epoch=? "
            "AND w.status='terminal' AND w.last_sequence=? AND r.sequence=? AND r.payload_digest=?",
            (execution_id, session_id, generation, epoch, sequence, sequence, digest)).fetchone() is not None

