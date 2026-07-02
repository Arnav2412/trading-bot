"""
Static-site publisher: bake the live dashboard into docs/ so GitHub Pages can
serve it 24/7 - no laptop, no Flask server, nothing running on your machine.

Each scheduled cloud run (9:40 lock, midday scans, 4pm review) calls
`python run.py publish`, which:
  1. builds the exact same state payload the local dashboard API serves,
  2. writes it to  docs/state.json,
  3. copies the dashboard template to docs/index.html with a STATIC flag so
     the page reads state.json instead of the local API.

Then persist_state.sh commits docs/ and GitHub Pages serves it at
https://<username>.github.io/<repo>/
"""
from __future__ import annotations

import json
import os

from .config import load_config
from .utils import now_ist, get_logger, market_is_open
from .paper import tracker
from .reporting import DISCLAIMER
from .web.app import _clean_json, _saved_picks

log = get_logger()


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_state(cfg: dict) -> dict:
    """Same payload as the local /api/state, minus scan controls."""
    picks, gen = _saved_picks(cfg)
    return _clean_json({
        "generated_at": gen or now_ist().isoformat(),
        "published_at": now_ist().isoformat(),
        "scanning": False,
        "status_msg": "",
        "market_open": market_is_open(cfg),
        "demo": False,
        "picks": picks,
        "new_picks": [],
        "locked_today": tracker.locked_symbols_today(cfg),
        "paper": _paper(cfg),
        "today_pnl": tracker.day_pnl_on_capital(cfg),
        "today_mtm": tracker.marktomarket(cfg),
        "today_peak": tracker.peak_today(cfg).get("peak_pct"),
        "new_today": tracker.new_today_mtm(cfg),
        "history": tracker.history_rows(cfg),
        "portfolio": tracker.portfolio(cfg),
        "disclaimer": DISCLAIMER,
    })


def _paper(cfg: dict) -> dict:
    # Local import to avoid importing pandas at module load for no reason.
    from .web.app import _paper_payload
    return _paper_payload(cfg)


def publish(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    docs = os.path.join(_root(), "docs")
    os.makedirs(docs, exist_ok=True)

    state = build_state(cfg)
    with open(os.path.join(docs, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)

    tpl = os.path.join(_root(), "src", "web", "templates", "index.html")
    with open(tpl, "r", encoding="utf-8") as fh:
        html = fh.read()
    # Flip the page into static mode: read state.json, hide the scan buttons.
    html = html.replace("<script>",
                        "<script>window.__STATIC__=true;</script>\n<script>", 1)
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    # .nojekyll: make sure GitHub Pages serves the files verbatim.
    open(os.path.join(docs, ".nojekyll"), "w").close()
    log.info("Published static dashboard -> docs/ (index.html + state.json)")
    return docs
