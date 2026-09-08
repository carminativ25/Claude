"""Opening range breakout (ORB) logic shared by live trading and the backtester.

Everything here is pure: it takes minute bars and returns a decision. The
same functions run against today's bars in the live loop and against
historical bars in the backtester, so the backtest measures exactly the
rules that trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..models import MinuteBar

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
SESSION_MINUTES = 390


def session_open(day: date) -> datetime:
    return datetime.combine(day, SESSION_OPEN, tzinfo=ET)


def session_close(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE, tzinfo=ET)


def session_bars(bars: list[MinuteBar], day: date) -> list[MinuteBar]:
    """Only regular-hours bars for `day`, in order."""
    start, end = session_open(day), session_close(day)
    return sorted((b for b in bars if start <= b.t < end), key=lambda b: b.t)


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    volume: float
    bars: int
    end: datetime

    @property
    def height(self) -> float:
        return self.high - self.low

    def height_pct(self) -> float:
        mid = (self.high + self.low) / 2
        return self.height / mid * 100.0 if mid > 0 else 0.0


def opening_range(bars: list[MinuteBar], day: date, minutes: int) -> OpeningRange | None:
    """High/low of the first `minutes` of the session. None until the range has fully formed."""
    start = session_open(day)
    cutoff = start + timedelta(minutes=minutes)
    window = [b for b in bars if start <= b.t < cutoff]
    if not window:
        return None
    # The range is complete only once we have seen a bar at or after the cutoff
    # (or the window is as long as it can be, i.e. the last bar starts at cutoff-1min).
    if not any(b.t >= cutoff for b in bars) and window[-1].t < cutoff - timedelta(minutes=1):
        return None
    return OpeningRange(
        high=max(b.h for b in window),
        low=min(b.l for b in window),
        volume=sum(b.v for b in window),
        bars=len(window),
        end=cutoff,
    )


def vwap(bars: list[MinuteBar]) -> float:
    vol = sum(b.v for b in bars)
    if vol <= 0:
        return bars[-1].c if bars else 0.0
    return sum((b.h + b.l + b.c) / 3 * b.v for b in bars) / vol


def relative_volume(volume_so_far: float, avg_daily_volume: float, minutes_elapsed: int) -> float:
    """Volume so far vs. what an average day would have traded by now (pro rata).

    Volume is front-loaded in the first hour, so early readings run high on
    every stock. The threshold in config is tuned for that.
    """
    if avg_daily_volume <= 0 or minutes_elapsed <= 0:
        return 0.0
    expected = avg_daily_volume * min(minutes_elapsed, SESSION_MINUTES) / SESSION_MINUTES
    return volume_so_far / expected if expected > 0 else 0.0


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str       # long | short
    entry: float         # reference price at signal time (last close)
    stop: float
    target: float
    range_high: float
    range_low: float
    rvol: float
    vwap: float
    at: datetime

    @property
    def side(self) -> str:
        return "buy" if self.direction == "long" else "sell"

    @property
    def stop_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry * 100.0 if self.entry else 0.0


@dataclass(frozen=True)
class NoSignal:
    reason: str
    final: bool = False  # True when this symbol is done for the day (e.g. range too wide)
    rvol: float | None = None
    key: str = ""        # short category for tallies


def evaluate_breakout(
    cfg: Config,
    symbol: str,
    bars: list[MinuteBar],
    day: date,
    avg_daily_volume: float,
    direction: str = "long",
    now: datetime | None = None,
) -> Signal | NoSignal:
    """Decide whether the latest bar is a valid breakout of the opening range."""
    dt = cfg.daytrade
    bars = session_bars(bars, day)
    if not bars:
        return NoSignal("no bars yet", key="no bars")
    now = now or bars[-1].t + timedelta(minutes=1)
    start = session_open(day)
    rng = opening_range(bars, day, dt.range_minutes)
    if rng is None:
        return NoSignal("opening range still forming", key="range forming")
    if now > start + timedelta(minutes=dt.entry_window_minutes):
        return NoSignal("entry window closed", final=True, key="window closed")
    height_pct = rng.height_pct()
    if height_pct < dt.min_range_pct:
        return NoSignal(f"range too narrow ({height_pct:.2f}%)", final=True, key="range too narrow")
    if height_pct > dt.max_stop_pct:
        return NoSignal(f"range too wide ({height_pct:.2f}%), stop would exceed {dt.max_stop_pct}%", final=True, key="range too wide")

    after = [b for b in bars if b.t >= rng.end]
    if not after:
        return NoSignal("waiting for the first bar after the range", key="range forming")
    last = after[-1]
    elapsed = int((last.t - start).total_seconds() // 60) + 1
    rvol = relative_volume(sum(b.v for b in bars), avg_daily_volume, elapsed)
    current_vwap = vwap(bars)

    if direction == "long":
        broke = last.c > rng.high
        stop, entry = rng.low, last.c
        target = entry + dt.reward_risk * (entry - stop)
        vwap_ok = entry > current_vwap
    else:
        broke = last.c < rng.low
        stop, entry = rng.high, last.c
        target = entry - dt.reward_risk * (stop - entry)
        vwap_ok = entry < current_vwap

    if not broke:
        return NoSignal("no breakout yet", rvol=rvol, key="no breakout")
    if rvol < dt.min_relative_volume:
        return NoSignal(f"relative volume {rvol:.1f}x below {dt.min_relative_volume}x", rvol=rvol, key="low relative volume")
    if dt.require_vwap_confirmation and not vwap_ok:
        return NoSignal(f"price on the wrong side of VWAP ({current_vwap:.2f})", rvol=rvol, key="wrong side of VWAP")
    stop_dist_pct = abs(entry - stop) / entry * 100.0
    if stop_dist_pct > dt.max_stop_pct:
        return NoSignal(f"breakout too extended: stop {stop_dist_pct:.2f}% away", rvol=rvol, key="breakout too extended")
    if stop_dist_pct < dt.min_stop_pct:
        # widen a hair so the stop is not inside the noise
        stop = entry * (1 - dt.min_stop_pct / 100) if direction == "long" else entry * (1 + dt.min_stop_pct / 100)
        target = entry + dt.reward_risk * (entry - stop) if direction == "long" else entry - dt.reward_risk * (stop - entry)
    entry, stop = round(entry, 2), round(stop, 2)
    target = entry + dt.reward_risk * (entry - stop) if direction == "long" else entry - dt.reward_risk * (stop - entry)
    return Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop=stop,
        target=round(target, 2),
        range_high=rng.high,
        range_low=rng.low,
        rvol=round(rvol, 2),
        vwap=round(current_vwap, 2),
        at=last.t,
    )
