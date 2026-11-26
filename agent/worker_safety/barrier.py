"""A receipt must commit before any tool middleware or dispatch can run."""
from agent.runtime_session_store import call_off_event_loop


class ToolBarrier:
    def __init__(self, store, *, runtime=None):
        self.store, self.started = store, False
        # Only these runtimes execute every tool through Hermes' tool_executor. Native
        # command runtimes (including codex_app_server), unknown modes and legacy callers
        # leave the durable state NULL: absence of UI events is never no-tool proof.
        if runtime in ('chat_completions', 'anthropic_messages', 'codex_responses'):
            store._apply('execution.tool_guard', {})

    def __call__(self):
        return call_off_event_loop(self._start)

    def _start(self):
        with self.store.lock:
            if not self.started:
                self.store._apply('execution.tool_start', {})
                self.started = True
