from datetime import date, timedelta

from conftest import make_config
from stock_agent.agent import run_once
from stock_agent.broker import SimBroker
from stock_agent.models import Bar

# Loose caps so a fresh $10k account can be deployed in a single run.
LOOSE = make_config(risk={"max_order_value": 1e6, "max_daily_trade_value": 1e6})

PRICES = {"SCHD": 80.0, "VYM": 120.0, "VTI": 250.0, "JEPI": 55.0, "BND": 72.0, "SPY": 500.0}


def with_bars(broker, closes):
    d0 = date.today() - timedelta(days=len(closes))
    broker.bars["SPY"] = [Bar(d0 + timedelta(days=i), c) for i, c in enumerate(closes)]
    return broker


def test_dry_run_submits_nothing():
    cfg = LOOSE
    broker = with_bars(SimBroker(cash=10_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    report = run_once(cfg, broker, dry_run=True)
    assert report.halted_reason is None
    assert len(report.approved) == 5
    assert broker.orders == []
    assert "DRY RUN" in report.summary()


def test_execute_invests_fresh_account():
    cfg = LOOSE
    broker = with_bars(SimBroker(cash=10_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    report = run_once(cfg, broker, dry_run=False)
    assert len(report.submitted) == 5
    assert broker.cash == __import__("pytest").approx(300.0, abs=0.05)
    weights = {p.symbol: p.market_value / broker.equity for p in broker.get_positions()}
    assert abs(weights["SCHD"] - 0.30) < 0.001
    assert report.regime.startswith("risk-on")


def test_execute_respects_order_caps(cfg):
    broker = with_bars(SimBroker(cash=100_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    report = run_once(cfg, broker, dry_run=False)
    assert sum(o.notional for o in report.submitted) <= 5000.0 + 1e-6
    assert max(o.notional for o in report.submitted) <= 2000.0 + 1e-6


def test_halts_when_market_closed(cfg):
    broker = SimBroker(cash=10_000, prices=PRICES, market_open=False)
    report = run_once(cfg, broker, dry_run=False)
    assert report.halted_reason and "closed" in report.halted_reason
    assert broker.orders == []


def test_risk_off_moves_into_defensive():
    cfg = LOOSE
    broker = with_bars(SimBroker(cash=10_000, prices=PRICES), [520, 515, 510, 505, 500, 450])
    report = run_once(cfg, broker, dry_run=False)
    assert report.regime.startswith("risk-off")
    weights = {p.symbol: p.market_value / broker.equity for p in broker.get_positions()}
    assert abs(weights["BND"] - 0.56) < 0.001
    assert abs(weights["SCHD"] - 0.15) < 0.001


def test_drawdown_pauses_buys_but_not_sells(cfg):
    broker = with_bars(SimBroker(cash=1_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    broker.shares = {"SCHD": 100}  # $8000 -> heavily overweight
    broker.equity_history = [15_000, 9_000]  # 40% drawdown
    report = run_once(cfg, broker, dry_run=False)
    assert any("drawdown" in w for w in report.warnings)
    assert [o.side for o in report.submitted] == ["sell"]
    assert any("buys paused" in why for _, why in report.rejected)


def test_open_orders_skip_symbol(cfg):
    from stock_agent.models import Order

    broker = with_bars(SimBroker(cash=10_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    broker.open_orders = [Order(id="1", symbol="VTI", side="buy", status="new")]
    report = run_once(cfg, broker, dry_run=False)
    assert "VTI" not in {o.symbol for o in report.submitted}


def test_missing_benchmark_history_warns_and_stays_risk_on(cfg):
    broker = SimBroker(cash=10_000, prices=PRICES)
    report = run_once(cfg, broker, dry_run=True)
    assert any("not enough benchmark history" in w for w in report.warnings)
    assert report.targets == cfg.targets


def test_trade_log_written(tmp_path):
    cfg = LOOSE
    broker = with_bars(SimBroker(cash=10_000, prices=PRICES), [480, 485, 490, 495, 500, 505])
    log = tmp_path / "trades.csv"
    run_once(cfg, broker, dry_run=False, trade_log=log)
    lines = log.read_text().splitlines()
    assert lines[0].startswith("timestamp_utc,mode,symbol")
    assert len(lines) == 6


def test_unfilled_sell_skips_buys(cfg, monkeypatch):
    from stock_agent.models import Order

    broker = with_bars(SimBroker(cash=100, prices=PRICES), [480, 485, 490, 495, 500, 505])
    broker.shares = {"SCHD": 100}  # $8000, overweight

    real_submit = broker.submit_notional_order

    def slow_sell(symbol, side, notional):
        order = real_submit(symbol, side, notional)
        stuck = Order(id=order.id, symbol=symbol, side=side, status="new", notional=notional)
        broker.orders[-1] = stuck
        return stuck

    monkeypatch.setattr(broker, "submit_notional_order", slow_sell)
    report = run_once(cfg, broker, dry_run=False, fill_timeout=0.0)
    assert [o.side for o in report.submitted] == ["sell"]
    assert any("not filled" in w for w in report.warnings)
    held = [t.symbol for t, why in report.rejected if why == "waiting for sell proceeds"]
    assert held == ["VYM", "VTI"]  # the buys that had funding were held back
