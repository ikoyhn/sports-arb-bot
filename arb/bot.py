import logging
import time

from .approvals import ApprovalQueue
from .bookie import BetResult
from .bookies import get_adapter
from .engine import find_arb
from .odds import OddsClient
from .planner import PollPlanner
from .storage import Storage
from .web_bookie import PreparedBet, WebBookieAdapter

log = logging.getLogger("arbbot")


class ArbBot:
    """Orchestrator: poll odds -> detect arbs -> size stakes -> execute via adapters.

    Browser-book legs are executed in two phases: every leg's slip is filled
    first (AI agent + code verification), then the whole arb is approved on
    the dashboard (``execution_mode: approve``) or auto-committed
    (``execution_mode: auto``), then each slip is committed by code.
    """

    def __init__(self, cfg, storage: Storage):
        self.cfg = cfg
        self.storage = storage
        self.approvals = ApprovalQueue()
        self.odds = OddsClient(cfg.odds_api_key, cfg.odds_api_url)
        self.adapters = {}    # bookie name -> authenticated adapter
        self.balances = {}    # bookie name -> float | None
        self.last_poll = None
        self.last_error = None
        self.odds_credits = None
        self.planner = PollPlanner(
            self.odds, cfg.sports,
            OddsClient.cost(cfg.markets, cfg.regions, cfg.bookmakers), cfg.polling)
        self._latest = {}     # sport -> opportunities from its last poll
        self._executed = set()  # arbs already acted on (never re-place the same one)

    @property
    def latest(self):
        """Most recent opportunities across sports (for dashboard)."""
        return sorted((o for opps in self._latest.values() for o in opps),
                      key=lambda o: o.roi_pct, reverse=True)

    def _init_adapters(self):
        for name, creds in (self.cfg.bookies or {}).items():
            creds = dict(creds or {})
            if self.cfg.state and "state" not in creds:
                creds["state"] = self.cfg.state
            try:
                adapter = get_adapter(name, creds)
            except KeyError as e:
                log.warning("%s", e)
                continue
            except Exception as e:
                log.warning("Adapter %s init failed: %s", name, e)
                continue
            if hasattr(adapter, "max_price_drift"):
                adapter.max_price_drift = float(self.cfg.max_price_drift)
            if name in (self.cfg.exchange_fees or {}) and hasattr(adapter, "fee_rate"):
                adapter.fee_rate = float(self.cfg.exchange_fees[name])
            if isinstance(adapter, WebBookieAdapter):
                adapter.ai = self.cfg.ai
            try:
                if adapter.authenticate():
                    self.adapters[name] = adapter
                    log.info("Adapter %s authenticated", name)
                else:
                    log.warning("Adapter %s authentication failed", name)
            except Exception as e:
                log.warning("Adapter %s authentication error: %s", name, e)

    def start(self):
        # Browser adapters must be created on the thread that drives them
        # (Playwright's sync API is thread-bound), so init happens here.
        self._init_adapters()
        while True:
            try:
                self.run_once()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Poll cycle failed")
            time.sleep(max(5, self.cfg.poll_interval_sec))

    def run_once(self):
        t0 = time.time()
        sports = self.planner.due()
        self.odds_credits = self.odds.credits_remaining
        if not sports:
            return
        events = []
        for sport in sports:
            evs = self.odds.fetch_odds(sport, self.cfg.regions, self.cfg.markets,
                                       self.cfg.bookmakers)
            self.planner.polled(sport)
            self._latest[sport] = find_arb(evs, self.cfg)
            events.extend(evs)
        self.odds_credits = self.odds.credits_remaining
        for name, adapter in self.adapters.items():
            try:
                self.balances[name] = adapter.get_balance()
            except Exception as e:
                self.balances[name] = None
                log.warning("Balance fetch for %s failed: %s", name, e)

        opps = [o for s in sports for o in self._latest[s]]
        self.last_poll = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_error = None
        log.info("Polled %s in %.1fs: %d events, %d opportunities, %s credits left",
                 ",".join(sports), time.time() - t0, len(events), len(opps),
                 self.odds_credits)
        for opp in sorted(opps, key=lambda o: o.roi_pct, reverse=True):
            self._handle(opp)

    def _handle(self, opp):
        key = (opp.event_id, opp.market, tuple(sorted((l.label, l.bookie) for l in opp.legs)))
        if key in self._executed:
            return  # already acted on this exact arb — don't stack positions
        self.storage.record_opportunity(opp)
        desc = " | ".join(f"{l.label}@{l.bookie} {l.odds} (${l.stake:.2f})" for l in opp.legs)
        log.info("OPPORTUNITY %s [%s] ROI %.2f%% %s", opp.event, opp.market, opp.roi_pct, desc)
        if self.cfg.dry_run:
            return  # detect + log only
        self._executed.add(key)

        # 1) pre-flight every leg before touching any book
        for leg in opp.legs:
            adapter = self.adapters.get(leg.bookie)
            if adapter is None:
                self._skip_all(opp, f"no adapter for {leg.bookie} — arb not executed")
                return
            if not adapter.supports(leg):
                self._skip_all(opp, f"{leg.bookie} can't place {opp.market} {leg.label}")
                return
            bal = self.balances.get(leg.bookie)
            if bal is None and isinstance(adapter, WebBookieAdapter):
                self._skip_all(opp, f"{leg.bookie} balance unreadable — not logged in? "
                                    "run once with headless: false and log in")
                return
            if bal is not None and bal < leg.stake:
                self._skip_all(opp, f"{leg.bookie} insufficient balance "
                                    f"({bal:.2f} < {leg.stake:.2f})")
                return

        # 2) fill every browser slip; any failure means nothing gets placed
        prepared = {}
        for leg in opp.legs:
            adapter = self.adapters[leg.bookie]
            if not isinstance(adapter, WebBookieAdapter):
                continue
            log.info("Preparing %s slip: %s @ %.3f", leg.bookie, leg.label, leg.odds)
            res = adapter.prepare_bet(opp, leg, leg.stake)
            if isinstance(res, BetResult):
                self._skip_all(opp, f"{leg.bookie} slip not prepared: {res.message}")
                return
            prepared[leg.bookie] = res

        # 3) approval gate
        if self.cfg.execution_mode != "auto":
            summary = {"event": opp.event, "commence": opp.commence_time, "market": opp.market,
                       "roi_pct": opp.roi_pct, "net_profit": opp.net_profit,
                       "legs": [prepared[l.bookie].summary() if l.bookie in prepared else
                                {"bookie": l.bookie, "outcome": l.label,
                                 "priced": l.odds, "stake": l.stake, "notes": "API order"}
                                for l in opp.legs]}
            log.info("Waiting up to %ss for dashboard approval: %s",
                     self.cfg.approval_timeout_sec, opp.event)
            if not self.approvals.request(summary, float(self.cfg.approval_timeout_sec)):
                self._skip_all(opp, "not approved (rejected or timed out)", status="REJECTED")
                return

        # 4) commit. API legs go first: they are fill-or-kill, so if one doesn't
        #    fill nothing else is placed. Browser slips are re-verified by code.
        order = sorted(opp.legs, key=lambda l: l.bookie in prepared)
        placed_any = False
        for i, leg in enumerate(order):
            adapter = self.adapters[leg.bookie]
            try:
                p = prepared.get(leg.bookie)
                res = adapter.commit(p) if isinstance(p, PreparedBet) \
                    else adapter.place_bet(opp, leg, leg.stake)
            except Exception as e:
                res = BetResult(False, message=f"{type(e).__name__}: {e}")
            self.storage.record_bet(opp, leg, "PLACED" if res.success else "FAILED",
                                    res.ref, res.message)
            if res.success:
                placed_any = True
                continue
            rest = order[i + 1:]
            for other in rest:
                self.storage.record_bet(opp, other, "SKIPPED", "",
                                        f"not placed: {leg.bookie} leg failed")
            if placed_any:
                log.error("Leg FAILED after other legs were placed (%s): %s — you have an "
                          "UNHEDGED position, check the bet log", leg.bookie, res.message)
            else:
                log.warning("First leg failed (%s): %s — nothing placed",
                            leg.bookie, res.message)
            return

    def _skip_all(self, opp, message, status="SKIPPED"):
        log.info("Arb %s not executed: %s", opp.event, message)
        for leg in opp.legs:
            self.storage.record_bet(opp, leg, status, "", message)
