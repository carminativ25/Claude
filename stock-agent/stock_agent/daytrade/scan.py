"""Morning scan: gather candidates from screeners and news, then apply liquidity filters."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..broker import Broker, BrokerError
from ..config import Config
from ..models import Bar, NewsItem
from .plan import Candidate

log = logging.getLogger(__name__)

LEVERAGED_HINTS = ("2X", "3X", "ULTRA", "BULL", "BEAR", "INVERSE", "LEVERAGED")


@dataclass
class ScanResult:
    candidates: dict[str, Candidate]
    news: list[NewsItem]
    dropped: list[tuple[str, str]] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)


def average_true_range_pct(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(bars[-period - 1 : -1], bars[-period:]):
        high = cur.high or cur.close
        low = cur.low or cur.close
        trs.append(max(high - low, abs(high - prev.close), abs(low - prev.close)))
    if not trs or bars[-1].close <= 0:
        return 0.0
    return sum(trs) / len(trs) / bars[-1].close * 100.0


def average_dollar_volume(bars: list[Bar], period: int = 20) -> float:
    window = bars[-period:]
    if not window:
        return 0.0
    return sum(b.close * b.volume for b in window) / len(window)


def gather_symbols(cfg: Config, broker: Broker, news: list[NewsItem]) -> dict[str, str]:
    """Union of screener output and news-mentioned tickers, tagged with where they came from."""
    top = cfg.daytrade.screener_top
    sources: dict[str, str] = {}
    try:
        gainers, losers = broker.get_movers(top)
        for m in gainers:
            sources.setdefault(m.symbol, "gainer")
        for m in losers:
            sources.setdefault(m.symbol, "loser")
    except BrokerError as exc:
        log.warning("movers screener unavailable: %s", exc)
    try:
        for m in broker.get_most_active(top):
            sources.setdefault(m.symbol, "active")
    except BrokerError as exc:
        log.warning("most-active screener unavailable: %s", exc)
    for item in news:
        for sym in item.symbols:
            sources.setdefault(sym, "news")
    return sources


def scan(cfg: Config, broker: Broker, today: date | None = None, now: datetime | None = None) -> ScanResult:
    dt = cfg.daytrade
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    news = broker.get_news(now - timedelta(hours=dt.news_lookback_hours), limit=50)
    sources = gather_symbols(cfg, broker, news)
    dropped: list[tuple[str, str]] = []

    symbols = [s for s in sources if s.isalpha() and len(s) <= 5]
    for s in set(sources) - set(symbols):
        dropped.append((s, "not a plain US equity ticker"))
    symbols = [s for s in symbols if s not in dt.blocklist] or []
    for s in dt.blocklist:
        if s in sources:
            dropped.append((s, "blocklisted"))

    bars = broker.get_daily_bars(symbols, today - timedelta(days=45), today - timedelta(days=1)) if symbols else {}
    quotes = broker.get_latest_quotes(symbols) if symbols else {}
    headlines_by_symbol: dict[str, list[str]] = {}
    for item in news:
        for sym in item.symbols:
            headlines_by_symbol.setdefault(sym, []).append(item.headline)

    candidates: dict[str, Candidate] = {}
    for sym in symbols:
        series = bars.get(sym, [])
        quote = quotes.get(sym)
        if len(series) < 15:
            dropped.append((sym, "not enough price history"))
            continue
        if quote is None or quote.mid <= 0:
            dropped.append((sym, "no live quote"))
            continue
        asset = broker.get_asset(sym)
        if asset is None or not asset.tradable:
            dropped.append((sym, "not tradable"))
            continue
        if asset.exchange.upper() == "OTC":
            dropped.append((sym, "OTC listing"))
            continue
        price = quote.mid
        if price < dt.min_price or price > dt.max_price:
            dropped.append((sym, f"price {price:.2f} outside {dt.min_price}-{dt.max_price}"))
            continue
        adv = average_dollar_volume(series)
        if adv < dt.min_avg_dollar_volume:
            dropped.append((sym, f"avg dollar volume ${adv/1e6:.1f}M below minimum"))
            continue
        if quote.spread_pct > dt.max_spread_pct:
            dropped.append((sym, f"spread {quote.spread_pct:.2f}% too wide"))
            continue
        prev_close = series[-1].close
        candidates[sym] = Candidate(
            symbol=sym,
            price=round(price, 2),
            prev_close=round(prev_close, 2),
            gap_pct=round((price - prev_close) / prev_close * 100.0, 2) if prev_close else 0.0,
            avg_dollar_volume=round(adv, 0),
            atr_pct=round(average_true_range_pct(series), 2),
            spread_pct=round(quote.spread_pct, 3),
            headlines=headlines_by_symbol.get(sym, [])[:5],
            source=sources[sym],
        )
    log.info("scan: %d symbols, %d candidates, %d dropped", len(sources), len(candidates), len(dropped))
    return ScanResult(candidates=candidates, news=news, dropped=dropped)
