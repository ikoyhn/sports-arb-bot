"""Offline tests: spreads/totals, exchange fees + liquidity, the credit-aware
planner, and the Kalshi / Polymarket US adapters (HTTP is faked)."""
import base64
import json
from types import SimpleNamespace as NS

import pytest

from arb.bookies.kalshi import KalshiAdapter
from arb.bookies.polymarket_us import PolymarketUSAdapter
from arb.bot import ArbBot
from arb.bookie import BetResult
from arb.config import Config
from arb.engine import Leg, Opportunity, find_arb
from arb.planner import PollPlanner, seconds_until_reset


def _cfg(**over):
    base = {"min_roi_pct": 0.5, "bankroll": 1000.0, "max_stake_per_bet": 250.0,
            "min_stake_per_bet": 2.0, "commissions": {}, "exchange_fees": {}}
    base.update(over)
    return Config(base)


def _ev(books, home="A", away="B"):
    """books: [(key, market, [outcome dicts])]"""
    bms = {}
    for key, market, outcomes in books:
        bms.setdefault(key, {"key": key, "markets": []})["markets"].append(
            {"key": market, "outcomes": outcomes})
    return {"id": "e", "sport_key": "nba", "home_team": home, "away_team": away,
            "commence_time": "2099-01-01T00:00:00Z", "bookmakers": list(bms.values())}


# -- spreads & totals ---------------------------------------------------------
def test_spread_arb_needs_matching_lines():
    ev = _ev([("dk", "spreads", [{"name": "A", "price": 2.10, "point": -3.5},
                                 {"name": "B", "price": 1.75, "point": 3.5}]),
              ("fd", "spreads", [{"name": "A", "price": 1.75, "point": -3.5},
                                 {"name": "B", "price": 2.10, "point": 3.5}]),
              # different line on a third book must not pair with -3.5
              ("mgm", "spreads", [{"name": "B", "price": 3.0, "point": 2.5},
                                  {"name": "A", "price": 1.2, "point": -2.5}])])
    (opp,) = find_arb([ev], _cfg())
    assert opp.market == "spreads"
    assert {(l.bookie, l.label) for l in opp.legs} == {("dk", "A -3.5"), ("fd", "B +3.5")}


def test_totals_arb_over_under_same_point():
    ev = _ev([("dk", "totals", [{"name": "Over", "price": 2.08, "point": 215.5},
                                {"name": "Under", "price": 1.8, "point": 215.5}]),
              ("fd", "totals", [{"name": "Over", "price": 1.8, "point": 215.5},
                                {"name": "Under", "price": 2.08, "point": 215.5}])])
    (opp,) = find_arb([ev], _cfg())
    assert sorted(l.label for l in opp.legs) == ["Over 215.5", "Under 215.5"]


# -- exchange fees & liquidity --------------------------------------------------
def test_exchange_fee_can_kill_a_thin_arb():
    ev = _ev([("kalshi", "h2h", [{"name": "A", "price": 2.04}]),
              ("dk", "h2h", [{"name": "B", "price": 2.0}])])
    assert find_arb([ev], _cfg())                                        # 1% gross
    assert find_arb([ev], _cfg(exchange_fees={"kalshi": 0.07})) == []    # fee eats it


def test_liquidity_shrinks_the_whole_arb():
    ev = _ev([("kalshi", "h2h", [{"name": "A", "price": 2.2, "bet_limit": 40}]),
              ("dk", "h2h", [{"name": "B", "price": 2.2}])])
    (opp,) = find_arb([ev], _cfg(liquidity_in_contracts=["kalshi"]))
    by = {l.bookie: l for l in opp.legs}
    assert by["kalshi"].stake <= 40 / 2.2 + 0.01           # 40 contracts at ~$0.45
    assert abs(by["kalshi"].stake - by["dk"].stake) < 0.02  # still hedged


