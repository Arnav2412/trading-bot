"""
Paper-trading tracker: a live, no-real-money track record.

Every pick the bot surfaces is logged to a CSV ledger as an OPEN paper trade
(entry/stop/targets/qty). On each later run the tracker pulls real intraday
candles and closes any open trade that has hit its target, its stop, or the
end-of-day square-off - exactly like the backtester, but forward in time on
live data. Over days this builds an honest record of how the strategy would
have actually done, with zero money at risk.

Ledger: reports/paper_trades.csv  (one row per trade, human-readable).
"""
from __future__ import annotations

import csv
import os

import pandas as pd

from ..data import market_data
from ..utils import now_ist, get_logger, market_is_open

log = get_logger()


def _safe_num(x, default=0.0):
    """float(x) but returns `default` for blank/None/NaN/inf."""
    import math
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

FIELDS = ["id", "logged_at", "symbol", "direction", "entry", "stop",
          "target1", "target2", "qty", "rupees_at_risk", "status",
          "exit_time", "exit_price", "pnl", "r_multiple", "outcome", "tag", "score"]


def get_intraday_safe(symbol: str, cfg: dict):
    """Fetch intraday candles; return None on any failure (network etc.)."""
    try:
        df = market_data.get_intraday(symbol, cfg)
        return df if df is not None and not df.empty else None
    except Exception as exc:
        log.warning("Paper-tracker data fetch failed for %s: %s", symbol, exc)
        return None


def _ledger_path(cfg: dict) -> str:
    report_dir = cfg["output"].get("report_dir", "reports")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = os.path.join(root, report_dir)
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, "paper_trades.csv")


def load(cfg: dict) -> pd.DataFrame:
    path = _ledger_path(cfg)
    if not os.path.exists(path):
        return pd.DataFrame(columns=FIELDS)
    df = pd.read_csv(path)
    if "tag" not in df.columns:        # older ledgers: treat existing rows as core
        df["tag"] = "core"
    if "score" not in df.columns:      # older ledgers: score unknown
        df["score"] = ""
    return df


# --------------------------------------------------------------------------- #
#  Daily pick-LOCK: freeze the day's official picks (set at the pre-market run)
#  so later 2-hour scans don't keep changing what we're evaluating.
# --------------------------------------------------------------------------- #
def _lock_path(cfg: dict, day: str = None) -> str:
    import os as _os
    day = day or now_ist().strftime("%Y%m%d")
    report_dir = cfg["output"].get("report_dir", "reports")
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    out = _os.path.join(root, report_dir, "locked")
    _os.makedirs(out, exist_ok=True)
    return _os.path.join(out, f"locked_{day}.json")


def locked_symbols_today(cfg: dict):
    """Symbols locked as TODAY's official picks, or None if not locked yet."""
    import json
    p = _lock_path(cfg)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        syms = d.get("symbols") or []
        return syms or None
    except Exception:
        return None


def write_lock(cfg: dict, symbols: list) -> list:
    """Persist today's locked picks (idempotent; overwrites the day's file)."""
    import json
    syms = list(dict.fromkeys(symbols))   # de-dupe, keep order
    p = _lock_path(cfg)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"locked_at": now_ist().isoformat(), "symbols": syms},
                  fh, indent=2)
    log.info("Locked %d picks for today: %s", len(syms), ", ".join(syms))
    return syms


def save(df: pd.DataFrame, cfg: dict) -> None:
    df.to_csv(_ledger_path(cfg), index=False, quoting=csv.QUOTE_MINIMAL)


