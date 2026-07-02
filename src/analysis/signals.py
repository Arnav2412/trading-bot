"""
Signal engine: converts indicators into a scored, directional intraday signal.

No single indicator is reliable. We combine several independent edges into a
weighted composite score (0-100) for each direction. A signal is only emitted
when the score clears `min_score` AND the higher-timeframe trend agrees (if
required). Each contributing factor is recorded so every call is explainable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import indicators as ind


WEIGHTS = {
    "ema_stack": 18, "vwap": 14, "supertrend": 16, "macd": 14,
    "rsi": 10, "adx": 12, "volume": 10, "pullback": 6,
}


@dataclass
class Signal:
    symbol: str
    direction: str
    score: float
    price: float
    atr: float
    reasons: list = field(default_factory=list)
    factors: dict = field(default_factory=dict)
    trend_ok: bool = True
    sentiment: float = 0.0


def _direction_from_factors(bull: float, bear: float) -> str:
    if bull >= bear and bull > 0:
        return "LONG"
    if bear > bull and bear > 0:
        return "SHORT"
    return "NONE"


def evaluate(symbol: str, df: pd.DataFrame, cfg: dict,
             trend_dir=None) -> Signal:
    """Score the latest candle. `trend_dir` from higher TF: +1 up, -1 down."""
    df = ind.add_all(df)
    return evaluate_indicatored(symbol, df, cfg, trend_dir)


def evaluate_indicatored(symbol: str, df: pd.DataFrame, cfg: dict,
                         trend_dir=None) -> Signal:
    """Same as evaluate() but assumes indicators are already attached.
    Used by the backtester to avoid recomputing indicators every bar."""
    if df.empty or len(df) < 50 or "ema50" not in df:
        return Signal(symbol, "NONE", 0.0, 0.0, 0.0, ["insufficient data"])

    last = df.iloc[-1]
    price = float(last["close"])
    atr_v = float(last["atr"]) if last["atr"] == last["atr"] else 0.0

    bull = 0.0
    bear = 0.0
    factors: dict = {}
    reasons: list = []

    if last["ema9"] > last["ema21"] > last["ema50"]:
        bull += WEIGHTS["ema_stack"]; factors["ema_stack"] = "+bull"
        reasons.append("EMA 9>21>50 (uptrend)")
    elif last["ema9"] < last["ema21"] < last["ema50"]:
        bear += WEIGHTS["ema_stack"]; factors["ema_stack"] = "+bear"
        reasons.append("EMA 9<21<50 (downtrend)")

    if price > last["vwap"]:
        bull += WEIGHTS["vwap"]; reasons.append("Above VWAP")
    elif price < last["vwap"]:
        bear += WEIGHTS["vwap"]; reasons.append("Below VWAP")

    if last["st_dir"] == 1:
        bull += WEIGHTS["supertrend"]; reasons.append("Supertrend up")
    elif last["st_dir"] == -1:
        bear += WEIGHTS["supertrend"]; reasons.append("Supertrend down")

    hist = last["macd_hist"]
    hist_prev = df["macd_hist"].iloc[-2]
    if hist > 0 and hist >= hist_prev:
        bull += WEIGHTS["macd"]; reasons.append("MACD bullish & rising")
    elif hist < 0 and hist <= hist_prev:
        bear += WEIGHTS["macd"]; reasons.append("MACD bearish & falling")
    elif hist > 0:
        bull += WEIGHTS["macd"] * 0.5
    elif hist < 0:
        bear += WEIGHTS["macd"] * 0.5

    rsi_v = last["rsi"]
    if 50 <= rsi_v < 70:
        bull += WEIGHTS["rsi"]; reasons.append(f"RSI {rsi_v:.0f} bullish")
    elif 30 < rsi_v <= 50:
        bear += WEIGHTS["rsi"]; reasons.append(f"RSI {rsi_v:.0f} bearish")
    elif rsi_v >= 70:
        reasons.append(f"RSI {rsi_v:.0f} overbought (caution)")
    elif rsi_v <= 30:
        reasons.append(f"RSI {rsi_v:.0f} oversold (caution)")

    adx_v = last["adx"]
    if adx_v >= 25:
        if bull >= bear:
            bull += WEIGHTS["adx"]
        else:
            bear += WEIGHTS["adx"]
        reasons.append(f"ADX {adx_v:.0f} strong trend")
    elif adx_v >= 20:
        if bull >= bear:
            bull += WEIGHTS["adx"] * 0.5
        else:
            bear += WEIGHTS["adx"] * 0.5

    vol_ok = last["volume"] > (last["vol_sma20"] or 0) * 1.2
    if vol_ok:
        if bull >= bear:
            bull += WEIGHTS["volume"]
        else:
            bear += WEIGHTS["volume"]
        reasons.append("Above-avg volume")

    dist = abs(price - last["ema21"]) / price if price else 1
    if dist < 0.004:
        if bull >= bear:
            bull += WEIGHTS["pullback"]
        else:
            bear += WEIGHTS["pullback"]
        reasons.append("Entry near EMA21 (good R:R)")

    direction = _direction_from_factors(bull, bear)
    score = max(bull, bear)

    trend_ok = True
    if cfg["signals"].get("require_trend_alignment", True) and trend_dir is not None:
        if direction == "LONG" and trend_dir < 0:
            trend_ok = False
        if direction == "SHORT" and trend_dir > 0:
            trend_ok = False
        if not trend_ok:
            reasons.append("Higher-TF trend disagrees (filtered)")

    if (last["vol_sma20"] or 0) < cfg["signals"].get("min_avg_volume", 0):
        direction = "NONE"
        reasons.append("Below min liquidity")

    return Signal(
        symbol=symbol, direction=direction, score=round(score, 1),
        price=price, atr=atr_v, reasons=reasons, factors=factors,
        trend_ok=trend_ok,
    )


def higher_tf_trend(df: pd.DataFrame) -> int:
    """Return +1/-1/0 trend from a higher-timeframe DataFrame using EMA stack."""
    df = ind.add_all(df)
    if df.empty or "ema50" not in df or len(df) < 50:
        return 0
    last = df.iloc[-1]
    if last["ema21"] > last["ema50"] and last["close"] > last["ema21"]:
        return 1
    if last["ema21"] < last["ema50"] and last["close"] < last["ema21"]:
        return -1
    return 0


# =========================================================================== #
#  SWING-MOMENTUM engine (multi-day holds, bigger moves, daily data)
# =========================================================================== #
SWING_WEIGHTS = {
    "trend": 20, "momentum": 22, "breakout": 16,
    "volume": 10, "rsi": 8, "adx": 8,
    # Momentum QUALITY edges (steady movers beat one-day wonders):
    "consistency": 8,   # most of the last 20 days closed in the move's direction
    "hi52": 8,          # near the 52-week high/low - momentum persists there
}


def exhaustion_assessment(df: pd.DataFrame, direction: str, cfg: dict):
    """Mean-reversion overlay: how STRETCHED / TIRED is this move?

    Pure trend-following buys strength and shorts weakness - which is exactly
    what gets caught when a move snaps back. This overlay measures how
    overextended the current move is, using four independent reversal-risk cues:

      1. RSI at an extreme (overbought for longs / oversold for shorts)
      2. Price stretched far from its 20-EMA, measured in ATRs (z-distance)
      3. Price pushed outside the Bollinger band
      4. Momentum divergence (new price extreme NOT confirmed by RSI)

    It returns a penalty (score points to subtract) plus plain-English reasons.
    It does NOT predict the exact top - nothing can - it just stops the bot from
    chasing a move that is already overextended and likely to revert.
    """
    ec = cfg.get("swing", {}).get("exhaustion", {})
    if not ec.get("enabled", True) or direction not in ("LONG", "SHORT"):
        return 0.0, []
    c = df["close"]
    if len(c) < 25:
        return 0.0, []

    price = float(c.iloc[-1])
    rsi_s = ind.rsi(c, 14)
    rsi_v = float(rsi_s.iloc[-1])
    ema20 = float(ind.ema(c, 20).iloc[-1])
    atr_v = float(ind.atr(df, 14).iloc[-1]) or (price * 0.01)
    up, _mid, low = ind.bollinger(c, 20, 2.0)
    band = float(up.iloc[-1] - low.iloc[-1]) or 1.0
    pctB = (price - float(low.iloc[-1])) / band      # >1 above upper, <0 below lower
    stretch = (price - ema20) / atr_v                # ATRs above/below the mean
    look = int(ec.get("div_lookback", 14))
    rsi_then = float(rsi_s.iloc[-1 - look]) if len(rsi_s) > look else rsi_v
    price_then = float(c.iloc[-1 - look]) if len(c) > look else price

    rsi_ob = float(ec.get("rsi_overbought", 75))
    rsi_os = float(ec.get("rsi_oversold", 25))
    stretch_max = float(ec.get("stretch_atr", 3.0))
    cap = float(ec.get("max_penalty", 25))

    pen, reasons = 0.0, []
    if direction == "LONG":
        if rsi_v >= rsi_ob:
            pen += min(8.0, (rsi_v - rsi_ob) / max(1.0, 90 - rsi_ob) * 8.0 + 3.0)
            reasons.append(f"Overbought RSI {rsi_v:.0f} - up-move stretched")
        if stretch >= stretch_max:
            pen += min(8.0, (stretch - stretch_max) / stretch_max * 8.0 + 3.0)
            reasons.append(f"Price {stretch:.1f} ATR above its mean - extended")
        if pctB > 1.0:
            pen += min(5.0, (pctB - 1.0) * 20.0)
            reasons.append("Outside upper Bollinger band - overbought")
        if price > price_then and rsi_v < rsi_then - 3:
            pen += 6.0
            reasons.append("Bearish divergence - new high, weaker momentum")
    else:  # SHORT
        if rsi_v <= rsi_os:
            pen += min(8.0, (rsi_os - rsi_v) / max(1.0, rsi_os - 10) * 8.0 + 3.0)
            reasons.append(f"Oversold RSI {rsi_v:.0f} - down-move stretched")
        if stretch <= -stretch_max:
            pen += min(8.0, (abs(stretch) - stretch_max) / stretch_max * 8.0 + 3.0)
            reasons.append(f"Price {abs(stretch):.1f} ATR below its mean - extended")
        if pctB < 0.0:
            pen += min(5.0, (-pctB) * 20.0)
            reasons.append("Outside lower Bollinger band - oversold")
        if price < price_then and rsi_v > rsi_then + 3:
            pen += 6.0
            reasons.append("Bullish divergence - new low, stronger momentum")

    pen = round(min(pen, cap), 1)
    if pen >= cap * 0.8:
        reasons.append("EXHAUSTION HIGH - likely near a reversal; not chasing")
    return pen, reasons


def evaluate_swing(symbol: str, df: pd.DataFrame, cfg: dict) -> Signal:
    """Score a stock for a multi-day momentum swing using DAILY candles.
    Looks for strong, fresh movers: uptrend + high 20d momentum + breakout +
    above-average volume. Mirrors for short setups."""
    sc = cfg.get("swing", {})
    if df.empty or len(df) < 60:
        return Signal(symbol, "NONE", 0.0, 0.0, 0.0, ["insufficient daily data"])

    c = df["close"]
    ema20 = ind.ema(c, 20)
    ema50 = ind.ema(c, 50)
    ema100 = ind.ema(c, 100) if len(df) >= 100 else ind.ema(c, 50)
    rsi = ind.rsi(c, 14)
    atr = ind.atr(df, 14)
    adx = ind.adx(df, 14)
    roc20 = ind.roc(c, 20)
    vol_sma = ind.sma(df["volume"], 20)
    bo_n = int(sc.get("breakout_lookback", 20))
    prior_high = df["high"].shift(1).rolling(bo_n).max()
    prior_low = df["low"].shift(1).rolling(bo_n).min()

    last = df.iloc[-1]
    price = float(last["close"])
    atr_v = float(atr.iloc[-1]) if atr.iloc[-1] == atr.iloc[-1] else 0.0
    roc_v = float(roc20.iloc[-1]) if roc20.iloc[-1] == roc20.iloc[-1] else 0.0
    min_roc = float(sc.get("min_roc_pct", 10))

    bull = 0.0
    bear = 0.0
    reasons: list = []

    up = price > ema20.iloc[-1] > ema50.iloc[-1] and ema50.iloc[-1] > ema100.iloc[-1]
    dn = price < ema20.iloc[-1] < ema50.iloc[-1] and ema50.iloc[-1] < ema100.iloc[-1]
    if up:
        bull += SWING_WEIGHTS["trend"]; reasons.append("Stacked uptrend (20>50>100 EMA)")
    elif dn:
        bear += SWING_WEIGHTS["trend"]; reasons.append("Stacked downtrend (20<50<100 EMA)")

    # Momentum: 20-day rate of change, scaled
    if roc_v >= min_roc:
        bull += min(SWING_WEIGHTS["momentum"], SWING_WEIGHTS["momentum"] * roc_v / (min_roc * 2))
        reasons.append(f"Strong 20d momentum (+{roc_v:.1f}%)")
    elif roc_v <= -min_roc:
        bear += min(SWING_WEIGHTS["momentum"], SWING_WEIGHTS["momentum"] * abs(roc_v) / (min_roc * 2))
        reasons.append(f"Strong 20d down-momentum ({roc_v:.1f}%)")

    # Breakout of prior N-day range
    if price >= (prior_high.iloc[-1] or price * 1.01):
        bull += SWING_WEIGHTS["breakout"]; reasons.append(f"{bo_n}-day breakout (new high)")
    elif price <= (prior_low.iloc[-1] or price * 0.99):
        bear += SWING_WEIGHTS["breakout"]; reasons.append(f"{bo_n}-day breakdown (new low)")

    # Volume surge confirms conviction
    vs = vol_sma.iloc[-1] or 0
    if vs and last["volume"] > vs * 1.5:
        (reasons.append("Volume surge (>1.5x avg)"))
        if bull >= bear:
            bull += SWING_WEIGHTS["volume"]
        else:
            bear += SWING_WEIGHTS["volume"]

    rv = rsi.iloc[-1]
    if 55 <= rv <= 80:
        bull += SWING_WEIGHTS["rsi"]; reasons.append(f"RSI {rv:.0f} (momentum zone)")
    elif 20 <= rv <= 45:
        bear += SWING_WEIGHTS["rsi"]; reasons.append(f"RSI {rv:.0f} (weak)")

    av = adx.iloc[-1]
    if av >= 25:
        if bull >= bear:
            bull += SWING_WEIGHTS["adx"]
        else:
            bear += SWING_WEIGHTS["adx"]
        reasons.append(f"ADX {av:.0f} (strong trend)")

    # Momentum QUALITY: a steady grind beats a one-day spike. Count how many
    # of the last 20 sessions closed in the move's direction.
    delta = c.diff().tail(20)
    up_days = int((delta > 0).sum())
    if up_days >= 13:
        bull += SWING_WEIGHTS["consistency"]
        reasons.append(f"Consistent move ({up_days}/20 up days)")
    elif up_days <= 7:
        bear += SWING_WEIGHTS["consistency"]
        reasons.append(f"Consistent weakness ({20 - up_days}/20 down days)")

    # 52-week positioning: names pressing their yearly high (or low) tend to
    # keep going - one of the most robust momentum edges there is.
    if len(df) >= 100:
        hi52 = float(df["high"].tail(252).max())
        lo52 = float(df["low"].tail(252).min())
        if hi52 and price >= hi52 * 0.95:
            bull += SWING_WEIGHTS["hi52"]
            reasons.append("Within 5% of 52-week high")
        elif lo52 and price <= lo52 * 1.05:
            bear += SWING_WEIGHTS["hi52"]
            reasons.append("Within 5% of 52-week low")

    direction = _direction_from_factors(bull, bear)
    score = round(max(bull, bear), 1)

    # Gap-chase guard: if today already GAPPED hard in our direction, most of
    # the easy move may be gone and the entry is chasing a news spike.
    if direction in ("LONG", "SHORT") and len(c) >= 2:
        prev_close = float(c.iloc[-2])
        gap = ((float(last["open"]) - prev_close) / prev_close * 100) if prev_close else 0.0
        gmax = float(sc.get("gap_chase_pct", 3.0))
        if (direction == "LONG" and gap >= gmax) or \
           (direction == "SHORT" and gap <= -gmax):
            score = round(max(0.0, score - 8.0), 1)
            reasons.append(f"Gapped {gap:+.1f}% today - chasing risk (-8 score)")

    # Exhaustion overlay: dock the score when the move is overextended, so the
    # bot avoids chasing a name that's stretched and likely to revert.
    exh_pen, exh_reasons = exhaustion_assessment(df, direction, cfg)
    if exh_pen:
        score = round(max(0.0, score - exh_pen), 1)
        reasons.extend(exh_reasons)

    if (vs or 0) < cfg["signals"].get("min_avg_volume", 0):
        direction = "NONE"
        reasons.append("Below min liquidity")

    sig = Signal(symbol=symbol, direction=direction, score=score, price=price,
                 atr=atr_v, reasons=reasons)
    sig.factors = {"roc20": round(roc_v, 1), "exhaustion": exh_pen}
    return sig
