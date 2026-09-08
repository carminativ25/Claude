"""Command line interface: run, status, income, backtest, check."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .agent import run_once, send_alert
from .backtest import run_backtest
from .broker import AlpacaBroker, BrokerError
from .config import ConfigError, load_config, load_credentials
from .reporting import income_report, status_report

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def _broker(args):
    creds = load_credentials()
    if creds.live and not args.__dict__.get("live", False):
        # The environment allows live trading, but the caller did not ask for it.
        creds = creds.__class__(api_key=creds.api_key, secret_key=creds.secret_key, live=False, alert_webhook_url=creds.alert_webhook_url)
    if args.__dict__.get("live", False) and not creds.live:
        raise ConfigError(
            "--live requested but STOCK_AGENT_LIVE_TRADING is not set to the confirmation phrase; refusing to trade real money"
        )
    return creds, AlpacaBroker(creds)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    creds, broker = _broker(args)
    dry_run = not args.execute
    report = run_once(cfg, broker, dry_run=dry_run, live=creds.live, trade_log=Path(args.trade_log) if args.trade_log else None)
    text = report.summary()
    print(text)
    if not dry_run or report.halted_reason or report.warnings:
        send_alert(creds.alert_webhook_url, f"stock-agent\n{text}")
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    _, broker = _broker(args)
    print(status_report(cfg, broker))
    return 0


def cmd_income(args) -> int:
    _, broker = _broker(args)
    print(income_report(broker, days=args.days))
    return 0


def cmd_backtest(args) -> int:
    cfg = load_config(args.config)
    _, broker = _broker(args)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    symbols = sorted(set(cfg.targets) | {cfg.regime.benchmark})
    # Pull extra history so the moving average is warmed up from day one.
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


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    print(f"config OK: {len(cfg.targets)} holdings, cash buffer {cfg.cash_weight:.1%}, regime {'on' if cfg.regime.enabled else 'off'}")
    for sym, w in cfg.targets.items():
        print(f"  {sym:8}{w:>7.1%}")
    try:
        creds, broker = _broker(args)
    except ConfigError as exc:
        print(f"credentials: {exc}")
        return 1
    account = broker.get_account()
    clock = broker.get_clock()
    print(f"broker OK ({'LIVE' if creds.live else 'paper'}): status {account.status}, equity ${account.equity:,.2f}, market {'open' if clock.is_open else 'closed'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stock-agent", description="Rules-based stock investment agent (Alpaca)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.yaml")
    p.add_argument("--live", action="store_true", help="trade with the live account (also needs STOCK_AGENT_LIVE_TRADING)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one agent cycle (dry run unless --execute)")
    run.add_argument("--execute", action="store_true", help="actually submit orders")
    run.add_argument("--trade-log", default="logs/trades.csv", help="CSV file to append submitted trades to")
    run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="show portfolio vs. targets").set_defaults(func=cmd_status)

    inc = sub.add_parser("income", help="dividend income report")
    inc.add_argument("--days", type=int, default=365)
    inc.set_defaults(func=cmd_income)

    bt = sub.add_parser("backtest", help="backtest the strategy on historical daily bars")
    bt.add_argument("--start", required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    bt.add_argument("--curve", help="write the equity curve to this CSV file")
    bt.set_defaults(func=cmd_backtest)

    sub.add_parser("check", help="validate config and broker credentials").set_defaults(func=cmd_check)
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
