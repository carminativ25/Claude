from datetime import date, datetime, timedelta

import pytest

from conftest import make_config
from stock_agent.daytrade.backtest import build_watchlist, run_intraday_backtest, simulate_day
from stock_agent.daytrade.orb import ET, NoSignal, Signal, evaluate_breakout, opening_range, relative_volume, session_open, vwap
from stock_agent.models import Bar, MinuteBar

DAY = date(2026, 9, 8)


def cfg(**kw):
    base = {"range_minutes": 15, "entry_window_minutes": 90, "min_relative_volume": 2.0, "min_range_pct": 0.3, "reward_risk": 2.0,
            "risk_per_trade_pct": 1.0, "max_position_pct": 50.0, "slippage_bps": 0.0, "min_gap_pct": 2.0}
    base.update(kw)
    return make_config(daytrade=base)


def session(day=DAY, base=100.0, range_pct=1.0, breakout_at=20, breakout_pct=0.5, vol=50_000, after="rally", n=390, breakout_vol=None):
    """Synthetic session: a flat opening range, then a breakout bar, then `after` behaviour."""
    bars = []
    t0 = session_open(day)
    hi, lo = base * (1 + range_pct / 200), base * (1 - range_pct / 200)
    for i in range(n):
        t = t0 + timedelta(minutes=i)
        if i < 15:
            o, h, l, c = base, hi, lo, base
        elif i < breakout_at:
            o = h = l = c = base
        elif i == breakout_at:
            c = hi * (1 + breakout_pct / 100)
            o, h, l = base, c, base
        else:
            k = i - breakout_at
            if after == "rally":
                px = hi * (1 + breakout_pct / 100) * (1 + 0.0005 * k)
            elif after == "crash":
                px = hi * (1 + breakout_pct / 100) * (1 - 0.001 * k)
            else:
                px = hi * (1 + breakout_pct / 100)
            o = h = l = c = px
        v = breakout_vol if (i == breakout_at and breakout_vol is not None) else vol
        bars.append(MinuteBar(t, round(o, 4), round(h, 4), round(l, 4), round(c, 4), v))
    return bars


def test_opening_range_forms_after_cutoff():
    bars = session()
    assert opening_range(bars[:10], DAY, 15) is None          # still forming
    rng = opening_range(bars[:16], DAY, 15)
    assert rng is not None and rng.bars == 15
    assert rng.high == pytest.approx(100.5) and rng.low == pytest.approx(99.5)
    assert rng.height_pct() == pytest.approx(1.0)


def test_vwap_and_relative_volume():
    bars = session()[:5]
    assert vwap(bars) == pytest.approx(100.0, abs=0.01)
    # 100k traded in 10 minutes vs 3.9M/day -> expected 100k -> 1.0x
    assert relative_volume(100_000, 3_900_000, 10) == pytest.approx(1.0)
    assert relative_volume(0, 0, 10) == 0.0


def test_breakout_signal_long():
    c = cfg()
    bars = session()
    adv = 50_000 * 390 / 3  # so rvol is ~3x
    before = evaluate_breakout(c, "X", bars[:20], DAY, adv, "long")
    assert isinstance(before, NoSignal) and "no breakout" in before.reason
    sig = evaluate_breakout(c, "X", bars[:21], DAY, adv, "long")
    assert isinstance(sig, Signal)
    assert sig.stop == pytest.approx(99.5) and sig.entry == pytest.approx(101.0025, abs=0.01)
    assert sig.target == pytest.approx(sig.entry + 2 * (sig.entry - sig.stop), abs=0.01)
    assert sig.rvol > 2.0 and sig.entry > sig.vwap


def test_breakout_rejected_on_low_volume_and_vwap():
    c = cfg()
    bars = session()
    weak = evaluate_breakout(c, "X", bars[:21], DAY, 50_000 * 390 * 2, "long")  # rvol ~0.5x
    assert isinstance(weak, NoSignal) and "relative volume" in weak.reason
    # price above the range but below VWAP cannot happen with a flat range; check the switch works
    c2 = cfg(require_vwap_confirmation=False)
    assert isinstance(evaluate_breakout(c2, "X", bars[:21], DAY, 50_000 * 390 / 3, "long"), Signal)


def test_range_too_wide_or_narrow_is_final():
    wide = evaluate_breakout(cfg(max_stop_pct=2.0), "X", session(range_pct=3.0)[:21], DAY, 1, "long")
    assert isinstance(wide, NoSignal) and wide.final and "too wide" in wide.reason
    narrow = evaluate_breakout(cfg(), "X", session(range_pct=0.1)[:21], DAY, 1, "long")
    assert isinstance(narrow, NoSignal) and narrow.final and "too narrow" in narrow.reason


