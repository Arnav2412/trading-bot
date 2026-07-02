"""
Local web dashboard for BEST TRADING BOT.

Run with:  python run.py web   (then open http://127.0.0.1:5000)
"""
from __future__ import annotations

import glob
import json
import os
import math
import threading

from flask import Flask, jsonify, render_template

from ..config import load_config
from ..scanner import scan
from ..data import market_data
from ..notify.email_report import _enrich
from ..paper import tracker
from ..utils import now_ist, get_logger, market_is_open
from ..reporting import DISCLAIMER
from ..risk.position_sizing import build_trade_plan
from ..analysis.signals import Signal

log = get_logger()


def _clean_json(o):
    """Replace NaN/Infinity (invalid JSON) so the browser can always parse it."""
    if isinstance(o, float):
        return o if math.isfinite(o) else 0.0
    if isinstance(o, dict):
        return {k: _clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean_json(v) for v in o]
    return o

_STATE = {"generated_at": None, "picks": [], "scanning": False,
          "status_msg": "", "demo": False}
_LOCK = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__)
    cfg = load_config()

    @app.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/")
    def index():
        return render_template("index.html",
                               capital=cfg["account"]["capital"],
                               recipient=cfg.get("email", {}).get("recipient", ""))

    @app.route("/api/state")
    def api_state():
        demo = _STATE.get("demo")
        picks = _STATE["picks"]
        gen = _STATE["generated_at"]
        smsg = _STATE["status_msg"]
        if not demo and not picks:
            saved, sgen = _saved_picks(cfg)
            if saved:
                picks, gen = saved, sgen
                smsg = smsg or "Showing the latest emailed picks (saved). Click Scan now to refresh."
        if demo:
            today_mtm = _demo_mtm()
            peak = {"peak_pct": 1.92, "peak_at": None}
            new_today = {"n": 0, "rows": []}
            history = _demo_history()
        else:
            today_mtm = tracker.marktomarket(cfg)
            peak = tracker.peak_today(cfg)   # true intraday high across ALL today's picks (cached ~10min)
            new_today = tracker.new_today_mtm(cfg)
            history = tracker.history_rows(cfg)
        return jsonify(_clean_json({
            "generated_at": gen,
            "scanning": _STATE["scanning"],
            "status_msg": smsg,
            "market_open": market_is_open(cfg),
            "demo": demo,
            "picks": picks,
            "new_picks": _STATE.get("new_syms", []),
            "locked_today": None if demo else tracker.locked_symbols_today(cfg),
            "paper": _demo_paper() if demo else _paper_payload(cfg),
            "today_pnl": _demo_today() if demo else tracker.day_pnl_on_capital(cfg),
            "today_mtm": today_mtm,
            "today_peak": peak.get("peak_pct"),
            "new_today": new_today,
            "history": history,
            "portfolio": _demo_portfolio() if demo else tracker.portfolio(cfg),
            "disclaimer": DISCLAIMER,
        }))

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        if _STATE["scanning"]:
            return jsonify({"status": "already_scanning"})
        threading.Thread(target=_run_scan, args=(cfg,), daemon=True).start()
        return jsonify({"status": "started"})

    @app.route("/api/demo", methods=["POST"])
    def api_demo():
        _load_demo(cfg)
        return jsonify({"status": "demo_loaded"})

    @app.route("/api/paper_update", methods=["POST"])
    def api_paper_update():
        try:
            closed = tracker.update_open(cfg)   # resolve any that hit stop/target
            return jsonify({"status": "ok", "closed": closed})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)})

    return app




def _latest_signal_file(cfg: dict):
    import os as _os
    report_dir = cfg["output"].get("report_dir", "reports")
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    files = sorted(glob.glob(_os.path.join(root, report_dir, "signals_*.json")))
    return files[-1] if files else None


