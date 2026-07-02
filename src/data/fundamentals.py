"""
Fundamental quality data from Screener.in (free).

We scrape the public company page's "ratios" block to extract a few
quality/valuation metrics used purely as a FILTER (avoid junk stocks),
not as a precise valuation engine. Results are cached to disk for a day
because fundamentals barely change intraday.

If scraping fails (site change / no network), the filter "fails open"
(returns None) so the technical engine still works.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

import requests

from ..utils import get_logger

log = get_logger()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".fundamentals_cache.json",
)
_CACHE_TTL = 24 * 3600  # one day


def _load_cache() -> dict:
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def _num(text: str) -> Optional[float]:
    m = re.search(r"-?\d[\d,]*\.?\d*", text.replace("%", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def fetch_fundamentals(symbol: str) -> dict:
    """Return {roe, debt_to_equity, pe, market_cap} where available."""
    cache = _load_cache()
    entry = cache.get(symbol)
    if entry and time.time() - entry.get("_ts", 0) < _CACHE_TTL:
        return entry["data"]

    data: dict = {}
    for suffix in ("", "/consolidated"):
        url = f"https://www.screener.in/company/{symbol}{suffix}/"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            data = _parse_screener(resp.text)
            if data:
                break
        except Exception as exc:
            log.warning("Screener fetch failed for %s: %s", symbol, exc)

    cache[symbol] = {"_ts": time.time(), "data": data}
    _save_cache(cache)
    return data


def _parse_screener(html: str) -> dict:
    """Extract metrics from the top ratios list on a Screener company page."""
    out: dict = {}
    # The ratios appear as: <li ...><span class="name">ROE</span>
    #   <span class="value"> 18.4 %</span></li>
    pattern = re.compile(
        r'<span class="name">\s*([^<]+?)\s*</span>.*?'
        r'<span class="(?:nowrap )?value">(.*?)</span>',
        re.DOTALL,
    )
    label_map = {
        "roe": "roe",
        "return on equity": "roe",
        "debt to equity": "debt_to_equity",
        "stock p/e": "pe",
        "p/e": "pe",
        "market cap": "market_cap",
    }
    for raw_name, raw_val in pattern.findall(html):
        name = re.sub(r"<[^>]+>", "", raw_name).strip().lower()
        for key, mapped in label_map.items():
            if key in name and mapped not in out:
                val = _num(re.sub(r"<[^>]+>", "", raw_val))
                if val is not None:
                    out[mapped] = val
    return out


def passes_quality_filter(symbol: str, cfg: dict) -> tuple[bool, dict]:
    """True if the stock clears the quality gate (or data unavailable)."""
    if not cfg["filters"].get("use_fundamental_filter", False):
        return True, {}
    f = fetch_fundamentals(symbol)
    if not f:
        return True, {}  # fail open - don't block on missing data
    max_de = cfg["filters"]["max_debt_to_equity"]
    min_roe = cfg["filters"]["min_roe_pct"]
    ok = True
    if f.get("debt_to_equity") is not None and f["debt_to_equity"] > max_de:
        ok = False
    if f.get("roe") is not None and f["roe"] < min_roe:
        ok = False
    return ok, f
