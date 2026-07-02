"""
Event-driven, walk-forward backtester for the intraday signal engine.

Design goals
------------
* NO lookahead bias. A signal is computed on the CLOSE of bar i using only
  data up to bar i, and the trade is ENTERED at the OPEN of bar i+1.
* Reuses the exact live scoring logic (`signals.evaluate_indicatored`) so the
  backtest measures the same strategy you'll trade.
* Intraday rules: at most one open position per symbol; every position is
  squared off at the last candle of its trading day.
* Conservative intrabar fills: if a single bar touches BOTH stop and target,
  we assume the STOP filled first (worst case).
* Realistic costs: round-trip cost in basis points (brokerage + slippage).

Outputs per-trade records and aggregate stats: win rate, expectancy (in R and
INR), profit factor, gross return %, and max drawdown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .analysis import indicators as ind
from .analysis import signals as sig
from .data import market_data
from .risk.position_sizing import build_trade_plan
from .utils import get_logger  # noqa: F401

log = get_logger()


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_time: str
    entry: float
    exit_time: str
    exit: float
    qty: int
    stop: float
    target: float
    pnl: float            # net of costs, INR
    r_multiple: float     # pnl in units of initial risk
    outcome: str          # "target" | "stop" | "eod"


def _trend_series(df: pd.DataFrame, higher_tf: str) -> pd.Series:
    """Higher-timeframe trend (+1/-1/0) aligned to the base index via ffill.
    Built by resampling the base frame, so it uses only closed higher-TF bars."""
    rule = {"15m": "15min", "30m": "30min", "60m": "60min", "1h": "60min"}.get(
        higher_tf, "15min")
    agg = df.resample(rule, label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna()
    if len(agg) < 50:
        return pd.Series(0, index=df.index)
    h = ind.add_all(agg)
    trend = pd.Series(0, index=h.index)
    up = (h["ema21"] > h["ema50"]) & (h["close"] > h["ema21"])
    dn = (h["ema21"] < h["ema50"]) & (h["close"] < h["ema21"])
    trend[up] = 1
    trend[dn] = -1
    # shift(1): only use a higher-TF bar AFTER it has closed (no lookahead)
    return trend.shift(1).reindex(df.index, method="ffill").fillna(0)


def backtest_symbol(symbol: str, cfg: dict, days: int = 55,
                    cost_bps: float = 5.0) -> tuple[list[Trade], pd.DataFrame]:
    """Run the walk-forward backtest for one symbol. Returns (trades, base_df)."""
    interval = cfg["data"]["interval"]
    df = market_data.fetch_yfinance(symbol, interval, days)
    if df.empty or len(df) < 80:
        log.warning("Not enough data to backtest %s", symbol)
        return [], df

    df = ind.add_all(df)
    use_trend = cfg["signals"].get("require_trend_alignment", True)
    trend = (_trend_series(df, cfg["data"]["trend_interval"])
             if use_trend else pd.Series(0, index=df.index))

    day_id = df.index.normalize()
    min_score = cfg["signals"]["min_score"]
    n = len(df)
    trades: list[Trade] = []

    i = 50
    while i < n - 1:
        row_slice = df.iloc[: i + 1]
        tdir = int(trend.iloc[i]) if use_trend else None
        s = sig.evaluate_indicatored(symbol, row_slice, cfg, trend_dir=tdir)
        if s.direction in ("LONG", "SHORT") and s.score >= min_score and s.trend_ok:
            entry_idx = i + 1
            entry_px = float(df["open"].iloc[entry_idx])
            plan = build_trade_plan(symbol, s.direction, entry_px,
                                    float(df["atr"].iloc[i]), cfg)
            if plan is None:
                i += 1
                continue
            trade = _simulate_trade(df, day_id, entry_idx, plan, s.direction, cost_bps)
            if trade:
                trades.append(trade)
                # jump past the trade's exit bar (no overlapping positions)
                i = _index_of_time(df, trade.exit_time) + 1
                continue
        i += 1

    return trades, df


def _index_of_time(df: pd.DataFrame, ts_iso: str) -> int:
    return int(df.index.get_indexer([pd.Timestamp(ts_iso)])[0])


def _simulate_trade(df: pd.DataFrame, day_id: pd.Series, entry_idx: int,
                    plan, direction: str, cost_bps: float) -> Trade | None:
    n = len(df)
    entry_px = plan.entry
    qty = plan.quantity
    stop = plan.stop_loss
    target = plan.target1          # primary booking target (conservative)
    entry_day = day_id[entry_idx]
    cost = (entry_px + target) * qty * (cost_bps / 10000.0)  # rough round trip

    for j in range(entry_idx, n):
        bar = df.iloc[j]
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        eod = (j == n - 1) or (day_id[j + 1] != entry_day)

        if direction == "LONG":
            if lo <= stop:                     # worst-case: stop first
                return _close(plan, df, entry_idx, j, stop, "stop", cost, direction)
            if hi >= target:
                return _close(plan, df, entry_idx, j, target, "target", cost, direction)
        else:  # SHORT
            if hi >= stop:
                return _close(plan, df, entry_idx, j, stop, "stop", cost, direction)
            if lo <= target:
                return _close(plan, df, entry_idx, j, target, "target", cost, direction)

        if eod:                                # square-off at this bar's close
            return _close(plan, df, entry_idx, j, close, "eod", cost, direction)
    return None


def _close(plan, df, entry_idx, j, exit_px, outcome, cost, direction) -> Trade:
    qty = plan.quantity
    sign = 1 if direction == "LONG" else -1
    gross = (exit_px - plan.entry) * qty * sign
    pnl = gross - cost
    risk = plan.rupees_at_risk or 1.0
    return Trade(
        symbol=plan.symbol, direction=direction,
        entry_time=df.index[entry_idx].isoformat(),
        entry=plan.entry, exit_time=df.index[j].isoformat(), exit=exit_px,
        qty=qty, stop=plan.stop_loss, target=plan.target1,
        pnl=round(pnl, 2), r_multiple=round(pnl / risk, 2), outcome=outcome,
    )


def aggregate(trades: list[Trade], cfg: dict) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = np.array([t.pnl for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    capital = float(cfg["account"]["capital"])

    equity = capital + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    profit_factor = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")

    return {
        "trades": len(trades),
        "wins": int((pnls > 0).sum()),
        "losses": int((pnls <= 0).sum()),
        "win_rate_pct": round(100 * (pnls > 0).mean(), 1),
        "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.mean(), 2) if len(losses) else 0.0,
        "expectancy_inr": round(pnls.mean(), 2),
        "expectancy_R": round(rs.mean(), 3),
        "profit_factor": round(profit_factor, 2),
        "total_pnl_inr": round(pnls.sum(), 2),
        "gross_return_pct": round(100 * pnls.sum() / capital, 2),
        "max_drawdown_pct": round(100 * dd.min(), 2),
    }


def run_backtest(cfg: dict, symbols: list[str], days: int = 55,
                 cost_bps: float = 5.0) -> dict:
    all_trades: list[Trade] = []
    per_symbol: dict[str, dict] = {}
    for sym in symbols:
        trades, _ = backtest_symbol(sym, cfg, days=days, cost_bps=cost_bps)
        all_trades.extend(trades)
        per_symbol[sym] = aggregate(trades, cfg)
        log.info("%-12s trades=%-3d win%%=%-5s expR=%-6s",
                 sym, per_symbol[sym].get("trades", 0),
                 per_symbol[sym].get("win_rate_pct", "-"),
                 per_symbol[sym].get("expectancy_R", "-"))
    return {
        "overall": aggregate(all_trades, cfg),
        "per_symbol": per_symbol,
        "trades": all_trades,
    }


# =========================================================================== #
#  SWING backtest (daily bars) - measure the strategy's real returns
# =========================================================================== #
def backtest_swing_symbol(symbol: str, cfg: dict, capital_per_trade: float = 10000,
                          regime_series=None):
    """Walk daily bars: enter on a swing signal, exit on stop / T1 / max-hold.
    Returns a list of trade dicts with % return and Rs P&L on capital_per_trade."""
    from .analysis import signals as sig
    from .risk.position_sizing import build_swing_plan
    sc = cfg.get("swing", {})
    df = market_data.fetch_yfinance(symbol, sc.get("interval", "1d"),
                                    sc.get("lookback_days", 400))
    if df.empty or len(df) < 130:
        return []
    floor = sc.get("min_score", 50)
    max_hold = int(sc.get("max_hold_days", 15))
    n = len(df)
    trades, i = [], 120
    while i < n - 1:
        s = sig.evaluate_swing(symbol, df.iloc[: i + 1], cfg)
        ok_regime = True
        if regime_series is not None and len(regime_series):
            try:
                reg = int(regime_series.get(df.index[i].normalize(), 0))
            except Exception:
                reg = 0
            if reg > 0 and s.direction == "SHORT":
                ok_regime = False
            if reg < 0 and s.direction == "LONG":
                ok_regime = False
        if s.direction in ("LONG", "SHORT") and s.score >= floor and ok_regime:
            entry = float(df["open"].iloc[i + 1])
            plan = build_swing_plan(symbol, s.direction, entry, cfg, atr=s.atr)
            if plan:
                from .risk.position_sizing import walk_exit
                long = s.direction == "LONG"
                fwd = df.iloc[i + 1: min(n, i + 1 + max_hold + 1)]
                res = walk_exit(fwd, s.direction, entry, plan.stop_loss,
                                plan.target1, plan.target2, max_hold)
                if res is None:   # ran out of data still open -> mark at close
                    j = min(n, i + 1 + max_hold) - 1
                    exit_px, outcome = float(df["close"].iloc[j]), "timeout"
                else:
                    exit_px, _ts, outcome = res
                    try:
                        j = i + 1 + fwd.index.get_loc(pd.Timestamp(_ts))
                    except Exception:
                        j = i + 1 + len(fwd) - 1
                sign = 1 if long else -1
                ret = (exit_px - entry) / entry * sign * 100
                qty = capital_per_trade / entry
                trades.append({"symbol": symbol, "direction": s.direction,
                               "entry": round(entry, 2), "exit": round(exit_px, 2),
                               "ret_pct": round(ret, 2),
                               "pnl": round((exit_px - entry) * qty * sign, 2),
                               "outcome": outcome})
                i = j + 1
                continue
        i += 1
    return trades


def run_swing_backtest(cfg: dict, symbols: list, capital_per_trade: float = 10000) -> dict:
    import numpy as np
    regime_series = None
    if cfg.get("swing", {}).get("use_market_regime", False):
        regime_series = market_data.nifty_regime_series(cfg)
    all_t = []
    for sym in symbols:
        t = backtest_swing_symbol(sym, cfg, capital_per_trade, regime_series)
        all_t.extend(t)
        log.info("%-12s swing trades=%d", sym, len(t))
    if not all_t:
        return {"trades": 0}
    rets = np.array([t["ret_pct"] for t in all_t])
    pnls = np.array([t["pnl"] for t in all_t])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    return {
        "trades": len(all_t),
        "win_rate_pct": round(100 * (rets > 0).mean(), 1),
        "avg_return_pct": round(rets.mean(), 2),
        "avg_win_pct": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss_pct": round(losses.mean(), 2) if len(losses) else 0.0,
        "best_pct": round(rets.max(), 2), "worst_pct": round(rets.min(), 2),
        "total_pnl_per_trade_capital": round(pnls.sum(), 2),
        "expectancy_pct": round(rets.mean(), 2),
    }