def _pick_from_saved(sig: dict, cfg: dict) -> dict:
    """Rebuild a full pick card (why / upside / risks / exit / Rs10k) from a saved
    signal so the website shows EXACTLY what the email did."""
    pl = sig.get("plan", {})
    entry = float(pl.get("entry", 0)); t1 = float(pl.get("target1", 0))
    t2 = float(pl.get("target2", 0)); stop = float(pl.get("stop_loss", 0))
    direction = sig.get("direction", "LONG"); long = direction == "LONG"
    hypo = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1) or 1
    swing = cfg.get("mode") == "swing"
    hold = cfg.get("swing", {}).get("max_hold_days", 15)
    pct = lambda a, b: 0.0 if not b else round((a - b) / b * 100, 2)
    up1, up2, rk = abs(pct(t1, entry)), abs(pct(t2, entry)), abs(pct(stop, entry))
    shares = int((hypo * lev) // entry) if entry else 0
    sign = 1 if long else -1
    g1 = round((t1 - entry) * shares * sign); g2 = round((t2 - entry) * shares * sign)
    gs = round((stop - entry) * shares * sign)
    rr1 = pl.get("rr_t1", ""); rr2 = pl.get("rr_t2", "")
    upside = [
        f"Target 1: Rs {t1:.2f}  (+{up1:.2f}% / {rr1}R) - book ~50% here",
        f"Target 2: Rs {t2:.2f}  (+{up2:.2f}% / {rr2}R) - trail the rest",
        f"On Rs {hypo:,}: ~{shares} shares -> +Rs {g1:,} at T1, +Rs {g2:,} at T2",
    ]
    risks = [
        f"Hard stop: Rs {stop:.2f}  (-{rk:.2f}%) - exit immediately if hit; "
        f"only Rs {pl.get('rupees_at_risk',0):.0f} at risk on {pl.get('quantity',0)} shares",
        f"On Rs {hypo:,}: a stop-out loses about Rs {abs(gs):,}",
        f"Idea invalidates if price {'closes back below VWAP/breakout' if long else 'reclaims VWAP'} "
        f"or the {'up' if long else 'down'}trend breaks",
        (f"Swing trade: hold days-to-weeks; exit if it closes back below the breakout / 20-EMA"
         if swing else "Intraday only: square off by ~15:20 IST even if neither stop nor target is hit"),
    ]
    return {"symbol": sig.get("symbol"), "direction": direction,
            "score": sig.get("score", 0), "entry": entry,
            "sentiment": sig.get("sentiment", 0.0), "why": sig.get("reasons", [])[:6],
            "upside": upside, "risks": risks, "fundamentals": sig.get("fundamentals", {}),
            "budget": {"capital": round(hypo), "shares": shares, "pnl_t1": g1,
                       "pnl_t2": g2, "pnl_stop": gs}}


def _saved_picks(cfg: dict):
    f = _latest_signal_file(cfg)
    if not f:
        return [], None
    try:
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        picks = [_pick_from_saved(s, cfg) for s in data.get("signals", [])]
        return picks, data.get("generated_at")
    except Exception:
        return [], None


def _run_scan(cfg: dict) -> None:
    with _LOCK:
        _STATE["scanning"] = True
        _STATE["demo"] = False
        _STATE["status_msg"] = "Scanning... (first run can take ~30-60s)"
    try:
        # Fast availability check (intraday only; swing uses reliable daily data).
        if cfg.get("mode") != "swing" and not market_data.quick_probe(cfg):
            with _LOCK:
                _STATE["picks"] = []
                _STATE["generated_at"] = now_ist().isoformat()
                _STATE["status_msg"] = _no_data_msg(cfg)
            return
        results = scan(cfg)
        hypo = cfg.get('simulation', {}).get('hypothetical_capital', 10000)
        lev = cfg['account'].get('intraday_leverage', 1)
        picks = [_pick_dict(_enrich(r, hypo, lev)) for r in results]
        # Which symbols are NEW vs the previous scan? (highlight on the dashboard)
        prev = set(p.get("symbol") for p in _STATE.get("picks", []))
        new_syms = [p["symbol"] for p in picks if p["symbol"] not in prev]
        try:
            tracker.update_open(cfg)   # resolve any that hit stop/target
            # NOTE: 'Scan now' is a PREVIEW only - it does NOT log to the paper book.
            # The book is fed solely by the scheduled premarket (lock) + 2-hourly runs,
            # so manual scans (e.g. at 1 AM) never pollute your track record.
        except Exception as exc:
            log.warning("Paper update failed: %s", exc)
        with _LOCK:
            _STATE["picks"] = picks
            _STATE["new_syms"] = new_syms
            _STATE["generated_at"] = now_ist().isoformat()
            _STATE["status_msg"] = (
                f"Scan complete: {len(picks)} setup(s)." if picks else
                "Scan complete: 0 setups cleared the filters. (Normal when "
                "the market is quiet or rangebound.)")
    except Exception as exc:
        log.warning("Scan failed: %s", exc)
        with _LOCK:
            _STATE["status_msg"] = f"Scan error: {exc}"
    finally:
        with _LOCK:
            _STATE["scanning"] = False


def _no_data_msg(cfg: dict) -> str:
    if not market_is_open(cfg):
        return ("Market is closed. Live intraday data isn't available right now - "
                "run during market hours (Mon-Fri, 09:15-15:30 IST). "
                "Click 'Load demo' to see how the dashboard looks with data.")
    return ("Couldn't fetch live data - Yahoo may be rate-limiting. Wait a minute "
            "and scan again, or click 'Load demo' to preview the dashboard.")


def _pick_dict(e: dict) -> dict:
    return {"symbol": e["symbol"], "direction": e["direction"], "score": e["score"],
            "entry": e["entry"], "sentiment": e["sentiment"], "why": e["why"],
            "upside": e["upside"], "risks": e["risks"], "fundamentals": e["fundamentals"]}


def _paper_payload(cfg: dict) -> dict:
    summ = tracker.summary(cfg)
    df = tracker._core_rows(tracker.load(cfg))   # stats measure the core picks
    trades, equity = [], []
    if len(df):
        import pandas as pd
        closed = df[df["status"] == "CLOSED"].copy()
        cum = 0.0
        for _, row in closed.iterrows():
            cum += float(pd.to_numeric(row.get("pnl"), errors="coerce") or 0)
            equity.append(round(cum, 2))
        for _, row in df.tail(15).iloc[::-1].iterrows():
            trades.append({"symbol": row["symbol"], "direction": row["direction"],
                           "entry": row["entry"], "status": row["status"],
                           "outcome": row.get("outcome", ""), "pnl": row.get("pnl", ""),
                           "r": row.get("r_multiple", "")})
    return {"summary": summ, "equity": equity, "trades": trades}


# --------------------------------------------------------------------------- #
#  Demo mode - sample data so the UI can be seen working anytime
# --------------------------------------------------------------------------- #
def _load_demo(cfg: dict) -> None:
    samples = [
        ("TATAMOTORS", "LONG", 980.50, 0.33,
         ["EMA 9>21>50 (uptrend)", "Above VWAP", "Supertrend up",
          "MACD bullish & rising", "Above-avg volume", "ADX 28 strong trend"]),
        ("ICICIBANK", "LONG", 1185.20, 0.18,
         ["EMA 9>21>50 (uptrend)", "Above VWAP", "RSI 58 bullish",
          "Above-avg volume"]),
        ("WIPRO", "SHORT", 245.20, -0.25,
         ["EMA 9<21<50 (downtrend)", "Below VWAP", "Supertrend down",
          "RSI 41 bearish"]),
    ]
    picks = []
    for sym, d, px, sent, reasons in samples:
        s = Signal(sym, d, 80.0 if d == "LONG" else 74.0, px, atr=px * 0.01,
                   reasons=reasons, sentiment=sent)
        plan = build_trade_plan(sym, d, px, px * 0.01, cfg)
        e = _enrich({"signal": s, "plan": plan, "mode": "swing",
                     "fundamentals": {"roe": 18.4, "debt_to_equity": 0.3}},
                    cfg.get('simulation', {}).get('hypothetical_capital', 10000),
                    cfg['account'].get('intraday_leverage', 1))
        picks.append(_pick_dict(e))
    with _LOCK:
        _STATE["picks"] = picks
        _STATE["demo"] = True
        _STATE["generated_at"] = now_ist().isoformat()
        _STATE["status_msg"] = ("DEMO DATA - illustrative only, not live signals. "
                                "Click 'Scan now' during market hours for real picks.")


def _demo_portfolio() -> dict:
    openp = [
        {"symbol": "ONGC", "direction": "SHORT", "entry": 233.10, "current": 226.40,
         "pnl": 287.0, "ret_pct": 2.87, "logged_at": "2026-06-29"},
        {"symbol": "CHOLAFIN", "direction": "LONG", "entry": 1799.20, "current": 1832.0,
         "pnl": 182.0, "ret_pct": 1.82, "logged_at": "2026-06-29"},
        {"symbol": "VEDL", "direction": "SHORT", "entry": 273.45, "current": 277.10,
         "pnl": -133.0, "ret_pct": -1.33, "logged_at": "2026-06-29"},
    ]
    closed = [
        {"symbol": "HDFCBANK", "direction": "LONG", "entry": 1620.0, "exit": 1750.0,
         "pnl": 802.0, "ret_pct": 8.02, "outcome": "target", "lived_up": True,
         "result": "hit target", "logged_at": "2026-06-24"},
        {"symbol": "SBIN", "direction": "LONG", "entry": 842.0, "exit": 799.9,
         "pnl": -500.0, "ret_pct": -5.0, "outcome": "stop", "lived_up": False,
         "result": "stopped out", "logged_at": "2026-06-23"},
        {"symbol": "INFY", "direction": "SHORT", "entry": 1555.0, "exit": 1430.6,
         "pnl": 800.0, "ret_pct": 8.0, "outcome": "target", "lived_up": True,
         "result": "hit target", "logged_at": "2026-06-20"},
    ]
    return {"capital_per_trade": 10000, "n_open": 3, "n_closed": 3,
            "invested_open": 30000, "invested_total": 60000,
            "realized_pnl": 1102.0, "unrealized_pnl": 336.0, "total_pnl": 1438.0,
            "total_pct": 2.40, "win_rate_pct": 66.7, "lived_up": 2, "lived_up_pct": 66.7,
            "open": openp, "closed": closed, "equity": [802.0, 302.0, 1102.0]}


def _demo_mtm() -> dict:
    rows = [
        {"symbol": "ONGC", "direction": "SHORT", "entry": 233.10, "current": 226.40,
         "move_pct": 2.87, "pnl": 287.0, "status": "OPEN", "outcome": ""},
        {"symbol": "CHOLAFIN", "direction": "LONG", "entry": 1799.20, "current": 1832.0,
         "move_pct": 1.82, "pnl": 182.0, "status": "OPEN", "outcome": ""},
        {"symbol": "VEDL", "direction": "SHORT", "entry": 273.45, "current": 277.10,
         "move_pct": -1.33, "pnl": -133.0, "status": "OPEN", "outcome": ""},
        {"symbol": "NATIONALUM", "direction": "SHORT", "entry": 332.15, "current": 325.0,
         "move_pct": 2.15, "pnl": 215.0, "status": "OPEN", "outcome": ""},
    ]
    return {"capital_per_trade": 10000, "n": 4, "invested": 40000,
            "total_pnl": 551.0, "pct": 1.38, "goal_pct": 1, "rows": rows}


def _demo_today() -> dict:
    return {"capital": 10000, "closed": 3, "open": 1, "total_pnl": 286.0,
            "pct": 2.86, "rows": [
                {"symbol": "HDFCBANK", "direction": "LONG", "outcome": "target",
                 "ret_pct": 0.92, "pnl": 230.0, "alloc": 3333.0},
                {"symbol": "INFY", "direction": "SHORT", "outcome": "target",
                 "ret_pct": 0.97, "pnl": 323.0, "alloc": 3333.0},
                {"symbol": "SBIN", "direction": "LONG", "outcome": "stop",
                 "ret_pct": -0.80, "pnl": -267.0, "alloc": 3333.0}]}


def _demo_history() -> list:
    return [
        {"date": "2026-06-29", "picks": 10, "total_pnl": 470.0, "pct": 0.47,
         "peak_pct": 1.92, "win_rate_pct": 60.0, "best_sym": "SUPREMEIND",
         "best_pnl": 199.0, "worst_sym": "ZYDUSLIFE", "worst_pnl": -46.0,
         "notes": "Reviewed 10 picks: 6 up, 4 down. 3/4 losers fought the regime."},
        {"date": "2026-06-26", "picks": 10, "total_pnl": -180.0, "pct": -0.18,
         "peak_pct": 0.62, "win_rate_pct": 40.0, "best_sym": "CHOLAFIN",
         "best_pnl": 210.0, "worst_sym": "VEDL", "worst_pnl": -133.0,
         "notes": "Stop-heavy day. Auto-tuned: min_score 50->51."},
        {"date": "2026-06-25", "picks": 10, "total_pnl": absolute_demo_pnl(),
         "pct": 0.83, "peak_pct": 1.40, "win_rate_pct": 70.0, "best_sym": "HDFCBANK",
         "best_pnl": 480.0, "worst_sym": "SBIN", "worst_pnl": -90.0,
         "notes": "Strong day (win 70%). Auto-tuned: min_score 51->50."},
    ]


def absolute_demo_pnl():
    return 830.0


def _demo_paper() -> dict:
    eq, cum = [], 0.0
    for p in [1490, -980, 1510, -990, 620, 1480, -985, 1505, 700, -980,
              1495, 640, -990, 1500, 1490, -985, 660, 1510]:
        cum += p; eq.append(round(cum, 2))
    trades = [
        {"symbol": "HDFCBANK", "direction": "LONG", "entry": 1620.0,
         "status": "CLOSED", "outcome": "target", "pnl": 1490, "r": 1.5},
        {"symbol": "SBIN", "direction": "LONG", "entry": 842.0,
         "status": "CLOSED", "outcome": "stop", "pnl": -980, "r": -1.0},
        {"symbol": "INFY", "direction": "SHORT", "entry": 1555.0,
         "status": "CLOSED", "outcome": "target", "pnl": 1510, "r": 1.5},
        {"symbol": "TATAMOTORS", "direction": "LONG", "entry": 980.5,
         "status": "OPEN", "outcome": "", "pnl": "", "r": ""},
    ]
    return {"summary": {"open": 3, "closed": 18, "win_rate_pct": 61.1,
                        "expectancy_R": 0.34, "total_pnl": 4120.0},
            "equity": eq, "trades": trades}
