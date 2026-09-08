"""Intraday snapshot of one symbol: where it is, how it got there, and whether volume is holding up."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..broker import Broker
from ..config import Config
from .orb import ET, opening_range, relative_volume, session_bars, session_open, vwap


def _window_volume(bars, start: datetime, end: datetime) -> float:
    return sum(b.v for b in bars if start <= b.t < end)


def snapshot(cfg: Config, broker: Broker, symbol: str, today: date | None = None, now: datetime | None = None) -> str:
    symbol = symbol.upper()
    today = today or date.today()
    clock = broker.get_clock()
    try:
        now = now or datetime.fromisoformat(clock.timestamp.replace("Z", "+00:00"))
    except ValueError:
        now = datetime.now(timezone.utc)
    now_et = now.astimezone(ET)

    daily = broker.get_daily_bars([symbol], today - timedelta(days=45), today - timedelta(days=1)).get(symbol, [])
    if len(daily) < 5:
        return f"{symbol}: not enough daily history (is the symbol right?)"
    hist = daily[-20:]
    adv = sum(b.volume for b in hist) / len(hist)
    prev_close = daily[-1].close

    bars = session_bars(broker.get_minute_bars([symbol], session_open(today), now).get(symbol, []), today)
    quote = broker.get_latest_quotes([symbol]).get(symbol)
    last = quote.mid if quote and quote.mid > 0 else (bars[-1].c if bars else prev_close)
    lines = [f"{symbol}  {now_et:%Y-%m-%d %H:%M} ET  market {'open' if clock.is_open else 'closed'}"]
    lines.append(f"last {last:.2f}  vs prev close {prev_close:.2f}  ({(last - prev_close) / prev_close * 100:+.2f}%)")
    if not bars:
        lines.append("no session bars yet today")
        return "\n".join(lines)

    open_px = bars[0].o
    day_high = max(b.h for b in bars)
    day_low = min(b.l for b in bars)
    lines.append(f"open {open_px:.2f}  high {day_high:.2f}  low {day_low:.2f}  (from open {(last - open_px) / open_px * 100:+.2f}%)")

    rng = opening_range(bars, today, cfg.daytrade.range_minutes)
    if rng:
        where = "above" if last > rng.high else "below" if last < rng.low else "inside"
        lines.append(f"{cfg.daytrade.range_minutes}-min opening range {rng.low:.2f}-{rng.high:.2f}: price is {where}")
    v = vwap(bars)
    lines.append(f"VWAP {v:.2f}: price is {'above' if last > v else 'below'} ({(last - v) / v * 100:+.2f}%)")

    elapsed = int((bars[-1].t - session_open(today)).total_seconds() // 60) + 1
    total_vol = sum(b.v for b in bars)
    rvol = relative_volume(total_vol, adv, elapsed)
    lines.append(f"volume so far {total_vol:,.0f} = {rvol:.2f}x a normal day's pace at this time ({elapsed} min in)")

    # Volume trend: compare the last 30 minutes with the 30 minutes before, and with the first 30 minutes.
    t_end = bars[-1].t + timedelta(minutes=1)
    last30 = _window_volume(bars, t_end - timedelta(minutes=30), t_end)
    prior30 = _window_volume(bars, t_end - timedelta(minutes=60), t_end - timedelta(minutes=30))
    first30 = _window_volume(bars, session_open(today), session_open(today) + timedelta(minutes=30))
    if elapsed >= 60 and prior30 > 0 and first30 > 0:
        lines.append(f"last 30 min volume {last30:,.0f}: {last30 / prior30:.2f}x the previous 30 min, {last30 / first30:.2f}x the opening 30 min")
    # Price trend over the last hour
    if elapsed >= 60:
        hour_ago = [b for b in bars if b.t <= t_end - timedelta(minutes=60)]
        if hour_ago:
            ref = hour_ago[-1].c
            lines.append(f"last hour: {(last - ref) / ref * 100:+.2f}%  (high of day {'was in the last hour' if any(b.h == day_high for b in bars if b.t > t_end - timedelta(minutes=60)) else 'was earlier'})")

    # Mechanical read, same rules the agent trades on
    verdict = []
    if rng and last > rng.high and last > v:
        verdict.append("above the opening range and VWAP: trend intact")
    elif last < v:
        verdict.append("below VWAP: buyers are not in control")
    else:
        verdict.append("inside the range or hugging VWAP: no edge either way")
    if rvol < 1.0:
        verdict.append("volume is below normal, moves are less trustworthy")
    elif elapsed >= 60 and prior30 > 0 and last30 < 0.6 * prior30:
        verdict.append("volume is thinning out over the last half hour")
    elif elapsed >= 60 and prior30 > 0 and last30 > 1.3 * prior30:
        verdict.append("volume is picking up")
    lines.append("read: " + "; ".join(verdict))
    lines.append("(the free IEX feed shows a fraction of total volume; ratios are meaningful, absolute counts are not)")
    return "\n".join(lines)
