"""Shared Playwright adapter for books with no public betting API
(DraftKings, FanDuel, BetMGM, Caesars, theScore/ESPN Bet, Fanatics, BetRivers,
Hard Rock, bet365, ...).

Flow
----
Bets go through two phases so a human (or strict code checks) sits between
"slip filled" and "money committed":

1. ``prepare_bet`` — a Claude agent (``arb/agent.py``) navigates the site, puts
   the selection in the slip and types the engine-sized stake. Code then
   verifies the slip independently: outcome present, price within
   ``max_price_drift``, stake field equals the sized stake.
2. ``commit`` — code (never the model) re-checks the price and stake and
   clicks *Place bet*.

Login is manual: run once with ``headless: false``, log in yourself (2FA,
location check and all), and the session persists in ``.profiles/<book>/``.

Anti-detection hygiene
----------------------
* Persistent Chromium profile per book → stable cookies/device fingerprint
* Real desktop user agent; fixed locale + timezone (set them to match your
  account's state/region — geo mismatch is a common flag)
* ``navigator.webdriver`` masked; chrome runtime / plugins / languages shims
* ``--disable-blink-features=AutomationControlled``

Risks
-----
This still violates most books' ToS; CAPTCHAs, device checks and account
limiting can still happen. Keep dry_run on until the flow is verified.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .bookie import BetResult, BookieAdapter

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# One Playwright driver per process: the sync API doesn't support several
# instances in one thread, and every book runs on the bot thread.
_PW = None


def _playwright():
    global _PW
    if _PW is None:
        from playwright.sync_api import sync_playwright  # lazy import
        _PW = sync_playwright().start()
    return _PW


def _to_decimal(token: str) -> Optional[float]:
    """'1.85' → 1.85 ; '+185' → 2.85 ; '-150' → 1.667 (American odds)."""
    token = token.strip().replace("−", "-")
    try:
        if token.startswith("+"):
            return 1.0 + int(token[1:]) / 100.0
        if token.startswith("-"):
            return 1.0 + 100.0 / abs(int(token[1:]))
        v = float(token)
        return v if v > 1.01 else None
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class PreparedBet:
    adapter: "WebBookieAdapter"
    opportunity: object
    leg: object
    stake: float
    stake_ref: str
    slip_price: Optional[float]
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"bookie": self.adapter.name, "outcome": self.leg.label,
                "priced": self.leg.odds, "slip_price": self.slip_price,
                "stake": round(self.stake, 2), "notes": self.notes}


class WebBookieAdapter(BookieAdapter):
    # Defaults per book come from arb/bookies/__init__.py; config can override.
    home_url: str = ""
    allowed_domains: tuple = ()
    place_bet_text: str = r"place\b.*\bbet|place \$|bet now"
    success_texts: tuple = ("success", "placed", "confirmed", "accepted", "receipt")
    error_texts: tuple = ("insufficient", "invalid", "not available", "unable to",
                          "bet has not", "odds have changed", "price changed", "error")
    nav_timeout_ms = 25_000
    action_timeout_ms = 15_000

    # price safety (bot injects cfg.max_price_drift)
    max_price_drift: float = 0.02     # max allowed slip-price movement (decimal)
    require_price_check: bool = True  # abort if slip price can't be read
    ai: dict = {}                     # bot injects cfg.ai

    def __init__(self, creds: dict):
        super().__init__(creds)
        self._ctx = None
        self._page: Optional[object] = None
        state = str(creds.get("state", "")).lower()
        self.home_url = creds.get("home_url", self.home_url).format(state=state)
        self.allowed_domains = tuple(creds.get("allowed_domains", self.allowed_domains))
        self.place_bet_text = creds.get("place_bet_text", self.place_bet_text)

    # -- lifecycle --------------------------------------------------------
    def authenticate(self) -> bool:
        profile = Path(self.creds.get("profile_dir", f".profiles/{self.name}")).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._ctx = _playwright().chromium.launch_persistent_context(
            str(profile),
            headless=bool(self.creds.get("headless", True)),
            user_agent=self.creds.get("user_agent", DEFAULT_UA),
            locale=self.creds.get("locale", "en-US"),
            timezone_id=self.creds.get("timezone_id", "America/New_York"),
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._ctx.add_init_script(STEALTH_JS)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)
        try:
            self._page.goto(self.home_url, timeout=self.nav_timeout_ms)
            return True  # login itself is checked via balance read
        except Exception:
            return False

    def _require_page(self):
        if self._page is None or self._page.is_closed():
            if not self.authenticate():
                raise RuntimeError("browser session unavailable")
        return self._page

    # -- balance ------------------------------------------------------------
    def get_balance(self) -> float:
        """Parse the account balance from the page (top bar) — best effort."""
        page = self._require_page()
        try:
            body = page.locator("body").inner_text(timeout=8000)
        except Exception as e:
            raise RuntimeError(f"could not read page: {e}")
        m = re.search(r"\$\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})", body)
        if not m:
            raise RuntimeError(
                "no balance found on page — you may not be logged in. "
                "Set headless: false for this book, start once and log in manually.")
        return float(m.group(1).replace(",", ""))

    # -- price safety ----------------------------------------------------------
    def _read_slip_price(self, page, outcome: str) -> Optional[float]:
        """Read the decimal price shown for `outcome` on the page.

        Best effort: looks at the LAST occurrence of the outcome name (the
        slip usually renders after the market grid) and takes the first
        plausible price token within ~80 characters (decimal '1.85' or
        American '+185'/'-150'). Returns None if nothing plausible is found.
        """
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            return None
        idx = body.lower().rfind(outcome.lower())
        if idx == -1:
            return None
        window = body[max(0, idx - 80): idx + 80]
        for token in re.findall(r"[+\-−]\d{3,5}\b|\b\d{1,2}\.\d{2}\b", window):
            p = _to_decimal(token)
            if p and 1.02 <= p <= 100.0:
                return p
        return None

    def _line_shown(self, page, outcome: str, point: float) -> bool:
        """Spreads/totals: the slip must show the exact line (e.g. 3.5 / +3.5)."""
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            return False
        idx = body.lower().rfind(outcome.lower())
        if idx == -1:
            return False
        window = body[max(0, idx - 80): idx + 120].replace("\u2212", "-").replace("\u00bd", ".5")
        num = f"{abs(point):g}"
        return re.search(rf"(?<![\d.]){re.escape(num)}(?![\d])", window) is not None

    def _drift_exceeded(self, slip_price: Optional[float], priced: float) -> bool:
        return slip_price is not None and abs(slip_price - priced) > self.max_price_drift

    def _check_slip(self, page, outcome: str, priced: float, stake: float,
                    stake_ref: str, point: Optional[float] = None) -> Optional[str]:
        """Return a failure reason, or None if the slip matches what was sized."""
        if point is not None and not self._line_shown(page, outcome, point):
            return f"line {point:g} not shown next to {outcome} in the slip"
        slip = self._read_slip_price(page, outcome)
        if self.require_price_check and slip is None:
            return "could not read slip price"
        if self._drift_exceeded(slip, priced):
            return (f"price moved: slip {slip:.3f} vs priced {priced:.3f} "
                    f"(drift limit {self.max_price_drift})")
        try:
            raw = page.locator(f'[data-arb-ref="{stake_ref}"]').first.input_value(timeout=3000)
            typed = float(re.sub(r"[^\d.]", "", raw) or "nan")
        except Exception as e:
            return f"could not read stake field: {e}"
        if not abs(typed - stake) < 0.005:
            return f"stake field reads {raw!r}, expected {stake:.2f}"
        return None

    # -- bet flow --------------------------------------------------------------
    def prepare_bet(self, opportunity, leg, stake: float):
        """Fill the slip via the AI agent. Returns PreparedBet or BetResult(False)."""
        from .agent import BetAgent  # lazy: anthropic only needed for browser books
        page = self._require_page()
        ai = self.ai or {}
        agent = BetAgent(
            page, book=self.name,
            allowed_domains=self.allowed_domains or (urlparse(self.home_url).hostname,),
            stake=stake,
            model=ai.get("model", "claude-opus-5-5"),
            effort=ai.get("effort", "low"),
            max_steps=int(ai.get("max_steps", 30)),
            timeout_sec=float(ai.get("timeout_sec", 150)),
            vision=bool(ai.get("vision", True)),
        )
        try:
            out = agent.run(event=opportunity.event, home=opportunity.home,
                            away=opportunity.away, commence=opportunity.commence_time,
                            outcome=leg.outcome, target_odds=leg.odds,
                            market=leg.market, point=leg.point,
                            start_url=leg.link or leg.event_link)
        except Exception as e:
            return BetResult(False, message=f"agent crashed: {type(e).__name__}: {e}")
        if not out.ready:
            return BetResult(False, message=out.message)
        problem = self._check_slip(page, leg.outcome, leg.odds, stake, out.stake_ref,
                                   point=leg.point)
        if problem:
            return BetResult(False, message=f"slip verification failed: {problem}")
        return PreparedBet(self, opportunity, leg, stake, out.stake_ref,
                           self._read_slip_price(page, leg.outcome),
                           notes=f"{out.message} ({out.steps} agent steps)")

    def commit(self, prepared: PreparedBet) -> BetResult:
        """Re-verify and click Place bet. Code-only — the model is not involved."""
        page = self._require_page()
        leg = prepared.leg
        try:
            problem = self._check_slip(page, leg.outcome, leg.odds,
                                       prepared.stake, prepared.stake_ref, point=leg.point)
            if problem:
                return BetResult(False, message=f"aborted at placement: {problem}")
            btn = (page.locator("button, [role='button']")
                     .filter(has_text=re.compile(self.place_bet_text, re.I)).last)
            btn.click(timeout=self.action_timeout_ms)
            page.wait_for_timeout(4000)
            low = page.locator("body").inner_text(timeout=5000).lower()
            if any(t in low for t in self.error_texts):
                return BetResult(False, message="bookie reported an error (see browser)")
            if any(t in low for t in self.success_texts):
                return BetResult(True, message="success confirmation detected")
            return BetResult(False,
                             message="no confirmation seen — check the book's bet slip manually")
        except Exception as e:
            return BetResult(False, message=f"{type(e).__name__}: {e}")

    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        """One-shot prepare + commit (no approval). The bot normally uses the
        two phases separately; this exists to satisfy the adapter interface."""
        prepared = self.prepare_bet(opportunity, leg, stake)
        if isinstance(prepared, BetResult):
            return prepared
        return self.commit(prepared)
