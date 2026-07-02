"""
Scanner: run the full pipeline across the universe and rank actionable signals (v2).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .analysis import signals as sig_engine
from .data import market_data, fundamentals, sentiment
from .data.fundamentals import passes_quality_filter
from .risk.position_sizing import build_trade_plan, build_swing_plan
from .universe import get_universe
from .utils import get_logger

log = get_logger()


def _analyze_symbol(symbol: str, cfg: dict) -> dict | None:
    df = market_data.get_intraday(symbol, cfg)
    if df.empty or len(df) < 50:
        return None

    trend_dir = None
    if cfg["signals"].get("require_trend_alignment", True):
        tdf = market_data.get_intraday(symbol, cfg, interval=cfg["data"]["trend_interval"])
        if not tdf.empty:
            trend_dir = sig_engine.higher_tf_trend(tdf)

    signal = sig_engine.evaluate(symbol, df, cfg, trend_dir=trend_dir)
    if signal.direction == "NONE" or not signal.trend_ok:
        return None
    if signal.score < cfg["signals"]["min_score"]:
        return None

    ok, fund = passes_quality_filter(symbol, cfg)
    if not ok:
        return None

    sent = 0.0
    if cfg["filters"].get("use_sentiment", False):
        sent = sentiment.headline_sentiment(symbol)
        if signal.direction == "LONG":
            signal.score = min(100, signal.score + sent * 5)
        else:
            signal.score = min(100, signal.score - sent * 5)
    signal.sentiment = sent

    plan = build_trade_plan(symbol, signal.direction, signal.price, signal.atr, cfg)
    if plan is None:
        return None
    return {"signal": signal, "plan": plan, "fundamentals": fund, "mode": "intraday"}


def _analyze_symbol_swing(symbol: str, cfg: dict) -> dict | None:
    sc = cfg.get("swing", {})
    df = market_data.fetch_yfinance(symbol, sc.get("interval", "1d"),
                                    sc.get("lookback_days", 400))
    if df.empty or len(df) < 60:
        return None
    signal = sig_engine.evaluate_swing(symbol, df, cfg)
    if signal.direction == "NONE":
        return None
    floor = sc.get("score_floor", 40)
    if signal.score < floor:
        return None
    ok, fund = passes_quality_filter(symbol, cfg)
    if not ok:
        return None
    sent = 0.0
    if cfg["filters"].get("use_sentiment", False):
        sent = sentiment.headline_sentiment(symbol)
        signal.score = min(100, signal.score + (sent * 5 if signal.direction == "LONG"
                                                 else -sent * 5))
    signal.sentiment = sent
    plan = build_swing_plan(symbol, signal.direction, signal.price, cfg,
                            atr=signal.atr)
    if plan is None:
        return None
    high_conv = signal.score >= sc.get("min_score", 50)
    return {"signal": signal, "plan": plan, "fundamentals": fund,
            "mode": "swing", "high_conviction": high_conv}


def scan(cfg: dict, max_workers: int = 6) -> list[dict]:
    universe = get_universe(cfg)
    results: list[dict] = []
    mode = cfg.get("mode", "intraday")
    analyzer = _analyze_symbol_swing if mode == "swing" else _analyze_symbol
    if mode != "swing" and not market_data.quick_probe(cfg):
        log.warning("No usable intraday data right now (market closed or data "
                    "source unavailable). Skipping scan.")
        return []
    regime = 0
    if mode == "swing" and cfg.get("swing", {}).get("use_market_regime", False):
        regime = market_data.market_regime(cfg)
        if regime:
            log.info("Market regime: %s", "BULLISH (longs favoured)" if regime > 0
                     else "BEARISH (shorts favoured)")
    log.info("Scanning %d symbols (%s mode)...", len(universe), mode)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(analyzer, s, cfg): s for s in universe}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
            except Exception as exc:
                log.warning("Error analyzing %s: %s", sym, exc)

    # Market-regime handling: counter-trend trades are ALLOWED (a weak stock can
    # fall in an up-market), but they must earn it - we dock their score so only
    # genuinely strong counter-trend setups survive the ranking. Aligned trades
    # get a small nudge. This keeps the freedom to short into a rally when the
    # individual setup is strong, without casually fighting the tape.
    if mode == "swing" and regime:
        pen = float(cfg.get("swing", {}).get("counter_trend_penalty", 10))
        for r in results:
            d = r["signal"].direction
            aligned = (regime > 0 and d == "LONG") or (regime < 0 and d == "SHORT")
            if aligned:
                r["signal"].reasons.append("Aligned with market regime")
            else:
                r["signal"].score = max(0.0, r["signal"].score - pen)
                r["signal"].reasons.append(
                    f"Counter-trend vs market (needs extra conviction; -{pen:.0f} score)")

    # Relative-strength boost (swing): reward the biggest movers across the
    # whole universe so the list leans toward "max return" names.
    if mode == "swing" and results:
        ranked = sorted(results,
                        key=lambda r: abs(r["signal"].factors.get("roc20", 0)),
                        reverse=True)
        top_n = max(1, len(ranked) // 4)
        for i, r in enumerate(ranked):
            if i < top_n:
                r["signal"].score = min(100, r["signal"].score + 6)
                r["signal"].reasons.append("Top relative strength (universe)")

    # Freshness: gently prefer NEW names over ones already picked in the last few
    # days, so the list keeps some variety instead of the same recurring tickers.
    if mode == "swing" and results:
        sc = cfg.get("swing", {})
        fpen = float(sc.get("freshness_penalty", 0) or 0)
        if fpen:
            try:
                from .paper import tracker
                recent = tracker.recent_pick_symbols(cfg, sc.get("recency_days", 2))
            except Exception:
                recent = set()
            for r in results:
                if r["signal"].symbol in recent:
                    r["signal"].score = max(0.0, r["signal"].score - fpen)
                    r["signal"].reasons.append(
                        f"Picked recently (-{fpen:.0f} to favour fresh names)")

    results.sort(key=lambda r: (r["signal"].score, r["plan"].reward_risk_t2),
                 reverse=True)
    max_pos = cfg["account"].get("max_open_positions", 12)
    return results[:max_pos]
