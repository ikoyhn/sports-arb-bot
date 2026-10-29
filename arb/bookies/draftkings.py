"""DraftKings Sportsbook adapter (browser automation — no public betting API).

One-time setup: set headless: false, start the bot once, log in at the
profile window (.profiles/draftkings/), then switch back to headless.
"""
from ..web_bookie import WebBookieAdapter


class DraftKingsAdapter(WebBookieAdapter):
    name = "draftkings"
    home_url = "https://sportsbook.draftkings.com/sb/pci-main-menu"
    login_url = "https://sportsbook.draftkings.com/login"
    place_bet_text = "place bet"
    success_texts = ("success", "placed", "confirmed")
