import requests


class OddsError(Exception):
    pass


class OddsClient:
    """Thin client for The Odds API (https://the-odds-api.com)."""

    def __init__(self, api_key: str, base_url: str = "https://api.the-odds-api.com"):
        self.api_key = api_key
        self.base = base_url.rstrip("/")

    def _get(self, path: str, **params):
        r = requests.get(
            self.base + path,
            params=params,
            headers={"Authorization": f"ApiKey {self.api_key}"},
            timeout=30,
        )
        if r.status_code == 429:
            raise OddsError("Odds API rate limit hit (429) — wait and retry")
        if r.status_code != 200:
            raise OddsError(f"Odds API error {r.status_code}: {r.text[:200]}")
        return r.json()

    def credits(self):
        return self._get("/v4/account").get("credits")

    def fetch_events(self, sports, regions, markets):
        """Return the standard v4 events list with bookmakers/markets/odds."""
        return self._get(
            "/v4/sports/odds",
            sports=",".join(sports),
            regions=",".join(regions),
            markets=",".join(markets),
            oddsFormat="decimal",
            minOdds=1.2,
        )
