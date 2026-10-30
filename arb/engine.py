"""Arbitrage detection and stake sizing.

An arbitrage in an n-way market exists when you can cover every outcome on
(different) bookmakers at a combined implied probability < 100%. Stakes are
proportional so that whichever outcome happens, the payout is identical:
stake_i = total * (1/odd_i) / sum(1/odd_j).

Markets:
  * ``h2h``     — moneyline (2-way, or 3-way 1X2 for soccer)
  * ``spreads`` — Team A at -p vs Team B at +p (same line on both books)
  * ``totals``  — Over p vs Under p (same line on both books)
Only exactly matching lines are paired; a push refunds both legs, so it
can't lose money.

Exchange fees (Kalshi, Polymarket US) are charged per contract as
``k * p * (1 - p)`` on a contract costing ``p``; the engine prices legs on
the fee-inclusive cost (``exchange_fees``). Liquidity (``bet_limit`` in the
feed, in contracts) caps how large the whole arb may be.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Leg:
    outcome: str
    bookie: str
    odds: float              # quoted decimal price (what orders are priced on)
    stake: float = 0.0
    market: str = "h2h"
    point: Optional[float] = None
    eff_odds: float = 0.0    # fee-inclusive decimal price (what sizing uses)
    sid: str = ""            # book's own outcome id (Kalshi ticker, Polymarket side id, ...)
    link: str = ""           # outcome add-to-slip link, if the feed has one
    event_link: str = ""     # book's event page

    @property
    def label(self) -> str:
        if self.point is None:
            return self.outcome
        return f"{self.outcome} {self.point:+g}" if self.market == "spreads" \
            else f"{self.outcome} {self.point:g}"


@dataclass
class Opportunity:
    event_id: str
    sport: str
    event: str
    home: str
    away: str
    commence_time: str
    legs: List[Leg]
    roi_pct: float
    total_stake: float
    net_profit: float
    bookies: List[str] = field(default_factory=list)
    market: str = "h2h"

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "sport": self.sport,
            "event": self.event,
            "home": self.home,
            "away": self.away,
            "commence_time": self.commence_time,
            "market": self.market,
            "roi_pct": self.roi_pct,
            "total_stake": round(self.total_stake, 2),
            "net_profit": round(self.net_profit, 2),
            "bookies": self.bookies,
            "legs": [
                {"outcome": l.label, "bookie": l.bookie,
                 "odds": l.odds, "stake": round(l.stake, 2)}
                for l in self.legs
            ],
        }


def find_arb(events, cfg) -> List[Opportunity]:
    """Scan odds events; return opportunities sorted by best net ROI."""
    opps = []
    for ev in events or []:
        opps.extend(_arbs_for_event(ev, cfg))
    return sorted(opps, key=lambda o: o.roi_pct, reverse=True)


def _normalize_outcomes(outcomes, odds=None):
    """Return [outcome-dict, ...] for either API shape.

    Live v4: outcomes is a list of {"name", "price", "point"?, "sid"?, ...}.
    Also accept the legacy parallel-lists shape used in offline fixtures:
    outcomes=[names], odds=[prices].
    """
    if outcomes and isinstance(outcomes[0], dict):
        return [dict(o, price=o.get("price") or 0.0) for o in outcomes]
    if outcomes and odds and len(outcomes) == len(odds):
        return [{"name": n, "price": p or 0.0} for n, p in zip(outcomes, odds)]
    return []


def _started(commence: str) -> bool:
    try:
        t = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return t <= datetime.now(timezone.utc)


def _line(market: str, o: dict, home: str):
    """Group key so only matching lines are compared.

    spreads: the home team's handicap (away at +p belongs to home at -p)
    totals:  the total
    h2h:     a single group
    """
    if market == "h2h":
        return None
    p = o.get("point")
    if p is None:
        return False  # malformed — skipped
    if market == "spreads":
        return round(p if o.get("name") == home else -p, 2)
    return round(p, 2)


def _effective(price: float, bookie: str, cfg) -> float:
    """Fee-inclusive decimal price. Exchanges charge k*p*(1-p) per contract."""
    k = (getattr(cfg, "exchange_fees", None) or {}).get(bookie)
    if not k:
        return price
    p = 1.0 / price
    return 1.0 / (p + k * p * (1.0 - p))


def _arbs_for_event(ev, cfg) -> List[Opportunity]:
    # in-play prices move too fast (and are often stale in the feed)
    if _started(ev.get("commence_time", "")):
        return []
    allowed = set(getattr(cfg, "bookmakers", None) or [])
    home = ev.get("home_team", "")

    # (market, line) -> {"best": {name: (eff, quote, bookie, outcome, bm, m)}, "all": set}
    groups = {}
    for bm in ev.get("bookmakers", []):
        if allowed and bm.get("key") not in allowed:
            continue
        for m in bm.get("markets", []):
            mkey = m.get("key")
            if mkey not in ("h2h", "spreads", "totals"):
                continue
            for o in _normalize_outcomes(m.get("outcomes", []), m.get("odds", [])):
                line = _line(mkey, o, home)
                if line is False:
                    continue
                g = groups.setdefault((mkey, line), {"best": {}, "all": set()})
                g["all"].add(o["name"])
                price = o["price"]
                if not price or price <= 1.0:
                    continue
                eff = _effective(price, bm["key"], cfg)
                cur = g["best"].get(o["name"])
                if cur is None or eff > cur[0]:
                    g["best"][o["name"]] = (eff, price, bm["key"], o, bm, m)

    opps = []
    for (mkey, line), g in groups.items():
        opp = _size(ev, cfg, mkey, g["best"], g["all"])
        if opp:
            opps.append(opp)
    return opps


def _size(ev, cfg, market, best, all_outcomes) -> Optional[Opportunity]:
    # every outcome of the market must be covered, or it's not an arb
    # (e.g. a soccer 1X2 with the heavy favourite missing looks like +400%)
    if not (2 <= len(best) <= 3) or set(best) != all_outcomes:
        return None
    if market != "h2h" and len(best) != 2:
        return None

    sum_inv = sum(1.0 / v[0] for v in best.values())
    if sum_inv >= 1.0:
        return None  # not an arbitrage

    bookies = {v[2] for v in best.values()}
    if len(bookies) < len(best):
        return None  # two legs on the same book — no real arb

    # worst-case commission on the winning leg (e.g. betfair exchange 5%)
    comm = max(cfg.commissions.get(b, 0.0) for b in bookies)
    roi = (1.0 / sum_inv - 1.0) * (1.0 - comm) * 100.0
    if roi < cfg.min_roi_pct:
        return None

    # size so every leg pays the same; if any leg would exceed the per-bet cap
    # or the book's available liquidity, shrink the WHOLE arb (capping one leg
    # alone breaks the hedge)
    in_contracts = set(getattr(cfg, "liquidity_in_contracts", None) or [])
    total = cfg.bankroll
    for eff, price, bookie, o, _, _ in best.values():
        share = (1.0 / eff) / sum_inv
        cap = cfg.max_stake_per_bet
        limit = o.get("bet_limit")
        if limit is not None and bookie in in_contracts:
            cap = min(cap, float(limit) / eff)   # contracts × fee-inclusive cost
        if total * share > cap:
            total = cap / share

    legs = []
    for name, (eff, price, bookie, o, bm, m) in best.items():
        stake = math.floor(total * (1.0 / eff) / sum_inv * 100) / 100
        if stake < cfg.min_stake_per_bet:
            return None
        legs.append(Leg(outcome=name, bookie=bookie, odds=price, stake=stake,
                        market=market, point=o.get("point"), eff_odds=eff,
                        sid=str(o.get("sid") or ""), link=o.get("link") or "",
                        event_link=bm.get("link") or m.get("link") or ""))

    total_stake = round(sum(l.stake for l in legs), 2)
    payout = min(l.stake * l.eff_odds for l in legs)
    gross = payout - total_stake
    return Opportunity(
        event_id=ev.get("id", ""),
        sport=ev.get("sport_key", ""),
        event=f"{ev.get('home_team', '?')} vs {ev.get('away_team', '?')}",
        home=ev.get("home_team", ""),
        away=ev.get("away_team", ""),
        commence_time=ev.get("commence_time", ""),
        legs=legs,
        roi_pct=round(roi, 3),
        total_stake=total_stake,
        net_profit=round(gross * (1.0 - comm), 2),
        bookies=sorted(bookies),
        market=market,
    )
