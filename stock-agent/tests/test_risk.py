from conftest import make_config
from stock_agent.models import Account, Clock, Trade
from stock_agent.risk import apply_risk_limits, drawdown_pct, preflight_checks


def test_preflight_blocks_closed_market(cfg):
    acct = Account(equity=1000, cash=1000, buying_power=1000)
    assert preflight_checks(cfg, acct, Clock(is_open=False, next_open="tomorrow")).startswith("market is closed")
    assert preflight_checks(cfg, acct, Clock(is_open=True)) is None


def test_preflight_blocks_blocked_account(cfg):
    acct = Account(equity=1000, cash=1000, buying_power=1000, trading_blocked=True)
    assert "blocked" in preflight_checks(cfg, acct, Clock(is_open=True))


def test_drawdown_pct():
    assert drawdown_pct([100, 120, 90]) == 25.0
    assert drawdown_pct([]) == 0.0
    assert drawdown_pct([100, 110]) == 0.0


def test_order_value_and_daily_caps(cfg):
    trades = [Trade("SCHD", "buy", 3000, "x"), Trade("VTI", "buy", 2500, "x"), Trade("BND", "buy", 1500, "x")]
    approved, rejected = apply_risk_limits(cfg, trades, 100_000, {}, set())
    amounts = {t.symbol: t.notional for t in approved}
    assert amounts["SCHD"] == 2000  # per-order cap
    assert amounts["VTI"] == 2000
    assert amounts["BND"] == 1000  # daily cap 5000 reached
    assert rejected == []


def test_position_weight_cap(cfg):
    trades = [Trade("SCHD", "buy", 1000, "x")]
    approved, rejected = apply_risk_limits(cfg, trades, 10_000, {"SCHD": 5800}, set())
    assert approved[0].notional == 200  # 60% of 10k = 6000 max
    approved, rejected = apply_risk_limits(cfg, trades, 10_000, {"SCHD": 6000}, set())
    assert approved == [] and "max weight" in rejected[0][1]


def test_open_order_and_unknown_symbol_rejected(cfg):
    trades = [Trade("SCHD", "buy", 100, "x"), Trade("TSLA", "buy", 100, "x"), Trade("TSLA", "sell", 100, "x", close_position=True)]
    approved, rejected = apply_risk_limits(cfg, trades, 10_000, {"TSLA": 100}, {"SCHD"})
    assert [t.symbol for t in approved] == ["TSLA"]
    assert approved[0].close_position
    reasons = [r for _, r in rejected]
    assert any("already open" in r for r in reasons)
    assert any("not in the configured allocation" in r for r in reasons)


def test_buys_halted_keeps_sells(cfg):
    trades = [Trade("SCHD", "buy", 100, "x"), Trade("VTI", "sell", 100, "x")]
    approved, rejected = apply_risk_limits(cfg, trades, 10_000, {"VTI": 500}, set(), "drawdown")
    assert [t.side for t in approved] == ["sell"]
    assert rejected[0][1] == "drawdown"


def test_clipped_sell_shrinks_buy_budget(cfg):
    # Plan assumed $3000 of SCHD proceeds, but the order cap allows only $2000.
    trades = [Trade("SCHD", "sell", 3000, "x"), Trade("BND", "buy", 3100, "x")]
    approved, rejected = apply_risk_limits(cfg, trades, 10_000, {"SCHD": 6000}, set(), cash_available=100.0)
    amounts = {t.symbol: t.notional for t in approved}
    assert amounts["SCHD"] == 2000
    assert amounts["BND"] == 2000  # min(order cap, 100 + 2000 budget)
    assert not rejected


def test_no_cash_rejects_buys(cfg):
    approved, rejected = apply_risk_limits(cfg, [Trade("SCHD", "buy", 500, "x")], 10_000, {}, set(), cash_available=2.0)
    assert approved == []
    assert "not enough cash" in rejected[0][1]