# --------------------------------------------------------------------------- #
#  Logging new picks
# --------------------------------------------------------------------------- #
def log_signals(results: list, cfg: dict, lock: bool = False) -> int:
    """Add an OPEN paper trade for each pick not already logged today.

    Tagging keeps the day honest:
      - lock=True (pre-market run): these become today's CORE picks (the fixed
        scorecard) and get frozen via the lock file.
      - lock=False while a lock exists (2-hour / Scan-now): the CORE set stays
        fixed; any genuinely NEW symbol is still recorded, but tagged "intraday"
        so it shows under 'New today' and in trade history - never polluting the
        morning scorecard.
      - lock=False with no lock yet: behaves as before (tagged core).
    Returns the number of new rows added.
    """
    # HARD GUARD: never log picks while the market is CLOSED. A pre-open scan
    # only sees yesterday's close, so "entries" would be stale prices that show
    # 0% all morning and corrupt the day's scorecard. Scans outside market
    # hours are previews only.
    if not market_is_open(cfg):
        log.info("Paper-tracker: market closed - scan is a PREVIEW only, "
                 "nothing logged (entries would be stale).")
        return 0
    # CORE-ONLY tracking (default): the paper book measures the PRODUCT - the
    # locked morning picks. Midday scans stay informational (emails/dashboard)
    # but do NOT add positions, so the track record isn't diluted by dozens of
    # extra names. Set simulation.track_intraday_adds: true to log them again.
    track_extras = bool(cfg.get("simulation", {}).get("track_intraday_adds", False))
    if not lock and not track_extras:
        log.info("Paper-tracker: non-lock scan - informational only, "
                 "not logged (core-only tracking).")
        return 0
    locked = locked_symbols_today(cfg)
    tag = "core" if (lock or locked is None) else "intraday"
    df = load(cfg)
    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    open_today = set(
        df[(df["status"] == "OPEN") &
           (df["logged_at"].astype(str).str.startswith(today))]["symbol"]
    ) if len(df) else set()
    # Don't re-add a symbol already locked as core today.
    core_today = set(locked) if locked else set()

    new_rows = []
    for r in results:
        s, p = r["signal"], r["plan"]
        if s.symbol in open_today or (tag == "intraday" and s.symbol in core_today):
            continue
        new_rows.append({
            "id": f"{s.symbol}_{now.strftime('%Y%m%d_%H%M%S')}",
            "logged_at": now.isoformat(),
            "symbol": s.symbol, "direction": s.direction,
            "entry": round(p.entry, 2), "stop": round(p.stop_loss, 2),
            "target1": round(p.target1, 2), "target2": round(p.target2, 2),
            "qty": p.quantity, "rupees_at_risk": round(p.rupees_at_risk, 2),
            "status": "OPEN", "exit_time": "", "exit_price": "",
            "pnl": "", "r_multiple": "", "outcome": "", "tag": tag,
            "score": round(float(getattr(s, "score", 0) or 0), 1),
        })
    if new_rows:
        add = pd.DataFrame(new_rows, columns=FIELDS)
        df = add if df.empty else pd.concat([df, add], ignore_index=True)
        save(df, cfg)
        log.info("Paper-tracker: logged %d new %s trade(s).", len(new_rows), tag)
    if lock:
        # Freeze exactly these symbols as today's official picks.
        write_lock(cfg, [r["signal"].symbol for r in results])
    return len(new_rows)


# --------------------------------------------------------------------------- #
#  Closing open trades against real data
# --------------------------------------------------------------------------- #
def update_open(cfg: dict) -> int:
    """Resolve OPEN trades using real intraday candles. Returns closed count."""
    df = load(cfg)
    if df.empty:
        return 0
    for c in ["status", "exit_time", "exit_price", "pnl", "r_multiple", "outcome"]:
        df[c] = df[c].astype(object)
    closed = 0
    for idx, row in df[df["status"] == "OPEN"].iterrows():
        result = _resolve(row, cfg)
        if result is None:
            continue
        exit_price, exit_time, outcome = result
        sign = 1 if row["direction"] == "LONG" else -1
        pnl = (exit_price - float(row["entry"])) * int(row["qty"]) * sign
        risk = float(row["rupees_at_risk"]) or 1.0
        df.loc[idx, ["status", "exit_time", "exit_price", "pnl",
                     "r_multiple", "outcome"]] = [
            "CLOSED", exit_time, round(exit_price, 2), round(pnl, 2),
            round(pnl / risk, 2), outcome]
        closed += 1
    if closed:
        save(df, cfg)
        log.info("Paper-tracker: closed %d trade(s).", closed)
    return closed


def _resolve(row, cfg):
    """Return (exit_price, exit_time_iso, outcome) or None if still open."""
    symbol = row["symbol"]
    entry_ts = pd.Timestamp(row["logged_at"])
    if cfg.get("mode") == "swing":
        return _resolve_swing(row, cfg, entry_ts)
    data = get_intraday_safe(symbol, cfg)
    if data is None or data.empty:
        return None
    entry_date = entry_ts.normalize()
    # only candles on the entry date, at or after the log time
    day = data[(data.index >= entry_ts) &
               (data.index.normalize() == entry_date)]
    if day.empty:
        return None

    stop = float(row["stop"]); target = float(row["target1"])
    long = row["direction"] == "LONG"
    last_i = len(day) - 1
    for i in range(len(day)):
        bar = day.iloc[i]
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        ts = day.index[i].isoformat()
        if long:
            if lo <= stop:
                return stop, ts, "stop"
            if hi >= target:
                return target, ts, "target"
        else:
            if hi >= stop:
                return stop, ts, "stop"
            if lo <= target:
                return target, ts, "target"
        # End-of-day square-off: last candle of that date that we can see,
        # AND that date is in the past (so the day is actually complete).
        is_last = (i == last_i)
        day_complete = entry_date < now_ist().normalize()
        if is_last and day_complete:
            return close, ts, "eod"
    return None  # day still in progress, no level hit -> stay OPEN


# --------------------------------------------------------------------------- #
#  Track-record summary
# --------------------------------------------------------------------------- #
def _core_rows(df):
    """The rows that count for the track record: the locked core picks."""
    if not len(df) or "tag" not in df.columns:
        return df
    return df[df["tag"] == "core"]


def summary(cfg: dict) -> dict:
    df = _core_rows(load(cfg))
    closed = df[df["status"] == "CLOSED"] if len(df) else df
    out = {"open": int((df["status"] == "OPEN").sum()) if len(df) else 0,
           "closed": len(closed)}
    if len(closed):
        pnl = pd.to_numeric(closed["pnl"], errors="coerce").fillna(0)
        r = pd.to_numeric(closed["r_multiple"], errors="coerce").fillna(0)
        wins = pnl[pnl > 0]
        out.update({
            "win_rate_pct": round(100 * (pnl > 0).mean(), 1),
            "total_pnl": round(pnl.sum(), 2),
            "expectancy_R": round(r.mean(), 3),
            "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
            "best": round(pnl.max(), 2),
            "worst": round(pnl.min(), 2),
        })
    return out


def format_summary(cfg: dict) -> str:
    s = summary(cfg)
    if s["closed"] == 0:
        return (f"Paper track record: {s['open']} open, 0 closed yet. "
                "Stats appear once trades resolve.")
    return (f"Paper track record: {s['closed']} closed | "
            f"win {s.get('win_rate_pct')}% | "
            f"expectancy {s.get('expectancy_R')}R | "
            f"net Rs {s.get('total_pnl')} | {s['open']} still open")


# --------------------------------------------------------------------------- #
#  "What would Rs X have made/lost today?"  (end-of-day P&L on a hypothetical)
# --------------------------------------------------------------------------- #
def todays_rows(cfg: dict) -> "pd.DataFrame":
    df = load(cfg)
    if df.empty:
        return df
    today = now_ist().strftime("%Y-%m-%d")
    sub = df[df["logged_at"].astype(str).str.startswith(today)]
    locked = locked_symbols_today(cfg)
    if locked is not None:
        # Only the locked picks count as "today's" - frozen evaluation set.
        sub = sub[sub["symbol"].isin(locked)]
    return sub


def day_pnl_on_capital(cfg: dict, capital: float = None) -> dict:
    """Realized P&L if `capital` were split equally across TODAY's resolved
    paper picks. Uses each trade's actual return %; honest, not hypothetical."""
    import pandas as pd
    if capital is None:
        capital = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1) or 1
    df = todays_rows(cfg)
    closed = df[df["status"] == "CLOSED"] if len(df) else df
    n_closed = len(closed)
    n_open = (len(df) - n_closed) if len(df) else 0
    if n_closed == 0:
        return {"capital": capital, "closed": 0, "open": n_open,
                "total_pnl": 0.0, "pct": 0.0, "rows": []}
    alloc = capital / n_closed
    rows, total = [], 0.0
    for _, r in closed.iterrows():
        entry = float(r["entry"])
        exitp = float(pd.to_numeric(r["exit_price"], errors="coerce") or entry)
        sign = 1 if r["direction"] == "LONG" else -1
        ret = ((exitp - entry) / entry) * sign * lev if entry else 0.0
        pnl = alloc * ret
        total += pnl
        rows.append({"symbol": r["symbol"], "direction": r["direction"],
                     "outcome": r.get("outcome", ""), "ret_pct": round(ret * 100, 2),
                     "pnl": round(pnl, 2), "alloc": round(alloc, 2)})
    rows.sort(key=lambda x: x["pnl"], reverse=True)
    return {"capital": capital, "closed": n_closed, "open": n_open,
            "total_pnl": round(total, 2), "pct": round(total / capital * 100, 2),
            "rows": rows}


