# Sports Arbitrage Bot

A local arbitrage-betting bot:

1. **Polls** odds from [The Odds API](https://the-odds-api.com) (any bookmaker they aggregate).
2. **Detects** arbitrage in 2-way/3-way moneyline markets: best odds per outcome across books → if `sum(1/odd) < 1` it's an arb. Commission on the (worst-case) winning leg is included in the ROI, plus min/max stake and balance checks.
3. **Sizes** stakes so every leg pays identically, capped by your `max_stake_per_bet`.
4. **Places bets** through pluggable bookmaker adapters:
   - **Betfair Exchange** — real REST API adapter (limit orders, fills at your price or not at all).
   - **DraftKings, FanDuel, BetMGM, BetRivers** — shared **Playwright browser-automation** adapter with **price-drift checks** and **stealth hardening**.
   - Any book without an adapter still gets its legs logged as `MANUAL`.
5. **Logs** every opportunity and bet to SQLite and shows them on a **web dashboard**.

## How to run it (step by step)

**0) Prereqs:** Python 3.10+ (check with `python3 --version`). `make` is handy but optional.

**1) Get the code.** `git clone` the repo onto a machine that will stay on (a VPS works well, but keep in mind CAPTCHAs are easier to solve from your own machine/browser).

**2) Install.** The fastest path is the Makefile:

```bash
make setup        # venv + editable install (core deps) + copy config.yaml
```
or do it by hand:

```bash
cd sports-arb-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                 # core deps + the `arbbot` command
```

**Browser books** (DraftKings/FanDuel/BetMGM/BetRivers) additionally need Playwright:

```bash
make browser       # pip install -e .[browser] + playwright install chromium
```
(or `pip install -e .[browser]` followed by `playwright install chromium`). The dry-run / Betfair-Exchange path works without any of this.

**3) Get an odds API key:** sign up at https://the-odds-api.com (free tier = 500 credits/month, plenty for 60s polling of a few sports).

**3) Get an odds API key:** sign up at https://the-odds-api.com (free tier = 500 credits/month, plenty for 60s polling of a few sports).

**4) Configure.** `make setup` already copied the example to `config.yaml`; otherwise:

```bash
cp config.example.yaml config.yaml
# then edit config.yaml (NEVER commit it — it's gitignored):
#   odds_api_key → your key
#   sports / bankroll / max_stake_per_bet → your settings
#   bookies.betfair.* → your exchange keys (if using Betfair)
#   bookies.draftkings / fanduel / betmgm / betrivers → uncomment the ones you have
```

**5) One-time browser logins (only for the browser books you use):**

For each book, set `headless: false`, run `python main.py`, log in manually in the browser window (finish any 2FA/CAPTCHA), stop the bot, set `headless: true`. The session is saved in `.profiles/<book>/` and reused — you only repeat this when the session expires (you'll see `SKIPPED: balance unreadable` in the bet log).

**6) Run:** (all three are equivalent — from an activated venv, or with `.venv/bin/python` in place of `python`)

```bash
make run                 # = python -m arb
python -m arb            # start dashboard + bot loop
arbbot                   # same, the installed console command
python main.py           # legacy shim — still works
```

→ open http://127.0.0.1:8321 for the dashboard (live opps, opportunity history, bet log). The terminal logs every poll cycle.

**Tests:** `make test` runs the arb-engine smoke tests (offline, no browser/API needed).

**7) Go live (gradually):**

- Keep `dry_run: true` for a few days. Watch which opportunities appear and that dry-run sizing looks right.
- Flip `dry_run: false`. The first few real arbs: watch a browser-book flow once with `headless: false` to confirm every step works, then switch back to headless.

**8) Run it persistently (optional):**

```bash
# simple:
nohup .venv/bin/python -m arb > bot.log 2>&1 &
```

or a systemd unit:

```ini
[Unit]
Description=sports-arb-bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/sports-arb-bot
ExecStart=/path/to/sports-arb-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
sudo systemctl enable --now sports-arb-bot
journalctl -fu sports-arb-bot
```

## Price safety (browser books)

