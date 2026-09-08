import pytest

from conftest import make_config
from stock_agent.config import ConfigError, load_config, load_credentials


def test_weights_must_sum_to_one():
    with pytest.raises(ConfigError, match="sum to 1.0"):
        make_config(targets={"VTI": 0.5, "BND": 0.3})


def test_defensive_symbol_must_be_in_targets():
    with pytest.raises(ConfigError, match="defensive_symbol"):
        make_config(regime={"defensive_symbol": "TLT"})


def test_unknown_key_rejected():
    with pytest.raises(ConfigError, match="Unknown keys"):
        make_config(risk={"max_leverage": 3})


def test_symbols_are_uppercased():
    cfg = make_config(targets={"schd": 0.5, "bnd": 0.47}, regime={"enabled": False})
    assert set(cfg.targets) == {"SCHD", "BND"}
    assert cfg.equity_symbols == ["SCHD"]


def test_shipped_config_loads():
    from stock_agent.cli import DEFAULT_CONFIG

    cfg = load_config(DEFAULT_CONFIG)
    assert abs(sum(cfg.targets.values()) + cfg.cash_weight - 1.0) < 1e-6
    assert cfg.regime.defensive_symbol == "SGOV" and cfg.regime.evaluate == "monthly"
    assert cfg.risk.order_cap(50_000) == 5_000 and cfg.risk.daily_cap(50_000) == 15_000


def test_credentials_default_to_paper():
    creds = load_credentials({"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert not creds.live
    assert "paper-api" in creds.trading_base_url


def test_live_requires_exact_phrase():
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "STOCK_AGENT_LIVE_TRADING": "yes"}
    assert not load_credentials(env).live
    env["STOCK_AGENT_LIVE_TRADING"] = "I_UNDERSTAND_THE_RISKS"
    assert load_credentials(env).live


def test_missing_credentials():
    with pytest.raises(ConfigError):
        load_credentials({})


def test_risk_off_weight_must_fit_position_cap():
    with pytest.raises(ConfigError, match="risk-off"):
        make_config(risk={"max_position_weight": 0.40})


def test_single_target_above_cap_rejected():
    with pytest.raises(ConfigError, match="exceed risk.max_position_weight"):
        make_config(targets={"VTI": 0.97}, regime={"enabled": False})


def test_overrides_apply_and_coerce(tmp_path):
    from stock_agent.cli import DEFAULT_CONFIG

    cfg = load_config(DEFAULT_CONFIG, ["daytrade.reward_risk=1.5", "daytrade.allow_short=true", "daytrade.blocklist=[TSLA]"])
    assert cfg.daytrade.reward_risk == 1.5 and cfg.daytrade.allow_short is True and cfg.daytrade.blocklist == ("TSLA",)
    with pytest.raises(ConfigError, match="section.key"):
        load_config(DEFAULT_CONFIG, ["reward_risk=1"])
    with pytest.raises(ConfigError, match="Unknown keys"):
        load_config(DEFAULT_CONFIG, ["daytrade.nope=1"])
