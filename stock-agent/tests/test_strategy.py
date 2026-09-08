from datetime import date, timedelta

from conftest import make_config
from stock_agent.models import Bar
from stock_agent.strategy import detect_regime, effective_targets, plan_trades


def bars(closes):
    d0 = date(2024, 1, 1)
    return [Bar(d0 + timedelta(days=i), c) for i, c in enumerate(closes)]


def test_regime_risk_on_and_off(cfg):
    assert detect_regime(cfg, bars([100, 101, 102, 103, 104, 110])).name == "risk-on"
    assert detect_regime(cfg, bars([110, 108, 106, 104, 102, 90])).name == "risk-off"
    assert detect_regime(cfg, bars([100, 101])).name == "unknown"


def test_regime_disabled():
    cfg = make_config(regime={"enabled": False})
    assert detect_regime(cfg, bars([1, 1, 1, 1, 1, 0])).name == "disabled"


def test_effective_targets_shift_to_defensive(cfg):
    regime = detect_regime(cfg, bars([110, 108, 106, 104, 102, 90]))
    targets, cash_w = effective_targets(cfg, regime)
    assert targets["SCHD"] == 0.15 and targets["VTI"] == 0.125
    assert abs(targets["BND"] - (0.15 + 0.41)) < 1e-9
    assert abs(sum(targets.values()) + cash_w - 1.0) < 1e-9


def test_effective_targets_to_cash_when_no_defensive():
    cfg = make_config(regime={"defensive_symbol": None, "risk_off_equity_scale": 0.0})
    regime = detect_regime(cfg, bars([110, 108, 106, 104, 102, 90]))
    targets, cash_w = effective_targets(cfg, regime)
    assert targets == {}
    assert abs(cash_w - 1.0) < 1e-9


def test_fresh_account_buys_by_target_weight(cfg):
    trades = plan_trades(cfg.targets, cfg.cash_weight, {}, 10_000.0, cfg)
    assert all(t.side == "buy" for t in trades)
    by = {t.symbol: t.notional for t in trades}
    assert abs(by["SCHD"] - 3000) < 0.01
    assert abs(by["BND"] - 1500) < 0.01
    assert abs(sum(by.values()) - 9700) < 0.05  # 3% cash buffer kept


def test_idle_cash_fills_underweight_first(cfg):
    holdings = {"SCHD": 3000, "VYM": 1500, "VTI": 2500, "JEPI": 1200, "BND": 500}  # BND short by 1000
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 1300.0, cfg)
    buys = {t.symbol: t.notional for t in trades if t.side == "buy"}
    # total = 10000; BND target 1500 -> short 1000; investable = 1300 - 300 = 1000
    assert set(buys) == {"BND"}
    assert abs(buys["BND"] - 1000) < 0.01
    assert not [t for t in trades if t.side == "sell"]


def test_small_drift_does_not_sell(cfg):
    holdings = {"SCHD": 3150, "VYM": 1500, "VTI": 2500, "JEPI": 1200, "BND": 1350}  # SCHD +1.5%
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 300.0, cfg)
    assert not [t for t in trades if t.side == "sell"]


def test_large_drift_sells_and_redeploys(cfg):
    holdings = {"SCHD": 3800, "VYM": 1500, "VTI": 2500, "JEPI": 1200, "BND": 700}  # SCHD +8%
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 300.0, cfg)
    sells = {t.symbol: t.notional for t in trades if t.side == "sell"}
    buys = {t.symbol: t.notional for t in trades if t.side == "buy"}
    assert abs(sells["SCHD"] - 800) < 0.01
    assert abs(buys["BND"] - 800) < 0.01


def test_allow_sells_false_never_sells():
    cfg = make_config(trading={"allow_sells": False})
    holdings = {"SCHD": 5000, "XYZ": 1000}
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 0.0, cfg)
    assert not [t for t in trades if t.side == "sell"]


def test_symbol_outside_allocation_is_closed(cfg):
    holdings = {"SCHD": 3000, "XYZ": 1000}
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 0.0, cfg)
    closes = [t for t in trades if t.close_position]
    assert [t.symbol for t in closes] == ["XYZ"]


def test_dust_is_ignored(cfg):
    holdings = {"SCHD": 3000, "VYM": 1500, "VTI": 2500, "JEPI": 1200, "BND": 1500}
    trades = plan_trades(cfg.targets, cfg.cash_weight, holdings, 303.0, cfg)  # only $3 above buffer
    assert trades == []