Before typing the stake, the adapter reads the **slip price** next to the outcome (decimal or American display) and **aborts if it moved more than `max_price_drift`** (default `0.02` = two cents) from the price the arb was sized on. The price is **re-checked immediately before the final *Place bet* click**. If the slip price can't be read at all, the bet is also aborted (set `require_price_check: false` in the adapter only if you know why).

Failed price checks show up in the bet log as `FAILED: price moved: slip X vs priced Y` — that's the guard doing its job, not a bug.

## Anti-detection measures (browser books)

Built in:
- **Persistent Chromium profile per book** → stable cookies + device fingerprint; login is one-time, not per-bet.
- **Real desktop user-agent**, fixed `locale` + `timezone_id` (set both to match your account's state — geo/timezone mismatch vs your account is a classic flag).
- **`navigator.webdriver` masked** via init script; `window.chrome`, `plugins`, `languages` shims; `--disable-blink-features=AutomationControlled`.
- **Human-ish pacing:** randomised delays between every step, character-by-character stake typing, no fixed robotic sleep.
- **No blind retries** on failure — failures are logged and you decide.

Not built in (deliberately):
- No CAPTCHA solver. If you hit one, solve it manually in a headed session.
- No proxy rotation. If you run from a datacenter IP, consider a **residential/proxy in the same state as your account** — books do flag datacenter IPs. You can route Playwright through one by adding a `proxy` key to the context launch (see Playwright docs) and passing it in `web_bookie.authenticate()`.

## Configuration (config.yaml)

| key | meaning |
|---|---|
| `odds_api_key` | The Odds API key (free tier: 500 credits/mo) |
| `sports`, `regions`, `markets` | what to scan (e.g. `basketball_nba`, `eu`, `h2h`) |
| `min_roi_pct` | minimum **net** ROI to act (after commission) |
| `bankroll` | total split across the legs of one arb |
| `max_stake_per_bet` / `min_stake_per_bet` | per-leg caps/floors |
| `max_price_drift` | browser books: max slip-price movement (decimal) before aborting |
| `commissions` | per-bookie commission on winnings (e.g. `betfair: 0.05`) |
| `dry_run` | **true = detect & log only. Keep true until you trust it.** |
| `poll_interval_sec` | polling cadence (respect your API rate limit) |
| `bookies.<name>` | credentials/settings for each adapter |

## Adding another bookmaker adapter

Implement `arb/bookie.py::BookieAdapter`:

```python
class MyBookAdapter(BookieAdapter):
    name = "mybook"                       # must match the bookmaker key in The Odds API
    def authenticate(self) -> bool: ...
    def get_balance(self) -> float: ...
    def place_bet(self, opportunity, leg, stake: float) -> BetResult: ...
```

Register it in `arb/bookies/__init__.py` + add creds under `bookies.mybook`.
For API-less books, the fastest path is subclassing `arb/web_bookie.py::WebBookieAdapter` with just URLs and button texts.

## How detection works

For each event's `h2h` market, take the **best** decimal odds per outcome across all bookmakers. If there are 2–3 outcomes, each leg is on a *different* book, and `sum(1/odd) < 1`, compute:

- `ROI = (1/sum(1/odd) − 1) × (1 − worst_winning_leg_commission)`
- `stake_i = total × (1/odd_i) / sum(1/odd)` (every leg pays `total / sum(1/odd)`)
- skip if any stake violates min/max or a book's balance is too low.

## ⚠️ Read this before disabling dry_run

- **ToS / ban risk:** browser automation of DraftKings/FanDuel/BetMGM/BetRivers almost certainly violates their ToS, and arbing in general gets accounts limited. Betfair Exchange is the one venue that explicitly supports automated betting.
- **Legality:** confirm sports betting is legal where you are (and that each book serves your region); the bot is a tool, compliance is yours.
- **Slippage:** the price-drift guard stops browser books from locking in a worse price than sized; Betfair uses limit orders. Anything still outside those guards (e.g. a book shortening the line *before* you click) is on you — keep `max_price_drift` tight and ROI thresholds realistic.
- **Execution risk:** one leg books and the other fails → naked position. Reconcile the bet log (dashboard → Bet log) and close legs manually if needed.
- **No profit guarantee.** Use at your own risk, with money you can afford to lose.
