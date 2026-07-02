"""
Lightweight news-headline sentiment from FREE sources.

Uses Google News RSS (no key) for recent headlines about a company and
scores them with a small finance lexicon. This is a confidence tie-breaker,
NOT a primary signal. Fails open (neutral 0.0) on any error.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

from ..utils import get_logger

log = get_logger()

_POS = {
    "surge", "jump", "gain", "rise", "profit", "beat", "record", "growth",
    "upgrade", "buy", "bullish", "outperform", "expansion", "wins", "approval",
    "rally", "soar", "strong", "high", "boost", "dividend", "order", "deal",
}
_NEG = {
    "fall", "drop", "plunge", "loss", "decline", "miss", "downgrade", "sell",
    "bearish", "underperform", "probe", "fraud", "fine", "penalty", "lawsuit",
    "weak", "cut", "slump", "crash", "default", "resign", "ban", "raid", "slips",
}

_CACHE: dict[str, tuple[float, float]] = {}
_TTL = 30 * 60  # 30 minutes


def _score_text(text: str) -> int:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & _POS) - len(words & _NEG)


def headline_sentiment(symbol: str, company_hint: str = "") -> float:
    """Return a sentiment score in roughly [-1, 1]. 0 = neutral/unknown."""
    key = symbol
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _TTL:
        return _CACHE[key][1]

    query = f"{company_hint or symbol} stock NSE".replace(" ", "%20")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    score = 0.0
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            titles = [item.findtext("title") or ""
                      for item in root.iter("item")][:12]
            if titles:
                raw = sum(_score_text(t) for t in titles)
                # squash into [-1, 1]
                score = max(-1.0, min(1.0, raw / (len(titles) * 1.5)))
    except Exception as exc:
        log.warning("News sentiment failed for %s: %s", symbol, exc)
        score = 0.0

    _CACHE[key] = (now, score)
    return score