def _resolve_swing(row, cfg, entry_ts):
    """Resolve a multi-day swing trade using DAILY candles from the entry day
    forward, with the SAME two-stage exit the plan promises: half booked at
    T1 (stop -> breakeven), rest runs to T2 / breakeven / max-hold."""
    from ..data import market_data
    from ..risk.position_sizing import walk_exit
    sc = cfg.get("swing", {})
    df = market_data.fetch_yfinance(row["symbol"], sc.get("interval", "1d"),
                                    sc.get("lookback_days", 400))
    if df is None or df.empty:
        return None
    entry_date = entry_ts.normalize()
    fwd = df[df.index.normalize() >= entry_date]
    if len(fwd) < 2:
        return None  # only the entry bar so far -> still open
    return walk_exit(fwd.iloc[1:], row["direction"], float(row["entry"]),
                     float(row["stop"]), float(row["target1"]),
                     float(row["target2"]), int(sc.get("max_hold_days", 15)))


# --------------------------------------------------------------------------- #
#  Mark-to-market: "Rs X in EACH of today's trades -> P&L at today's close"
# --------------------------------------------------------------------------- #
def _latest_close(symbol: str, cfg: dict):
    from ..data import market_data
    try:
        df = market_data.fetch_yfinance(symbol, "1d", 10)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return None


def marktomarket(cfg: dict, capital_per_trade: float = None, scope: str = "today") -> dict:
    """For each of today's picks, P&L if you put `capital_per_trade` into EACH,
    marked at the latest close. Honest daily scorecard vs the goal %."""
    import pandas as pd
    if capital_per_trade is None:
        capital_per_trade = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    goal = cfg.get("simulation", {}).get("daily_goal_pct", 10)
    df = load(cfg)
    if scope == "today":
        sub = todays_rows(cfg)        # already filtered to the locked set
    else:
        sub = df
    rows, total = [], 0.0
    for _, r in sub.iterrows():
        entry = float(r["entry"]) if r["entry"] not in ("", None) else 0.0
        if entry <= 0:
            continue
        closed = r["status"] == "CLOSED"
        if closed and str(r.get("exit_price", "")) not in ("", "nan"):
            current = _safe_num(r["exit_price"], entry)
        else:
            current = _latest_close(r["symbol"], cfg) or entry
        sign = 1 if r["direction"] == "LONG" else -1
        qty = capital_per_trade / entry
        pnl = (current - entry) * qty * sign
        move = ((current - entry) / entry) * sign * 100
        total += pnl
        rows.append({"symbol": r["symbol"], "direction": r["direction"],
                     "entry": round(entry, 2), "current": round(current, 2),
                     "move_pct": round(move, 2), "pnl": round(pnl, 2),
                     "status": r["status"], "outcome": r.get("outcome", "")})
    rows.sort(key=lambda x: x["pnl"], reverse=True)
    n = len(rows)
    invested = capital_per_trade * n
    return {"capital_per_trade": capital_per_trade, "n": n, "invested": invested,
            "total_pnl": round(total, 2),
            "pct": round(total / invested * 100, 2) if invested else 0.0,
            "goal_pct": goal, "rows": rows}


