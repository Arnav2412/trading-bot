#!/usr/bin/env python3
"""
BEST TRADING BOT - main entry point (v2 refined).

Usage
-----
  python run.py once               # single scan now, print + save report
  python run.py live               # continuous scan loop during market hours
  python run.py symbol RELIANCE    # deep-dive one stock
  python run.py backtest           # backtest the whole universe (~55 days)
  python run.py backtest RELIANCE INFY   # backtest specific symbols
  python run.py email              # scan once and EMAIL the momentum report
  python run.py auto               # scan + email every N hours (config.email.every_hours)
  python run.py paper              # scan, log paper trades, update + show track record
  python run.py web                # launch the local web dashboard (http://127.0.0.1:5000)
  python run.py eod                # email the end-of-day paper P&L (run after 15:45 IST)
  python run.py premarket          # early-AM email: top picks for the day + exit plans
  python run.py publish            # bake the dashboard into docs/ (GitHub Pages)

Everything is configured in config.yaml.
"""
from __future__ import annotations

import sys
import time

from src.config import load_config
from src.scanner import scan, _analyze_symbol
from src.reporting import format_console, save_report, format_backtest, save_backtest
from src.universe import get_universe
from src.backtest import run_backtest, run_swing_backtest
from src.notify.email_report import send_report, send_eod, send_premarket, send_test
from src.paper import tracker
from src.utils import (get_logger, market_is_open, can_take_new_entry,
                       now_ist)

log = get_logger()


def _track(results: list, cfg: dict, lock: bool = False) -> str:
    """Update paper ledger with these picks and return a one-line track record.

    lock=True (pre-market run) FREEZES these as today's official picks; later
    2-hour scans won't add new ones, so the day's evaluation set stays fixed.
    """
    try:
        tracker.update_open(cfg)        # resolve anything that hit stop/target/eod
        tracker.log_signals(results, cfg, lock=lock)  # log today's fresh picks
        return tracker.format_summary(cfg)
    except Exception as exc:
        log.warning("Paper-tracker step failed: %s", exc)
        return ""


def run_once(cfg: dict) -> list:
    results = scan(cfg)
    if cfg["output"].get("console", True):
        print(format_console(results))
    path = save_report(results, cfg)
    log.info("Report saved -> %s", path)
    return results


def run_live(cfg: dict) -> None:
    interval = cfg["schedule"].get("scan_interval_seconds", 300)
    log.info("Live mode. Scanning every %ds during market hours (IST).", interval)
    while True:
        now = now_ist()
        if not market_is_open(cfg, now):
            log.info("Market closed (%s). Sleeping 5 min.", now.strftime("%H:%M"))
            time.sleep(300)
            continue
        if not can_take_new_entry(cfg, now):
            log.info("Past new-entry cutoff. Monitoring only; no fresh signals.")
        run_once(cfg)
        time.sleep(interval)


def run_symbol(cfg: dict, symbol: str) -> None:
    res = _analyze_symbol(symbol.upper(), cfg)
    if not res:
        print(f"No qualifying intraday setup for {symbol.upper()} right now.")
        return
    print(format_console([res]))


def run_bt(cfg: dict, symbols: list) -> None:
    symbols = symbols or get_universe(cfg)
    if cfg.get("mode") == "swing":
        cap = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
        log.info("Swing-backtesting %d symbol(s) on daily data...", len(symbols))
        r = run_swing_backtest(cfg, symbols, cap)
        print("\n" + "=" * 70)
        print("  SWING BACKTEST (Rs %s per trade)" % f"{cap:,}")
        print("=" * 70)
        if r.get("trades", 0) == 0:
            print("  No trades generated (need data / looser thresholds).")
        else:
            print(f"  Trades:        {r['trades']}")
            print(f"  Win rate:      {r['win_rate_pct']}%")
            print(f"  Avg return:    {r['avg_return_pct']}% per trade  (expectancy)")
            print(f"  Avg win/loss:  +{r['avg_win_pct']}% / {r['avg_loss_pct']}%")
            print(f"  Best/Worst:    +{r['best_pct']}% / {r['worst_pct']}%")
            print(f"  Total P&L:     Rs {r['total_pnl_per_trade_capital']:,} (Rs {cap:,}/trade)")
            verdict = ("PROMISING - paper-trade it." if r['avg_return_pct'] > 0.5 else
                       "Edge is thin/negative - tune before trusting." )
            print("  VERDICT: " + verdict)
        print("=" * 70)
        return
    log.info("Backtesting %d symbol(s) over ~55 days of %s data...",
             len(symbols), cfg["data"]["interval"])
    result = run_backtest(cfg, symbols)
    print(format_backtest(result, cfg))
    path = save_backtest(result, cfg)
    log.info("Backtest report saved -> %s", path)


