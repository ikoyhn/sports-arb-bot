"""Kalshi adapter (official Trade API v2 — no browser, no AI).

Each game outcome is its own binary market; the Odds API outcome ``sid`` is
that market's ticker (e.g. ``KXNBAGAME-26OCT29CLEATL-ATL``). A leg is bought
as YES on that ticker with a **fill-or-kill limit** at the worst price the
arb tolerates, so it fills completely at that price or better, or not at all.

Config::

    kalshi:
      key_id: "..."                    # API key id
      private_key_path: "kalshi.pem"   # RSA or Ed25519 private key (PEM)
      demo: false                      # true = demo exchange

Fees are charged per contract as ``0.07 * p * (1 - p)`` (see
``exchange_fees`` in config.yaml); the engine already sized the stake on the
fee-inclusive cost, so the contract count here = stake / fee-inclusive cost.
"""
import base64
import math
import time
import uuid

import requests

from ..bookie import BetResult, BookieAdapter

PROD = "https://external-api.kalshi.com/trade-api/v2"
DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiAdapter(BookieAdapter):
    name = "kalshi"
    max_price_drift: float = 0.02   # bot injects cfg.max_price_drift
    fee_rate: float = 0.07          # bot injects cfg.exchange_fees["kalshi"]

    def __init__(self, creds: dict):
        super().__init__(creds)
        self.base = creds.get("base_url") or (DEMO if creds.get("demo") else PROD)
        self._key = None

    # -- auth ----------------------------------------------------------------
    def _load_key(self):
        from cryptography.hazmat.primitives import serialization
        with open(self.creds["private_key_path"], "rb") as f:
            self._key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding
        ts = str(int(time.time() * 1000))
        # signed path includes the /trade-api/v2 prefix, never the query string
        full = "/" + self.base.split("://", 1)[1].split("/", 1)[1] + path.split("?")[0]
        msg = (ts + method.upper() + full).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH),
                                 hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.creds["key_id"],
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def _req(self, method, path, **kw):
        r = requests.request(method, self.base + path, headers=self._sign(method, path),
                             timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def authenticate(self) -> bool:
        self._load_key()
        self.get_balance()
        return True

    def get_balance(self) -> float:
        return self._req("GET", "/portfolio/balance")["balance"] / 100.0

    def supports(self, leg) -> bool:
        # outcome tickers are only mapped for moneylines
        return leg.market == "h2h" and leg.sid.count("-") >= 2

    # -- orders ------------------------------------------------------------------
    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        if not self.supports(leg):
            return BetResult(False, message=f"no Kalshi market ticker for {leg.label}")
        # worst YES price we accept: the quoted price minus the allowed drift
        worst_decimal = max(1.01, leg.odds - self.max_price_drift)
        max_price = math.floor(100.0 / worst_decimal) / 100.0      # cents, rounded down
        if not 0.01 <= max_price <= 0.99:
            return BetResult(False, message=f"price {max_price} out of range")
        try:
            m = self._req("GET", f"/markets/{leg.sid}")["market"]
            if m.get("status") != "active":
                return BetResult(False, message=f"market {leg.sid} is {m.get('status')}")
            ask = float(m.get("yes_ask_dollars") or 0)
            if not 0 < ask <= max_price:
                return BetResult(False, message=f"price moved: ask {ask:.2f} > limit {max_price:.2f}")
            # size on the limit price so a fill anywhere up to it stays within stake
            per_contract = max_price + self.fee_rate * max_price * (1 - max_price)
            count = math.floor(stake / per_contract * 100) / 100
            if count < 1:
                return BetResult(False, message="stake buys less than 1 contract")
            res = self._req("POST", "/portfolio/events/orders", json={
                "ticker": leg.sid,
                "side": "bid",                       # buy YES
                "count": f"{count:.2f}",
                "price": f"{max_price:.4f}",         # limit; fills at ask or better
                "time_in_force": "fill_or_kill",
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4()),
            })
        except Exception as e:
            return BetResult(False, message=f"{type(e).__name__}: {e}")
        filled = float(res.get("fill_count") or 0)
        if filled <= 0:
            return BetResult(False, ref=res.get("order_id", ""), message="not filled (FOK)")
        return BetResult(True, ref=res.get("order_id", ""),
                         message=f"filled {filled:g} @ {res.get('average_fill_price')}")
