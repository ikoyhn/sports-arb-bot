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
    - Outcomes are objects: [{"name": "...", "price": 1.87}, ...]
    """

    def __init__(self, api_key: str, base_url: str = "https://api.the-odds-api.com"):
        self.api_key = api_key
        self.base = base_url.rstrip("/")

    def _get(self, path: str, **params):
        params = dict(params, apiKey=self.api_key)
        r = requests.get(self.base + path, params=params, timeout=30)
        if r.status_code == 429:
            raise OddsError("Odds API rate limit hit (429) — wait and retry")
        if r.status_code == 401:
            raise OddsError(f"Odds API rejected the API key (401): {r.text[:200]}")
        if r.status_code != 200:
            raise OddsError(f"Odds API error {r.status_code}: {r.text[:200]}")
        return r.json()

    def fetch_events(self, sports, regions, markets):
        """Fetch h2h odds for each sport and return the combined event list.

        Skips (and warns) on any single sport the API doesn't know, so one
        bad key in config.yaml doesn't kill the whole poll cycle.
        """
        events = []
        skipped = []
        for sport in sports or []:
            try:
                data = self._get(
                    f"/v4/sports/{sport}/odds",
                    regions=",".join(regions),
                    markets=",".join(markets),
                    oddsFormat="decimal",
                    minOdds=1.2,
                )
                events.extend(data or [])
            except OddsError as e:
                if "error 404" in str(e) and "Unknown sport" in str(e):
                    skipped.append(sport)
                    continue
                raise
        if skipped:
            raise OddsError(
                f"These sport keys are unknown to the API — pick valid keys "
                f"from config.yaml (see https://the-odds-api.com/liveapi/guides/v4/): "
                f"{', '.join(skipped)}"
            )
        return events
