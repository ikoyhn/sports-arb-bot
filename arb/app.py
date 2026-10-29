"""FastAPI app + entry point for the Sports Arb Bot dashboard and polling loop.

Importable so the app can be served by a WSGI/ASGI proxy or tested; the
``main()`` below is what ``python -m arb`` and the ``arbbot`` console script
call.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .bot import ArbBot
from .config import Config
from .storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# The project root (folder that contains both the `arb` package and `web/`).
_PKG_ROOT = Path(__file__).resolve().parent.parent


def resolve_web_dir() -> Path:
    """Locate the `web/` directory robustly.

    Prefer a repo-local `web/` next to the package (source checkout), and fall
    back to a `web/` directory in the current working directory. This keeps
    `python main.py`, `python -m arb`, and the installed `arbbot` command all
    working from any CWD.
    """
    candidates = (_PKG_ROOT / "web", Path("web"), Path("arb") / ".." / "web")
    for c in candidates:
        if (c / "index.html").is_file():
            return c.resolve()
    raise FileNotFoundError(
        "Could not find web/index.html. Run from the project root or pass "
        "a valid CWD so the dashboard can locate its static files."
    )


def create_app(cfg: Config, storage: Storage, bot: ArbBot,
               web_dir: Optional[Path] = None) -> FastAPI:
    """Build the FastAPI app. Routes are registered *before* the static
    mount at ``/`` so the ``/api/*`` endpoints take precedence."""
    app = FastAPI(title="Sports Arb Bot")

    @app.get("/api/status")
    def status():
        return {
            "dry_run": cfg.dry_run,
            "last_poll": bot.last_poll,
            "last_error": bot.last_error,
            "odds_credits": bot.odds_credits,
            "balances": bot.balances,
            "adapters": list(bot.adapters),
            "poll_interval_sec": cfg.poll_interval_sec,
            "min_roi_pct": cfg.min_roi_pct,
            "bankroll": cfg.bankroll,
        }

    @app.get("/api/opportunities")
    def opportunities():
        return bot.latest

    @app.get("/api/history")
    def history():
        return {
            "opportunities": storage.list_opportunities(),
            "bets": storage.list_bets(),
        }

    web = Path(web_dir) if web_dir is not None else resolve_web_dir()
    app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    return app


def main(host: str = "127.0.0.1", port: int = 8321,
         log_level: str = "warning") -> None:
    """Start the dashboard + polling loop. Runs until interrupted."""
    cfg = Config.load()
    storage = Storage()
    bot = ArbBot(cfg, storage)
    app = create_app(cfg, storage, bot)

    threading.Thread(target=bot.start, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":  # pragma: no cover
    main()
