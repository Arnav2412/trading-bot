"""
EOD auto-tuner: nudges the strategy's own thresholds based on the day's
post-mortem, so the bot adapts over time.

SAFE BY DESIGN:
  * at most ONE small step per knob per day (anti-overfitting),
  * every value CLAMPED to the bounds in config.yaml -> learning:,
  * every change appended to reports/learning_log.csv (full audit trail),
  * live overrides kept in reports/learning.json (delete it to fully reset).

Knobs it can move:
  - min_score      : how strong a setup must be to qualify (raise = pickier)
  - regime_buffer  : how far the Nifty must clear its 50-EMA before we trust the
                     trend (raise = stop trading against an unclear market)
"""
from __future__ import annotations

import csv
import json
import os

from .utils import now_ist, get_logger
from .paper import tracker

log = get_logger()


def _paths(cfg: dict):
    report_dir = cfg.get("output", {}).get("report_dir", "reports")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, report_dir)
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, "learning.json"), os.path.join(out, "learning_log.csv")


def _load_state(cfg: dict) -> dict:
    jpath, _ = _paths(cfg)
    state = {"min_score": cfg.get("swing", {}).get("min_score", 50),
             "regime_buffer": cfg.get("swing", {}).get("regime_buffer", 0.0),
             "counter_trend_penalty": cfg.get("swing", {}).get("counter_trend_penalty", 10)}
    if os.path.exists(jpath):
        try:
            with open(jpath, "r", encoding="utf-8") as fh:
                state.update(json.load(fh))
        except Exception:
            pass
    return state


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def autotune(cfg: dict) -> dict:
    """Review today and apply safe, logged threshold nudges. Returns a summary."""
    lc = cfg.get("learning", {})
    review = tracker.review_today(cfg)
    if not lc.get("enabled", False):
        return {"applied": [], "review": review, "skipped": "learning disabled"}
    if review.get("n", 0) == 0:
        return {"applied": [], "review": review, "skipped": "no picks today"}

    jpath, logpath = _paths(cfg)
    # Idempotent: only tune once per day, so re-running `eod` is safe.
    if os.path.exists(logpath):
        try:
            with open(logpath, "r", encoding="utf-8") as fh:
                if any(line.startswith(review["date"] + ",") for line in fh):
                    return {"applied": [], "review": review,
                            "skipped": "already tuned today"}
        except Exception:
            pass
    state = _load_state(cfg)
    sb = lc.get("min_score_bounds", [44, 64])
    rb = lc.get("regime_buffer_bounds", [0.0, 3.0])
    cb = lc.get("counter_trend_penalty_bounds", [0, 25])
    n, losers, winners = review["n"], review["losers"], review["winners"]
    wr = (winners / n * 100) if n else 0.0
    changes = []

    # 1) Counter-trend trades kept losing -> raise the bar they must clear (we
    #    penalise counter-trend picks harder, but still don't ban them outright).
    if losers and review["fought_regime"] >= max(1, losers // 2):
        old = state["counter_trend_penalty"]
        new = _clamp(old + 2, cb[0], cb[1])
        if new != old:
            changes.append(("counter_trend_penalty", old, new,
                            "counter-trend picks kept losing"))
            state["counter_trend_penalty"] = new

    # 2) Stop-heavy day, weak win rate, OR clear evidence that winners scored
    #    higher than losers -> demand stronger setups.
    edge = review.get("edge", {})
    wa, la = edge.get("win_avg_score"), edge.get("los_avg_score")
    score_gap = (wa is not None and la is not None and wa - la >= 4)
    if (losers and review["stops"] >= max(1, losers // 2)) or wr < 40 or score_gap:
        old = state["min_score"]
        new = _clamp(old + 1, sb[0], sb[1])
        if new != old:
            why = (f"winners scored {wa} vs losers {la}" if score_gap
                   else f"win rate {wr:.0f}% / stop-heavy")
            changes.append(("min_score", old, new, why))
            state["min_score"] = new
    # 3) Strong day -> relax slightly to surface a few more ideas tomorrow.
    elif wr >= 65:
        old = state["min_score"]
        new = _clamp(old - 1, sb[0], sb[1])
        if new != old:
            changes.append(("min_score", old, new, f"strong day (win {wr:.0f}%)"))
            state["min_score"] = new

    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    if changes:
        new_file = not os.path.exists(logpath)
        with open(logpath, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(["date", "knob", "old", "new", "reason"])
            for k, o, nv, why in changes:
                w.writerow([review["date"], k, o, nv, why])
        log.info("Auto-tune: %d change(s) %s", len(changes),
                 [(k, o, nv) for k, o, nv, _ in changes])
    else:
        log.info("Auto-tune: no change needed today.")

    return {"applied": [{"knob": k, "old": o, "new": nv, "reason": why}
                        for k, o, nv, why in changes],
            "review": review, "state": state}


def summarize(result: dict) -> str:
    """One short paragraph for the EOD email / dashboard."""
    rv = result.get("review") or {}
    if not rv or rv.get("n", 0) == 0:
        return "No picks today, so nothing to learn from."
    parts = [f"Reviewed {rv['n']} picks: {rv['winners']} up, {rv['losers']} down "
             f"({rv['pct']:+.2f}% on the day)."]
    edge = rv.get("edge", {})
    if edge.get("win_avg_score") is not None and edge.get("los_avg_score") is not None:
        parts.append(f"Winners avg score {edge['win_avg_score']} vs losers "
                     f"{edge['los_avg_score']}; winners {edge['win_aligned_pct']}% "
                     f"trend-aligned vs losers {edge['los_aligned_pct']}%.")
    for p in rv.get("patterns", []):
        parts.append(p)
    applied = result.get("applied", [])
    if applied:
        ch = "; ".join(f"{a['knob']} {a['old']}->{a['new']} ({a['reason']})" for a in applied)
        parts.append("Auto-tuned for tomorrow: " + ch + ".")
    else:
        parts.append("No threshold change needed - today was within normal range.")
    return " ".join(parts)