def test_entry_window_closes():
    c = cfg(entry_window_minutes=30)
    bars = session(breakout_at=40)
    res = evaluate_breakout(c, "X", bars[:41], DAY, 1, "long")
    assert isinstance(res, NoSignal) and res.final and "window closed" in res.reason


def test_short_breakout_when_allowed():
    c = cfg(allow_short=True)
    bars = session()
    # invert: breakout bar closes below the range low
    b = bars[20]
    bars[20] = MinuteBar(b.t, 100.0, 100.0, 99.0, 99.0, b.v)
    sig = evaluate_breakout(c, "X", bars[:21], DAY, 50_000 * 390 / 3, "short")
    assert isinstance(sig, Signal) and sig.side == "sell" and sig.stop == pytest.approx(100.5)


# ---------------------------------------------------------------- backtest
def daily(sym_gaps: dict[str, float], day=DAY, days=25, price=100.0, vol=2_000_000):
    out = {}
    for sym, gap in sym_gaps.items():
        series = []
        for i in range(days):
            d = day - timedelta(days=days - 1 - i)
            o = price * (1 + gap / 100) if d == day else price
            series.append(Bar(d, close=price, high=price * 1.01, low=price * 0.99, volume=vol, open=o))
        out[sym] = series
    return out


def test_build_watchlist_ranks_gappers():
    c = cfg(max_watchlist=2)
    d = daily({"AAA": 5.0, "BBB": 3.0, "CCC": 1.0, "DDD": -6.0})
    watch, avg = build_watchlist(c, DAY, d)
    assert watch == [("AAA", "long"), ("BBB", "long")]      # CCC below min gap, DDD short not allowed
    assert avg["AAA"] == 2_000_000
    watch, _ = build_watchlist(cfg(max_watchlist=3, allow_short=True), DAY, d)
    assert watch[0] == ("DDD", "short")


def test_simulate_day_target_stop_and_eod():
    c = cfg()
    adv = 50_000 * 390 / 3
    minutes = {"UP": session(after="rally"), "DN": session(after="crash"), "FLAT": session(after="flat")}
    watch = [("UP", "long"), ("DN", "long"), ("FLAT", "long")]
    trades, halted = simulate_day(c, DAY, watch, minutes, {s: adv for s, _ in watch}, 100_000)
    by = {t.symbol: t for t in trades}
    assert by["UP"].exit_reason == "target" and by["UP"].pnl > 0
    assert by["DN"].exit_reason == "stop" and by["DN"].pnl < 0
    assert by["FLAT"].exit_reason == "eod"
    assert by["UP"].entry == pytest.approx(minutes["UP"][21].o, abs=0.01)   # filled at next bar open
    assert by["DN"].r_multiple == pytest.approx(-1.0, abs=0.05)
    assert not halted


def test_simulate_day_respects_max_picks_and_loss_limit():
    c = cfg(max_picks=1)
    adv = 50_000 * 390 / 3
    minutes = {"A": session(after="rally"), "B": session(after="rally")}
    trades, _ = simulate_day(c, DAY, [("A", "long"), ("B", "long")], minutes, {"A": adv, "B": adv}, 100_000)
    assert len(trades) == 1
    # loss limit: 1% risk per trade but a 0.5% daily limit -> halted on the crash
    c = cfg(max_daily_loss_pct=0.5, risk_per_trade_pct=1.0)
    trades, halted = simulate_day(c, DAY, [("DN", "long")], {"DN": session(after="crash")}, {"DN": adv}, 100_000)
    assert halted and trades[0].exit_reason == "loss-limit"


def test_run_intraday_backtest_end_to_end():
    c = cfg(max_watchlist=2)
    d = daily({"UP": 4.0, "DN": 3.0, "QUIET": 0.0})
    adv = 50_000 * 390 / 3
    for sym in d:
        for i, b in enumerate(d[sym]):
            d[sym][i] = Bar(b.day, b.close, b.high, b.low, adv, b.open)
    sessions = {"UP": session(after="rally"), "DN": session(after="crash")}
    calls = []

    def fetch(symbols, day):
        calls.append((tuple(symbols), day))
        return {s: sessions[s] for s in symbols}

    res = run_intraday_backtest(c, d, fetch, DAY - timedelta(days=3), DAY)
    assert calls == [(("UP", "DN"), DAY)]                     # only the gap day has a watchlist
    assert res.days == 4 and len(res.trades) == 2 and res.watched == 2
    assert res.profit_factor is not None and "win rate" in res.summary()
    assert res.total_pnl == pytest.approx(sum(res.daily_pnl.values()))
