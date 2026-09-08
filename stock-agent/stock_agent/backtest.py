"""Backtest the strategy on daily closes with periodic contributions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .broker import SimBroker
from .config import Config
from .models import Bar
from .risk import apply_risk_limits, drawdown_pct
from .strategy import detect_regime, effective_targets, plan_trades


@dataclass
class BacktestResult:
    start: date
    end: date
    contributed: float
    final_value: float
    benchmark_final_value: float
    max_drawdown_pct: float
    benchmark_max_drawdown_pct: float
    irr_pct: float | None
    benchmark_irr_pct: float | None
    trades: int
    risk_off_days: int
    equity_curve: list[tuple[date, float]] = field(default_factory=list)

    def summary(self) -> str:
        def pct(v):
            return "n/a" if v is None else f"{v:.2f}%"

        lines = [
            f"period            {self.start} -> {self.end}",
            f"contributed       ${self.contributed:,.2f}",
            f"final value       ${self.final_value:,.2f}   (benchmark buy-and-hold ${self.benchmark_final_value:,.2f})",
            f"profit            ${self.final_value - self.contributed:,.2f}   (benchmark ${self.benchmark_final_value - self.contributed:,.2f})",
            f"annualized IRR    {pct(self.irr_pct)}   (benchmark {pct(self.benchmark_irr_pct)})",
            f"max drawdown      {self.max_drawdown_pct:.2f}%   (benchmark {self.benchmark_max_drawdown_pct:.2f}%)",
            f"trades            {self.trades}",
            f"risk-off days     {self.risk_off_days}",
        ]
        return "\n".join(lines)


def align_bars(bars: dict[str, list[Bar]]) -> tuple[list[date], dict[str, dict[date, float]]]:
    """Return the trading days shared by every symbol plus a close lookup."""
    closes = {sym: {b.day: b.close for b in series} for sym, series in bars.items()}
    if not closes:
        return [], {}
    common = set.intersection(*(set(c) for c in closes.values()))
    return sorted(common), closes


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized internal rate of return for dated cashflows (negative = invested)."""
    if len(cashflows) < 2:
        return None
    t0 = cashflows[0][0]
    flows = [((d - t0).days / 365.25, amt) for d, amt in cashflows]
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None

    def npv(rate: float) -> float:
        return sum(amt / (1.0 + rate) ** t for t, amt in flows)

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2 * 100.0


def max_drawdown(curve: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100.0)
    return worst


def run_backtest(
    cfg: Config,
    bars: dict[str, list[Bar]],
    benchmark: str | None = None,
    warmup_bars: list[Bar] | None = None,
) -> BacktestResult:
    """Simulate the strategy over `bars` (symbol -> daily bars).

    `warmup_bars` are benchmark closes from before the period, used to seed the
    moving average so the regime filter is active from the first day.
    """
    bt = cfg.backtest
    benchmark = (benchmark or cfg.regime.benchmark).upper()
    needed = set(cfg.targets) | {benchmark}
    missing = needed - set(bars)
    if missing:
        raise ValueError(f"missing price history for {sorted(missing)}")

    days, closes = align_bars({s: bars[s] for s in needed})
    if len(days) < 2:
        raise ValueError("not enough overlapping price history to backtest")

    broker = SimBroker(cash=0.0)
    bench_shares, bench_cash = 0.0, 0.0
    contributed = 0.0
    cashflows: list[tuple[date, float]] = []
    curve: list[tuple[date, float]] = []
    bench_curve: list[float] = []
    bench_history: list[Bar] = list(warmup_bars or [])
    trades = 0
    risk_off_days = 0
    last_month: tuple[int, int] | None = None

    for i, day in enumerate(days):
        prices = {s: closes[s][day] for s in needed}
        broker.set_prices(prices)
        bench_history.append(Bar(day, prices[benchmark]))

        month = (day.year, day.month)
        deposit = bt.initial_cash if i == 0 else 0.0
        if last_month is not None and month != last_month:
            deposit += bt.monthly_contribution
        last_month = month
        if deposit > 0:
            broker.cash += deposit
            bench_cash += deposit
            contributed += deposit
            cashflows.append((day, -deposit))

        # Benchmark: buy-and-hold, every deposit invested immediately.
        if bench_cash > 0:
            bench_shares += bench_cash / prices[benchmark]
            bench_cash = 0.0

        if i % max(1, bt.rebalance_every_days) == 0:
            regime = detect_regime(cfg, bench_history)
            if regime.risk_off:
                risk_off_days += bt.rebalance_every_days
            targets, cash_weight = effective_targets(cfg, regime)
            holdings = {p.symbol: p.market_value for p in broker.get_positions()}
            planned = plan_trades(targets, cash_weight, holdings, broker.cash, cfg)
            history = [v for _, v in curve]
            halted = None
            if drawdown_pct(history) > cfg.risk.max_drawdown_pct:
                halted = "drawdown limit"
            cash_available = broker.cash - broker.equity * cash_weight
            approved, _ = apply_risk_limits(cfg, planned, broker.equity, holdings, set(), halted, cash_available)
            for trade in sorted(approved, key=lambda t: 0 if t.side == "sell" else 1):
                if trade.close_position:
                    broker.close_position(trade.symbol)
                else:
                    broker.submit_notional_order(trade.symbol, trade.side, trade.notional)
                trades += 1

        curve.append((day, broker.equity))
        bench_curve.append(bench_shares * prices[benchmark] + bench_cash)

    final_value = broker.equity
    bench_final = bench_curve[-1]
    end_day = days[-1]
    return BacktestResult(
        start=days[0],
        end=end_day,
        contributed=contributed,
        final_value=final_value,
        benchmark_final_value=bench_final,
        max_drawdown_pct=max_drawdown([v for _, v in curve]),
        benchmark_max_drawdown_pct=max_drawdown(bench_curve),
        irr_pct=xirr(cashflows + [(end_day, final_value)]),
        benchmark_irr_pct=xirr(cashflows + [(end_day, bench_final)]),
        trades=trades,
        risk_off_days=min(risk_off_days, len(days)),
        equity_curve=curve,
    )
