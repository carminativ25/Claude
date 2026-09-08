"""The four daily commands: plan (pre-market), open, monitor (intraday), close."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..broker import Broker, BrokerError
from ..config import Config
from ..models import Order
from . import journal
from .analyst import Analyst
from .orb import NoSignal, evaluate_breakout, session_open
from .plan import GamePlan, load_plan, save_plan, size_position, validate_picks
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
# trade: opening range breakout entries
# ---------------------------------------------------------------------------
def _avg_daily_volumes(broker: Broker, symbols: list[str], today: date) -> dict[str, float]:
    bars = broker.get_daily_bars(symbols, today - timedelta(days=45), today - timedelta(days=1)) if symbols else {}
    out = {}
    for sym, series in bars.items():
        window = series[-20:]
        out[sym] = sum(b.volume for b in window) / len(window) if window else 0.0
    return out


def _attempted_today(cfg: Config, day: date) -> set[str]:
    return {e["symbol"] for e in journal.load_day(cfg, day).get("events", []) if e.get("kind") in ("entry", "done")}


def trade_once(
    cfg: Config,
    broker: Broker,
    *,
    dry_run: bool = True,
    live: bool = False,
    today: date | None = None,
    now: datetime | None = None,
    plan: GamePlan | None = None,
    avg_volumes: dict[str, float] | None = None,
) -> SessionReport:
    """One pass over the watchlist: enter any symbol that has just broken out of its opening range."""
    today = today or date.today()
    dt = cfg.daytrade
    report = SessionReport(command="trade", dry_run=dry_run, live=live)
    account = broker.get_account()
    clock = broker.get_clock()
    report.equity = account.equity
    now = now or _parse_ts(clock.timestamp) or datetime.now(timezone.utc)

    plan = plan or load_plan(cfg, today)
    if plan is None:
        report.halted_reason = f"no game plan for {today}; run 'plan' first"
        return report
    if not plan.picks:
        report.lines.append("watchlist is empty; nothing to do")
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

    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    held = {p.symbol for p in positions} | {o.symbol for o in open_orders}
    attempted = _attempted_today(cfg, today)
    slots = dt.max_picks - len({p.symbol for p in positions} | {e for e in attempted})
    if dt.respect_pdt_rule and account.equity < PDT_EQUITY_THRESHOLD:
        pdt_slots = max(0, PDT_MAX_DAY_TRADES - account.daytrade_count - len(positions))
        if pdt_slots < slots:
            report.warnings.append(f"PDT rule: equity under $25k and {account.daytrade_count} day trades used; {pdt_slots} new position(s) allowed")
            slots = pdt_slots
    if slots <= 0:
        report.lines.append("no free position slots today")
        return report

    watch = [p for p in plan.picks if p.symbol not in held and p.symbol not in attempted]
    if not watch:
        report.lines.append("every watchlist symbol is already handled today")
        return report
    symbols = [p.symbol for p in watch]
    avg_volumes = avg_volumes or _avg_daily_volumes(broker, symbols, today)
    bars = broker.get_minute_bars(symbols, session_open(today), now)
    quotes = None
    cash = account.cash

    for pick in watch:
        if slots <= 0:
            break
        result = evaluate_breakout(cfg, pick.symbol, bars.get(pick.symbol, []), today, avg_volumes.get(pick.symbol, 0.0), pick.direction, now)
        if isinstance(result, NoSignal):
            report.lines.append(f"  {pick.symbol:6} {result.reason}")
            if result.final and not dry_run:
                journal.record(cfg, today, "done", {"symbol": pick.symbol, "reason": result.reason})
            continue
        if quotes is None:
            quotes = broker.get_latest_quotes(symbols)
        quote = quotes.get(pick.symbol)
        entry = result.entry
        if quote is not None and quote.ask > 0 and quote.bid > 0:
            if quote.spread_pct > dt.max_spread_pct:
                report.lines.append(f"  {pick.symbol:6} breakout but spread {quote.spread_pct:.2f}% too wide")
                continue
            entry = quote.ask if pick.direction == "long" else quote.bid
        # the stop stays at the range boundary; the target is measured from the price we actually pay
        target = entry + dt.reward_risk * (entry - result.stop) if pick.direction == "long" else entry - dt.reward_risk * (result.stop - entry)
        sized = size_position(cfg, pick.symbol, result.side, entry, result.stop, target, account.equity, cash)
        if sized is None:
            report.lines.append(f"  {pick.symbol:6} breakout but position would be under one share")
            if not dry_run:
                journal.record(cfg, today, "done", {"symbol": pick.symbol, "reason": "too small"})
            continue
        report.lines.append(
            f"{sized.side.upper():4} {sized.symbol:6} {sized.qty:>5} @ ~{sized.entry:.2f}  stop {sized.stop:.2f}  target {sized.target:.2f}  "
            f"range {result.range_low:.2f}-{result.range_high:.2f}  rvol {result.rvol:.1f}x  vwap {result.vwap:.2f}  risk ${sized.risk_dollars:,.2f}"
        )
        if dry_run:
            continue
        try:
            order = broker.submit_bracket_order(sized.symbol, sized.side, sized.qty, sized.target, sized.stop)
        except BrokerError as exc:
            report.warnings.append(f"{sized.symbol}: broker rejected order: {exc}")
            continue
        cash -= sized.qty * sized.entry
        slots -= 1
        report.orders.append(order)
        journal.record(cfg, today, "entry", {
            "symbol": sized.symbol, "side": sized.side, "qty": sized.qty, "entry": sized.entry, "stop": sized.stop, "target": sized.target,
            "range_high": result.range_high, "range_low": result.range_low, "rvol": result.rvol, "vwap": result.vwap,
            "order_id": order.id, "catalyst": pick.catalyst,
        })
    if not dry_run and report.orders:
        report.lines.append(f"submitted {len(report.orders)} bracket order(s)")
    return report


def trade_loop(cfg: Config, broker: Broker, *, dry_run: bool, live: bool, today: date | None = None, on_report=None, sleep=time.sleep) -> list[SessionReport]:
    """Poll for breakouts during the entry window, then keep enforcing the loss limit until flat at the close."""
    today = today or date.today()
    reports: list[SessionReport] = []
    plan = load_plan(cfg, today)
    avg_volumes = _avg_daily_volumes(broker, [p.symbol for p in plan.picks], today) if plan and plan.picks else {}
    window_end = session_open(today) + timedelta(minutes=cfg.daytrade.entry_window_minutes)
    while True:
        clock = broker.get_clock()
        now = _parse_ts(clock.timestamp) or datetime.now(timezone.utc)
        if now < window_end:
            rep = trade_once(cfg, broker, dry_run=dry_run, live=live, today=today, now=now, plan=plan, avg_volumes=avg_volumes)
        else:
            rep = monitor(cfg, broker, dry_run=dry_run, live=live, today=today, now=now)
        reports.append(rep)
        if on_report:
            on_report(rep)
        if rep.halted_reason and "loss limit" in rep.halted_reason:
            break
        if not clock.is_open or any("end of day" in line for line in rep.lines):
            break
        sleep(cfg.daytrade.poll_seconds)
    return reports


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
