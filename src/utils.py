"""Shared helpers: time, logging, IST clock."""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def market_is_open(cfg: dict, now: dt.datetime | None = None) -> bool:
    now = now or now_ist()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    sched = cfg["schedule"]
    open_t = parse_hhmm(sched["market_open"])
    close_t = parse_hhmm(sched["market_close"])
    return open_t <= now.time() <= close_t


def can_take_new_entry(cfg: dict, now: dt.datetime | None = None) -> bool:
    now = now or now_ist()
    cutoff = parse_hhmm(cfg["schedule"]["no_new_entries_after"])
    return market_is_open(cfg, now) and now.time() <= cutoff


def get_logger(name: str = "tradingbot") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                         datefmt="%H:%M:%S"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger
