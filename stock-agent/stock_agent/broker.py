"""Broker abstractions: a live Alpaca client and an in-memory simulator."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import requests

from .config import Credentials
from .models import Account, Asset, Bar, Clock, Dividend, Fill, Mover, NewsItem, Order, Position, Quote

log = logging.getLogger(__name__)

DIVIDEND_ACTIVITY_TYPES = "DIV,DIVCGL,DIVCGS,DIVNRA,DIVROC,DIVTXEX"


class BrokerError(RuntimeError):
    """Raised when the broker rejects a request or returns bad data."""


class Broker(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_open_orders(self) -> list[Order]: ...
    def get_clock(self) -> Clock: ...
    def submit_notional_order(self, symbol: str, side: str, notional: float) -> Order: ...
    def close_position(self, symbol: str) -> Order: ...
    def get_order(self, order_id: str) -> Order: ...
    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]: ...
    def get_daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[Bar]]: ...
    def get_equity_history(self, period: str = "1A") -> list[float]: ...
    def get_dividends(self, since: date) -> list[Dividend]: ...
    # day-trading surface
    def submit_bracket_order(self, symbol: str, side: str, qty: int, take_profit: float, stop_loss: float) -> Order: ...
    def cancel_all_orders(self) -> None: ...
    def close_all_positions(self) -> list[Order]: ...
    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def get_news(self, since: datetime, limit: int = 50, symbols: list[str] | None = None) -> list[NewsItem]: ...
    def get_movers(self, top: int = 20) -> tuple[list[Mover], list[Mover]]: ...
    def get_most_active(self, top: int = 20) -> list[Mover]: ...
    def get_asset(self, symbol: str) -> Asset | None: ...
    def get_fills(self, day: date) -> list[Fill]: ...


def _f(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _parse_order(raw: dict) -> Order:
    return Order(
        id=str(raw.get("id", "")),
        symbol=str(raw.get("symbol", "")).upper(),
        side=str(raw.get("side", "")),
        status=str(raw.get("status", "")),
        notional=_f(raw.get("notional")) if raw.get("notional") is not None else None,
        qty=_f(raw.get("qty")) if raw.get("qty") is not None else None,
        filled_avg_price=_f(raw.get("filled_avg_price")) if raw.get("filled_avg_price") else None,
    )


class AlpacaBroker:
    """Thin wrapper over the Alpaca Trading and Market Data REST APIs."""

    def __init__(self, creds: Credentials, session: requests.Session | None = None, timeout: float = 20.0):
        self.creds = creds
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": creds.api_key,
                "APCA-API-SECRET-KEY": creds.secret_key,
                "Accept": "application/json",
            }
        )

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, base: str, path: str, *, params: dict | None = None, json: dict | None = None, retries: int = 3):
        url = f"{base}{path}"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
            except requests.RequestException as exc:  # network trouble: retry
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = BrokerError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise BrokerError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise BrokerError(f"{method} {path} failed after {retries} attempts: {last_exc}")

    def _trading(self, method: str, path: str, **kw):
        return self._request(method, self.creds.trading_base_url, path, **kw)

    def _data(self, method: str, path: str, **kw):
        return self._request(method, self.creds.data_base_url, path, **kw)

    # -- account -----------------------------------------------------------
    def get_account(self) -> Account:
        raw = self._trading("GET", "/v2/account")
        return Account(
            equity=_f(raw.get("equity")),
            cash=_f(raw.get("cash")),
            buying_power=_f(raw.get("buying_power")),
            status=str(raw.get("status", "")),
            trading_blocked=bool(raw.get("trading_blocked", False)),
            account_blocked=bool(raw.get("account_blocked", False)),
            pattern_day_trader=bool(raw.get("pattern_day_trader", False)),
            last_equity=_f(raw.get("last_equity")),
            daytrade_count=int(raw.get("daytrade_count") or 0),
        )

    def get_positions(self) -> list[Position]:
        raw = self._trading("GET", "/v2/positions") or []
        return [
            Position(
                symbol=str(p["symbol"]).upper(),
                qty=_f(p.get("qty")),
                market_value=_f(p.get("market_value")),
                avg_entry_price=_f(p.get("avg_entry_price")),
                unrealized_pl=_f(p.get("unrealized_pl")),
            )
            for p in raw
        ]

    def get_open_orders(self) -> list[Order]:
        raw = self._trading("GET", "/v2/orders", params={"status": "open", "limit": 500}) or []
        return [_parse_order(o) for o in raw]

    def get_clock(self) -> Clock:
        raw = self._trading("GET", "/v2/clock")
        return Clock(
            is_open=bool(raw.get("is_open")),
            next_open=str(raw.get("next_open", "")),
            next_close=str(raw.get("next_close", "")),
            timestamp=str(raw.get("timestamp", "")),
        )

    # -- orders ------------------------------------------------------------
    def submit_notional_order(self, symbol: str, side: str, notional: float) -> Order:
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "notional": f"{notional:.2f}",
        }
        raw = self._trading("POST", "/v2/orders", json=payload, retries=1)
        return _parse_order(raw)

    def close_position(self, symbol: str) -> Order:
        raw = self._trading("DELETE", f"/v2/positions/{symbol}", retries=1)
        return _parse_order(raw or {"symbol": symbol, "side": "sell", "status": "accepted"})

    def get_order(self, order_id: str) -> Order:
        return _parse_order(self._trading("GET", f"/v2/orders/{order_id}"))

    def submit_bracket_order(self, symbol: str, side: str, qty: int, take_profit: float, stop_loss: float) -> Order:
        """Market entry with an attached take-profit limit and stop-loss. Whole shares only."""
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "qty": str(int(qty)),
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
        }
        return _parse_order(self._trading("POST", "/v2/orders", json=payload, retries=1))

    def cancel_all_orders(self) -> None:
        self._trading("DELETE", "/v2/orders", retries=1)

    def close_all_positions(self) -> list[Order]:
        raw = self._trading("DELETE", "/v2/positions", params={"cancel_orders": "true"}, retries=1) or []
        out = []
        for item in raw:
            body = item.get("body") if isinstance(item, dict) and "body" in item else item
            if isinstance(body, dict) and body.get("symbol"):
                out.append(_parse_order(body))
        return out

    def get_asset(self, symbol: str) -> Asset | None:
        try:
            raw = self._trading("GET", f"/v2/assets/{symbol}")
        except BrokerError as exc:
            if "404" in str(exc):
                return None
            raise
        return Asset(
            symbol=str(raw.get("symbol", symbol)).upper(),
            tradable=bool(raw.get("tradable")) and str(raw.get("status", "")).lower() == "active",
            exchange=str(raw.get("exchange", "")),
            shortable=bool(raw.get("shortable")),
            fractionable=bool(raw.get("fractionable")),
            asset_class=str(raw.get("class", "us_equity")),
        )

    def get_fills(self, day: date) -> list[Fill]:
        raw = self._trading("GET", "/v2/account/activities/FILL", params={"date": day.isoformat(), "page_size": 100}) or []
        return [
            Fill(symbol=str(a.get("symbol", "")).upper(), side=str(a.get("side", "")), qty=_f(a.get("qty")), price=_f(a.get("price")), time=str(a.get("transaction_time", "")))
            for a in raw
        ]

    # -- market data -------------------------------------------------------
    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        raw = self._data("GET", "/v2/stocks/trades/latest", params={"symbols": ",".join(symbols), "feed": "iex"})
        trades = raw.get("trades", {})
        prices = {sym.upper(): _f(t.get("p")) for sym, t in trades.items()}
        missing = [s for s in symbols if s not in prices or prices[s] <= 0]
        if missing:
            raise BrokerError(f"No recent price for {missing}")
        return prices

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        out: dict[str, Quote] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i : i + 100]
            raw = self._data("GET", "/v2/stocks/quotes/latest", params={"symbols": ",".join(chunk), "feed": "iex"})
            for sym, q in (raw.get("quotes") or {}).items():
                out[sym.upper()] = Quote(symbol=sym.upper(), bid=_f(q.get("bp")), ask=_f(q.get("ap")))
        return out

    def get_news(self, since: datetime, limit: int = 50, symbols: list[str] | None = None) -> list[NewsItem]:
        params: dict = {"start": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": min(limit, 50), "sort": "desc", "exclude_contentless": "true"}
        if symbols:
            params["symbols"] = ",".join(symbols)
        raw = self._data("GET", "/v1beta1/news", params=params)
        items = []
        for n in raw.get("news") or []:
            items.append(
                NewsItem(
                    headline=str(n.get("headline", "")).strip(),
                    summary=str(n.get("summary", "")).strip(),
                    symbols=tuple(str(x).upper() for x in (n.get("symbols") or [])),
                    source=str(n.get("source", "")),
                    created_at=str(n.get("created_at", "")),
                    url=str(n.get("url", "")),
                )
            )
        return items

    def get_movers(self, top: int = 20) -> tuple[list[Mover], list[Mover]]:
        raw = self._data("GET", "/v1beta1/screener/stocks/movers", params={"top": top})

        def conv(rows):
            return [Mover(symbol=str(r["symbol"]).upper(), price=_f(r.get("price")), percent_change=_f(r.get("percent_change"))) for r in rows or []]

        return conv(raw.get("gainers")), conv(raw.get("losers"))

    def get_most_active(self, top: int = 20) -> list[Mover]:
        raw = self._data("GET", "/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": top})
        return [Mover(symbol=str(r["symbol"]).upper(), price=0.0, percent_change=0.0, volume=_f(r.get("volume"))) for r in raw.get("most_actives") or []]

    def get_daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "adjustment": "all",
            "feed": "iex",
            "sort": "asc",
        }
        while True:
            raw = self._data("GET", "/v2/stocks/bars", params=params)
            for sym, bars in (raw.get("bars") or {}).items():
                out.setdefault(sym.upper(), []).extend(
                    Bar(
                        day=datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date(),
                        close=_f(b["c"]),
                        high=_f(b.get("h"), _f(b["c"])),
                        low=_f(b.get("l"), _f(b["c"])),
                        volume=_f(b.get("v")),
                    )
                    for b in bars
                )
            token = raw.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        for sym in symbols:
            out[sym].sort(key=lambda b: b.day)
        return out

    def get_equity_history(self, period: str = "1A") -> list[float]:
        raw = self._trading("GET", "/v2/account/portfolio/history", params={"period": period, "timeframe": "1D"})
        return [float(v) for v in (raw.get("equity") or []) if v is not None and float(v) > 0]

    def get_dividends(self, since: date) -> list[Dividend]:
        raw = self._trading(
            "GET",
            "/v2/account/activities",
            params={"activity_types": DIVIDEND_ACTIVITY_TYPES, "after": since.isoformat(), "page_size": 100},
        ) or []
        out = []
        for a in raw:
            amount = _f(a.get("net_amount"))
            if amount == 0:
                continue
            out.append(Dividend(symbol=str(a.get("symbol", "")).upper(), amount=amount, day=date.fromisoformat(str(a.get("date"))[:10])))
        return out


class SimBroker:
    """In-memory broker that fills notional market orders instantly at the current price.

    Used by the backtester and the test-suite. It never touches the network.
    """

    def __init__(self, cash: float, prices: dict[str, float] | None = None, market_open: bool = True):
        self.cash = cash
        self.prices: dict[str, float] = dict(prices or {})
        self.shares: dict[str, float] = {}
        self.market_open = market_open
        self.orders: list[Order] = []
        self.open_orders: list[Order] = []
        self.bars: dict[str, list[Bar]] = {}
        self.equity_history: list[float] = []
        self.dividends: list[Dividend] = []
        self._next_id = 1
        # day-trading state (set by tests / backtests)
        self.last_equity = cash
        self.daytrade_count = 0
        self.quotes: dict[str, Quote] = {}
        self.news: list[NewsItem] = []
        self.gainers: list[Mover] = []
        self.losers: list[Mover] = []
        self.actives: list[Mover] = []
        self.assets: dict[str, Asset] = {}
        self.fills: list[Fill] = []
        self.brackets: list[dict] = []
        self.clock_timestamp = ""
        self.next_close = ""

    # helpers for callers
    def set_prices(self, prices: dict[str, float]) -> None:
        self.prices.update(prices)

    def position_value(self, symbol: str) -> float:
        return self.shares.get(symbol, 0.0) * self.prices.get(symbol, 0.0)

    @property
    def equity(self) -> float:
        return self.cash + sum(self.position_value(s) for s in self.shares)

    # Broker protocol
    def get_account(self) -> Account:
        return Account(equity=self.equity, cash=self.cash, buying_power=self.cash, last_equity=self.last_equity, daytrade_count=self.daytrade_count)

    def get_positions(self) -> list[Position]:
        return [
            Position(symbol=s, qty=q, market_value=self.position_value(s))
            for s, q in self.shares.items()
            if q > 1e-9
        ]

    def get_open_orders(self) -> list[Order]:
        return list(self.open_orders)

    def get_clock(self) -> Clock:
        return Clock(is_open=self.market_open, next_close=self.next_close, timestamp=self.clock_timestamp)

    def submit_bracket_order(self, symbol: str, side: str, qty: int, take_profit: float, stop_loss: float) -> Order:
        price = self.prices.get(symbol)
        if not price:
            raise BrokerError(f"no price for {symbol}")
        if side != "buy":
            raise BrokerError("SimBroker brackets are long-only")
        notional = qty * price
        if notional > self.cash + 1e-6:
            raise BrokerError(f"insufficient cash for {symbol}")
        self.cash -= notional
        self.shares[symbol] = self.shares.get(symbol, 0.0) + qty
        self.fills.append(Fill(symbol, "buy", qty, price, self.clock_timestamp))
        self.brackets.append({"symbol": symbol, "qty": qty, "take_profit": take_profit, "stop_loss": stop_loss})
        return self._new_order(symbol, side, notional, qty)

    def cancel_all_orders(self) -> None:
        self.open_orders = []

    def close_all_positions(self) -> list[Order]:
        self.open_orders = []
        out = []
        for symbol in list(self.shares):
            if self.shares[symbol] > 1e-9:
                qty = self.shares[symbol]
                order = self.close_position(symbol)
                self.fills.append(Fill(symbol, "sell", qty, self.prices.get(symbol, 0.0), self.clock_timestamp))
                out.append(order)
        return out

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def get_news(self, since: datetime, limit: int = 50, symbols: list[str] | None = None) -> list[NewsItem]:
        items = self.news
        if symbols:
            wanted = set(symbols)
            items = [n for n in items if wanted & set(n.symbols)]
        return items[:limit]

    def get_movers(self, top: int = 20) -> tuple[list[Mover], list[Mover]]:
        return self.gainers[:top], self.losers[:top]

    def get_most_active(self, top: int = 20) -> list[Mover]:
        return self.actives[:top]

    def get_asset(self, symbol: str) -> Asset | None:
        return self.assets.get(symbol, Asset(symbol=symbol, tradable=True, exchange="NASDAQ"))

    def get_fills(self, day: date) -> list[Fill]:
        return list(self.fills)

    def _new_order(self, symbol: str, side: str, notional: float, qty: float) -> Order:
        order = Order(id=f"sim-{self._next_id}", symbol=symbol, side=side, status="filled", notional=notional, qty=qty, filled_avg_price=self.prices[symbol])
        self._next_id += 1
        self.orders.append(order)
        return order

    def submit_notional_order(self, symbol: str, side: str, notional: float) -> Order:
        price = self.prices.get(symbol)
        if not price:
            raise BrokerError(f"no price for {symbol}")
        qty = notional / price
        if side == "buy":
            if notional > self.cash + 1e-6:
                raise BrokerError(f"insufficient cash for {symbol}: need {notional:.2f}, have {self.cash:.2f}")
            self.cash -= notional
            self.shares[symbol] = self.shares.get(symbol, 0.0) + qty
        elif side == "sell":
            held = self.shares.get(symbol, 0.0)
            if qty > held + 1e-9:
                raise BrokerError(f"cannot sell {qty:.4f} {symbol}, only hold {held:.4f}")
            self.shares[symbol] = held - qty
            self.cash += notional
        else:
            raise BrokerError(f"bad side {side}")
        return self._new_order(symbol, side, notional, qty)

    def close_position(self, symbol: str) -> Order:
        qty = self.shares.pop(symbol, 0.0)
        notional = qty * self.prices.get(symbol, 0.0)
        self.cash += notional
        return self._new_order(symbol, "sell", notional, qty)

    def get_order(self, order_id: str) -> Order:
        for o in self.orders:
            if o.id == order_id:
                return o
        raise BrokerError(f"unknown order {order_id}")

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        missing = [s for s in symbols if s not in self.prices]
        if missing:
            raise BrokerError(f"No recent price for {missing}")
        return {s: self.prices[s] for s in symbols}

    def get_daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        return {s: [b for b in self.bars.get(s, []) if start <= b.day <= end] for s in symbols}

    def get_equity_history(self, period: str = "1A") -> list[float]:
        return list(self.equity_history)

    def get_dividends(self, since: date) -> list[Dividend]:
        return [d for d in self.dividends if d.day >= since]


def bars_lookback_start(today: date, sma_days: int) -> date:
    """Calendar start date that comfortably covers `sma_days` trading days."""
    return today - timedelta(days=int(sma_days * 1.6) + 15)