def test_sids_and_links_flow_into_legs():
    ev = _ev([("kalshi", "h2h", [{"name": "A", "price": 2.2, "sid": "KX-1-A"}]),
              ("dk", "h2h", [{"name": "B", "price": 2.2, "link": "https://dk/?o=1"}])])
    by = {l.bookie: l for l in find_arb([ev], _cfg())[0].legs}
    assert by["kalshi"].sid == "KX-1-A" and by["dk"].link == "https://dk/?o=1"


# -- planner ------------------------------------------------------------------------
class FakeOdds:
    def __init__(self, remaining, sports, events):
        self.credits_remaining = remaining
        self._sports, self._events = sports, events

    def list_sports(self):
        return [{"key": k, "has_outrights": False} for k in self._sports]

    def list_events(self, sport):
        return self._events.get(sport, [])


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def test_planner_wildcards_schedule_and_budget():
    now = 1_900_000_000.0
    odds = FakeOdds(1000, ["tennis_atp_a", "tennis_wta_b", "soccer_epl", "basketball_nba"], {
        "tennis_atp_a": [{"commence_time": _iso(now + 1800)}],      # 30 min → fast
        "tennis_wta_b": [{"commence_time": _iso(now + 3 * 86400)}],  # beyond lookahead
        "basketball_nba": [{"commence_time": _iso(now + 4 * 3600)}],  # mid
    })
    pl = PollPlanner(odds, ["tennis_*", "basketball_nba"], cost_per_sport=3)
    assert pl.due(now) == ["tennis_atp_a", "basketball_nba"]   # soonest first; WTA skipped
    assert pl.interval("tennis_atp_a", now) == 60
    assert pl.interval("basketball_nba", now) == 300
    assert pl.interval("tennis_wta_b", now) is None
    for s in ("tennis_atp_a", "basketball_nba"):
        pl.polled(s, now)
    assert pl.due(now + 10) == []                               # not due yet


def test_planner_never_spends_past_budget():
    now = 1_900_000_000.0
    odds = FakeOdds(12, ["s1", "s2", "s3"],
                    {s: [{"commence_time": _iso(now + 600)}] for s in ("s1", "s2", "s3")})
    pl = PollPlanner(odds, ["s1", "s2", "s3"], cost_per_sport=3,
                     settings={"credit_reserve": 10})
    assert pl.due(now) == []          # only 2 spendable credits, a poll costs 3


def test_seconds_until_reset_rolls_month():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp()
    assert round(seconds_until_reset(1, t) / 86400) == 3


# -- exchange adapters (HTTP faked) ----------------------------------------------------
class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def _kalshi(monkeypatch, ask, fill="10.00"):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    a = KalshiAdapter({"key_id": "k"})
    a._key = ed25519.Ed25519PrivateKey.generate()
    sent = []

    def fake(method, url, headers=None, json=None, **kw):
        sent.append((method, url, json, headers))
        if url.endswith("/portfolio/events/orders"):
            return FakeResp({"order_id": "o1", "fill_count": fill, "remaining_count": "0",
                             "average_fill_price": "0.45"})
        return FakeResp({"market": {"status": "active", "yes_ask_dollars": ask}})
    monkeypatch.setattr("arb.bookies.kalshi.requests.request", fake)
    return a, sent


LEG = Leg("A", "kalshi", 2.2, 50.0, sid="KXNBAGAME-26OCT29CLEATL-ATL")


def test_kalshi_places_fok_limit_and_signs_full_path(monkeypatch):
    a, sent = _kalshi(monkeypatch, "0.4500")
    res = a.place_bet(None, LEG, 50.0)
    assert res.success and res.ref == "o1"
    method, url, body, headers = sent[-1]
    assert body["time_in_force"] == "fill_or_kill" and body["side"] == "bid"
    # limit = worst acceptable price for 2.20 - 0.02 drift → floor(100/2.18)/100
    assert body["price"] == "0.4500"
    assert float(body["count"]) * (0.45 + 0.07 * 0.45 * 0.55) <= 50.0
    assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                            "KALSHI-ACCESS-SIGNATURE"}


