import math
from datetime import date, timedelta

from conftest import make_config
from stock_agent.backtest import align_bars, max_drawdown, run_backtest, xirr
from stock_agent.models import Bar


def synthetic_bars(symbols, days=300, seed=7):
    """Deterministic, mildly trending price paths with a mid-period dip."""
    out = {}
    d0 = date(2023, 1, 2)
    for k, sym in enumerate(symbols):
        price = 100.0 + 10 * k
        series = []
        for i in range(days):
            wobble = math.sin((i + seed * k) / 9.0) * 0.004
            trend = 0.0006 if sym != "BND" else 0.0001
            dip = -0.006 if 120 <= i < 170 and sym != "BND" else 0.0
            price *= 1 + trend + wobble + dip
            series.append(Bar(d0 + timedelta(days=i), round(price, 4)))
        out[sym] = series
    return out


def test_xirr_simple():
    flows = [(date(2020, 1, 1), -1000.0), (date(2021, 1, 1), 1100.0)]
    assert abs(xirr(flows) - 10.0) < 0.05
    assert xirr([(date(2020, 1, 1), -1.0)]) is None


def test_max_drawdown():
    assert max_drawdown([100, 80, 120, 60]) == 50.0


def test_align_bars_uses_common_days():
    a = [Bar(date(2024, 1, 1), 1), Bar(date(2024, 1, 2), 2)]
    b = [Bar(date(2024, 1, 2), 3), Bar(date(2024, 1, 3), 4)]
    days, closes = align_bars({"A": a, "B": b})
    assert days == [date(2024, 1, 2)]
    assert closes["B"][date(2024, 1, 2)] == 3


def test_backtest_runs_and_accounts_for_contributions():
    cfg = make_config(regime={"sma_days": 50})
    bars = synthetic_bars(list(cfg.targets) + ["SPY"])
    result = run_backtest(cfg, bars)
    months = len({(d.year, d.month) for d in (b.day for b in bars["SPY"])})
    assert result.contributed == 10_000 + 500 * (months - 1)
    assert result.final_value > 0
    assert result.trades > 0
    assert result.risk_off_days > 0  # the dip pushes SPY below its SMA
    assert len(result.equity_curve) == len(bars["SPY"])
    assert result.irr_pct is not None and result.benchmark_irr_pct is not None
    assert "final value" in result.summary()


def test_backtest_requires_all_symbols(cfg):
    bars = synthetic_bars(["SPY", "SCHD"])
    try:
        run_backtest(cfg, bars)
    except ValueError as exc:
        assert "missing price history" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_warmup_enables_regime_from_day_one():
    cfg = make_config(regime={"sma_days": 50}, backtest={"rebalance_every_days": 1})
    bars = synthetic_bars(list(cfg.targets) + ["SPY"], days=80)
    warm = [Bar(date(2022, 1, 1) + timedelta(days=i), 500.0) for i in range(60)]  # far above prices -> risk-off
    without = run_backtest(cfg, bars)
    with_warm = run_backtest(cfg, bars, warmup_bars=warm)
    assert with_warm.risk_off_days > without.risk_off_days
