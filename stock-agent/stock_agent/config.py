"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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
    evaluate: str = "monthly"     # monthly: use the last close of the previous month; daily: use yesterday's close


@dataclass(frozen=True)
class RiskConfig:
    max_order_value: float = 2_000.0        # dollar cap per order (used when max_order_pct is 0)
    max_daily_trade_value: float = 5_000.0  # dollar cap per run (used when max_daily_trade_pct is 0)
    max_order_pct: float = 0.0              # per-order cap as % of equity; overrides the dollar cap when > 0
    max_daily_trade_pct: float = 0.0        # per-run cap as % of equity; overrides the dollar cap when > 0
    max_position_weight: float = 0.40
    max_drawdown_pct: float = 20.0          # beyond this drawdown from the 1-year peak: alert ...
    drawdown_pauses_buys: bool = False      # ... and, if true, also stop buying (off: a long-term investor keeps buying dips)
    require_market_open: bool = True

    def order_cap(self, equity: float) -> float:
        return equity * self.max_order_pct / 100.0 if self.max_order_pct > 0 else self.max_order_value

    def daily_cap(self, equity: float) -> float:
        return equity * self.max_daily_trade_pct / 100.0 if self.max_daily_trade_pct > 0 else self.max_daily_trade_value


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
class DayTradeConfig:
    """Settings for the news-driven intraday strategy."""

    max_picks: int = 3                    # positions opened per day
    risk_per_trade_pct: float = 0.5       # % of equity lost if a stop is hit
    max_position_pct: float = 20.0        # % of equity in one position
    max_daily_loss_pct: float = 2.0       # flatten everything and stop for the day
    min_price: float = 5.0
    max_price: float = 1000.0
    min_avg_dollar_volume: float = 20_000_000.0   # 20-day average $ traded per day
    max_spread_pct: float = 0.3           # bid/ask spread as % of price
    min_stop_pct: float = 0.75
    max_stop_pct: float = 4.0
    min_reward_risk: float = 1.5          # target distance / stop distance
    allow_short: bool = False
    entry_delay_minutes: int = 5          # wait this long after the open before entering
    flatten_minutes_before_close: int = 10
    news_lookback_hours: int = 18
    screener_top: int = 25
    respect_pdt_rule: bool = True         # <$25k accounts: max 3 day trades per 5 days
    analyst_model: str = "claude-opus-5"
    analyst_effort: str = "high"
    blocklist: tuple[str, ...] = ()
    journal_dir: str = "journal"
    # --- opening range breakout entry ---
    max_watchlist: int = 8                # symbols watched for a breakout each day
    range_minutes: int = 15               # opening range length
    entry_window_minutes: int = 90        # no new entries after 9:30 + this
    min_relative_volume: float = 2.0      # volume so far vs. pro-rata average day
    min_range_pct: float = 0.3            # ignore ranges narrower than this
    reward_risk: float = 2.0              # target = entry + reward_risk * (entry - stop)
    require_vwap_confirmation: bool = True
    min_gap_pct: float = 2.0              # watchlist: open vs. previous close, in percent
    min_gap_atr: float = 0.0              # watchlist: gap must also be at least this many daily ATRs (0 = off)
    mode: str = "breakout"                # breakout: trade with the gap; fade: trade against it (needs allow_short for gap-ups)
    poll_seconds: int = 60                # live loop interval
    data_feed: str = "iex"                # iex (free) or sip (paid Alpaca data plan)
    slippage_bps: float = 5.0             # backtest fill penalty per side, basis points
    universe: tuple[str, ...] = ()        # backtest universe; empty = built-in default list


@dataclass(frozen=True)
class Config:
    targets: dict[str, float]
    trading: TradingConfig = field(default_factory=TradingConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    daytrade: DayTradeConfig = field(default_factory=DayTradeConfig)

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


def _parse_daytrade(raw: dict[str, Any] | None) -> DayTradeConfig:
    raw = dict(raw or {})
    for key in ("blocklist", "universe"):
        if key in raw:
            raw[key] = tuple(str(x).upper() for x in (raw[key] or []))
    dt = _build(DayTradeConfig, raw, "daytrade")
    if dt.range_minutes < 1 or dt.entry_window_minutes <= dt.range_minutes:
        raise ConfigError("daytrade.entry_window_minutes must be longer than range_minutes")
    if dt.reward_risk < 1.0:
        raise ConfigError("daytrade.reward_risk must be at least 1.0")
    if dt.data_feed not in ("iex", "sip"):
        raise ConfigError("daytrade.data_feed must be iex or sip")
    if dt.mode not in ("breakout", "fade"):
        raise ConfigError("daytrade.mode must be breakout or fade")
    if dt.max_picks < 1:
        raise ConfigError("daytrade.max_picks must be at least 1")
    if not 0 < dt.risk_per_trade_pct <= 5:
        raise ConfigError("daytrade.risk_per_trade_pct must be between 0 and 5")
    if not 0 < dt.max_position_pct <= 100:
        raise ConfigError("daytrade.max_position_pct must be between 0 and 100")
    if not 0 < dt.max_daily_loss_pct <= 25:
        raise ConfigError("daytrade.max_daily_loss_pct must be between 0 and 25")
    if dt.min_stop_pct <= 0 or dt.max_stop_pct < dt.min_stop_pct:
        raise ConfigError("daytrade stop bounds must satisfy 0 < min_stop_pct <= max_stop_pct")
    if dt.min_reward_risk < 1.0:
        raise ConfigError("daytrade.min_reward_risk must be at least 1.0")
    if dt.analyst_effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("daytrade.analyst_effort must be one of low, medium, high, xhigh, max")
    return dt


def parse_config(raw: dict[str, Any]) -> Config:
    daytrade = _parse_daytrade(raw.get("daytrade"))
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
    regime = replace(regime, benchmark=regime.benchmark.upper(), defensive_symbol=regime.defensive_symbol.upper() if regime.defensive_symbol else None)
    if regime.evaluate not in ("monthly", "daily"):
        raise ConfigError("regime.evaluate must be monthly or daily")
    if regime.enabled and regime.defensive_symbol:
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

    return Config(targets=targets, trading=trading, regime=regime, risk=risk, backtest=backtest, daytrade=daytrade)


def apply_overrides(raw: dict[str, Any], overrides: list[str] | None) -> dict[str, Any]:
    """Apply command-line overrides like 'daytrade.reward_risk=1.5' to the raw config mapping."""
    for item in overrides or []:
        if "=" not in item:
            raise ConfigError(f"override must look like section.key=value, got {item!r}")
        dotted, value = item.split("=", 1)
        parts = dotted.strip().split(".")
        if len(parts) < 2:
            raise ConfigError(f"override key must be section.key, got {dotted!r}")
        node = raw
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"cannot override inside non-mapping {dotted!r}")
        node[parts[-1]] = yaml.safe_load(value)
    return raw


def load_config(path: str | Path, overrides: list[str] | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a YAML mapping")
    return parse_config(apply_overrides(raw, overrides))


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


def has_anthropic_credentials(env: dict[str, str] | None = None) -> bool:
    """True when the Anthropic SDK will be able to authenticate from the environment."""
    env = env if env is not None else dict(os.environ)
    return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))
