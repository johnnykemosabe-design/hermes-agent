"""Receipt deadlines are transport budgets, not evidence a write failed."""
import json
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import WorkerRPC


@pytest.mark.parametrize('content', ['[]', 'true', 'kanban: wrong', 'kanban: [1]', 'kanban: ['])
def test_bad_config_shape_falls_back_to_bounded_default_budget(tmp_path, content):
    from agent.runtime_session_store import WorkerReplyBudget
    (tmp_path / 'config.yaml').write_text(content)
    assert WorkerReplyBudget.for_home(tmp_path) == WorkerReplyBudget()


def test_reply_budget_is_registered_with_config_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    defaults = DEFAULT_CONFIG['kanban']
    assert defaults['worker_reply_timeout_seconds'] == 20
    assert defaults['worker_reply_retries'] == 2
    assert defaults['worker_reply_backoff_seconds'] == 1


class Link:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.waits = []
        self.sent = []
        self.reader = SimpleNamespace(is_alive=lambda: True)
        self.ws = SimpleNamespace(close=lambda: None)

    def send(self, request_id, message):
        self.sent.append(json.loads(message))

    def receive(self, timeout):
        self.waits.append(timeout)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_configured_wait_retries_late_reply_without_resending(tmp_path, monkeypatch):
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {
        'worker_reply_timeout_seconds': 0.05, 'worker_reply_retries': 2,
        'worker_reply_backoff_seconds': 0.01}}))
    link = Link([TimeoutError('worker_receipt_timeout'), {'result': {'committed': True}}])
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: link)
    assert rpc('prompt.receipt') == {'committed': True}
    assert link.waits == [0.05, 0.05]
    assert len(link.sent) == 1


def test_adoption_timeout_rereads_same_owner_assignment(tmp_path, monkeypatch):
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {
        'worker_reply_timeout_seconds': .01, 'worker_reply_retries': 0}}))
    first = Link([TimeoutError('worker_receipt_timeout')])
    second = Link([{'result': {'status': 'running'}}])
    links = iter([first, second])
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: next(links))
    assert rpc('worker.adopt', execution_id='same-assignment') == {'status': 'running'}
    assert first.sent == second.sent


def test_reply_retry_is_bounded_and_default_wait_is_unchanged(tmp_path, monkeypatch):
    link = Link([TimeoutError('worker_receipt_timeout')] * 4)
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: link)
    with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
        rpc('prompt.receipt')
    assert link.waits == [20.0, 20.0, 20.0]
    assert len(link.sent) == 1
