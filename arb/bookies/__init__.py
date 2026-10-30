"""Register bookmaker adapters here.

Exchanges (Kalshi, Polymarket US, Betfair) have API adapters. Browser books
are one table row each: the Claude agent handles each site's
layout, so a book only needs a start URL and the domains the agent may visit.
Keys must match The Odds API bookmaker keys. ``{state}`` in a URL is filled
from the book's ``state:`` config value (two-letter, e.g. ``nj``); any field
can be overridden per book in config.yaml.
"""
from ..web_bookie import WebBookieAdapter
from .betfair import BetfairAdapter
from .kalshi import KalshiAdapter
from .polymarket_us import PolymarketUSAdapter

WEB_BOOKS = {
    # odds-api key: (home_url, allowed_domains)
    "draftkings":     ("https://sportsbook.draftkings.com", ("draftkings.com",)),
    "fanduel":        ("https://sportsbook.fanduel.com", ("fanduel.com",)),
    "betmgm":         ("https://sports.{state}.betmgm.com/en/sports", ("betmgm.com",)),
    "williamhill_us": ("https://sportsbook.caesars.com/us/{state}/bet", ("caesars.com",)),  # Caesars
    "espnbet":        ("https://thescore.bet", ("thescore.bet", "espnbet.com")),  # theScore Bet / ESPN Bet
    "fanatics":       ("https://sportsbook.fanatics.com", ("fanatics.com",)),
    "betrivers":      ("https://{state}.betrivers.com", ("betrivers.com",)),
    "hardrockbet":    ("https://app.hardrock.bet", ("hardrock.bet",)),
    "ballybet":       ("https://play.ballybet.com", ("ballybet.com",)),
    "betparx":        ("https://{state}.betparx.com", ("betparx.com",)),
    # bet365 is not in The Odds API's US feed, so the engine won't route arbs
    # here unless your odds source includes it.
    "bet365":         ("https://www.{state}.bet365.com", ("bet365.com",)),
}

# exchanges with real trading APIs (no browser, no AI)
_ADAPTERS = {a.name: a for a in (BetfairAdapter, KalshiAdapter, PolymarketUSAdapter)}
for _key, (_url, _domains) in WEB_BOOKS.items():
    _ADAPTERS[_key] = type(f"{_key.title().replace('_', '')}Adapter", (WebBookieAdapter,),
                           {"name": _key, "home_url": _url, "allowed_domains": _domains})


def get_adapter(name: str, creds: dict):
    cls = _ADAPTERS.get(name)
    if cls is None:
        raise KeyError(
            f"No adapter registered for bookmaker '{name}'. Add it to WEB_BOOKS or "
            f"implement BookieAdapter in arb/bookies/ (supported: {sorted(_ADAPTERS)})"
        )
    creds = creds or {}
    if creds.get("state") is None and "{state}" in getattr(cls, "home_url", ""):
        raise KeyError(f"bookmaker '{name}' needs a `state:` (e.g. nj) in config.yaml")
    return cls(creds)
