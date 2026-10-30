"""Polymarket US adapter (official API — no browser, no AI).

A game's moneyline (and each spread/total line) is ONE market with a long and
a short side. The Odds API outcome ``sid`` is the side id and the bookmaker
``link`` is the event page (``https://polymarket.us/event/<slug>``), so a leg
is resolved by loading the event (public gateway) and finding the side.

Orders are **fill-or-kill limits**. Polymarket US quotes every price as the
long (YES) price: buying the short side at no more than $0.40 is sent as
``ORDER_INTENT_BUY_SHORT`` at a long price of $0.60.

Config::

    polymarket_us:
      key_id: "..."          # API key id (UUID)
      secret_key: "..."      # base64 secret from the developer portal

Fees: ``feeCoefficient * p * (1 - p)`` per contract (0.0695 on sports at the
time of writing — set ``exchange_fees.polymarket_us`` to match).
"""
import base64
import math
import time
from urllib.parse import urlparse

import requests

from ..bookie import BetResult, BookieAdapter

API = "https://api.polymarket.us"
GATEWAY = "https://gateway.polymarket.us"


class PolymarketUSAdapter(BookieAdapter):
    name = "polymarket_us"
    max_price_drift: float = 0.02   # bot injects cfg.max_price_drift
    fee_rate: float = 0.0695        # bot injects cfg.exchange_fees["polymarket_us"]

    def __init__(self, creds: dict):
        super().__init__(creds)
        self._key = None

    def _sign(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self._key.sign(f"{ts}{method.upper()}{path.split('?')[0]}".encode())
        return {"X-PM-Access-Key": self.creds["key_id"], "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode(),
                "Content-Type": "application/json"}

    def _req(self, method, path, **kw):
        r = requests.request(method, API + path, headers=self._sign(method, path),
                             timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Polymarket {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def authenticate(self) -> bool:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        raw = base64.b64decode(self.creds["secret_key"])[:32]
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
        self.get_balance()
        return True

    def get_balance(self) -> float:
        bals = self._req("GET", "/v1/account/balances").get("balances", [])
        usd = [b for b in bals if b.get("currency", "USD") == "USD"]
        return float(usd[0]["buyingPower"]) if usd else 0.0

    def supports(self, leg) -> bool:
        return bool(leg.sid) and "/event/" in (leg.event_link or "")

    def _resolve(self, leg):
        """→ (market dict, side dict) for the leg's side id."""
        slug = urlparse(leg.event_link).path.rstrip("/").split("/")[-1]
        r = requests.get(f"{GATEWAY}/v1/events/slug/{slug}", timeout=15)
        r.raise_for_status()
        for m in r.json()["event"].get("markets", []):
            for s in m.get("marketSides", []):
                if str(s.get("id")) == leg.sid:
                    return m, s
        raise LookupError(f"side {leg.sid} not found in event {slug}")

    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        if not self.supports(leg):
            return BetResult(False, message=f"no Polymarket side id/event for {leg.label}")
        worst_decimal = max(1.01, leg.odds - self.max_price_drift)
        try:
            m, side = self._resolve(leg)
            if m.get("closed") or m.get("status") not in (None, "MARKET_STATUS_OPEN"):
                return BetResult(False, message=f"market {m.get('slug')} is {m.get('status')}")
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            # worst price for THIS side, rounded to tick in our favour
            max_cost = math.floor((1.0 / worst_decimal) / tick) * tick
            if not tick <= max_cost <= 1 - tick:
                return BetResult(False, message=f"price {max_cost} out of range")
            long_side = bool(side.get("long"))
            long_price = max_cost if long_side else 1.0 - max_cost
            per_contract = max_cost + self.fee_rate * max_cost * (1 - max_cost)
            min_qty = float(m.get("minimumTradeQty") or 1)
            qty = math.floor(stake / per_contract / min_qty) * min_qty
            if qty < min_qty:
                return BetResult(False, message="stake below minimum trade size")
            res = self._req("POST", "/v1/orders", json={
                "marketSlug": m["slug"],
                "intent": "ORDER_INTENT_BUY_LONG" if long_side else "ORDER_INTENT_BUY_SHORT",
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{long_price:.4f}", "currency": "USD"},
                "quantity": round(qty, 4),
                "tif": "TIME_IN_FORCE_FILL_OR_KILL",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "synchronousExecution": True,
                "maxBlockTime": "5",
            })
        except Exception as e:
            return BetResult(False, message=f"{type(e).__name__}: {e}")
        execs = res.get("executions") or []
        rejected = [e.get("orderRejectReason") for e in execs if e.get("orderRejectReason")]
        filled = sum(float(e.get("lastShares") or 0) for e in execs)
        if rejected or filled <= 0:
            return BetResult(False, ref=res.get("id", ""),
                             message=f"not filled: {rejected or 'FOK killed'}")
        return BetResult(True, ref=res.get("id", ""), message=f"filled {filled:g} contracts")
