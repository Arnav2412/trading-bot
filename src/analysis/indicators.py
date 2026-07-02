"""
Technical indicators - vectorized with pandas/numpy (no external TA lib needed).

Every function takes a DataFrame with columns [open, high, low, close, volume]
and returns a Series (or adds columns). All are intraday-friendly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP, reset each trading day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    day = df.index.normalize() if df.index.tz is None else df.index.tz_convert(df.index.tz).normalize()
    grouped = pd.Series(day, index=df.index)
    cum_pv = pv.groupby(grouped).cumsum()
    cum_vol = df["volume"].groupby(grouped).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index - trend strength (not direction)."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """Returns (supertrend_line, direction) where direction +1=up, -1=down."""
    atr_ = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * atr_
    lower = hl2 - mult * atr_
    n = len(df)
    final_upper = upper.copy()
    final_lower = lower.copy()
    close = df["close"]
    for i in range(1, n):
        final_upper.iloc[i] = (
            upper.iloc[i] if (upper.iloc[i] < final_upper.iloc[i - 1]
                              or close.iloc[i - 1] > final_upper.iloc[i - 1])
            else final_upper.iloc[i - 1]
        )
        final_lower.iloc[i] = (
            lower.iloc[i] if (lower.iloc[i] > final_lower.iloc[i - 1]
                              or close.iloc[i - 1] < final_lower.iloc[i - 1])
            else final_lower.iloc[i - 1]
        )
    direction = np.ones(n)
    st = final_lower.copy()
    for i in range(1, n):
        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction[i] = 1
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        st.iloc[i] = final_lower.iloc[i] if direction[i] == 1 else final_upper.iloc[i]
    return st, pd.Series(direction, index=df.index)


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the full indicator set used by the signal engine."""
    if df.empty or len(df) < 30:
        return df
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema21"] = ema(out["close"], 21)
    out["ema50"] = ema(out["close"], 50)
    out["rsi"] = rsi(out["close"], 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["atr"] = atr(out, 14)
    out["vwap"] = vwap(out)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(out["close"])
    out["adx"] = adx(out, 14)
    out["st"], out["st_dir"] = supertrend(out, 10, 3.0)
    out["vol_sma20"] = sma(out["volume"], 20)
    return out


def roc(series: pd.Series, period: int = 20) -> pd.Series:
    """Rate of change (%) over `period` bars - core momentum measure."""
    return (series / series.shift(period) - 1.0) * 100.0
