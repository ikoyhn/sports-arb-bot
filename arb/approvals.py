"""Human approval gate between "slips are filled" and "bets are placed".

The bot thread submits a ticket and blocks; the dashboard (uvicorn thread)
lists pending tickets and approves/rejects them. Unanswered tickets expire as
rejected — prices move, so a stale approval is worse than none.
"""
import itertools
import threading
import time


class ApprovalQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}  # id -> ticket dict (incl. private "_event"/"_decision")

    def request(self, summary: dict, timeout_sec: float) -> bool:
        """Block until the ticket is approved (True), rejected or expired (False)."""
        ev = threading.Event()
        with self._lock:
            tid = next(self._ids)
            self._pending[tid] = {
                "id": tid, "created": time.time(),
                "expires": time.time() + timeout_sec,
                "summary": summary, "_event": ev, "_decision": False,
            }
        ev.wait(timeout_sec)
        with self._lock:
            ticket = self._pending.pop(tid)
        return ticket["_decision"]

    def decide(self, tid: int, approve: bool) -> bool:
        with self._lock:
            ticket = self._pending.get(tid)
            if ticket is None or ticket["_event"].is_set():
                return False
            ticket["_decision"] = approve
            ticket["_event"].set()
            return True

    def list(self):
        with self._lock:
            return [{k: v for k, v in t.items() if not k.startswith("_")}
                    for t in self._pending.values() if not t["_event"].is_set()]
