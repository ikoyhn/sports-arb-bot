"""Example bookmaker adapter: Betfair Exchange.

Betfair Exchange is the worked example because it is the one major venue
with a public REST API for placing bets (and it explicitly supports
automated betting). Most other bookmakers have no such API — for those,
implement a sibling adapter using their partner/affiliate API or browser
automation (ToS + ban risk; see README).

Docs: https://docs.developer.betfair.com/display/1smkk77u236bzo4U13k30544/Place+Bets
"""
import shlex
import uuid

import requests

from ..bookie import BetResult, BookieAdapter

APP_NAME = "sports-arb-bot"


class BetfairAdapter(BookieAdapter):
    name = "betfair"
    BASE = "https://apis.betfair.com"

    def __init__(self, creds):
        super().__init__(creds)
        self._login_key = None

    # -- auth ------------------------------------------------------------
    def authenticate(self) -> bool:
        try:
            r = requests.post(
                f"{self.BASE}/identity/v1/apis/login/loginKey",
                json={
                    "appName": APP_NAME,
                    "appKey": self.creds.get("app_key", ""),
                    "userName": self.creds.get("user_name", ""),
                    "password": self.creds.get("password", ""),
                    "token": self.creds.get("token", ""),
                },
                timeout=30,
            )
            r.raise_for_status()
            self._login_key = r.json()
            return True
        except Exception:
            self._login_key = None
            return False

    def _headers(self):
        return {
            "X-Application-Name": APP_NAME,
            "X-Application-Key": self.creds.get("app_key", ""),
            "X-Authentication-Key": self._login_key,
            "Content-Type": "application/json",
        }

    # -- balance -----------------------------------------------------------
    def get_balance(self) -> float:
        r = requests.get(f"{self.BASE}/customer/accountjson",
                         headers=self._headers(), timeout=30)
        r.raise_for_status()
        return float(r.json().get("availableToBetBalance", 0.0))

    # -- market lookup -------------------------------------------------------
    def find_market(self, home, away, outcome):
        """Best-effort resolve (marketId, selectionId) for `outcome` in 'home vs away'."""
        r = requests.get(
            f"{self.BASE}/betting/v1.0/findMarket",
            params={
                "queryFilters": f"eventName={shlex.quote(f'{home} vs {away}')}",
                "marketFilters": "marketType='MATCH_ODDS',marketStatus='OPEN'",
                "maxResults": 20,
            },
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        want = outcome.casefold()
        for m in r.json():
            runners = m.get("runners", [])
            names = [runner.get("name", "") for runner in runners]
            # sanity: this market should actually contain our teams
            if home not in names and away not in names:
                continue
            for runner in runners:
                rn = runner.get("name", "").casefold()
                if rn == want or want in rn or rn in want:
                    return {"marketId": m["marketId"],
                            "selectionId": runner["selectionId"]}
        return None

    # -- betting -------------------------------------------------------------
    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        market = self.find_market(opportunity.home, opportunity.away, leg.outcome)
        if market is None:
            return BetResult(success=False,
                             message=f"could not resolve Betfair market for '{leg.outcome}'")
        payload = {
            "betSelector": {"marketId": market["marketId"],
                            "selectionId": market["selectionId"]},
            "orderType": "LIMIT",           # limit order: only fills at our price
            "persistenceType": "LAUNCH",    # LAUNCH = market-on-open markets
            "exChanges": ["BETFAIR_EXCHANGE"],
            "instructions": [{"action": "ADD",
                              "order": {"price": round(leg.odds, 3),
                                        "size": round(stake, 2)}}],
        }
        r = requests.post(
            f"{self.BASE}/betting/v1.0/placeBets",
            json={"customerRef": f"arbbot-{uuid.uuid4().hex[:10]}",
                  "exBets": [payload]},
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "UNKNOWN")
        bet_ids = data.get("betIds") or [""]
        return BetResult(success=status == "SUCCESS", ref=bet_ids[0], message=status)
