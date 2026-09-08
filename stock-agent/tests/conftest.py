import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from stock_agent.config import parse_config

BASE_RAW = {
    "targets": {"SCHD": 0.30, "VYM": 0.15, "VTI": 0.25, "JEPI": 0.12, "BND": 0.15},
    "trading": {"cash_buffer_pct": 3.0, "drift_threshold_pct": 3.0, "min_order_value": 5.0, "allow_sells": True},
    "regime": {"enabled": True, "benchmark": "SPY", "sma_days": 5, "risk_off_equity_scale": 0.5, "defensive_symbol": "BND", "evaluate": "daily"},
    "risk": {"max_order_value": 2000.0, "max_daily_trade_value": 5000.0, "max_position_weight": 0.60, "max_drawdown_pct": 20.0, "require_market_open": True},
    "backtest": {"initial_cash": 10000.0, "monthly_contribution": 500.0, "rebalance_every_days": 1},
}


def make_config(**overrides):
    raw = {k: dict(v) if isinstance(v, dict) else v for k, v in BASE_RAW.items()}
    for section, values in overrides.items():
        if isinstance(values, dict) and section in raw and section != "targets":
            raw[section].update(values)
        else:
            raw[section] = values
    return parse_config(raw)


@pytest.fixture
def cfg():
    return make_config()
