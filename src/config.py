"""Load and validate configuration from config.yaml."""
# (EOD auto-tune overrides applied via reports/learning.json)
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import yaml


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str | None = None) -> dict[str, Any]:
    """Read config.yaml into a plain dict. Falls back to project-root config."""
    if path is None:
        path = os.path.join(_project_root(), "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    _apply_learning(cfg)
    _validate(cfg)
    return cfg


def _apply_learning(cfg: dict[str, Any]) -> None:
    """Overlay the EOD auto-tuner's learned values (reports/learning.json) on top
    of config.yaml so the strategy adapts over time. Safe no-op if the file is
    missing or learning is disabled. Delete the file to reset to config defaults."""
    if not cfg.get("learning", {}).get("enabled", False):
        return
    report_dir = cfg.get("output", {}).get("report_dir", "reports")
    fpath = os.path.join(_project_root(), report_dir, "learning.json")
    if not os.path.exists(fpath):
        return
    try:
        with open(fpath, "r", encoding="utf-8") as fh:
            learned = json.load(fh)
    except Exception:
        return
    if "min_score" in learned:
        cfg.setdefault("swing", {})["min_score"] = learned["min_score"]
        cfg.setdefault("signals", {})["min_score"] = learned["min_score"]
    if "regime_buffer" in learned:
        cfg.setdefault("swing", {})["regime_buffer"] = learned["regime_buffer"]
    if "counter_trend_penalty" in learned:
        cfg.setdefault("swing", {})["counter_trend_penalty"] = learned["counter_trend_penalty"]
    cfg["_learning_applied"] = learned


def _validate(cfg: dict[str, Any]) -> None:
    acct = cfg.get("account", {})
    if acct.get("capital", 0) <= 0:
        raise ValueError("account.capital must be > 0")
    if not (0 < acct.get("risk_per_trade_pct", 0) <= 5):
        raise ValueError("account.risk_per_trade_pct should be between 0 and 5")
    if cfg.get("data", {}).get("interval") not in {"1m", "2m", "5m", "15m"}:
        raise ValueError("data.interval must be one of 1m, 2m, 5m, 15m")
