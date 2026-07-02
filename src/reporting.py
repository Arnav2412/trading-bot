"""
Reporting: format scan + backtest results for console and persist to disk.
"""
from __future__ import annotations

import json
import os

from .utils import now_ist


DISCLAIMER = (
    "EDUCATIONAL DECISION-SUPPORT ONLY - NOT FINANCIAL ADVICE. "
    "Signals are generated from delayed/free data and can be wrong. "
    "You are responsible for every trade. Never risk money you can't afford to lose."
)


def format_console(results: list) -> str:
    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
    lines = [
        "=" * 78,
        f"  BEST TRADING BOT  |  Intraday Signals  |  {ts}",
        "=" * 78,
    ]
    if not results:
        lines.append("  No qualifying setups right now. Patience is a position.")
        lines.append("=" * 78)
        return "\n".join(lines)
    for i, r in enumerate(results, 1):
        s, p = r["signal"], r["plan"]
        f = r.get("fundamentals", {})
        lines.append(
            f"\n  #{i}  {s.symbol:<12} {s.direction:<5}  score {s.score:>5.1f}/100"
            f"   sentiment {s.sentiment:+.2f}"
        )
        lines.append(
            f"      Entry {p.entry:>9.2f} | Stop {p.stop_loss:>9.2f} | "
            f"T1 {p.target1:>9.2f} | T2 {p.target2:>9.2f}"
        )
        lines.append(
            f"      Qty {p.quantity:>5} | Risk Rs.{p.rupees_at_risk:>8.0f} | "
            f"Notional Rs.{p.notional:>10.0f} | R:R {p.reward_risk_t1}/{p.reward_risk_t2}"
        )
        if f:
            fund_str = ", ".join(f"{k}={v}" for k, v in f.items())
            lines.append(f"      Fundamentals: {fund_str}")
        lines.append(f"      Why: {'; '.join(s.reasons[:6])}")
    lines.append("\n" + "-" * 78)
    lines.append("  " + DISCLAIMER)
    lines.append("=" * 78)
    return "\n".join(lines)


def save_report(results: list, cfg: dict) -> str:
    out_dir = _report_dir(cfg)
    stamp = now_ist().strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": now_ist().isoformat(),
        "disclaimer": DISCLAIMER,
        "signals": [
            {
                "symbol": r["signal"].symbol,
                "direction": r["signal"].direction,
                "score": r["signal"].score,
                "sentiment": r["signal"].sentiment,
                "reasons": r["signal"].reasons,
                "plan": r["plan"].as_dict(),
                "fundamentals": r.get("fundamentals", {}),
            }
            for r in results
        ],
    }
    json_path = os.path.join(out_dir, f"signals_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    with open(os.path.join(out_dir, f"signals_{stamp}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(format_console(results))
    # Stable copy: persisted to the repo by the cloud runs, so the published
    # dashboard always has the latest picks even on a fresh runner.
    # (Sorts after signals_2*.json lexically, so _latest_signal_file finds it.)
    with open(os.path.join(out_dir, "signals_latest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return json_path


def _verdict(o: dict) -> str:
    if o.get("trades", 0) < 20:
        return ("Too few trades to trust these numbers. Backtest more symbols / "
                "a longer window before drawing conclusions.")
    exp = o.get("expectancy_R", 0)
    pf = o.get("profit_factor", 0)
    if exp > 0.1 and pf >= 1.3:
        return ("Positive expectancy with a healthy profit factor. PROMISING - "
                "but paper-trade live before risking real money.")
    if exp > 0:
        return ("Marginally positive. Edge is thin and may not survive real "
                "costs/slippage. Tune thresholds; do NOT deploy capital yet.")
    return ("Negative expectancy over this window - this configuration LOSES "
            "money on past data. Do not trade it. Adjust the strategy/filters.")


def format_backtest(result: dict, cfg: dict) -> str:
    o = result["overall"]
    lines = ["=" * 78,
             "  BEST TRADING BOT  |  BACKTEST RESULTS",
             "=" * 78]
    if o.get("trades", 0) == 0:
        lines += ["  No trades were generated over the test window.",
                  "  (Either data was unavailable, or no setup cleared min_score.)",
                  "=" * 78]
        return "\n".join(lines)
    lines += [
        f"  Trades:        {o['trades']:>8}   ( {o['wins']} wins / {o['losses']} losses )",
        f"  Win rate:      {o['win_rate_pct']:>7}%",
        f"  Expectancy:    Rs {o['expectancy_inr']:>9}  per trade   ({o['expectancy_R']} R)",
        f"  Avg win:       Rs {o['avg_win']:>9}",
        f"  Avg loss:      Rs {o['avg_loss']:>9}",
        f"  Profit factor: {o['profit_factor']:>8}",
        f"  Total P&L:     Rs {o['total_pnl_inr']:>9}   ({o['gross_return_pct']}% of capital)",
        f"  Max drawdown:  {o['max_drawdown_pct']:>7}%",
        "-" * 78,
        "  VERDICT: " + _verdict(o),
        "-" * 78,
    ]
    ps = result.get("per_symbol", {})
    active = {k: v for k, v in ps.items() if v.get("trades", 0) > 0}
    if active:
        lines.append("  Per-symbol (symbols with trades):")
        lines.append(f"    {'SYMBOL':<12}{'TRADES':>7}{'WIN%':>7}{'EXP(R)':>9}{'P&L':>12}")
        for sym, st in sorted(active.items(),
                              key=lambda kv: kv[1].get("total_pnl_inr", 0),
                              reverse=True):
            lines.append(
                f"    {sym:<12}{st['trades']:>7}{st['win_rate_pct']:>7}"
                f"{st['expectancy_R']:>9}{st['total_pnl_inr']:>12}")
    lines += ["",
              "  " + DISCLAIMER,
              "  Backtest caveats: free/delayed data, assumes worst-case fills,",
              "  past performance does NOT predict future results.",
              "=" * 78]
    return "\n".join(lines)


def save_backtest(result: dict, cfg: dict) -> str:
    out_dir = _report_dir(cfg)
    stamp = now_ist().strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": now_ist().isoformat(),
        "disclaimer": DISCLAIMER,
        "overall": result["overall"],
        "per_symbol": result["per_symbol"],
        "trades": [t.__dict__ for t in result["trades"]],
    }
    path = os.path.join(out_dir, f"backtest_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    with open(os.path.join(out_dir, f"backtest_{stamp}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(format_backtest(result, cfg))
    return path


def _report_dir(cfg: dict) -> str:
    report_dir = cfg["output"].get("report_dir", "reports")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, report_dir)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir
