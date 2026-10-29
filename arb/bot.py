import logging
import time

from .bookies import get_adapter
from .engine import find_arb
from .odds import OddsClient
from .storage import Storage
from .web_bookie import WebBookieAdapter

log = logging.getLogger("arbbot")


class ArbBot:
    """Orchestrator: poll odds -> detect arbs -> size stakes -> execute via adapters."""

    def __init__(self, cfg, storage: Storage):
        self.cfg = cfg
        self.storage = storage
        self.odds = OddsClient(cfg.odds_api_key, cfg.odds_api_url)
        self.adapters = {}    # bookie name -> authenticated adapter
        self.balances = {}    # bookie name -> float | None
        self.last_poll = None
        self.last_error = None
        self.odds_credits = None
        self.latest = []      # most recent opportunities (for dashboard)
        self._init_adapters()

    def _init_adapters(self):
        for name, creds in (self.cfg.bookies or {}).items():
            try:
                adapter = get_adapter(name, creds)
            except KeyError as e:
                log.warning("%s", e)
                continue
            except Exception as e:
                log.warning("Adapter %s init failed: %s", name, e)
                continue
            try:
                if adapter.authenticate():
                    self.adapters[name] = adapter
                    if isinstance(adapter, WebBookieAdapter):
                        adapter.max_price_drift = float(self.cfg.max_price_drift)
                    log.info("Adapter %s authenticated", name)
                else:
                    log.warning("Adapter %s authentication failed", name)
            except Exception as e:
                log.warning("Adapter %s authentication error: %s", name, e)

    def start(self):
        while True:
            try:
                self.run_once()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Poll cycle failed")
            time.sleep(max(5, self.cfg.poll_interval_sec))

    def run_once(self):
        t0 = time.time()
        events = self.odds.fetch_events(self.cfg.sports, self.cfg.regions, self.cfg.markets)
        for name, adapter in self.adapters.items():
            try:
                self.balances[name] = adapter.get_balance()
            except Exception as e:
                self.balances[name] = None
                log.warning("Balance fetch for %s failed: %s", name, e)

        opps = find_arb(events, self.cfg)
        self.latest = opps
        self.last_poll = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_error = None
        log.info("Polled in %.1fs: %d events, %d opportunities",
                 time.time() - t0, len(events), len(opps))
        for opp in opps:
            self._handle(opp)

    def _handle(self, opp):
        self.storage.record_opportunity(opp)
        desc = " | ".join(f"{l.outcome}@{l.bookie} {l.odds} (${l.stake:.2f})" for l in opp.legs)
        log.info("OPPORTUNITY %s ROI %.2f%% %s", opp.event, opp.roi_pct, desc)
        if self.cfg.dry_run:
            return  # detect + log only
        for leg in opp.legs:
            adapter = self.adapters.get(leg.bookie)
            if adapter is None:
                self.storage.record_bet(opp, leg, "MANUAL", "",
                    "no adapter for this bookie — place manually")
                continue
            bal = self.balances.get(leg.bookie)
            if bal is not None and bal < leg.stake:
                self.storage.record_bet(opp, leg, "SKIPPED", "",
                    f"insufficient balance ({bal:.2f} < {leg.stake:.2f})")
                continue
            if isinstance(adapter, WebBookieAdapter) and bal is None:
                self.storage.record_bet(opp, leg, "SKIPPED", "",
                    "balance unreadable — not logged in? run once with headless: false and log in")
                continue
            try:
                res = adapter.place_bet(opp, leg, leg.stake)
                self.storage.record_bet(opp, leg,
                                        "PLACED" if res.success else "FAILED",
                                        res.ref, res.message)
            except Exception as e:
                self.storage.record_bet(opp, leg, "ERROR", "", str(e))
