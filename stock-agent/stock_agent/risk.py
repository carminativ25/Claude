"""Hard risk limits applied to every planned trade before it reaches the broker."""

from __future__ import annotations

from .config import Config
from .models import Account, Clock, Trade


def preflight_checks(cfg: Config, account: Account, clock: Clock) -> str | None:
    """Return a reason to halt the run entirely, or None if trading may proceed."""
    if account.status.upper() not in ("", "ACTIVE"):
        return f"account status is {account.status}"
    if account.trading_blocked or account.account_blocked:
        return "account is blocked from trading"
    if cfg.risk.require_market_open and not clock.is_open:
        return f"market is closed (next open {clock.next_open or 'unknown'})"
    if account.equity <= 0:
        return "account equity is zero"
    return None


def drawdown_pct(equity_history: list[float]) -> float:
    """Current drawdown from the running peak, in percent."""
    if not equity_history:
        return 0.0
    peak = max(equity_history)
    last = equity_history[-1]
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - last) / peak * 100.0)


def apply_risk_limits(
    cfg: Config,
    trades: list[Trade],
    total_equity: float,
    holdings: dict[str, float],
    open_order_symbols: set[str],
    buys_halted_reason: str | None = None,
    cash_available: float | None = None,
) -> tuple[list[Trade], list[tuple[Trade, str]]]:
    """Clip or reject trades so that no configured limit can be exceeded.

    Sells are evaluated first; `cash_available` (cash above the buffer) plus the
    proceeds of approved sells is the hard budget for buys, so a clipped sell can
    never leave the buys under-funded.
    """
    risk = cfg.risk
    approved: list[Trade] = []
    rejected: list[tuple[Trade, str]] = []
    daily_total = 0.0
    budget = float("inf") if cash_available is None else max(0.0, cash_available)

    for trade in sorted(trades, key=lambda t: 0 if t.side == "sell" else 1):
        if trade.symbol in open_order_symbols:
            rejected.append((trade, "an order for this symbol is already open"))
            continue
        if trade.side == "buy":
            if buys_halted_reason:
                rejected.append((trade, buys_halted_reason))
                continue
            if trade.symbol not in cfg.targets:
                rejected.append((trade, "symbol is not in the configured allocation"))
                continue

        notional = trade.notional
        notes: list[str] = []

        if trade.side == "buy" and total_equity > 0:
            room = risk.max_position_weight * total_equity - holdings.get(trade.symbol, 0.0)
            if room <= 0:
                rejected.append((trade, f"position already at max weight {risk.max_position_weight:.0%}"))
                continue
            if notional > room:
                notional = room
                notes.append("clipped to max position weight")

        if notional > risk.max_order_value:
            notional = risk.max_order_value
            notes.append("clipped to max order value")

        if trade.side == "buy":
            if budget < cfg.trading.min_order_value:
                rejected.append((trade, "not enough cash after sells and cash buffer"))
                continue
            if notional > budget:
                notional = budget
                notes.append("clipped to available cash")

        remaining_daily = risk.max_daily_trade_value - daily_total
        if remaining_daily <= 0:
            rejected.append((trade, "daily trade value limit reached"))
            continue
        if notional > remaining_daily:
            notional = remaining_daily
            notes.append("clipped to daily trade limit")

        if notional < cfg.trading.min_order_value:
            rejected.append((trade, "order too small after applying limits"))
            continue

        daily_total += notional
        if trade.side == "buy":
            budget -= notional
        else:
            budget += notional
        if notes and not (trade.close_position and abs(notional - trade.notional) < 0.01):
            approved.append(trade.with_notional(notional, f"{trade.reason}; {', '.join(notes)}"))
        else:
            approved.append(trade)
    return approved, rejected