def test_kalshi_aborts_when_price_moved(monkeypatch):
    a, sent = _kalshi(monkeypatch, "0.5000")
    res = a.place_bet(None, LEG, 50.0)
    assert not res.success and "price moved" in res.message
    assert not any(u.endswith("/orders") for _, u, _, _ in sent)


def test_kalshi_unfilled_fok_is_a_failure(monkeypatch):
    a, _ = _kalshi(monkeypatch, "0.4500", fill="0")
    assert not a.place_bet(None, LEG, 50.0).success


def _poly(monkeypatch, long_side):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    a = PolymarketUSAdapter({"key_id": "k", "secret_key": base64.b64encode(b"x" * 32).decode()})
    a._key = ed25519.Ed25519PrivateKey.generate()
    event = {"event": {"markets": [{"slug": "aec-nba-cle-atl", "status": "MARKET_STATUS_OPEN",
                                    "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01,
                                    "marketSides": [{"id": "111", "long": long_side},
                                                    {"id": "222", "long": not long_side}]}]}}
    sent = []
    monkeypatch.setattr("arb.bookies.polymarket_us.requests.get",
                        lambda url, timeout=None: FakeResp(event))

    def fake(method, url, headers=None, json=None, **kw):
        sent.append(json)
        return FakeResp({"id": "p1", "executions": [{"lastShares": "10", "lastPx": "0.45"}]})
    monkeypatch.setattr("arb.bookies.polymarket_us.requests.request", fake)
    return a, sent


PLEG = Leg("Hawks", "polymarket_us", 2.2, 50.0, sid="111",
           event_link="https://polymarket.us/event/nba-cle-atl-2026-10-29")


@pytest.mark.parametrize("long_side,intent,price", [
    (True, "ORDER_INTENT_BUY_LONG", "0.4500"),
    (False, "ORDER_INTENT_BUY_SHORT", "0.5500"),   # short side quoted as the long price
])
def test_polymarket_intent_and_long_price(monkeypatch, long_side, intent, price):
    a, sent = _poly(monkeypatch, long_side)
    res = a.place_bet(None, PLEG, 50.0)
    assert res.success
    body = sent[-1]
    assert body["intent"] == intent and body["price"]["value"] == price
    assert body["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert body["marketSlug"] == "aec-nba-cle-atl"


# -- bot: exchange legs first, never re-place ---------------------------------------
class FakeAPI:
    def __init__(self, name, ok=True):
        self.name, self.ok, self.calls = name, ok, 0

    def supports(self, leg):
        return True

    def place_bet(self, opp, leg, stake):
        self.calls += 1
        return BetResult(self.ok, message="x")


class Mem:
    def __init__(self):
        self.bets = []

    def record_opportunity(self, o):
        pass

    def record_bet(self, opp, leg, status, ref, msg):
        self.bets.append((leg.bookie, status))


def _bot(adapters):
    bot = ArbBot(Config({"dry_run": False, "execution_mode": "auto", "odds_api_key": "x"}), Mem())
    bot.adapters = {a.name: a for a in adapters}
    bot.balances = {a.name: 1e6 for a in adapters}
    return bot


def _opp():
    return Opportunity("e1", "nba", "A vs B", "A", "B", "t",
                       [Leg("A", "kalshi", 2.1, 50), Leg("B", "polymarket_us", 2.1, 50)],
                       4.0, 100.0, 5.0)


def test_failed_first_leg_places_nothing_else():
    k, p = FakeAPI("kalshi", ok=False), FakeAPI("polymarket_us")
    bot = _bot([k, p])
    bot._handle(_opp())
    assert k.calls == 1 and p.calls == 0
    assert bot.storage.bets == [("kalshi", "FAILED"), ("polymarket_us", "SKIPPED")]


def test_same_arb_is_not_placed_twice():
    k, p = FakeAPI("kalshi"), FakeAPI("polymarket_us")
    bot = _bot([k, p])
    bot._handle(_opp())
    bot._handle(_opp())
    assert k.calls == 1 and p.calls == 1
