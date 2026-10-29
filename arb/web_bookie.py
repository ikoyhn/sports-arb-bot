"""Shared Playwright-based adapter for books that have no public betting API
(DraftKings, FanDuel, BetMGM, BetRivers, ...).

Safety
------
* **Price check:** after the selection is in the slip, the slip price is read
  and compared to the price the arb was sized on. If it has moved by more
  than ``max_price_drift`` (decimal, e.g. 0.02) the bet is ABORTED before the
  stake is even typed. The price is re-read immediately before the final
  click, so the check covers the whole slip-to-place window.

Anti-detection hygiene
----------------------
* Persistent Chromium profile per book → stable cookies/device fingerprint
* Real desktop user agent; fixed locale + timezone (set them to match your
  account's state/region — geo mismatch is a common flag)
* ``navigator.webdriver`` masked; chrome runtime / plugins / languages shims
* ``--disable-blink-features=AutomationControlled``
* Human-ish pacing: randomised delays between steps, character-by-character
  typing instead of instant value fill

Risks
-----
This still violates most books' ToS; CAPTCHAs, device checks and account
limiting can still happen. Keep dry_run on until the flow is verified.
"""
import random
import re
import time
from pathlib import Path
from typing import Optional

from .bookie import BetResult, BookieAdapter

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _to_decimal(token: str) -> Optional[float]:
    """'1.85' → 1.85 ; '+185' → 2.85 ; '-150' → 1.667 (American odds)."""
    try:
        if token.startswith("+"):
            return 1.0 + int(token[1:]) / 100.0
        if token.startswith("-"):
            return 1.0 + 100.0 / abs(int(token[1:]))
        v = float(token)
        return v if v > 1.01 else None
    except (ValueError, ZeroDivisionError):
        return None


def _human_sleep(lo=0.5, hi=1.6):
    """Randomised pause instead of a fixed, robotic sleep."""
    time.sleep(random.uniform(lo, hi))


class WebBookieAdapter(BookieAdapter):
    # Subclasses override these per book:
    home_url: str = ""
    login_url: str = ""
    place_bet_text: str = "place bet"          # button text, case-insensitive
    success_texts: tuple = ("success", "placed", "confirmed", "accepted")
    error_texts: tuple = ("insufficient", "invalid", "not available",
                          "unable to", "bet has not", "error")
    nav_timeout_ms = 25_000
    action_timeout_ms = 15_000

    # price safety (bot injects cfg.max_price_drift)
    max_price_drift: float = 0.02     # max allowed slip-price movement (decimal)
    require_price_check: bool = True  # abort if slip price can't be read

    def __init__(self, creds: dict):
        super().__init__(creds)
        self._pw = None
        self._page: Optional[object] = None

    # -- lifecycle --------------------------------------------------------
    def authenticate(self) -> bool:
        from playwright.sync_api import sync_playwright  # lazy import
        self._pw = sync_playwright().start()
        profile = Path(self.creds.get("profile_dir", f".profiles/{self.name}")).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        ctx = self._pw.chromium.launch_persistent_context(
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
        ctx.add_init_script(STEALTH_JS)
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)
        try:
            self._page.goto(self.login_url or self.home_url, timeout=self.nav_timeout_ms)
            _human_sleep(1.0, 2.5)
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
        """Read the decimal price shown for `outcome` in the bet slip.

        Best effort: looks for the first plausible price token within ~80
        characters of the outcome name (handles decimal '1.85' and American
        '+185'/'-150' displays). Returns None if nothing plausible is found.
        """
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            return None
        idx = body.lower().find(outcome.lower())
        if idx == -1:
            return None
        window = body[max(0, idx - 80): idx + 80]
        for token in re.findall(r"[+-]?\d{2,6}(?:\.\d{1,2})?", window):
            p = _to_decimal(token)
            if p and 1.02 <= p <= 100.0:
                return p
        return None

    def _drift_exceeded(self, slip_price: Optional[float], priced: float) -> bool:
        return slip_price is not None and abs(slip_price - priced) > self.max_price_drift

    # -- bet flow --------------------------------------------------------------
    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        page = self._require_page()
        try:
            self._open_event(page, opportunity.home, opportunity.away)
            self._click_selection(page, leg.outcome)

            # price check BEFORE committing the stake
            slip = self._read_slip_price(page, leg.outcome)
            if self.require_price_check and slip is None:
                return BetResult(False, "could not read slip price — aborted for safety")
            if self._drift_exceeded(slip, leg.odds):
                return BetResult(False,
                                 f"price moved: slip {slip:.3f} vs priced {leg.odds:.3f} "
                                 f"(drift limit {self.max_price_drift})")

            self._set_stake(page, stake)
            return self._confirm_bet(page, leg.outcome, leg.odds)
        except Exception as e:
            return BetResult(False, message=f"{type(e).__name__}: {e}")

    def _open_event(self, page, home: str, away: str):
        page.goto(self.home_url, timeout=self.nav_timeout_ms)
        _human_sleep(1.5, 3.0)
        # 1) try the site search box if there is one
        try:
            search = page.locator('input[type="search"], input[placeholder*="earch" i]').first
            search.click(timeout=8000)
            search.fill(f"{home} {away}")
            search.press("Enter")
            _human_sleep(2.0, 4.0)
        except Exception:
            pass
        # 2) click the first link/button mentioning both teams (either order)
        pat = re.compile(
            rf"{re.escape(home)}.*{re.escape(away)}|{re.escape(away)}.*{re.escape(home)}", re.I)
        link = page.locator("a, button, [role='link'], [role='button']").filter(has_text=pat).first
        link.click(timeout=self.action_timeout_ms)
        _human_sleep(1.5, 3.0)

    def _click_selection(self, page, outcome: str):
        # odds buttons on these books show the outcome (team) name
        btn = (page.locator("button, [role='button']")
                 .filter(has_text=re.compile(rf"^\s*{re.escape(outcome)}\s*$", re.I)).first)
        btn.click(timeout=self.action_timeout_ms)
        _human_sleep(1.0, 2.0)

    def _set_stake(self, page, stake: float):
        field = page.locator('input[placeholder*="amount" i], input[placeholder*="stake" i], '
                             'input[type="number"]').first
        field.click(timeout=self.action_timeout_ms)
        _human_sleep(0.3, 0.9)
        # character-by-character typing looks far less like automation
        field.press_sequentially(f"{stake:.2f}", delay=random.randint(40, 120))
        _human_sleep(0.5, 1.2)

    def _confirm_bet(self, page, outcome: str, priced: float) -> BetResult:
        # re-check price at the moment of commitment — the window that matters
        slip = self._read_slip_price(page, outcome)
        if self.require_price_check and slip is None:
            return BetResult(False, "could not re-read slip price before placing — aborted")
        if self._drift_exceeded(slip, priced):
            return BetResult(False,
                             f"price moved at confirmation: slip {slip:.3f} vs {priced:.3f} — aborted")
        btn = (page.locator("button")
                 .filter(has_text=re.compile(self.place_bet_text, re.I)).first)
        btn.click(timeout=self.action_timeout_ms)
        _human_sleep(3.0, 5.0)
        low = page.locator("body").inner_text(timeout=5000).lower()
        if any(t in low for t in self.error_texts):
            return BetResult(False, message="bookie reported an error (see browser)")
        if any(t in low for t in self.success_texts):
            return BetResult(success=True, message="success confirmation detected")
        return BetResult(success=False,
                         message="no confirmation seen — check the book's bet slip manually")