def run_email(cfg: dict) -> None:
    results = run_once(cfg)
    track_line = _track(results, cfg)
    status = send_report(results, cfg, track_line=track_line)
    log.info("Email step: %s | %s", status, track_line)


def run_auto(cfg: dict) -> None:
    every_h = float(cfg.get("email", {}).get("every_hours", 2))
    period = every_h * 3600
    log.info("Auto mode: scan + email every %.1f h during market hours (IST).", every_h)
    while True:
        now = now_ist()
        if market_is_open(cfg, now):
            run_email(cfg)
            log.info("Sleeping %.1f h until next report.", every_h)
            time.sleep(period)
        else:
            log.info("Market closed (%s). Checking again in 15 min.", now.strftime("%H:%M"))
            time.sleep(900)


def run_testmail(cfg: dict) -> None:
    log.info("Sending a test email (no scan)...")
    status = send_test(cfg)
    log.info("Test email result: %s", status)
    if status == "sent":
        print("\n  SUCCESS: email sent. Check your inbox (and spam the first time).\n")
    elif status == "disabled":
        print("\n  Email is OFF. Set email.enabled: true in config.yaml.\n")
    elif str(status).endswith(".html"):
        print("\n  DRY RUN: creds not found. Make sure gmail_login.txt exists with 2 lines.\n")
    else:
        print("\n  Status:", status, "\n")


def run_premarket(cfg: dict) -> None:
    # STICKY LOCK: once today is locked, re-running does NOT change the picks
    # (so the day's set truly stays fixed). Use `premarket force` to override.
    force = len(sys.argv) > 2 and sys.argv[2].lower() == "force"
    # GUARD: locking only makes sense AFTER the open (entries must be live
    # prices, not yesterday's close). The scheduled 09:40 run is fine; a manual
    # 7 AM run is not.
    if not market_is_open(cfg) and not force:
        msg = ("Market is closed - NOT locking picks on stale prices. "
               "Run after 09:15 IST (the scheduled 09:40 lock handles this), "
               "or use `premarket force` to override.")
        log.warning(msg)
        print("\n  " + msg + "\n")
        return
    existing = tracker.locked_symbols_today(cfg)
    if existing and not force:
        msg = ("Today is already locked with %d picks - NOT changing them: %s"
               % (len(existing), ", ".join(existing)))
        log.info(msg)
        print("\n  " + msg)
        print("  (To deliberately re-lock with a fresh scan: python run.py premarket force)\n")
        return
    # News matters most here, so force sentiment ranking on for this run.
    cfg = dict(cfg); cfg["filters"] = dict(cfg["filters"]); cfg["filters"]["use_sentiment"] = True
    results = scan(cfg)
    top = _concentrate_core(results, cfg)  # high-conviction + regime-aligned only
    save_report(top, cfg)                  # persist so the website shows the same picks
    _track(top, cfg, lock=True)            # LOCK these as today's official picks
    status = send_premarket(top, cfg)
    log.info("Pre-market email: %s | %d core picks (concentrated)", status, len(top))


def _concentrate_core(results: list, cfg: dict) -> list:
    """Pick the LOCKED morning core: the highest-conviction setups only. Scores
    already carry the counter-trend penalty from the scanner, so a counter-trend
    pick CAN make the cut if it's strong enough - it just had to earn it. Weak
    trend-fighters fall below the bar and drop out."""
    sc = cfg.get("swing", {})
    core_min = sc.get("core_min_score", 55)
    n = cfg.get("simulation", {}).get("premarket_count", 12)
    strong = sorted([r for r in results if r["signal"].score >= core_min],
                    key=lambda r: r["signal"].score, reverse=True)
    if len(strong) >= n:
        return strong[:n]
    # QUALITY OVER QUANTITY: top up with next-best names only to a small
    # minimum (5). A short list of real setups beats a padded list of filler
    # picks that dilute the winners.
    floor_n = min(5, n)
    if len(strong) >= floor_n:
        return strong
    rest = sorted([r for r in results if r["signal"].score < core_min],
                  key=lambda r: r["signal"].score, reverse=True)
    return (strong + rest)[:floor_n]


