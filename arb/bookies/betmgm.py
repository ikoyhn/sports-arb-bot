"""BetMGM Sportsbook adapter (browser automation — no public betting API).

One-time setup: set headless: false, start the bot once, log in at the
profile window (.profiles/betmgm/), then switch back to headless.
"""
from ..web_bookie import WebBookieAdapter


class BetMGMAdapter(WebBookieAdapter):
    name = "betmgm"
    home_url = "https://sportsbook.betmgm.com"
    login_url = "https://sportsbook.betmgm.com/login"
    place_bet_text = "place bet"
    success_texts = ("success", "placed", "confirmed")
