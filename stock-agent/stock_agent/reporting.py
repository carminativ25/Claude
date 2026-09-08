"""Human-readable portfolio and income reports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .broker import Broker
from .config import Config


def status_report(cfg: Config, broker: Broker) -> str:
    account = broker.get_account()
    positions = broker.get_positions()
    total = account.cash + sum(p.market_value for p in positions)
    lines = [
        f"equity        ${account.equity:,.2f}",
        f"cash          ${account.cash:,.2f}  ({(account.cash / total if total else 0):.1%}, target {cfg.cash_weight:.1%})",
        "",
        f"{'symbol':8}{'value':>14}{'weight':>9}{'target':>9}{'drift':>9}{'unrl P/L':>12}",
    ]
    by_symbol = {p.symbol: p for p in positions}
    for sym in sorted(set(cfg.targets) | set(by_symbol)):
        pos = by_symbol.get(sym)
        value = pos.market_value if pos else 0.0
        weight = value / total if total else 0.0
        target = cfg.targets.get(sym, 0.0)
        pl = pos.unrealized_pl if pos else 0.0
        lines.append(f"{sym:8}{value:>14,.2f}{weight:>9.1%}{target:>9.1%}{weight - target:>+9.1%}{pl:>12,.2f}")
    return "\n".join(lines)


def income_report(broker: Broker, days: int = 365, today: date | None = None) -> str:
    today = today or date.today()
    since = today - timedelta(days=days)
    dividends = broker.get_dividends(since)
    account = broker.get_account()
    total = sum(d.amount for d in dividends)
    by_month: dict[str, float] = defaultdict(float)
    by_symbol: dict[str, float] = defaultdict(float)
    for d in dividends:
        by_month[d.day.strftime("%Y-%m")] += d.amount
        by_symbol[d.symbol] += d.amount

    lines = [f"dividend income, last {days} days: ${total:,.2f}"]
    if account.equity > 0 and days > 0:
        annualized = total * 365.0 / days
        lines.append(f"annualized run-rate: ${annualized:,.2f}  ({annualized / account.equity:.2%} of current equity)")
    if by_month:
        lines.append("")
        lines.append("by month")
        for month in sorted(by_month):
            lines.append(f"  {month}  ${by_month[month]:>10,.2f}")
        lines.append("")
        lines.append("by symbol")
        for sym, amt in sorted(by_symbol.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {sym:8}${amt:>10,.2f}")
    else:
        lines.append("no dividends recorded yet in this period")
    return "\n".join(lines)
