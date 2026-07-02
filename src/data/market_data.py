"""
Intraday OHLCV data from FREE sources, hardened against Yahoo's flaky access.

Primary:   yfinance (Yahoo) - we use a curl_cffi browser-impersonation session
           plus a retry, which beats most "401 Invalid Crumb" rate-limit blocks.
Fallback:  jugaad-data (NSE EOD) - daily bars only; used so the app still has
           *something* when Yahoo is fully down (not enough for intraday signals).

Returns a clean DataFrame indexed by IST timestamp with columns:
    open, high, low, close, volume
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Optional

import logging as _logging
import os as _os
import contextlib as _contextlib

import pandas as pd

# Silence yfinance's noisy "possibly delisted / 404" console spam.
for _name in ("yfinance", "yfinance.utils", "yfinance.data"):
    _logging.getLogger(_name).setLevel(_logging.CRITICAL)
_DEVNULL = open(_os.devnull, "w")  # yfinance noise sink

from ..universe import to_yahoo
from ..utils import IST, get_logger

log = get_logger()

_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 60  # seconds
_SESSION = None  # lazily-built curl_cffi impersonation session


def _session():
    """A Chrome-impersonating session helps get past Yahoo's crumb/401 blocks."""
    global _SESSION
    if _SESSION is None:
        try:
            from curl_cffi import requests as creq
            _SESSION = creq.Session(impersonate="chrome")
        except Exception:
            _SESSION = False
    return _SESSION or None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    cols = ["open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]].copy()
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    return df


def fetch_yfinance(symbol: str, interval: str, lookback_days: int,
                   raw: bool = False) -> pd.DataFrame:
    import yfinance as yf

    ticker = symbol if raw else to_yahoo(symbol)
    period = f"{max(1, lookback_days)}d"
    sess = _session()

    # Attempt 1: Ticker.history with an impersonation session (best vs 401).
    for use_session in ([True, False] if sess else [False]):
        try:
            t = yf.Ticker(ticker, session=sess) if use_session else yf.Ticker(ticker)
            with _contextlib.redirect_stderr(_DEVNULL):
                df = t.history(period=period, interval=interval, auto_adjust=False)
            if df is not None and not df.empty:
                return _normalize(df)
        except TypeError:
            # this yfinance version doesn't accept session= ; try plain
            try:
                df = yf.Ticker(ticker).history(period=period, interval=interval,
                                               auto_adjust=False)
                if df is not None and not df.empty:
                    return _normalize(df)
            except Exception:
                pass
        except Exception as exc:
            log.debug("yfinance Ticker.history failed for %s: %s", symbol, exc)

    # Attempt 2: classic download.
    try:
        with _contextlib.redirect_stderr(_DEVNULL):
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=False, threads=False)
        if df is not None and not df.empty:
            return _normalize(df)
    except Exception as exc:
        log.debug("yfinance download failed for %s: %s", symbol, exc)
    return pd.DataFrame()


def fetch_jugaad(symbol: str, lookback_days: int) -> pd.DataFrame:
    """Fallback DAILY/EOD data from NSE (no intraday). Trend context only."""
    try:
        from jugaad_data.nse import stock_df
    except Exception:
        return pd.DataFrame()
    end = dt.date.today()
    start = end - dt.timedelta(days=max(30, lookback_days * 6))
    try:
        df = stock_df(symbol=symbol, from_date=start, to_date=end, series="EQ")
    except Exception as exc:
        log.debug("jugaad-data failed for %s: %s", symbol, exc)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"OPEN": "open", "HIGH": "high", "LOW": "low",
                            "CLOSE": "close", "VOLUME": "volume", "DATE": "date"})
    df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(IST)
    return df.sort_index()


def get_intraday(symbol: str, cfg: dict, interval: Optional[str] = None) -> pd.DataFrame:
    interval = interval or cfg["data"]["interval"]
    lookback = cfg["data"]["lookback_days"]
    key = f"{symbol}:{interval}:{lookback}"
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]

    df = fetch_yfinance(symbol, interval, lookback)
    if df.empty:
        df = fetch_jugaad(symbol, lookback)
    _CACHE[key] = (now, df)
    return df


def quick_probe(cfg: dict) -> bool:
    """Fast check: is usable intraday data available right now?
    Tries a few of the most liquid names; True if any returns >=50 intraday bars."""
    interval = cfg["data"]["interval"]
    lookback = cfg["data"]["lookback_days"]
    for s in ("RELIANCE", "TCS", "INFY", "HDFCBANK"):
        try:
            # Probe Yahoo directly (skip the slow daily fallback, which can
            # never satisfy an intraday check) so this returns in seconds.
            df = fetch_yfinance(s, interval, lookback)
            if not df.empty and len(df) >= 50:
                return True
        except Exception:
            continue
    return False


def get_quote_snapshot(symbol: str, cfg: dict) -> dict:
    df = get_intraday(symbol, cfg)
    if df.empty:
        return {}
    last = df.iloc[-1]
    return {"symbol": symbol, "ltp": float(last["close"]),
            "volume": float(last["volume"]), "timestamp": df.index[-1].isoformat()}

# --------------------------------------------------------------------------- #
#  Market regime (Nifty 50 trend) - trade WITH the broad market, not against it
# --------------------------------------------------------------------------- #
_REGIME = {"ts": 0.0, "series": None}


def nifty_regime_series(cfg: dict):
    """A daily Series (+1 bull / -1 bear / 0 flat) of the Nifty vs its 50-EMA,
    indexed by normalized date. Cached ~30 min."""
    from ..analysis import indicators as ind
    now = time.time()
    if _REGIME["series"] is not None and now - _REGIME["ts"] < 1800:
        return _REGIME["series"]
    df = fetch_yfinance("^NSEI", "1d", 400, raw=True)
    if df.empty or len(df) < 60:
        _REGIME.update(ts=now, series=pd.Series(dtype=float))
        return _REGIME["series"]
    ema50 = ind.ema(df["close"], 50)
    reg = pd.Series(0, index=df.index)
    reg[df["close"] > ema50] = 1
    reg[df["close"] < ema50] = -1
    reg.index = reg.index.normalize()
    _REGIME.update(ts=now, series=reg)
    return reg


def market_regime(cfg: dict) -> int:
    """Latest market regime: +1 bullish, -1 bearish, 0 unknown/flat.

    A learned `swing.regime_buffer` (%) widens the neutral zone: the Nifty must
    clear its 50-EMA by that margin before we trust the trend. Higher buffer =
    stricter = fewer trades fought against an unclear market (set by auto-tune).
    """
    buf = float(cfg.get("swing", {}).get("regime_buffer", 0.0) or 0.0)
    if buf <= 0:
        s = nifty_regime_series(cfg)
        try:
            return int(s.iloc[-1]) if len(s) else 0
        except Exception:
            return 0
    # Re-derive the latest reading with the buffer applied.
    try:
        from ..analysis import indicators as ind
        df = fetch_yfinance("^NSEI", "1d", 400, raw=True)
        if df.empty or len(df) < 60:
            return 0
        ema50 = ind.ema(df["close"], 50)
        close = float(df["close"].iloc[-1]); e = float(ema50.iloc[-1])
        if close > e * (1 + buf / 100.0):
            return 1
        if close < e * (1 - buf / 100.0):
            return -1
        return 0
    except Exception:
        return 0
