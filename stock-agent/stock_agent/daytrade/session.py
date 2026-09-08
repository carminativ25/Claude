"""The four daily commands: plan (pre-market), open, monitor (intraday), close."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..broker import Broker, BrokerError
from ..config import Config
from ..models import Order
from . import journal
from .analyst import Analyst
from .plan import GamePlan, Sized, load_plan, save_plan, size_position, validate_picks
from .scan import scan

log = logging.getLogger(__name__)

PDT_EQUITY_THRESHOLD = 25_000.0
PDT_MAX_DAY_TRADES = 3


@dataclass
class SessionReport:
    command: str
    dry_run: bool
    live: bool
    equity: float = 0.0
    halted_reason: str | None = None
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    def summary(self) -> str:
        mode = "LIVE" if self.live else "PAPER"
        out = [f"[{mode}{' / DRY RUN' if self.dry_run else ''}] {self.command}: equity ${self.equity:,.2f}"]
        if self.halted_reason:
            out.append(f"HALTED: {self.halted_reason}")
        out.extend(f"warning: {w}" for w in self.warnings)
        out.extend(self.lines)
        return "\n".join(out)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def daily_loss_pct(equity: float, last_equity: float) -> float:
    if last_equity <= 0:
        return 0.0
    return max(0.0, (last_equity - equity) / last_equity * 100.0)


def loss_limit_hit(cfg: Config, equity: float, last_equity: float) -> bool:
    return daily_loss_pct(equity, last_equity) >= cfg.daytrade.max_daily_loss_pct


def _halted_today(cfg: Config, day: date) -> bool:
    return any(e.get("kind") == "halt" for e in journal.load_day(cfg, day).get("events", []))


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def plan_day(cfg: Config, broker: Broker, analyst: Analyst, *, today: date | None = None, live: bool = False) -> tuple[GamePlan, SessionReport]:
    today = today or date.today()
    report = SessionReport(command="plan", dry_run=True, live=live)
    account = broker.get_account()
    report.equity = account.equity

    result = scan(cfg, broker, today=today)
    plan = analyst.propose(cfg, today, result.candidates, result.news)
    picks, rejected = validate_picks(cfg, plan.picks, result.candidates)
    plan.picks = picks
    plan.rejected = rejected
    path = save_plan(cfg, plan)
    journal.record(cfg, today, "plan", {"analyst": plan.analyst, "picks": [p.symbol for p in picks], "rejected": rejected, "candidates": len(result.candidates), "headlines": len(result.news)})

    report.lines.append(f"{len(result.news)} headlines, {len(result.candidates)} candidates after filters, {len(result.dropped)} dropped")
    report.lines.append(plan.summary())
    report.lines.append(f"plan saved to {path}")
    return plan, report


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------
def open_day(cfg: Config, broker: Broker, *, dry_run: bool = True, live: bool = False, today: date | None = None, plan: GamePlan | None = None) -> SessionReport:
    today = today or date.today()
    dt = cfg.daytrade
    report = SessionReport(command="open", dry_run=dry_run, live=live)
    account = broker.get_account()
    clock = broker.get_clock()
    report.equity = account.equity

    plan = plan or load_plan(cfg, today)
    if plan is None:
        report.halted_reason = f"no game plan for {today}; run 'plan' first"
        return report
    if not plan.picks:
        report.lines.append("game plan has no picks; nothing to open")
        return report
    if account.trading_blocked or account.account_blocked or account.status.upper() not in ("", "ACTIVE"):
        report.halted_reason = "account is blocked or inactive"
        return report
    if not clock.is_open:
        report.halted_reason = f"market is closed (next open {clock.next_open or 'unknown'})"
        return report
    if _halted_today(cfg, today):
        report.halted_reason = "trading was halted earlier today by the loss limit"
        return report
    if loss_limit_hit(cfg, account.equity, account.last_equity):
        report.halted_reason = f"already down {daily_loss_pct(account.equity, account.last_equity):.2f}% today"
        return report

    already = {p.symbol for p in broker.get_positions()} | {o.symbol for o in broker.get_open_orders()}
    picks = list(plan.picks)
    if dt.respect_pdt_rule and account.equity < PDT_EQUITY_THRESHOLD:
        allowed = max(0, PDT_MAX_DAY_TRADES - account.daytrade_count)
        if allowed < len(picks):
            report.warnings.append(f"PDT rule: equity under $25k and {account.daytrade_count} day trades used; limiting to {allowed} new position(s)")
            picks = picks[:allowed]

    quotes = broker.get_latest_quotes([p.symbol for p in picks]) if picks else {}
    cash = account.cash
    for pick in picks:
        if pick.symbol in already:
            report.lines.append(f"skip {pick.symbol}: already has a position or open order")
            continue
        quote = quotes.get(pick.symbol)
        if quote is None or quote.ask <= 0:
            report.lines.append(f"skip {pick.symbol}: no live quote")
            continue
        if quote.spread_pct > dt.max_spread_pct:
            report.lines.append(f"skip {pick.symbol}: spread {quote.spread_pct:.2f}% too wide at the open")
            continue
        entry = quote.ask if pick.direction == "long" else quote.bid
        sized = size_position(cfg, pick, entry, account.equity, cash)
        if sized is None:
            report.lines.append(f"skip {pick.symbol}: position would be smaller than one share")
            continue
        cash -= sized.qty * sized.entry
        report.lines.append(
            f"{sized.side.upper():4} {sized.symbol:6} {sized.qty:>5} @ ~{sized.entry:.2f}  stop {sized.stop:.2f}  target {sized.target:.2f}  risk ${sized.risk_dollars:,.2f}"
        )
        if dry_run:
            continue
        try:
            order = broker.submit_bracket_order(sized.symbol, sized.side, sized.qty, sized.target, sized.stop)
        except BrokerError as exc:
            report.warnings.append(f"{sized.symbol}: broker rejected order: {exc}")
            continue
        report.orders.append(order)
        journal.record(cfg, today, "entry", {"symbol": sized.symbol, "side": sized.side, "qty": sized.qty, "entry": sized.entry, "stop": sized.stop, "target": sized.target, "order_id": order.id, "catalyst": pick.catalyst})
    if not dry_run:
        report.lines.append(f"submitted {len(report.orders)} bracket order(s)")
    return report


# ---------------------------------------------------------------------------
# monitor / close
# ---------------------------------------------------------------------------
def flatten(cfg: Config, broker: Broker, today: date, reason: str, report: SessionReport, dry_run: bool) -> None:
    positions = broker.get_positions()
    fills_before = len(broker.get_fills(today))
    if dry_run:
        report.lines.append(f"would close {len(positions)} position(s) and cancel open orders ({reason})")
        return
    broker.cancel_all_orders()
    orders = broker.close_all_positions()
    report.orders.extend(orders)
    report.lines.append(f"closed {len(orders)} position(s) and cancelled open orders ({reason})")
    fills = broker.get_fills(today)
    pnl = journal.realized_pnl_by_symbol(fills)
    account = broker.get_account()
    journal.record(cfg, today, "close", {"reason": reason, "pnl_by_symbol": pnl, "equity": account.equity, "fills": len(fills) - fills_before})
    for sym, value in sorted(pnl.items()):
        report.lines.append(f"  {sym:6} {value:>+10,.2f}")
    report.lines.append(f"  {'TOTAL':6} {sum(pnl.values()):>+10,.2f}")


def monitor(cfg: Config, broker: Broker, *, dry_run: bool = True, live: bool = False, today: date | None = None, now: datetime | None = None) -> SessionReport:
    today = today or date.today()
    report = SessionReport(command="monitor", dry_run=dry_run, live=live)
    account = broker.get_account()
    clock = broker.get_clock()
    report.equity = account.equity
    positions = broker.get_positions()
    pnl_today = account.equity - account.last_equity if account.last_equity else 0.0
    report.lines.append(f"{len(positions)} open position(s); day P&L {pnl_today:+,.2f} ({-daily_loss_pct(account.equity, account.last_equity):+.2f}%)")
    for p in positions:
        report.lines.append(f"  {p.symbol:6} qty {p.qty:g}  value ${p.market_value:,.2f}  unrealized {p.unrealized_pl:+,.2f}")

    if not clock.is_open:
        report.lines.append("market is closed")
        return report

    if loss_limit_hit(cfg, account.equity, account.last_equity):
        reason = f"daily loss limit hit ({daily_loss_pct(account.equity, account.last_equity):.2f}%)"
        report.halted_reason = reason
        if not dry_run:
            journal.record(cfg, today, "halt", {"reason": reason, "equity": account.equity})
        flatten(cfg, broker, today, reason, report, dry_run)
        return report

    now = now or _parse_ts(clock.timestamp) or datetime.now(timezone.utc)
    next_close = _parse_ts(clock.next_close)
    if next_close and positions and next_close - now <= timedelta(minutes=cfg.daytrade.flatten_minutes_before_close):
        flatten(cfg, broker, today, "end of day", report, dry_run)
    return report


def close_day(cfg: Config, broker: Broker, *, dry_run: bool = True, live: bool = False, today: date | None = None) -> SessionReport:
    today = today or date.today()
    report = SessionReport(command="close", dry_run=dry_run, live=live)
    account = broker.get_account()
    report.equity = account.equity
    flatten(cfg, broker, today, "end of day", report, dry_run)
    return report
