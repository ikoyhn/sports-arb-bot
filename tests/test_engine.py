"""Smoke tests for the arbitrage engine.

These need only the core package (no network, no Playwright, no browser).
They build events in the same shape The Odds API returns and assert that
detection + sizing behave as documented.

Run with:  pytest        (or)        python -m pytest
"""
from arb.config import Config
from arb.engine import find_arb


def _cfg(**over):
    base = {
        "min_roi_pct": 0.5,
        "bankroll": 1000.0,
        "max_stake_per_bet": 250.0,
        "min_stake_per_bet": 2.0,
        "commissions": {},
    }
    base.update(over)
    return Config(base)


def _event(bookmakers, sport="basketball_nba", home="A", away="B", event_id="ev"):
    """Bookmakers: list of (key, {outcome: decimal_odds}).
    Mirrors the v4 `/sports/odds` payload the bot consumes."""
    return {
        "id": event_id,
        "sport_key": sport,
        "home_team": home,
        "away_team": away,
        "commence_time": "2099-01-01T00:00:00Z",
        "bookmakers": [
            {"key": key, "markets": [
                {"key": "h2h", "outcomes": list(odds), "odds": list(odds.values())}
            ]}
            for key, odds in bookmakers
        ],
    }


# --- detection ----------------------------------------------------------
def test_no_arb_when_implied_over_100():
    # 1.50 + 1.50 -> sum(1/odd) = 1.33 >= 1  (house edge, no arb)
    ev = _event([("dkt", {"A": 1.50}), ("fanduel", {"B": 1.50})])
    assert find_arb([ev], _cfg()) == []


def test_break_even_is_not_an_arb():
    # 2.00 + 2.00 -> sum(1/odd) = exactly 1.0 -> not strictly < 1
    ev = _event([("dkt", {"A": 2.00}), ("fanduel", {"B": 2.00})])
    assert find_arb([ev], _cfg()) == []


def test_detects_two_way_arb_and_sizes_equal_payout():
    # 2.10 + 2.10 -> sum(1/odd) = 0.952 < 1  -> arb
    ev = _event([("dkt", {"A": 2.10}), ("fanduel", {"B": 2.10})])
    (opp,) = find_arb([ev], _cfg(bankroll=100.0))
    assert opp.roi_pct > 0.5
    # Every leg produces an identical payout (guaranteed profit).
    payouts = [l.stake * l.odds for l in opp.legs]
    assert all(abs(p - payouts[0]) < 0.05 for p in payouts)
    # Each leg is on a different book.
    assert {l.bookie for l in opp.legs} == {"dkt", "fanduel"}


def test_rejects_two_legs_on_the_same_book():
    # A single book has the best price on both outcomes -> no real arb.
    ev = _event([("dkt", {"A": 2.10, "B": 2.10})])
    assert find_arb([ev], _cfg()) == []


def test_best_price_per_outcome_wins_across_books():
    ev = _event([
        ("dkt",     {"A": 1.90, "B": 1.90}),   # worse, but offers both
        ("fanduel", {"A": 2.30}),              # best for A
        ("betmgm",  {"B": 2.30}),              # best for B
    ])
    (opp,) = find_arb([ev], _cfg())
    by_book = {l.bookie: l for l in opp.legs}
    assert by_book["fanduel"].odds == 2.30
    assert by_book["betmgm"].odds == 2.30
    assert "dkt" not in by_book


# --- sizing & risk caps -------------------------------------------------
def test_min_stake_vetoes_thin_leg():
    # 3-way arb where the longshot leg forces a tiny stake.
    # odds 2.5 / 2.5 / 50 -> sum(1/odd)=0.82 < 1 (arb); long leg stake ~18.
    ev = _event([
        ("book1", {"o1": 2.5}),
        ("book2", {"o2": 2.5}),
        ("book3", {"o3": 50.0}),
    ])
    assert find_arb([ev], _cfg(min_stake_per_bet=25.0)) == []   # vetoed
    assert len(find_arb([ev], _cfg(min_stake_per_bet=2.0))) == 1  # fine


def test_max_stake_caps_leg_size():
    ev = _event([("dkt", {"A": 2.10}), ("fanduel", {"B": 2.10})])
    (opp,) = find_arb([ev], _cfg(bankroll=100_000.0, max_stake_per_bet=50.0))
    assert all(l.stake <= 50.0 for l in opp.legs)


# --- commission accounting ---------------------------------------------
def test_commission_reduces_roi():
    ev = _event([("betfair", {"A": 2.10}), ("fanduel", {"B": 2.10})])
    plain = find_arb([ev], _cfg(commissions={}))[0]
    with_comm = find_arb([ev], _cfg(commissions={"betfair": 0.05}))[0]
    assert with_comm.roi_pct < plain.roi_pct


def test_below_min_roi_is_dropped():
    # A real-but-thin arb (5.03% ROI) disappears when the floor is raised.
    ev = _event([("dkt", {"A": 2.10}), ("fanduel", {"B": 2.10})])
    assert find_arb([ev], _cfg(min_roi_pct=0.5))           # kept
    assert find_arb([ev], _cfg(min_roi_pct=10.0)) == []    # dropped


# --- regressions from live data ------------------------------------------
def test_incomplete_three_way_market_is_not_an_arb():
    # Arsenal (1.10) must count: without it Hull + Draw looked like +487%.
    ev = _event([("book1", {"Arsenal": 1.10, "Hull": 19.5, "Draw": 8.4}),
                 ("book2", {"Arsenal": 1.08, "Draw": 8.0})], sport="soccer_epl")
    assert find_arb([ev], _cfg()) == []


def test_capped_arb_still_pays_equally_on_every_leg():
    ev = _event([("dkt", {"A": 12.0}), ("fanduel", {"B": 1.3})])
    (opp,) = find_arb([ev], _cfg(bankroll=1000.0, max_stake_per_bet=250.0))
    payouts = [l.stake * l.odds for l in opp.legs]
    assert max(l.stake for l in opp.legs) <= 250.0
    assert max(payouts) - min(payouts) < 0.1


def test_books_you_dont_have_are_ignored():
    ev = _event([("onexbet", {"A": 2.5}), ("draftkings", {"A": 1.9, "B": 2.1}),
                 ("fanduel", {"A": 2.05, "B": 1.8})])
    assert find_arb([ev], _cfg())                                   # 1xBet arb
    (opp,) = find_arb([ev], _cfg(bookmakers=["draftkings", "fanduel"]))
    assert {l.bookie for l in opp.legs} == {"draftkings", "fanduel"}


def test_started_events_are_skipped():
    ev = _event([("dkt", {"A": 2.10}), ("fanduel", {"B": 2.10})])
    ev["commence_time"] = "2020-01-01T00:00:00Z"
    assert find_arb([ev], _cfg()) == []
