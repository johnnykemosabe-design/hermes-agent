"""A receipt must commit before any tool middleware or dispatch can run."""
from agent.runtime_session_store import call_off_event_loop


class ToolBarrier:
    def __init__(self, store):
        self.store, self.started = store, False
        store._apply('execution.tool_guard', {})

    def __call__(self):
        return call_off_event_loop(self._start)

    def _start(self):
        with self.store.lock:
            if not self.started:
                self.store._apply('execution.tool_start', {})
                self.started = True
