"""Portfolio strategy: regime filter + target-weight rebalancing.

The strategy is deliberately simple and transparent:

1. Hold a fixed target allocation of diversified, income-oriented ETFs.
2. When the benchmark trades below its long-term moving average ("risk-off"),
   scale equity targets down and move the freed weight into the defensive
   asset (a bond ETF) or cash.
3. Invest all idle cash above the cash buffer into the most underweight
   holdings (this is how dividends get reinvested).
4. Only sell when a holding drifts above its target by more than the drift
   threshold, to keep turnover and taxable events low.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Config
from .models import Bar, Trade


@dataclass(frozen=True)
class Regime:
    name: str  # "risk-on", "risk-off" or "disabled"/"unknown"
    benchmark_close: float | None = None
    sma: float | None = None

    @property
    def risk_off(self) -> bool:
        return self.name == "risk-off"

    def describe(self) -> str:
        if self.benchmark_close is None or self.sma is None:
            return self.name
        return f"{self.name} (close {self.benchmark_close:.2f} vs SMA {self.sma:.2f})"


def simple_moving_average(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def detect_regime(cfg: Config, benchmark_bars: list[Bar], today: date | None = None) -> Regime:
    """Trend regime from the benchmark's close vs. its moving average.

    With regime.evaluate = "monthly" the signal is taken from the last close
    of the previous calendar month, so it can only change once a month. That
    is the classic timing-model cadence and avoids whipsaw around the average.
    """
    if not cfg.regime.enabled:
        return Regime("disabled")
    bars = sorted(benchmark_bars, key=lambda b: b.day)
    if cfg.regime.evaluate == "monthly" and bars:
        ref = today or bars[-1].day
        month_start = ref.replace(day=1)
        bars = [b for b in bars if b.day < month_start]
    closes = [b.close for b in bars]
    sma = simple_moving_average(closes, cfg.regime.sma_days)
    if sma is None or not closes:
        return Regime("unknown")
    last = closes[-1]
    return Regime("risk-off" if last < sma else "risk-on", benchmark_close=last, sma=sma)


def effective_targets(cfg: Config, regime: Regime) -> tuple[dict[str, float], float]:
    """Return (symbol -> weight, cash weight) after applying the regime filter."""
    targets = dict(cfg.targets)
    cash_weight = cfg.cash_weight
    if not regime.risk_off:
        return targets, cash_weight

    freed = 0.0
    for sym in cfg.equity_symbols:
        scaled = targets[sym] * cfg.regime.risk_off_equity_scale
        freed += targets[sym] - scaled
        targets[sym] = scaled
    if cfg.defensive_symbol:
        targets[cfg.defensive_symbol] += freed
    else:
        cash_weight += freed
    # drop zero-weight entries so they are treated as "not in allocation"
    targets = {s: w for s, w in targets.items() if w > 1e-12}
    return targets, cash_weight


def plan_trades(
    targets: dict[str, float],
    cash_weight: float,
    holdings: dict[str, float],
    cash: float,
    cfg: Config,
) -> list[Trade]:
    """Turn current holdings (symbol -> market value) into a list of trades."""
    tr = cfg.trading
    total = cash + sum(holdings.values())
    if total <= 0:
        return []

    trades: list[Trade] = []
    proceeds = 0.0
    drift_value = tr.drift_threshold_pct / 100.0 * total
    shortfalls: dict[str, float] = {}

    for sym, weight in targets.items():
        current = holdings.get(sym, 0.0)
        target_value = total * weight
        deviation = current - target_value
        if deviation > drift_value:
            if tr.allow_sells and deviation >= tr.min_order_value:
                amount = round(deviation, 2)
                trades.append(Trade(sym, "sell", amount, f"overweight by {deviation / total:.1%}"))
                proceeds += amount
        elif deviation < 0:
            shortfalls[sym] = -deviation

    if tr.allow_sells:
        for sym, value in holdings.items():
            if sym not in targets and value >= tr.min_order_value:
                trades.append(Trade(sym, "sell", round(value, 2), "not in target allocation", close_position=True))
                proceeds += value

    investable = cash + proceeds - total * cash_weight
    if investable < tr.min_order_value:
        return trades

    buys = _allocate_buys(investable, shortfalls, targets)
    for sym, amount in buys.items():
        if amount >= tr.min_order_value:
            short = shortfalls.get(sym, 0.0)
            reason = f"underweight by {short / total:.1%}" if short > 0 else "investing idle cash"
            trades.append(Trade(sym, "buy", round(amount, 2), reason))
    return trades


def _allocate_buys(investable: float, shortfalls: dict[str, float], targets: dict[str, float]) -> dict[str, float]:
    """Fill shortfalls first (pro rata), then spread any remainder by target weight."""
    alloc: dict[str, float] = {}
    total_short = sum(shortfalls.values())
    if total_short > 0:
        scale = min(1.0, investable / total_short)
        for sym, short in shortfalls.items():
            alloc[sym] = short * scale
    remaining = investable - sum(alloc.values())
    if remaining > 0.01:
        weight_sum = sum(targets.values())
        for sym, weight in targets.items():
            alloc[sym] = alloc.get(sym, 0.0) + remaining * weight / weight_sum
    return alloc