# --------------------------------------------------------------------------- #
#  Full paper PORTFOLIO: Rs X into each pick, held until it sells
# --------------------------------------------------------------------------- #
def portfolio(cfg: dict, capital_per_trade: float = None) -> dict:
    """Treat every logged pick as a paper position of `capital_per_trade` rupees.
    Open positions are marked to the latest close (unrealized); closed positions
    use their exit (realized). Returns positions + portfolio-level totals."""
    import pandas as pd
    if capital_per_trade is None:
        capital_per_trade = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    df = _core_rows(load(cfg))   # track record = locked core picks only
    open_rows, closed_rows = [], []
    realized = unrealized = 0.0
    for _, r in (df.iterrows() if len(df) else []):
        entry = _safe_num(r.get("entry"), 0.0)
        if entry <= 0:
            continue
        sign = 1 if r["direction"] == "LONG" else -1
        qty = capital_per_trade / entry
        closed = r["status"] == "CLOSED"
        if closed:
            exitp = _safe_num(r.get("exit_price"), entry)
            pnl = (exitp - entry) * qty * sign
            realized += pnl
            oc = r.get("outcome", "")
            closed_rows.append({"symbol": r["symbol"], "direction": r["direction"],
                                "entry": round(entry, 2), "exit": round(exitp, 2),
                                "pnl": round(pnl, 2),
                                "ret_pct": round((exitp - entry) / entry * sign * 100, 2),
                                "outcome": oc,
                                "lived_up": oc in ("target", "target2"),  # hit the predicted target
                                "result": ("hit both targets" if oc == "target2" else
                                           "hit target" if oc == "target" else
                                           "stopped out" if oc == "stop" else "no follow-through"),
                                "logged_at": str(r.get("logged_at", ""))[:10]})
        else:
            cur = _latest_close(r["symbol"], cfg) or entry
            pnl = (cur - entry) * qty * sign
            unrealized += pnl
            open_rows.append({"symbol": r["symbol"], "direction": r["direction"],
                              "entry": round(entry, 2), "current": round(cur, 2),
                              "pnl": round(pnl, 2),
                              "ret_pct": round((cur - entry) / entry * sign * 100, 2),
                              "logged_at": str(r.get("logged_at", ""))[:10]})
    # Equity curve: cumulative realized P&L (Rs X per trade), in time order.
    equity = []
    cum = 0.0
    for x in sorted(closed_rows, key=lambda r: r["logged_at"]):
        cum += x["pnl"]
        equity.append(round(cum, 2))
    open_rows.sort(key=lambda x: x["pnl"], reverse=True)
    closed_rows.sort(key=lambda x: x["logged_at"], reverse=True)
    n_open, n_closed = len(open_rows), len(closed_rows)
    lived_up = sum(1 for x in closed_rows if x.get("lived_up"))
    invested_total = capital_per_trade * (n_open + n_closed)
    total_pnl = realized + unrealized
    wins = sum(1 for x in closed_rows if x["pnl"] > 0)
    return {
        "capital_per_trade": capital_per_trade,
        "n_open": n_open, "n_closed": n_closed,
        "invested_open": round(capital_per_trade * n_open, 2),
        "invested_total": round(invested_total, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pct": round(total_pnl / invested_total * 100, 2) if invested_total else 0.0,
        "win_rate_pct": round(100 * wins / n_closed, 1) if n_closed else None,
        "lived_up": lived_up, "lived_up_pct": round(100 * lived_up / n_closed, 1) if n_closed else None,
        "open": open_rows, "closed": closed_rows[:20], "equity": equity,
    }


# --------------------------------------------------------------------------- #
#  "New today": intraday ideas the bot added AFTER the morning lock.
#  Kept separate from the core scorecard, but recorded in the ledger/history.
# --------------------------------------------------------------------------- #
def today_new_rows(cfg: dict) -> "pd.DataFrame":
    df = load(cfg)
    if df.empty:
        return df
    today = now_ist().strftime("%Y-%m-%d")
    sub = df[df["logged_at"].astype(str).str.startswith(today)]
    if "tag" in sub.columns:
        return sub[sub["tag"] == "intraday"]
    return sub.iloc[0:0]


def today_all_rows(cfg: dict) -> "pd.DataFrame":
    """Every pick logged today - core scorecard AND intraday adds combined."""
    df = load(cfg)
    if df.empty:
        return df
    today = now_ist().strftime("%Y-%m-%d")
    return df[df["logged_at"].astype(str).str.startswith(today)]


def recent_pick_symbols(cfg: dict, days: int = 2) -> set:
    """Symbols already picked in the PRIOR `days` days (today excluded), so the
    scanner can prefer fresh names over the same recurring ones."""
    import datetime
    df = load(cfg)
    if df.empty:
        return set()
    today = now_ist().date()
    cutoff = today - datetime.timedelta(days=int(days))
    out = set()
    for ds, sym in zip(df["logged_at"].astype(str).str[:10], df["symbol"].astype(str)):
        try:
            d = datetime.date.fromisoformat(ds)
        except Exception:
            continue
        if cutoff <= d < today:
            out.add(sym)
    return out


def _mtm_over(sub, cfg, capital_per_trade) -> dict:
    """Mark-to-market a slice of the ledger at Rs `capital_per_trade` per pick."""
    rows, total = [], 0.0
    for _, r in (sub.iterrows() if len(sub) else []):
        entry = _safe_num(r["entry"], 0.0)
        if entry <= 0:
            continue
        closed = r["status"] == "CLOSED"
        if closed and str(r.get("exit_price", "")) not in ("", "nan"):
            current = _safe_num(r["exit_price"], entry)
        else:
            current = _latest_close(r["symbol"], cfg) or entry
        sign = 1 if r["direction"] == "LONG" else -1
        qty = capital_per_trade / entry
        pnl = (current - entry) * qty * sign
        move = ((current - entry) / entry) * sign * 100
        total += pnl
        rows.append({"symbol": r["symbol"], "direction": r["direction"],
                     "entry": round(entry, 2), "current": round(current, 2),
                     "move_pct": round(move, 2), "pnl": round(pnl, 2),
                     "status": r["status"], "outcome": r.get("outcome", ""),
                     "added": str(r.get("logged_at", ""))[11:16]})
    rows.sort(key=lambda x: x["pnl"], reverse=True)
    n = len(rows)
    invested = capital_per_trade * n
    return {"capital_per_trade": capital_per_trade, "n": n, "invested": invested,
            "total_pnl": round(total, 2),
            "pct": round(total / invested * 100, 2) if invested else 0.0,
            "rows": rows}


def new_today_mtm(cfg: dict, capital_per_trade: float = None) -> dict:
    if capital_per_trade is None:
        capital_per_trade = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    return _mtm_over(today_new_rows(cfg), cfg, capital_per_trade)


# --------------------------------------------------------------------------- #
#  Intraday PEAK: the day's high-water mark of the core scorecard's P&L %.
#  Recorded every time the dashboard / a scan marks to market.
# --------------------------------------------------------------------------- #
def _peak_path(cfg: dict, day: str = None) -> str:
    import os as _os
    day = day or now_ist().strftime("%Y%m%d")
    report_dir = cfg["output"].get("report_dir", "reports")
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    out = _os.path.join(root, report_dir, "peaks")
    _os.makedirs(out, exist_ok=True)
    return _os.path.join(out, f"peak_{day}.json")


def get_peak(cfg: dict) -> dict:
    import json
    p = _peak_path(cfg)
    if not os.path.exists(p):
        return {"peak_pct": None, "peak_at": None}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"peak_pct": None, "peak_at": None}


