"""Adapter interface — implement this for each bookmaker the bot may bet through.

Rules for adapters:
  * place_bet() must be a LIMIT (or equivalent) order at the quoted price so
    the arb never fills worse than sized. Rejection is always acceptable;
    an over-price fill is not.
  * Keep latency low — this runs on the hot path of arb execution.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BetResult:
    success: bool
    ref: str = ""      # order/bet id when available
    message: str = ""  # human-readable status


class BookieAdapter(ABC):
    name = "base"      # must match the bookmaker `key` used by The Odds API

    def __init__(self, creds: dict):
        self.creds = creds

    @abstractmethod
    def authenticate(self) -> bool:
        """Log in / exchange credentials. Return True on success."""

    @abstractmethod
    def get_balance(self) -> float:
        """Available bettable balance in account currency."""

    def supports(self, leg) -> bool:
        """Can this adapter place this leg (market type, ids it needs)?"""
        return True

    @abstractmethod
    def place_bet(self, opportunity, leg, stake: float) -> BetResult:
        """Place one arb leg.

        opportunity: full Opportunity (event, teams, all legs) so the adapter
                     can resolve the correct market if it needs to.
        leg:         Leg(outcome, bookie, odds)
        stake:       stake in account currency
        """
