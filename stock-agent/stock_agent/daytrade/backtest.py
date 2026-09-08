"""Minute-bar backtest of the opening range breakout strategy.

For every trading day in the range:
  1. Build the watchlist mechanically from the universe: the largest gaps at
     the open (vs. the previous close) that pass the liquidity screen.
  2. Replay the session minute by minute through the same evaluate_breakout()
     the live loop uses, fill at the next bar's open plus slippage, and run
     the bracket (stop checked before target within a bar, so a bar that
     touches both counts as a loss).
  3. Flatten before the close, apply the daily loss limit, and record trades.

The news feed is not replayed, so the historical watchlist is "gap + volume"
only. Live, the analyst narrows that list with the news; here we measure the
mechanical core.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from ..config import Config
from ..models import Bar, MinuteBar
from .orb import NoSignal, evaluate_breakout, session_bars, session_close
from .plan import size_position

DEFAULT_UNIVERSE = (
    "AAPL MSFT NVDA AMD TSLA META AMZN GOOGL NFLX AVGO CRM ORCL ADBE INTC MU QCOM PLTR COIN MSTR SHOP "
    "UBER ABNB SNAP PINS ROKU PYPL SOFI HOOD RIVN NIO BABA PDD BA CAT DE GE F GM DAL UAL AAL CCL RCL "
    "NCLH MGM WYNN LVS DKNG ENPH FSLR PLUG CVX XOM OXY DVN MRNA PFE LLY UNH JPM BAC C WFC GS MS SCHW "
    "TGT WMT COST HD LOW NKE LULU CMG SBUX MCD DIS WBD SPOT ARM SMCI MARA RIOT"
).split()

MinuteFetcher = Callable[[list[str], date], dict[str, list[MinuteBar]]]


@dataclass
class BtTrade:
    day: date
    symbol: str
    side: str
    qty: int
    entry: float
    exit: float
    stop: float
    target: float
    pnl: float
    exit_reason: str
    rvol: float
    entry_time: str
    exit_time: str

    @property
    def r_multiple(self) -> float:
        risk = abs(self.entry - self.stop) * self.qty
        return self.pnl / risk if risk > 0 else 0.0


@dataclass
class IntradayResult:
    start: date
    end: date
    days: int
    trades: list[BtTrade]
    daily_pnl: dict[date, float]
    halted_days: int
    watched: int
    starting_equity: float

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def profit_factor(self) -> float | None:
        gp = sum(t.pnl for t in self.trades if t.pnl > 0)
        gl = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gp / gl if gl > 0 else None

    def max_drawdown(self) -> float:
        equity, peak, worst = self.starting_equity, self.starting_equity, 0.0
        for day in sorted(self.daily_pnl):
            equity += self.daily_pnl[day]
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak * 100 if peak > 0 else 0.0)
        return worst

    def summary(self) -> str:
        n = len(self.trades)
        pf = self.profit_factor
        reasons = Counter(t.exit_reason for t in self.trades)
        avg_r = sum(t.r_multiple for t in self.trades) / n if n else 0.0
        lines = [
            f"period            {self.start} -> {self.end}  ({self.days} trading days, {self.watched} symbol-days watched)",
            f"trades            {n}   ({', '.join(f'{k} {v}' for k, v in reasons.most_common())})",
            f"win rate          {self.wins / n:.0%}" if n else "win rate          n/a",
            f"total P&L         ${self.total_pnl:,.2f}  ({self.total_pnl / self.starting_equity:+.2%} on ${self.starting_equity:,.0f})",
            f"per trade         ${self.total_pnl / n:,.2f}   avg R {avg_r:+.2f}" if n else "per trade         n/a",
            f"profit factor     {'n/a' if pf is None else f'{pf:.2f}'}",
            f"max drawdown      {self.max_drawdown():.2f}%",
            f"loss-limit days   {self.halted_days}",
        ]
        return "\n".join(lines)

    def write_trades(self, path: str | Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "symbol", "side", "qty", "entry", "exit", "stop", "target", "pnl", "r", "exit_reason", "rvol", "entry_time", "exit_time"])
            for t in self.trades:
                w.writerow([t.day, t.symbol, t.side, t.qty, t.entry, t.exit, t.stop, t.target, f"{t.pnl:.2f}", f"{t.r_multiple:.2f}", t.exit_reason, t.rvol, t.entry_time, t.exit_time])


def build_watchlist(cfg: Config, day: date, daily: dict[str, list[Bar]]) -> tuple[list[tuple[str, str]], dict[str, float]]:
    """Largest gappers at today's open that pass the liquidity screen. Returns [(symbol, direction)], avg volumes."""
    dt = cfg.daytrade
    scored = []
    avg_vol: dict[str, float] = {}
    for sym, series in daily.items():
        idx = next((i for i, b in enumerate(series) if b.day == day), None)
        if idx is None or idx < 20:
            continue
        today_bar, prev = series[idx], series[idx - 1]
        if today_bar.open <= 0 or prev.close <= 0:
            continue
        hist = series[idx - 20 : idx]
        adv = sum(b.close * b.volume for b in hist) / len(hist)
        if adv < dt.min_avg_dollar_volume or not (dt.min_price <= today_bar.open <= dt.max_price):
            continue
        gap = (today_bar.open - prev.close) / prev.close * 100.0
        if abs(gap) < dt.min_gap_pct or sym in dt.blocklist:
            continue
        if gap < 0 and not dt.allow_short:
            continue
        avg_vol[sym] = sum(b.volume for b in hist) / len(hist)
        scored.append((abs(gap), sym, "long" if gap > 0 else "short"))
    scored.sort(reverse=True)
    return [(sym, direction) for _, sym, direction in scored[: dt.max_watchlist]], avg_vol


