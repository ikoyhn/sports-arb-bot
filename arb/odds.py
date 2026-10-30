import math

import requests


class OddsError(Exception):
    pass


class OddsClient:
    """Thin client for The Odds API (https://the-odds-api.com, v4).

    v4 notes learned from the live API:
    - The key is passed as an `apiKey` query parameter (header AuthKey/ApiKey
      is rejected with MISSING_KEY).
    - Odds live at `/v4/sports/{sport_key}/odds`; there is no combined
      `/v4/sports/odds` endpoint.
    - Outcomes are objects: [{"name": "...", "price": 1.87, "point"?, "sid"?, ...}]
    - `/v4/sports` and `/v4/sports/{sport}/events` are free (no credits).
    - Odds cost = markets × regions; with `bookmakers`, every 10 books count
      as one region. Unknown bookmaker keys are silently ignored.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.the-odds-api.com"):
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.credits_remaining = None   # from x-requests-remaining
        self.credits_used = None

    def _get(self, path: str, **params):
        params = dict(params, apiKey=self.api_key)
        r = requests.get(self.base + path, params=params, timeout=30)
        if r.status_code == 429:
            raise OddsError("Odds API rate limit hit (429) — wait and retry")
        if r.status_code == 401:
            raise OddsError(f"Odds API rejected the API key (401): {r.text[:200]}")
        if r.status_code != 200:
            raise OddsError(f"Odds API error {r.status_code}: {r.text[:200]}")
        if r.headers.get("x-requests-remaining") is not None:
            self.credits_remaining = float(r.headers["x-requests-remaining"])
            self.credits_used = float(r.headers.get("x-requests-used") or 0)
        return r.json()

    @staticmethod
    def cost(markets, regions, bookmakers=None) -> int:
        """Credits one odds call costs."""
        n_regions = math.ceil(len(bookmakers) / 10) if bookmakers else len(regions)
        return max(1, len(markets)) * max(1, n_regions)

    def list_sports(self):
        """In-season sports (free)."""
        return self._get("/v4/sports")

    def list_events(self, sport):
        """Upcoming + live events for a sport, no odds (free)."""
        return self._get(f"/v4/sports/{sport}/events")

    def fetch_odds(self, sport, regions, markets, bookmakers=None):
        """Odds for one sport, with book ids/links/limits for the adapters.

        With `bookmakers`, only those books are requested (the API then
        ignores `regions`). Returns [] for an unknown/finished sport.
        """
        where = ({"bookmakers": ",".join(bookmakers)} if bookmakers
                 else {"regions": ",".join(regions)})
        try:
            return self._get(
                f"/v4/sports/{sport}/odds",
                markets=",".join(markets),
                oddsFormat="decimal",
                includeSids="true",
                includeLinks="true",
                includeBetLimits="true",
                **where,
            ) or []
        except OddsError as e:
            if "error 404" in str(e) and "Unknown sport" in str(e):
                return []
            raise
