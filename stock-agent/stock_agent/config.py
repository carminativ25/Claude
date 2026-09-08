"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LIVE_TRADING_PHRASE = "I_UNDERSTAND_THE_RISKS"


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


@dataclass(frozen=True)
class RegimeConfig:
    enabled: bool = True
    benchmark: str = "SPY"
    sma_days: int = 200
    risk_off_equity_scale: float = 0.5
    defensive_symbol: str | None = "BND"


@dataclass(frozen=True)
class RiskConfig:
    max_order_value: float = 2_000.0
    max_daily_trade_value: float = 5_000.0
    max_position_weight: float = 0.40
    max_drawdown_pct: float = 20.0
    require_market_open: bool = True


@dataclass(frozen=True)
class TradingConfig:
    cash_buffer_pct: float = 3.0
    drift_threshold_pct: float = 3.0
    min_order_value: float = 5.0
    allow_sells: bool = True


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 10_000.0
    monthly_contribution: float = 500.0
    rebalance_every_days: int = 5


@dataclass(frozen=True)
class Config:
    targets: dict[str, float]
    trading: TradingConfig = field(default_factory=TradingConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    @property
    def cash_weight(self) -> float:
        return self.trading.cash_buffer_pct / 100.0

    @property
    def defensive_symbol(self) -> str | None:
        return self.regime.defensive_symbol

    @property
    def equity_symbols(self) -> list[str]:
        return [s for s in self.targets if s != self.regime.defensive_symbol]


def _build(dc: type, raw: dict[str, Any] | None, section: str):
    raw = raw or {}
    allowed = set(dc.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in '{section}': {sorted(unknown)}")
    return dc(**raw)


def parse_config(raw: dict[str, Any]) -> Config:
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ConfigError("'targets' must be a non-empty mapping of symbol -> weight")

    targets: dict[str, float] = {}
    for symbol, weight in targets_raw.items():
        try:
            w = float(weight)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Weight for {symbol} is not a number: {weight!r}") from exc
        if w <= 0:
            raise ConfigError(f"Weight for {symbol} must be positive")
        targets[str(symbol).upper()] = w

    trading = _build(TradingConfig, raw.get("trading"), "trading")
    regime = _build(RegimeConfig, raw.get("regime"), "regime")
    risk = _build(RiskConfig, raw.get("risk"), "risk")
    backtest = _build(BacktestConfig, raw.get("backtest"), "backtest")

    total = sum(targets.values()) + trading.cash_buffer_pct / 100.0
    if abs(total - 1.0) > 0.005:
        raise ConfigError(
            f"Target weights plus cash buffer must sum to 1.0 (got {total:.4f}). "
            "Adjust 'targets' or 'trading.cash_buffer_pct'."
        )
    if regime.enabled and regime.defensive_symbol:
        regime = RegimeConfig(
            enabled=regime.enabled,
            benchmark=regime.benchmark.upper(),
            sma_days=regime.sma_days,
            risk_off_equity_scale=regime.risk_off_equity_scale,
            defensive_symbol=regime.defensive_symbol.upper(),
        )
        if regime.defensive_symbol not in targets:
            raise ConfigError(
                f"regime.defensive_symbol {regime.defensive_symbol} must also appear in 'targets'"
            )
    if not 0.0 <= regime.risk_off_equity_scale <= 1.0:
        raise ConfigError("regime.risk_off_equity_scale must be between 0 and 1")
    if regime.sma_days < 2:
        raise ConfigError("regime.sma_days must be at least 2")
    if not 0.0 < risk.max_position_weight <= 1.0:
        raise ConfigError("risk.max_position_weight must be between 0 and 1")
    if trading.min_order_value < 1.0:
        raise ConfigError("trading.min_order_value must be at least $1 (Alpaca minimum)")
    over = [s for s, w in targets.items() if w > risk.max_position_weight + 1e-9]
    if over:
        raise ConfigError(f"targets {over} exceed risk.max_position_weight {risk.max_position_weight:.0%}")
    if regime.enabled and regime.defensive_symbol:
        equity_weight = sum(w for s, w in targets.items() if s != regime.defensive_symbol)
        risk_off_weight = targets[regime.defensive_symbol] + equity_weight * (1 - regime.risk_off_equity_scale)
        if risk_off_weight > risk.max_position_weight + 1e-9:
            raise ConfigError(
                f"in risk-off, {regime.defensive_symbol} would be {risk_off_weight:.0%} of the portfolio, above "
                f"risk.max_position_weight {risk.max_position_weight:.0%}; raise the cap or lower the shift"
            )

    return Config(targets=targets, trading=trading, regime=regime, risk=risk, backtest=backtest)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a YAML mapping")
    return parse_config(raw)


@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret_key: str
    live: bool
    alert_webhook_url: str | None = None

    @property
    def trading_base_url(self) -> str:
        return "https://api.alpaca.markets" if self.live else "https://paper-api.alpaca.markets"

    @property
    def data_base_url(self) -> str:
        return "https://data.alpaca.markets"


def load_credentials(env: dict[str, str] | None = None) -> Credentials:
    """Read broker credentials from the environment.

    Live trading is only enabled when STOCK_AGENT_LIVE_TRADING is set to the
    exact phrase in LIVE_TRADING_PHRASE. Anything else means paper trading.
    """
    env = env if env is not None else dict(os.environ)
    api_key = env.get("ALPACA_API_KEY", "").strip()
    secret_key = env.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise ConfigError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set (see .env.example)"
        )
    live = env.get("STOCK_AGENT_LIVE_TRADING", "").strip() == LIVE_TRADING_PHRASE
    webhook = env.get("ALERT_WEBHOOK_URL", "").strip() or None
    return Credentials(api_key=api_key, secret_key=secret_key, live=live, alert_webhook_url=webhook)
