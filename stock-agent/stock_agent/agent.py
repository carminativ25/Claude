"""One full agent cycle: observe -> decide -> check risk -> act -> report."""

from __future__ import annotations

import csv
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from .broker import Broker, BrokerError, bars_lookback_start
from .config import Config
from .models import RunReport, Trade
from .risk import apply_risk_limits, drawdown_pct, preflight_checks
from .strategy import detect_regime, effective_targets, plan_trades

log = logging.getLogger(__name__)


def run_once(
    cfg: Config,
    broker: Broker,
    *,
    dry_run: bool = True,
    live: bool = False,
    today: date | None = None,
    trade_log: Path | None = None,
    fill_timeout: float = 90.0,
) -> RunReport:
    today = today or date.today()
    report = RunReport(dry_run=dry_run, live=live)

    account = broker.get_account()
    clock = broker.get_clock()
    report.equity, report.cash = account.equity, account.cash

    halt = preflight_checks(cfg, account, clock)
    if halt:
        report.halted_reason = halt
        log.warning("run halted: %s", halt)
        return report
    if account.pattern_day_trader:
        report.warnings.append("account is flagged as a pattern day trader")

    positions = broker.get_positions()
    holdings = {p.symbol: p.market_value for p in positions}
    total = account.cash + sum(holdings.values())
    report.weights = {s: v / total for s, v in holdings.items()} if total > 0 else {}

    # Regime detection needs benchmark history; if data is unavailable we stay risk-on
    # but record a warning rather than trading blind on a stale signal.
    regime_symbols = [cfg.regime.benchmark] if cfg.regime.enabled else []
    try:
        bars = broker.get_daily_bars(regime_symbols, bars_lookback_start(today, cfg.regime.sma_days), today) if regime_symbols else {}
    except BrokerError as exc:
        bars = {}
        report.warnings.append(f"could not load benchmark bars: {exc}")
    regime = detect_regime(cfg, bars.get(cfg.regime.benchmark, []))
    if regime.name == "unknown":
        report.warnings.append("not enough benchmark history to detect regime; assuming risk-on")
    report.regime = regime.describe()

    targets, cash_weight = effective_targets(cfg, regime)
    report.targets = targets

    # Drawdown circuit breaker: stop adding money after a large loss from the peak.
    buys_halted = None
    try:
        dd = drawdown_pct(broker.get_equity_history("1A"))
    except BrokerError as exc:
        dd = 0.0
        report.warnings.append(f"could not load equity history: {exc}")
    if dd > cfg.risk.max_drawdown_pct:
        buys_halted = f"drawdown {dd:.1f}% exceeds limit {cfg.risk.max_drawdown_pct:.1f}%; buys paused"
        report.warnings.append(buys_halted)

    planned = plan_trades(targets, cash_weight, holdings, account.cash, cfg)
    report.planned = planned
    open_symbols = {o.symbol for o in broker.get_open_orders()}
    cash_available = account.cash - total * cash_weight
    approved, rejected = apply_risk_limits(cfg, planned, total, holdings, open_symbols, buys_halted, cash_available)
    report.approved, report.rejected = approved, rejected

    if dry_run:
        log.info("dry run: %d trade(s) would be submitted", len(approved))
        return report

    # Sells first, and wait for them to fill, so their proceeds fund the buys.
    sells = [t for t in approved if t.side == "sell"]
    buys = [t for t in approved if t.side == "buy"]
    sell_orders = _submit(broker, sells, report, trade_log, live)
    if sells and buys:
        unfilled = _wait_for_fills(broker, sell_orders, timeout=fill_timeout)
        if unfilled:
            report.warnings.append(f"sell orders not filled in time: {', '.join(unfilled)}; buys skipped this run")
            for t in buys:
                report.rejected.append((t, "waiting for sell proceeds"))
            return report
    _submit(broker, buys, report, trade_log, live)
    return report


def _submit(broker: Broker, trades: list[Trade], report: RunReport, trade_log: Path | None, live: bool) -> list[Order]:
    orders: list[Order] = []
    for trade in trades:
        try:
            order = broker.close_position(trade.symbol) if trade.close_position else broker.submit_notional_order(trade.symbol, trade.side, trade.notional)
        except BrokerError as exc:
            report.rejected.append((trade, f"broker rejected: {exc}"))
            log.error("order failed for %s: %s", trade.symbol, exc)
            continue
        orders.append(order)
        report.submitted.append(order)
        log.info("submitted %s %s $%.2f (order %s)", trade.side, trade.symbol, trade.notional, order.id)
        if trade_log:
            _append_trade_log(trade_log, trade, order.id, live)
    return orders


TERMINAL_STATES = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}


def _wait_for_fills(broker: Broker, orders: list[Order], timeout: float, poll: float = 2.0) -> list[str]:
    """Block until every order is in a terminal state; return symbols that did not fill."""
    pending = {o.id: o for o in orders if o.id}
    deadline = time.monotonic() + timeout
    unfilled: list[str] = []
    while pending:
        for oid in list(pending):
            try:
                current = broker.get_order(oid)
            except BrokerError as exc:
                log.warning("could not poll order %s: %s", oid, exc)
                continue
            if current.status.lower() in TERMINAL_STATES:
                if current.status.lower() != "filled":
                    unfilled.append(current.symbol)
                    log.warning("order %s for %s ended %s", oid, current.symbol, current.status)
                del pending[oid]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(poll)
    unfilled.extend(o.symbol for o in pending.values())
    return unfilled


def _append_trade_log(path: Path, trade: Trade, order_id: str, live: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["timestamp_utc", "mode", "symbol", "side", "notional", "reason", "order_id"])
        writer.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), "live" if live else "paper", trade.symbol, trade.side, f"{trade.notional:.2f}", trade.reason, order_id])


def send_alert(webhook_url: str | None, text: str) -> bool:
    """Post a summary to a Slack/Discord-compatible webhook. Never raises."""
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"text": text, "content": text}, timeout=10)
        return resp.status_code < 300
    except requests.RequestException as exc:
        log.warning("alert failed: %s", exc)
        return False
