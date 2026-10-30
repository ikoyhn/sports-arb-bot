import copy
import os

import yaml

DEFAULTS = {
    "odds_api_key": "",
    "odds_api_url": "https://api.the-odds-api.com",
    "sports": ["basketball_nba", "soccer_epl"],
    "regions": ["us", "us2"],
    "markets": ["h2h", "spreads", "totals"],
    "min_roi_pct": 0.5,
    "bankroll": 1000.0,
    "max_stake_per_bet": 250.0,
    "min_stake_per_bet": 2.0,
    # browser books: refuse to place if the slip price moved more than this
    # (decimal, e.g. 0.02 = two cents) vs the price the arb was sized on
    "max_price_drift": 0.02,
    "commissions": {},
    # per-contract exchange fee k (fee = k * p * (1 - p) on a contract costing p)
    "exchange_fees": {"kalshi": 0.07, "polymarket_us": 0.0695},
    # books whose feed `bet_limit` is in contracts (used to cap stake size)
    "liquidity_in_contracts": ["kalshi", "polymarket_us"],
    "dry_run": True,
    "poll_interval_sec": 60,
    "bookies": {},
    # Odds API bookmaker keys to scan. Empty = the books under `bookies:`.
    # Arbs are only built from these books (others are ignored entirely).
    "bookmakers": [],
    # default two-letter state for books whose URLs are state-specific
    "state": "",
    # browser books: "approve" = fill slips, then wait for a dashboard click;
    # "auto" = commit as soon as the code-level slip checks pass
    "execution_mode": "approve",
    "approval_timeout_sec": 45,
    # credit-aware polling — see arb/planner.py for every knob
    "polling": {},
    # Claude agent that navigates browser books (see arb/agent.py)
    "ai": {
        "model": "claude-opus-5-5",
        "effort": "low",
        "max_steps": 30,
        "timeout_sec": 150,
        "vision": True,
    },
}


class Config:
    """Flattened yaml config with defaults."""

    def __init__(self, data: dict):
        merged = copy.deepcopy(DEFAULTS)
        data = dict(data or {})
        merged["ai"].update(data.pop("ai", None) or {})
        merged.update(data)
        if not merged["bookmakers"]:
            merged["bookmakers"] = list(merged["bookies"] or {})
        self.__dict__.update(merged)

    @classmethod
    def load(cls, path="config.yaml"):
        if not os.path.exists(path):
            if os.path.exists("config.example.yaml"):
                path = "config.example.yaml"
            else:
                raise FileNotFoundError(
                    "No config.yaml found. Copy config.example.yaml to config.yaml and edit it."
                )
        with open(path) as f:
            return cls(yaml.safe_load(f) or {})
