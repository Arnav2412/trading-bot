"""
Risk management: stop-loss, targets, and position sizing.

Core principle (the part that actually protects your capital):
  - Risk a FIXED small % of capital per trade (e.g. 1%).
  - Stop-loss distance is volatility-based (ATR), not a guess.
  - Quantity = (capital * risk%) / (entry - stop).
  - Targets are multiples of the risk (reward:risk).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradePlan:
    symbol: str
    direction: str          # "LONG" or "SHORT"
    entry: float
    stop_loss: float
    target1: float
    target2: float
    quantity: int
    risk_per_share: float
    rupees_at_risk: float
    notional: float
    reward_risk_t1: float
    reward_risk_t2: float

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry": round(self.entry, 2),
            "stop_loss": round(self.stop_loss, 2),
            "target1": round(self.target1, 2),
            "target2": round(self.target2, 2),
            "quantity": self.quantity,
            "risk_per_share": round(self.risk_per_share, 2),
            "rupees_at_risk": round(self.rupees_at_risk, 2),
            "notional": round(self.notional, 2),
            "rr_t1": round(self.reward_risk_t1, 2),
            "rr_t2": round(self.reward_risk_t2, 2),
        }


def build_trade_plan(symbol: str, direction: str, entry: float, atr_value: float,
                     cfg: dict) -> TradePlan | None:
    acct = cfg["account"]
    sig = cfg["signals"]
    capital = float(acct["capital"]) * float(acct.get("intraday_leverage", 1))
    risk_rupees = float(acct["capital"]) * float(acct["risk_per_trade_pct"]) / 100.0

    stop_dist = max(atr_value * float(sig["atr_stop_multiplier"]), entry * 0.001)
    if stop_dist <= 0:
        return None

    if direction == "LONG":
        stop = entry - stop_dist
        t1 = entry + stop_dist * float(sig["target1_rr"])
        t2 = entry + stop_dist * float(sig["target2_rr"])
    else:  # SHORT
        stop = entry + stop_dist
        t1 = entry - stop_dist * float(sig["target1_rr"])
        t2 = entry - stop_dist * float(sig["target2_rr"])

    qty = int(risk_rupees // stop_dist)
    # Cap by available (leveraged) capital.
    if qty * entry > capital:
        qty = int(capital // entry)
    if qty <= 0:
        return None

    return TradePlan(
        symbol=symbol, direction=direction, entry=entry, stop_loss=stop,
        target1=t1, target2=t2, quantity=qty, risk_per_share=stop_dist,
        rupees_at_risk=qty * stop_dist, notional=qty * entry,
        reward_risk_t1=float(sig["target1_rr"]),
        reward_risk_t2=float(sig["target2_rr"]),
    )


def walk_exit(fwd, direction: str, entry: float, stop: float,
              t1: float, t2: float, max_hold: int):
    """Walk daily bars forward simulating the TWO-STAGE exit the plan promises:

      - Stop hit first          -> full exit at stop            ("stop")
      - T1 hit                  -> book HALF at T1, stop moves to BREAKEVEN
      - then T2 hit             -> rest exits at T2             ("target2")
      - then breakeven hit      -> rest exits flat              ("target")
      - max_hold reached        -> rest exits at close          ("target"/"timeout")

    Returns (effective_exit_price, exit_time_iso, outcome) or None if still
    open. The effective price blends the two halves, so P&L math stays a
    single number: e.g. half at T1 + half at breakeven = (t1 + entry) / 2.
    Conservative on same-bar ambiguity: stop is checked before targets, and
    T2/breakeven only apply from the bar AFTER T1 was booked.
    """
    long = direction == "LONG"
    booked = False
    cur_stop = stop
    for i in range(len(fwd)):
        bar = fwd.iloc[i]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        ts = fwd.index[i].isoformat()
        stop_hit = (lo <= cur_stop) if long else (hi >= cur_stop)
        if stop_hit:
            if booked:   # rest stopped at breakeven; half already banked at T1
                return (t1 + cur_stop) / 2.0, ts, "target"
            return cur_stop, ts, "stop"
        t2_hit = (hi >= t2) if long else (lo <= t2)
        if booked and t2_hit:
            return (t1 + t2) / 2.0, ts, "target2"
        t1_hit = (hi >= t1) if long else (lo <= t1)
        if not booked and t1_hit:
            booked = True
            cur_stop = entry          # rest of the position now risk-free
        if i + 1 >= max_hold:
            if booked:
                return (t1 + cl) / 2.0, ts, "target"
            return cl, ts, "timeout"
    return None


class DailyRiskGuard:
    """Tracks cumulative realized P&L vs the daily loss limit."""

    def __init__(self, cfg: dict):
        self.capital = float(cfg["account"]["capital"])
        self.max_loss = self.capital * float(cfg["account"]["max_daily_loss_pct"]) / 100.0
        self.realized = 0.0

    def record(self, pnl: float) -> None:
        self.realized += pnl

    def halted(self) -> bool:
        return self.realized <= -self.max_loss

    def remaining_risk_budget(self) -> float:
        return max(0.0, self.max_loss + self.realized)


def build_swing_plan(symbol: str, direction: str, entry: float, cfg: dict,
                     atr: float = 0.0) -> "TradePlan | None":
    """Volatility-adaptive plan for multi-day swing trades.

    Stops: ATR-based (2x ATR by default) instead of a one-size-fits-all %.
    A calm large-cap gets a tight stop (more shares per rupee of risk); a
    volatile mid-cap gets breathing room (fewer whipsaw stop-outs). The stop
    is clamped between min_stop_pct and max_stop_pct of entry.

    Targets: fixed R-multiples of the actual stop distance, so every trade
    risks 1 to make target1_rr / target2_rr - consistent expectancy math
    instead of an 8%/16% guess that ignores the stock's volatility.
    """
    acct = cfg["account"]
    sc = cfg.get("swing", {})
    risk_rupees = float(acct["capital"]) * float(acct["risk_per_trade_pct"]) / 100.0

    use_atr = bool(sc.get("use_atr_stops", True)) and atr and atr > 0
    if use_atr:
        stop_dist = atr * float(sc.get("atr_stop_mult", 2.0))
        lo = entry * float(sc.get("min_stop_pct", 1.5)) / 100.0
        hi = entry * float(sc.get("max_stop_pct", 5.0)) / 100.0
        stop_dist = min(max(stop_dist, lo), hi)
        t1_rr = float(sc.get("target1_rr", 1.8))
        t2_rr = float(sc.get("target2_rr", 3.5))
        t1_dist, t2_dist = stop_dist * t1_rr, stop_dist * t2_rr
    else:
        stop_dist = entry * float(sc.get("stop_pct", 5)) / 100.0
        t1_dist = entry * float(sc.get("target1_pct", 8)) / 100.0
        t2_dist = entry * float(sc.get("target2_pct", 16)) / 100.0

    if direction == "LONG":
        stop = entry - stop_dist
        t1 = entry + t1_dist
        t2 = entry + t2_dist
    else:
        stop = entry + stop_dist
        t1 = entry - t1_dist
        t2 = entry - t2_dist

    if stop_dist <= 0:
        return None
    qty = int(risk_rupees // stop_dist)
    if qty * entry > float(acct["capital"]):
        qty = int(float(acct["capital"]) // entry)
    if qty <= 0:
        qty = 1  # allow at least 1 share for the plan/illustration

    return TradePlan(
        symbol=symbol, direction=direction, entry=entry, stop_loss=stop,
        target1=t1, target2=t2, quantity=qty, risk_per_share=stop_dist,
        rupees_at_risk=qty * stop_dist, notional=qty * entry,
        reward_risk_t1=round(t1_dist / stop_dist, 2),
        reward_risk_t2=round(t2_dist / stop_dist, 2),
    )