@dataclass
class _Open:
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    rvol: float
    entry_time: str


def simulate_day(cfg: Config, day: date, watch: list[tuple[str, str]], minutes: dict[str, list[MinuteBar]], avg_vol: dict[str, float], equity: float) -> tuple[list[BtTrade], bool]:
    dt = cfg.daytrade
    slip = dt.slippage_bps / 10_000.0
    bars = {sym: session_bars(minutes.get(sym, []), day) for sym, _ in watch}
    direction = dict(watch)
    timeline = sorted({b.t for series in bars.values() for b in series})
    if not timeline:
        return [], False
    flatten_at = session_close(day) - timedelta(minutes=dt.flatten_minutes_before_close)
    entry_deadline = timeline[0].replace(hour=9, minute=30) + timedelta(minutes=dt.entry_window_minutes)

    open_pos: dict[str, _Open] = {}
    done: set[str] = set()
    trades: list[BtTrade] = []
    cash = equity
    realized = 0.0
    halted = False
    index = {sym: {b.t: i for i, b in enumerate(series)} for sym, series in bars.items()}

    def close_trade(pos: _Open, price: float, reason: str, when) -> None:
        nonlocal realized, cash
        price = price * (1 - slip) if pos.side == "buy" else price * (1 + slip)
        pnl = (price - pos.entry) * pos.qty if pos.side == "buy" else (pos.entry - price) * pos.qty
        realized += pnl
        cash += pos.qty * pos.entry + pnl
        trades.append(BtTrade(day, pos.symbol, pos.side, pos.qty, pos.entry, round(price, 4), pos.stop, pos.target, round(pnl, 2), reason, pos.rvol, pos.entry_time, when.strftime("%H:%M")))

    for t in timeline:
        # 1. manage open positions on this bar
        for sym in list(open_pos):
            pos = open_pos[sym]
            i = index[sym].get(t)
            if i is None:
                continue
            bar = bars[sym][i]
            if t >= flatten_at:
                close_trade(pos, bar.c, "eod", t)
                del open_pos[sym]
                continue
            if pos.side == "buy":
                if bar.l <= pos.stop:
                    close_trade(pos, min(pos.stop, bar.o), "stop", t)
                    del open_pos[sym]
                elif bar.h >= pos.target:
                    close_trade(pos, max(pos.target, bar.o) if bar.o >= pos.target else pos.target, "target", t)
                    del open_pos[sym]
            else:
                if bar.h >= pos.stop:
                    close_trade(pos, max(pos.stop, bar.o), "stop", t)
                    del open_pos[sym]
                elif bar.l <= pos.target:
                    close_trade(pos, min(pos.target, bar.o) if bar.o <= pos.target else pos.target, "target", t)
                    del open_pos[sym]
        # 2. daily loss limit on realized + unrealized
        unrealized = 0.0
        for sym, pos in open_pos.items():
            i = index[sym].get(t)
            if i is not None:
                px = bars[sym][i].c
                unrealized += (px - pos.entry) * pos.qty if pos.side == "buy" else (pos.entry - px) * pos.qty
        if not halted and equity > 0 and (realized + unrealized) <= -equity * dt.max_daily_loss_pct / 100.0:
            halted = True
            for sym in list(open_pos):
                i = index[sym].get(t)
                close_trade(open_pos[sym], bars[sym][i].c if i is not None else open_pos[sym].entry, "loss-limit", t)
                del open_pos[sym]
        # 3. new entries
        if halted or t >= entry_deadline or t >= flatten_at:
            continue
        slots = dt.max_picks - len(open_pos) - len([s for s in done if s not in open_pos and any(tr.symbol == s for tr in trades)])
        if slots <= 0:
            continue
        for sym, _ in watch:
            if sym in open_pos or sym in done or slots <= 0:
                continue
            i = index[sym].get(t)
            if i is None:
                continue
            history = bars[sym][: i + 1]
            result = evaluate_breakout(cfg, sym, history, day, avg_vol.get(sym, 0.0), direction[sym], now=t + timedelta(minutes=1))
            if isinstance(result, NoSignal):
                if result.final:
                    done.add(sym)
                continue
            if i + 1 >= len(bars[sym]):
                continue
            nxt = bars[sym][i + 1]
            fill = nxt.o * (1 + slip) if result.side == "buy" else nxt.o * (1 - slip)
            # the stop stays at the range boundary; the target is measured from the fill
            stop = result.stop
            target = fill + dt.reward_risk * abs(fill - stop) if result.side == "buy" else fill - dt.reward_risk * abs(fill - stop)
            sized = size_position(cfg, sym, result.side, fill, stop, target, equity, cash)
            done.add(sym)
            if sized is None:
                continue
            cash -= sized.qty * sized.entry
            slots -= 1
            open_pos[sym] = _Open(sym, sized.side, sized.qty, sized.entry, sized.stop, sized.target, result.rvol, nxt.t.strftime("%H:%M"))
    for sym, pos in list(open_pos.items()):  # safety: anything still open closes at the last bar
        close_trade(pos, bars[sym][-1].c, "eod", bars[sym][-1].t)
    return trades, halted


