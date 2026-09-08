"""Command line interface.

Day trading (default workflow):   plan, open, monitor, close, review, check
Long-term ETF portfolio:          portfolio run|status|income|backtest
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .agent import run_once, send_alert
from .backtest import run_backtest
from .broker import AlpacaBroker, BrokerError
from .config import ConfigError, Credentials, has_anthropic_credentials, load_config, load_credentials
from .daytrade import journal
from .daytrade.analyst import default_analyst
from .daytrade.session import close_day, monitor, open_day, plan_day
from .reporting import income_report, status_report

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def _broker(args) -> tuple[Credentials, AlpacaBroker]:
    creds = load_credentials()
    want_live = bool(getattr(args, "live", False))
    if want_live and not creds.live:
        raise ConfigError(
            "--live requested but STOCK_AGENT_LIVE_TRADING is not set to the confirmation phrase; refusing to trade real money"
        )
    if creds.live and not want_live:
        creds = Credentials(api_key=creds.api_key, secret_key=creds.secret_key, live=False, alert_webhook_url=creds.alert_webhook_url)
    return creds, AlpacaBroker(creds)


def _finish(creds: Credentials, report, always_alert: bool) -> int:
    text = report.summary()
    print(text)
    if always_alert or report.halted_reason or report.warnings:
        send_alert(creds.alert_webhook_url, f"stock-agent\n{text}")
    return 0


# ---------------------------------------------------------------------------
# day trading commands
# ---------------------------------------------------------------------------
def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    use_claude = not args.no_claude and has_anthropic_credentials()
    if not use_claude and not args.no_claude:
        print("note: no ANTHROPIC_API_KEY set; using the rules-based analyst", file=sys.stderr)
    _, report = plan_day(cfg, broker, default_analyst(cfg, use_claude), live=creds.live)
    return _finish(creds, report, always_alert=True)


def cmd_open(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    report = open_day(cfg, broker, dry_run=not args.execute, live=creds.live)
    return _finish(creds, report, always_alert=args.execute)


def cmd_monitor(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    report = monitor(cfg, broker, dry_run=not args.execute, live=creds.live)
    return _finish(creds, report, always_alert=bool(report.orders))


def cmd_close(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    report = close_day(cfg, broker, dry_run=not args.execute, live=creds.live)
    return _finish(creds, report, always_alert=args.execute)


def cmd_review(args) -> int:
    cfg = load_config(args.config)
    stats, rows = journal.review(cfg)
    if args.trades:
        print("\n".join(rows))
        print()
    print(stats.summary())
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    dt = cfg.daytrade
    print(f"config OK: day trading up to {dt.max_picks} picks, {dt.risk_per_trade_pct}% risk/trade, {dt.max_daily_loss_pct}% daily loss limit, analyst {dt.analyst_model}")
    print(f"claude analyst: {'available' if has_anthropic_credentials() else 'NOT configured (rules analyst will be used)'}")
    try:
        creds, broker = _broker(args)
    except ConfigError as exc:
        print(f"credentials: {exc}")
        return 1
    account = broker.get_account()
    clock = broker.get_clock()
    pdt = " (under $25k: PDT rule applies)" if account.equity < 25_000 else ""
    print(f"broker OK ({'LIVE' if creds.live else 'paper'}): status {account.status}, equity ${account.equity:,.2f}{pdt}, day trades used {account.daytrade_count}, market {'open' if clock.is_open else 'closed'}")
    return 0


# ---------------------------------------------------------------------------
# long-term portfolio commands
# ---------------------------------------------------------------------------
def cmd_portfolio_run(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    dry_run = not args.execute
    report = run_once(cfg, broker, dry_run=dry_run, live=creds.live, trade_log=Path(args.trade_log) if args.trade_log else None)
    return _finish(creds, report, always_alert=not dry_run)


def cmd_portfolio_status(args) -> int:
    cfg = load_config(args.config)
    _, broker = _broker(args)
    print(status_report(cfg, broker))
    return 0


def cmd_portfolio_income(args) -> int:
    _, broker = _broker(args)
    print(income_report(broker, days=args.days))
    return 0


def cmd_portfolio_backtest(args) -> int:
    cfg = load_config(args.config)
    _, broker = _broker(args)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    symbols = sorted(set(cfg.targets) | {cfg.regime.benchmark})
    bars = broker.get_daily_bars(symbols, start - timedelta(days=int(cfg.regime.sma_days * 1.6) + 15), end)
    in_period = {s: [b for b in series if b.day >= start] for s, series in bars.items()}
    warmup = [b for b in bars.get(cfg.regime.benchmark, []) if b.day < start]
    result = run_backtest(cfg, in_period, warmup_bars=warmup)
    print(result.summary())
    if args.curve:
        with open(args.curve, "w", encoding="utf-8") as fh:
            fh.write("date,equity\n")
            for day, value in result.equity_curve:
                fh.write(f"{day},{value:.2f}\n")
        print(f"equity curve written to {args.curve}")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stock-agent", description="News-driven day trading agent on Alpaca (paper by default)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.yaml")
    p.add_argument("--live", action="store_true", help="use the live account (also needs STOCK_AGENT_LIVE_TRADING)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="pre-market: scan news and screeners, write today's game plan")
    plan.add_argument("--no-claude", action="store_true", help="use the rules-based analyst even if Claude is configured")
    plan.set_defaults(func=cmd_plan)

    opn = sub.add_parser("open", help="at the open: size and submit bracket orders from the game plan")
    opn.add_argument("--execute", action="store_true", help="actually submit orders (default is a dry run)")
    opn.set_defaults(func=cmd_open)

    mon = sub.add_parser("monitor", help="intraday: enforce the daily loss limit and flatten before the close")
    mon.add_argument("--execute", action="store_true")
    mon.set_defaults(func=cmd_monitor)

    cls = sub.add_parser("close", help="flatten every position and cancel open orders")
    cls.add_argument("--execute", action="store_true")
    cls.set_defaults(func=cmd_close)

    rev = sub.add_parser("review", help="performance statistics from the journal")
    rev.add_argument("--trades", action="store_true", help="list every trade")
    rev.set_defaults(func=cmd_review)

    sub.add_parser("check", help="validate config and credentials").set_defaults(func=cmd_check)

    pf = sub.add_parser("portfolio", help="long-term ETF portfolio strategy")
    pfs = pf.add_subparsers(dest="portfolio_command", required=True)
    run = pfs.add_parser("run", help="one rebalance cycle (dry run unless --execute)")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--trade-log", default="logs/trades.csv")
    run.set_defaults(func=cmd_portfolio_run)
    pfs.add_parser("status", help="holdings vs. targets").set_defaults(func=cmd_portfolio_status)
    inc = pfs.add_parser("income", help="dividend income report")
    inc.add_argument("--days", type=int, default=365)
    inc.set_defaults(func=cmd_portfolio_income)
    bt = pfs.add_parser("backtest", help="backtest the ETF strategy")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end")
    bt.add_argument("--curve")
    bt.set_defaults(func=cmd_portfolio_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (ConfigError, BrokerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
