"""Arbitrage detection and stake sizing.

An arbitrage in an n-way moneyline market exists when you can cover every
outcome on (different) bookmakers at a combined implied probability < 100%.
Stakes are proportional so that whichever outcome happens, the payout is
identical: stake_i = total * (1/odd_i) / sum(1/odd_j).
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Leg:
    outcome: str
    bookie: str
    odds: float
    stake: float = 0.0


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

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "sport": self.sport,
            "event": self.event,
            "home": self.home,
            "away": self.away,
            "commence_time": self.commence_time,
            "roi_pct": self.roi_pct,
            "total_stake": round(self.total_stake, 2),
            "net_profit": round(self.net_profit, 2),
            "bookies": self.bookies,
            "legs": [
                {"outcome": l.outcome, "bookie": l.bookie,
                 "odds": l.odds, "stake": round(l.stake, 2)}
                for l in self.legs
            ],
        }


def find_arb(events, cfg) -> List[Opportunity]:
    """Scan odds events; return opportunities sorted by best net ROI."""
    opps = []
    for ev in events or []:
        opp = _arb_for_event(ev, cfg)
        if opp:
            opps.append(opp)
    return sorted(opps, key=lambda o: o.roi_pct, reverse=True)


def _arb_for_event(ev, cfg) -> Optional[Opportunity]:
    # best decimal odds per outcome, across bookmakers
    best = {}  # outcome -> (odds, bookie)
    for bm in ev.get("bookmakers", []):
        for m in bm.get("markets", []):
            if m.get("key") != "h2h":
                continue
            outcomes = m.get("outcomes", [])
            odds = m.get("odds", [])
            if not outcomes or len(outcomes) != len(odds):
                continue
            for outcome, o in zip(outcomes, odds):
                if not o or o < 1.2:
                    continue
                cur = best.get(outcome)
                if cur is None or o > cur[0]:
                    best[outcome] = (o, bm["key"])

    if not (2 <= len(best) <= 3):
        return None

    sum_inv = sum(1.0 / o for o, _ in best.values())
    if sum_inv >= 1.0:
        return None  # not an arbitrage

    bookies = {b for _, b in best.values()}
    if len(bookies) < len(best):
        return None  # two legs on the same book — no real arb

    # worst-case commission on the winning leg (e.g. betfair exchange 5%)
    comm = max(cfg.commissions.get(b, 0.0) for b in bookies)
    roi = (1.0 / sum_inv - 1.0) * (1.0 - comm) * 100.0
    if roi < cfg.min_roi_pct:
        return None

    n = len(best)
    total = min(cfg.bankroll, cfg.max_stake_per_bet * n)
    legs = []
    for outcome, (o, b) in best.items():
        stake = min(total * (1.0 / o) / sum_inv, cfg.max_stake_per_bet)
        if stake < cfg.min_stake_per_bet:
            return None
        legs.append(Leg(outcome=outcome, bookie=b, odds=o, stake=round(stake, 2)))

    total_stake = round(sum(l.stake for l in legs), 2)
    gross = total_stake / sum_inv - total_stake
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
    )
