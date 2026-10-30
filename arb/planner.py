"""Decides which sports to pull odds for on each tick, within a credit budget.

* ``sports:`` entries may be wildcards (``soccer_*``, ``tennis_*``), resolved
  against the in-season list (free endpoint, refreshed hourly).
* Each sport's schedule comes from the free events endpoint: sports with no
  game inside ``lookahead_hours`` are not polled at all, and the closer the
  next game, the more often the sport is polled.
* Spending is paced by a token bucket so the remaining credits last until
  the quota resets (``credits_reset_day`` of the month).
"""
import calendar
import fnmatch
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("arbbot.planner")

DEFAULTS = {
    "fast_sec": 60,          # next game within near_hours
    "near_hours": 1,
    "mid_sec": 300,          # next game within mid_hours
    "mid_hours": 6,
    "slow_sec": 1800,        # next game within lookahead_hours
    "lookahead_hours": 24,
    "credits_reset_day": 1,  # day of month the Odds API quota resets
    "credit_reserve": 10,    # never spend the last N credits
    "sports_refresh_sec": 3600,
    "events_refresh_sec": 900,
}


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def seconds_until_reset(reset_day: int, now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    y, m = d.year, d.month
    day = min(reset_day, calendar.monthrange(y, m)[1])
    reset = d.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if reset.timestamp() <= now:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        reset = reset.replace(year=y, month=m, day=min(reset_day, calendar.monthrange(y, m)[1]))
    return max(60.0, reset.timestamp() - now)


class PollPlanner:
    def __init__(self, client, patterns, cost_per_sport: int, settings: dict = None):
        self.client = client
        self.patterns = list(patterns or [])
        self.cost = max(1, int(cost_per_sport))
        self.s = dict(DEFAULTS, **(settings or {}))
        self.sports = []           # resolved sport keys
        self._sports_at = 0.0
        self.next_game = {}        # sport -> unix ts of next pre-game event
        self._events_at = {}
        self.last_polled = {}      # sport -> ts
        self.allowance = None      # credits we may spend right now
        self._last_tick = None

    # -- discovery (free calls) ------------------------------------------------
    def _resolve(self, now):
        if self.sports and now - self._sports_at < self.s["sports_refresh_sec"]:
            return
        if not any(any(c in p for c in "*?[") for p in self.patterns):
            self.sports, self._sports_at = list(self.patterns), now
            return
        active = [s["key"] for s in self.client.list_sports() if not s.get("has_outrights")]
        out = []
        for p in self.patterns:
            for k in (fnmatch.filter(active, p) if any(c in p for c in "*?[") else [p]):
                if k not in out:
                    out.append(k)
        self.sports, self._sports_at = out, now
        log.info("Scanning %d sports: %s", len(out), ", ".join(out))

    def _refresh_events(self, sport, now):
        if now - self._events_at.get(sport, 0) < self.s["events_refresh_sec"]:
            return
        try:
            events = self.client.list_events(sport)
        except Exception as e:
            log.warning("Event list for %s failed: %s", sport, e)
            events = []
        upcoming = [t for t in (_ts(e.get("commence_time", "")) for e in events) if t > now]
        self.next_game[sport] = min(upcoming) if upcoming else None
        self._events_at[sport] = now

    def interval(self, sport, now):
        """Seconds between polls for this sport, or None to skip it."""
        nxt = self.next_game.get(sport)
        if nxt is None:
            return None
        hours = (nxt - now) / 3600
        if hours > self.s["lookahead_hours"]:
            return None
        if hours <= self.s["near_hours"]:
            return self.s["fast_sec"]
        if hours <= self.s["mid_hours"]:
            return self.s["mid_sec"]
        return self.s["slow_sec"]

    # -- budget ------------------------------------------------------------------
    def _accrue(self, now):
        remaining = self.client.credits_remaining
        if remaining is None:
            return
        spendable = max(0.0, remaining - self.s["credit_reserve"])
        rate = spendable / seconds_until_reset(int(self.s["credits_reset_day"]), now)
        cap = max(self.cost * 3, rate * self.s["slow_sec"])
        if self.allowance is None:
            self.allowance = min(spendable, self.cost * 2)
        else:
            self.allowance = min(cap, spendable,
                                 self.allowance + rate * (now - (self._last_tick or now)))
        self._last_tick = now

    def due(self, now=None):
        """Sports to poll now, soonest game first, within budget."""
        now = now or time.time()
        self._resolve(now)
        for sport in self.sports:
            self._refresh_events(sport, now)   # also fills credits_remaining
        self._accrue(now)
        candidates = []
        for sport in self.sports:
            iv = self.interval(sport, now)
            if iv is not None and now - self.last_polled.get(sport, 0) >= iv:
                candidates.append((self.next_game[sport], sport))
        picked = []
        for _, sport in sorted(candidates):
            if self.allowance is not None and self.allowance < self.cost:
                break
            picked.append(sport)
            if self.allowance is not None:
                self.allowance -= self.cost
        return picked

    def polled(self, sport, now=None):
        self.last_polled[sport] = now or time.time()

    def status(self, now=None):
        now = now or time.time()
        return {
            "sports": self.sports,
            "credits_remaining": self.client.credits_remaining,
            "allowance": None if self.allowance is None else round(self.allowance, 1),
            "cost_per_sport": self.cost,
            "schedule": {s: self.interval(s, now) for s in self.sports},
        }