def run_intraday_backtest(cfg: Config, daily: dict[str, list[Bar]], fetch_minutes: MinuteFetcher, start: date, end: date, starting_equity: float | None = None, progress=None) -> IntradayResult:
    equity = starting_equity or cfg.backtest.initial_cash
    days = sorted({b.day for series in daily.values() for b in series if start <= b.day <= end})
    trades: list[BtTrade] = []
    daily_pnl: dict[date, float] = {}
    halted_days = watched = 0
    for day in days:
        watch, avg_vol = build_watchlist(cfg, day, daily)
        if not watch:
            daily_pnl[day] = 0.0
            continue
        watched += len(watch)
        minutes = fetch_minutes([s for s, _ in watch], day)
        day_trades, halted = simulate_day(cfg, day, watch, minutes, avg_vol, equity)
        pnl = sum(t.pnl for t in day_trades)
        trades.extend(day_trades)
        daily_pnl[day] = pnl
        equity += pnl
        halted_days += int(halted)
        if progress:
            progress(day, watch, day_trades)
    return IntradayResult(start=days[0] if days else start, end=days[-1] if days else end, days=len(days), trades=trades, daily_pnl=daily_pnl, halted_days=halted_days, watched=watched, starting_equity=starting_equity or cfg.backtest.initial_cash)
