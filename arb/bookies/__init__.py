"""Register bookmaker adapters here — one line per new adapter."""
from .betfair import BetfairAdapter
from .betrivers import BetRiversAdapter
from .betmgm import BetMGMAdapter
from .draftkings import DraftKingsAdapter
from .fanduel import FanDuelAdapter

_ADAPTERS = {
    BetfairAdapter.name: BetfairAdapter,
    DraftKingsAdapter.name: DraftKingsAdapter,
    FanDuelAdapter.name: FanDuelAdapter,
    BetMGMAdapter.name: BetMGMAdapter,
    BetRiversAdapter.name: BetRiversAdapter,
}


def get_adapter(name: str, creds: dict):
    cls = _ADAPTERS.get(name)
    if cls is None:
        raise KeyError(
            f"No adapter registered for bookmaker '{name}'. Implement BookieAdapter "
            f"in arb/bookies/ and register it here (supported: {sorted(_ADAPTERS)})"
        )
    return cls(creds)
