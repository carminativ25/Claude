"""Plain data structures shared across the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    status: str = "ACTIVE"
    trading_blocked: bool = False
    account_blocked: bool = False
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float = 0.0
    unrealized_pl: float = 0.0


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    side: str
    status: str
    notional: float | None = None
    qty: float | None = None
    filled_avg_price: float | None = None


@dataclass(frozen=True)
class Clock:
    is_open: bool
    next_open: str = ""
    next_close: str = ""


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


@dataclass(frozen=True)
class Dividend:
    symbol: str
    amount: float
    day: date


@dataclass(frozen=True)
class Trade:
    """An intended order, expressed in dollars."""

    symbol: str
    side: str  # "buy" or "sell"
    notional: float
    reason: str
    close_position: bool = False

    def with_notional(self, notional: float, reason: str | None = None) -> "Trade":
        return Trade(
            symbol=self.symbol,
            side=self.side,
            notional=round(notional, 2),
            reason=reason or self.reason,
            close_position=False,
        )


@dataclass
class RunReport:
    """Everything that happened during one agent run."""

    dry_run: bool
    live: bool
    equity: float = 0.0
    cash: float = 0.0
    regime: str = "n/a"
    targets: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    planned: list[Trade] = field(default_factory=list)
    approved: list[Trade] = field(default_factory=list)
    rejected: list[tuple[Trade, str]] = field(default_factory=list)
    submitted: list[Order] = field(default_factory=list)
    halted_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "LIVE" if self.live else "PAPER"
        lines = [f"[{mode}{' / DRY RUN' if self.dry_run else ''}] equity ${self.equity:,.2f}, cash ${self.cash:,.2f}, regime: {self.regime}"]
        if self.halted_reason:
            lines.append(f"HALTED: {self.halted_reason}")
        for w in self.warnings:
            lines.append(f"warning: {w}")
        if not self.planned:
            lines.append("no trades needed")
        for t in self.approved:
            what = "close" if t.close_position else f"${t.notional:,.2f}"
            lines.append(f"{t.side.upper():4} {t.symbol:6} {what:>12}  ({t.reason})")
        for t, why in self.rejected:
            lines.append(f"SKIP {t.side} {t.symbol} ${t.notional:,.2f}: {why}")
        if self.submitted:
            lines.append(f"submitted {len(self.submitted)} order(s)")
        return "\n".join(lines)