def record_peak(cfg: dict, pct: float) -> dict:
    """Update today's high-water mark if `pct` is a new high. Returns the record."""
    import json
    pct = _safe_num(pct, 0.0)
    cur = get_peak(cfg)
    best = cur.get("peak_pct")
    if best is None or pct > best:
        cur = {"peak_pct": round(pct, 2), "peak_at": now_ist().isoformat(),
               "computed_at": cur.get("computed_at")}
        try:
            with open(_peak_path(cfg), "w", encoding="utf-8") as fh:
                json.dump(cur, fh)
        except Exception:
            pass
    return cur


def peak_today(cfg: dict, capital_per_trade: float = None, max_age_sec: int = 600) -> dict:
    """TRUE intraday high-water mark of the LOCKED scorecard (the same picks the
    Close P&L is measured on), reconstructed from today's 5-min candles - so it
    captures the day's best moment even if the dashboard wasn't open then, and is
    always consistent with (>=) the close. Cached; recomputed at most every
    max_age_sec; never drops below the running high-water mark."""
    import json
    import time as _t
    import pandas as pd
    if capital_per_trade is None:
        capital_per_trade = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    cur = get_peak(cfg)
    stored = cur.get("peak_pct")
    last = cur.get("computed_at")
    if last and (_t.time() - float(last) < max_age_sec):
        return {"peak_pct": stored, "peak_at": cur.get("peak_at")}

    # Use the locked core (same set as the scorecard) so peak is always >= close.
    rows = todays_rows(cfg) if locked_symbols_today(cfg) is not None else today_all_rows(cfg)
    peak = stored if stored is not None else 0.0
    peak_at = cur.get("peak_at")
    try:
        from ..data import market_data
        today = pd.Timestamp(now_ist().strftime("%Y-%m-%d"))
        series, n = [], 0
        for _, r in rows.iterrows():
            entry = _safe_num(r["entry"], 0.0)
            if entry <= 0:
                continue
            df = market_data.fetch_yfinance(r["symbol"], "5m", 2)
            if df is None or df.empty:
                continue
            d2 = df[df.index.normalize() == today]
            if d2.empty:
                continue
            sign = 1 if r["direction"] == "LONG" else -1
            qty = capital_per_trade / entry
            pnl = (d2["close"].astype(float) - entry) * qty * sign
            pnl.name = f"{r['symbol']}_{r.name}"
            series.append(pnl)
            n += 1
        if series and n:
            mat = pd.concat(series, axis=1).sort_index().ffill().fillna(0.0)
            basket_pct = mat.sum(axis=1) / (capital_per_trade * n) * 100.0
            imax = basket_pct.max()
            if pd.notna(imax) and float(imax) > peak:
                peak = round(float(imax), 2)
                peak_at = basket_pct.idxmax().isoformat()
    except Exception as exc:
        log.warning("peak_today compute failed: %s", exc)

    out = {"peak_pct": round(peak, 2), "peak_at": peak_at, "computed_at": _t.time()}
    try:
        with open(_peak_path(cfg), "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except Exception:
        pass
    return {"peak_pct": out["peak_pct"], "peak_at": out["peak_at"]}


# --------------------------------------------------------------------------- #
#  End-of-day REVIEW: why did today's losers lose? (post-mortem for auto-tune)
# --------------------------------------------------------------------------- #
def _day_regime(cfg: dict) -> int:
    try:
        from ..data import market_data
        return market_data.market_regime(cfg)
    except Exception:
        return 0


def edge_analysis(cfg: dict) -> dict:
    """What separated today's CORE winners from losers? Compares the average
    conviction SCORE and regime-ALIGNMENT of winners vs losers, so the auto-tuner
    can adjust on evidence instead of guesses. (Needs scores in the ledger, which
    are recorded from now on; days before that just report counts.)"""
    import statistics as st
    df = todays_rows(cfg)
    if df.empty:
        return {"n": 0}
    regime = _day_regime(cfg)
    win_scores, los_scores = [], []
    win_aligned = los_aligned = winners = losers = 0
    for _, r in df.iterrows():
        entry = _safe_num(r["entry"], 0.0)
        if entry <= 0:
            continue
        if r["status"] == "CLOSED" and str(r.get("exit_price", "")) not in ("", "nan"):
            cur = _safe_num(r["exit_price"], entry)
        else:
            cur = _latest_close(r["symbol"], cfg) or entry
        sign = 1 if r["direction"] == "LONG" else -1
        pnl = (cur - entry) * sign
        score = _safe_num(r.get("score"), None) if str(r.get("score", "")) not in ("", "nan") else None
        aligned = (regime > 0 and r["direction"] == "LONG") or \
                  (regime < 0 and r["direction"] == "SHORT") or regime == 0
        if pnl >= 0:
            winners += 1
            if score is not None:
                win_scores.append(score)
            win_aligned += int(aligned)
        else:
            losers += 1
            if score is not None:
                los_scores.append(score)
            los_aligned += int(aligned)
    avg = lambda xs: round(st.mean(xs), 1) if xs else None
    return {"n": winners + losers, "winners": winners, "losers": losers,
            "regime": regime,
            "win_avg_score": avg(win_scores), "los_avg_score": avg(los_scores),
            "win_aligned_pct": round(100 * win_aligned / winners) if winners else None,
            "los_aligned_pct": round(100 * los_aligned / losers) if losers else None}


def review_today(cfg: dict) -> dict:
    """Tag why each of today's CORE losers lost + surface simple patterns."""
    d = marktomarket(cfg, scope="today")
    rows = d.get("rows", [])
    regime = _day_regime(cfg)
    loser_rows, winners = [], 0
    stops = fought_regime = 0
    for x in rows:
        if x["pnl"] >= 0:
            winners += 1
            continue
        oc = x.get("outcome", "")
        d_ = x["direction"]
        if oc == "stop":
            reason = "hit its hard stop (risk control worked, but entry was wrong)"
            stops += 1
        elif oc == "timeout":
            reason = "no follow-through within the hold window"
        elif d_ == "SHORT" and regime > 0:
            reason = "shorted while the market trended UP - fought the regime"
            fought_regime += 1
        elif d_ == "LONG" and regime < 0:
            reason = "bought while the market trended DOWN - fought the regime"
            fought_regime += 1
        else:
            reason = "drifted against the entry (momentum faded)"
        loser_rows.append({"symbol": x["symbol"], "direction": d_,
                           "move_pct": x["move_pct"], "pnl": x["pnl"],
                           "reason": reason})
    patterns = []
    n_loss = len(loser_rows)
    if fought_regime and fought_regime >= max(1, n_loss // 2):
        patterns.append(f"{fought_regime} of {n_loss} losers FOUGHT the market regime "
                        f"({'up' if regime > 0 else 'down'} market) - trust the regime more.")
    if stops and stops >= max(1, n_loss // 2):
        patterns.append(f"{stops} of {n_loss} losers were stop-outs - entries may be "
                        f"chasing extended moves; demand a higher score.")
    if not patterns and n_loss:
        patterns.append("Losses look like normal market noise, not a systematic mistake.")
    edge = edge_analysis(cfg)
    # Evidence pattern: did stronger-scored setups actually win today?
    wa, la = edge.get("win_avg_score"), edge.get("los_avg_score")
    if wa is not None and la is not None and wa - la >= 4:
        patterns.append(f"Winners scored higher on average ({wa}) than losers ({la}) "
                        f"- demanding a higher score should help.")
    return {"date": now_ist().strftime("%Y-%m-%d"), "regime": regime,
            "n": d["n"], "winners": winners, "losers": n_loss,
            "fought_regime": fought_regime, "stops": stops,
            "loser_rows": loser_rows, "patterns": patterns,
            "total_pnl": d["total_pnl"], "pct": d["pct"], "edge": edge}


# --------------------------------------------------------------------------- #
#  HISTORY: one permanent row per trading day (written at 4pm by `run.py eod`).
# --------------------------------------------------------------------------- #
def _history_path(cfg: dict) -> str:
    report_dir = cfg["output"].get("report_dir", "reports")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = os.path.join(root, report_dir)
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, "history.csv")


HISTORY_FIELDS = ["date", "picks", "total_pnl", "pct", "peak_pct", "win_rate_pct",
                  "best_sym", "best_pnl", "worst_sym", "worst_pnl", "notes"]


def snapshot_history(cfg: dict, notes: str = "") -> dict:
    """Freeze today's CORE scorecard into history.csv (overwrites same date)."""
    d = marktomarket(cfg, scope="today")
    rows = d.get("rows", [])
    if d["n"] == 0:
        return {}
    peak = get_peak(cfg).get("peak_pct")
    wins = sum(1 for x in rows if x["pnl"] >= 0)
    best = max(rows, key=lambda x: x["pnl"])
    worst = min(rows, key=lambda x: x["pnl"])
    rec = {"date": now_ist().strftime("%Y-%m-%d"), "picks": d["n"],
           "total_pnl": d["total_pnl"], "pct": d["pct"],
           "peak_pct": peak if peak is not None else d["pct"],
           "win_rate_pct": round(100 * wins / d["n"], 1),
           "best_sym": best["symbol"], "best_pnl": best["pnl"],
           "worst_sym": worst["symbol"], "worst_pnl": worst["pnl"],
           "notes": notes}
    path = _history_path(cfg)
    if os.path.exists(path):
        hdf = pd.read_csv(path)
        hdf = hdf[hdf["date"] != rec["date"]]            # replace today if re-run
        hdf = pd.concat([hdf, pd.DataFrame([rec], columns=HISTORY_FIELDS)],
                        ignore_index=True)
    else:
        hdf = pd.DataFrame([rec], columns=HISTORY_FIELDS)
    hdf.to_csv(path, index=False)
    log.info("History: stored %s -> Rs %s (%.2f%%), peak %.2f%%",
             rec["date"], rec["total_pnl"], rec["pct"], rec["peak_pct"])
    return rec


def history_rows(cfg: dict, limit: int = 40) -> list:
    path = _history_path(cfg)
    if not os.path.exists(path):
        return []
    try:
        df = pd.read_csv(path)
        return df.tail(limit).iloc[::-1].to_dict("records")
    except Exception:
        return []