def run_eod(cfg: dict) -> None:
    from src.paper import tracker
    from src import learn
    tracker.update_open(cfg)                       # resolve the day's trades first
    mtm = tracker.marktomarket(cfg)                # final core scorecard
    tracker.peak_today(cfg, max_age_sec=0)         # force a final full-basket peak calc
    tune = learn.autotune(cfg)                     # review losers + safe self-tune
    rec = tracker.snapshot_history(cfg, notes=learn.summarize(tune))  # store the day
    status = send_eod(cfg, tune=tune)              # email with peak + review
    peak = tracker.get_peak(cfg).get("peak_pct")
    log.info("EOD: %s | %.2f%% close, peak %.2f%% | %s",
             status, mtm["pct"], peak if peak is not None else mtm["pct"],
             learn.summarize(tune))


def run_web(cfg: dict) -> None:
    from src.web.app import create_app
    app = create_app()
    log.info("Web dashboard at http://127.0.0.1:5000  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5000, debug=False)


def run_locktoday(cfg: dict) -> None:
    """Lock today's PRE-MARKET batch (the picks emailed around the open) as the
    core scorecard, and move any later/extra picks to 'New today'. NON-DESTRUCTIVE
    - nothing is deleted, just re-tagged. From tomorrow the 9:10 run does this
    automatically, so this is only a one-time correction.
    """
    import pandas as pd
    df = tracker.load(cfg)
    if df.empty:
        print("No paper trades yet - nothing to lock.")
        return
    today = now_ist().strftime("%Y-%m-%d")
    tmask = df["logged_at"].astype(str).str.startswith(today)
    tdf = df[tmask].copy()
    if tdf.empty:
        print("No picks logged today to lock.")
        return
    n = cfg.get("simulation", {}).get("premarket_count", 10)
    tdf["_ts"] = pd.to_datetime(tdf["logged_at"], errors="coerce")
    # The pre-market run fires around the open - take the FIRST batch logged at or
    # after 08:00 IST, skipping any pre-dawn test scans (e.g. a 00:05 run).
    morning = tdf[tdf["_ts"].dt.hour >= 8]
    src = morning if len(morning) else tdf
    first_ts = src["_ts"].min()
    core = list(dict.fromkeys(src[src["_ts"] == first_ts]["symbol"]))[:n]
    tracker.write_lock(cfg, core)
    # Re-tag (don't delete): core picks = scorecard, everything else today = intraday.
    df.loc[tmask & (~df["symbol"].isin(core)), "tag"] = "intraday"
    df.loc[tmask & (df["symbol"].isin(core)), "tag"] = "core"
    tracker.save(df, cfg)
    moved = int((tmask & (~df["symbol"].isin(core))).sum())
    print("=" * 70)
    print(f"  Locked {len(core)} pre-market core pick(s) for today (logged {first_ts}):")
    print("   " + ", ".join(core))
    print(f"  Moved {moved} later pick(s) to 'New today' (nothing deleted).")
    if first_ts is not None and getattr(first_ts, "hour", 9) < 8:
        print("  NOTE: only a pre-dawn batch was found - your 9:10 picks may have")
        print("  been pruned earlier. Tomorrow's auto-lock will be correct regardless.")
    print("  Restart the web server (Ctrl+C, then python run.py web) to see it.")
    print("=" * 70)


def run_paper(cfg: dict) -> None:
    results = run_once(cfg)
    line = _track(results, cfg)
    print("\n" + "=" * 78)
    print("  PAPER TRADING TRACK RECORD")
    print("=" * 78)
    print("  " + (line or "No paper trades yet."))
    print(f"  Ledger: reports/paper_trades.csv")
    print("=" * 78)


def main() -> None:
    cfg = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        run_once(cfg)
    elif mode == "live":
        run_live(cfg)
    elif mode == "symbol" and len(sys.argv) > 2:
        run_symbol(cfg, sys.argv[2])
    elif mode == "backtest":
        run_bt(cfg, [s.upper() for s in sys.argv[2:]])
    elif mode == "email":
        run_email(cfg)
    elif mode == "auto":
        run_auto(cfg)
    elif mode == "paper":
        run_paper(cfg)
    elif mode == "locktoday":
        run_locktoday(cfg)
    elif mode == "web":
        run_web(cfg)
    elif mode == "eod":
        run_eod(cfg)
    elif mode == "premarket":
        run_premarket(cfg)
    elif mode == "publish":
        from src.publish import publish
        publish(cfg)
    elif mode == "testmail":
        run_testmail(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
