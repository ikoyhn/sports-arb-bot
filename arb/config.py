import copy
import os

import yaml

DEFAULTS = {
    "odds_api_key": "",
    "odds_api_url": "https://api.the-odds-api.com",
    "sports": ["basketball_nba", "soccer_epl"],
    "regions": ["eu"],
    "markets": ["h2h"],
    "min_roi_pct": 0.5,
    "bankroll": 1000.0,
    "max_stake_per_bet": 250.0,
    "min_stake_per_bet": 2.0,
    # browser books: refuse to place if the slip price moved more than this
    # (decimal, e.g. 0.02 = two cents) vs the price the arb was sized on
    "max_price_drift": 0.02,
    "commissions": {},
    "dry_run": True,
    "poll_interval_sec": 60,
    "bookies": {},
}


class Config:
    """Flattened yaml config with defaults."""

    def __init__(self, data: dict):
        merged = copy.deepcopy(DEFAULTS)
        merged.update(data or {})
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
