# Sports Arbitrage Bot

A local arbitrage-betting bot:

1. **Polls** odds from [The Odds API](https://the-odds-api.com) for the books you list, on a **credit-aware schedule**: sports are polled more often as game time nears, skipped when nothing is on, and spending is paced to last until your quota resets.
2. **Detects** arbitrage in **moneylines (2-/3-way), spreads and totals** (identical lines only): best fee-inclusive price per outcome across books → if `sum(1/odd) < 1` it's an arb. Exchange fees, commission, min/max stake, liquidity and balance checks included.
3. **Sizes** stakes so every leg pays identically; if any leg hits `max_stake_per_bet` or the exchange's available liquidity, the whole arb shrinks.
4. **Places bets** through pluggable bookmaker adapters:
   - **Kalshi** and **Polymarket US** — official trading APIs, fill-or-kill limit orders (fill at your price or better, or not at all). No browser, no AI.
   - **Betfair Exchange** — real REST API adapter (limit orders, fills at your price or not at all).
   - **DraftKings, FanDuel, BetMGM, Caesars, theScore/ESPN Bet, Fanatics, BetRivers, Hard Rock, Bally Bet, betPARX, bet365** — Playwright browsers driven by a **Claude agent** that finds the event and fills the bet slip. Code (not the model) verifies the slip and clicks *Place bet*, after **your approval on the dashboard** (or automatically with `execution_mode: auto`).
   - An arb with any leg on a book you have no adapter for is logged as `SKIPPED` and not executed.
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

For each book, set `headless: false`, run `python main.py`, log in manually in the browser window (finish any 2FA/CAPTCHA/location check), stop the bot, set `headless: true`. The session is saved in `.profiles/<book>/` and reused — you only repeat this when the session expires (you'll see `SKIPPED: balance unreadable` in the bet log). The bot never types your passwords.

Note: US books verify your location (usually GeoComply). If a book's location check fails in Playwright's Chromium, keep that book headed on your own machine or leave it out — the agent aborts on location prompts rather than working around them.

**5b) Claude API key (browser books):** the AI navigator needs `ANTHROPIC_API_KEY` in the environment (or `ant auth login`). Each slip costs a handful of Claude calls; tune `ai.effort` / `ai.model` in config.yaml.

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
- Flip `dry_run: false` with `execution_mode: approve` (the default). For every arb the agent fills **all** slips first; the dashboard then shows a card with each leg's priced vs. slip price and stake — click **Approve & place** within `approval_timeout_sec` or it's rejected. Watch a few with `headless: false`.
- Only once you trust it, consider `execution_mode: auto`: slips are committed as soon as the code checks pass (outcome present, price within `max_price_drift`, stake field equals the sized stake).

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

## Exchanges (Kalshi, Polymarket US)

`pip install -e .[exchanges]`, create API keys in each exchange's settings, and add them under `bookies:` (see `config.example.yaml`; keep key files out of git — `*.pem` is ignored).

- The Odds API feed carries each exchange's own ids (`includeSids`): the **Kalshi market ticker** and the **Polymarket US side id** + event slug, so no fuzzy matching is needed.
- Orders are **fill-or-kill limits** at the quoted price minus `max_price_drift`. Kalshi re-checks the live ask before sending.
- Fees (`exchange_fees`, `k·p·(1−p)` per contract) are priced in before an arb is accepted, and the feed's `bet_limit` (available contracts) caps arb size.
- When an arb mixes exchanges and browser books, the exchange legs go **first**; if one doesn't fill, nothing else is placed.
- Kalshi is moneyline-only for now; Polymarket US handles moneylines, spreads and totals.
- Test with Kalshi's demo exchange first (`demo: true`).

## Credits & polling

Odds calls cost `markets × ceil(books / 10)` credits per sport. The planner (`arb/planner.py`) uses free endpoints to find what's in season (wildcards like `tennis_*` work) and when each sport's next game is, then polls every `fast_sec` within `near_hours` of a game, `mid_sec` within `mid_hours`, `slow_sec` within `lookahead_hours`, and not at all beyond. A token bucket spreads your remaining credits evenly until `credits_reset_day`, keeping `credit_reserve` untouched. On the free plan (500/month) that means only a few polls per hour — the dashboard shows credits left and how many sports are active.

## AI navigator (browser books)

`arb/agent.py` gives Claude a small tool set over the logged-in page: `snapshot` (numbered interactive elements + page text; when the feed has an add-to-betslip link, the agent starts there), `screenshot`, `click`, `type_text` (search boxes), `enter_stake`, `scroll`, `navigate`, then `slip_ready` or `abort`. Hard limits enforced in code:

- **It can't choose the amount** — `enter_stake` always types the engine-sized stake; numeric `type_text` is refused.
- **It can't place the bet** — clicks on *Place/Submit/Confirm bet* controls are refused; placement happens in `WebBookieAdapter.commit()` after approval, with the price and stake re-verified.
- **It can't leave the book** — `navigate` is limited to the book's `allowed_domains`.
- **It stops on logins, location checks, CAPTCHAs** and anything unexpected, and page text is treated as data, not instructions.
- Any leg that fails to prepare cancels the whole arb before anything is placed.

Adding a book is one row in `WEB_BOOKS` (`arb/bookies/__init__.py`): Odds API key, start URL (`{state}` is filled from config), allowed domains.

## Anti-detection measures (browser books)

Built in:
- **Persistent Chromium profile per book** → stable cookies + device fingerprint; login is one-time, not per-bet.
- **Real desktop user-agent**, fixed `locale` + `timezone_id` (set both to match your account's state — geo/timezone mismatch vs your account is a classic flag).
- **`navigator.webdriver` masked** via init script; `window.chrome`, `plugins`, `languages` shims; `--disable-blink-features=AutomationControlled`.
- **No blind retries** on failure — failures are logged and you decide.

Not built in (deliberately):
- No CAPTCHA solver. If you hit one, solve it manually in a headed session.
- No proxy rotation. If you run from a datacenter IP, consider a **residential/proxy in the same state as your account** — books do flag datacenter IPs. You can route Playwright through one by adding a `proxy` key to the context launch (see Playwright docs) and passing it in `web_bookie.authenticate()`.

## Configuration (config.yaml)

| key | meaning |
|---|---|
| `odds_api_key` | The Odds API key (free tier: 500 credits/mo) |
| `bookmakers` | books to scan and arb between (default: the ones under `bookies:`) — books you don't have are ignored |
| `sports`, `regions`, `markets` | what to scan (e.g. `basketball_nba` or `tennis_*`, `us`/`us2`/`us_ex`, `h2h`/`spreads`/`totals`) |
| `min_roi_pct` | minimum **net** ROI to act (after commission) |
| `bankroll` | total split across the legs of one arb |
| `max_stake_per_bet` / `min_stake_per_bet` | per-leg caps/floors |
| `max_price_drift` | browser books: max slip-price movement (decimal) before aborting |
| `commissions` | per-bookie commission on winnings (e.g. `betfair: 0.05`) |
| `dry_run` | **true = detect & log only. Keep true until you trust it.** |
| `execution_mode` | `approve` (dashboard click per arb) or `auto` (browser books) |
| `approval_timeout_sec` | unanswered approvals are rejected after this |
| `ai.*` | Claude navigator: `model`, `effort`, `max_steps`, `timeout_sec`, `vision` |
| `state` | two-letter state for state-specific book sites (BetMGM, Caesars, BetRivers, bet365) |
| `poll_interval_sec` | scheduler tick; per-sport cadence is in `polling.*` |
| `polling.*` | credit-aware schedule — `fast_sec`, `near_hours`, `mid_sec`, `mid_hours`, `slow_sec`, `lookahead_hours`, `credits_reset_day`, `credit_reserve` |
| `exchange_fees` | per-contract fee factor k for Kalshi / Polymarket US |
| `liquidity_in_contracts` | books whose feed `bet_limit` caps stake size |
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
For API-less books, just add a row to `WEB_BOOKS` — the AI agent handles the site.

## How detection works

For each event's market — `h2h`, or each identical `spreads`/`totals` line — take the **best** fee-inclusive decimal odds per outcome across your books. If every outcome is covered, each leg is on a *different* book, and `sum(1/odd) < 1`, compute:

- `ROI = (1/sum(1/odd) − 1) × (1 − worst_winning_leg_commission)`
- `stake_i = total × (1/odd_i) / sum(1/odd)` (every leg pays `total / sum(1/odd)`)
- skip if any stake violates min/max or a book's balance is too low.

## ⚠️ Read this before disabling dry_run

- **ToS / ban risk:** browser automation of US sportsbooks almost certainly violates their ToS, and arbing in general gets accounts limited. Betfair Exchange is the one venue that explicitly supports automated betting.
- **Legality:** confirm sports betting is legal where you are (and that each book serves your region); the bot is a tool, compliance is yours.
- **Slippage:** the price-drift guard stops browser books from locking in a worse price than sized; Betfair uses limit orders. Anything still outside those guards (e.g. a book shortening the line *before* you click) is on you — keep `max_price_drift` tight and ROI thresholds realistic.
- **Execution risk:** one leg books and the other fails → naked position. Reconcile the bet log (dashboard → Bet log) and close legs manually if needed. Approval reduces this (all slips are filled before anything is placed) but can't remove it — a book can still reject at the final click.
- **No profit guarantee.** Use at your own risk, with money you can afford to lose.
