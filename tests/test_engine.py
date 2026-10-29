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
        "commence_time": "2026-01-01T00:00:00Z",
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
