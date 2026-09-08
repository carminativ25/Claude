from datetime import date

import pytest

from stock_agent.broker import AlpacaBroker, BrokerError
from stock_agent.config import Credentials


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # (method, path) -> list of responses or callable
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.split("markets", 1)[1]
        self.calls.append((method, path, params, json))
        handler = self.routes[(method, path)]
        if callable(handler):
            return handler(params, json)
        return handler.pop(0)


def make_broker(routes):
    creds = Credentials(api_key="k", secret_key="s", live=False)
    session = FakeSession(routes)
    return AlpacaBroker(creds, session=session), session


def test_paper_headers_and_account_parsing():
    broker, session = make_broker({("GET", "/v2/account"): [FakeResponse(200, {"equity": "1234.5", "cash": "34.5", "buying_power": "34.5", "status": "ACTIVE", "trading_blocked": False})]})
    acct = broker.get_account()
    assert session.headers["APCA-API-KEY-ID"] == "k"
    assert acct.equity == 1234.5 and acct.cash == 34.5 and not acct.trading_blocked


def test_submit_notional_order_payload():
    broker, session = make_broker({("POST", "/v2/orders"): [FakeResponse(200, {"id": "abc", "symbol": "SCHD", "side": "buy", "status": "accepted", "notional": "123.45"})]})
    order = broker.submit_notional_order("SCHD", "buy", 123.456)
    _, _, _, payload = session.calls[0]
    assert payload == {"symbol": "SCHD", "side": "buy", "type": "market", "time_in_force": "day", "notional": "123.46"}
    assert order.id == "abc" and order.notional == 123.45


def test_client_error_raises_without_retry():
    broker, session = make_broker({("POST", "/v2/orders"): [FakeResponse(403, {"message": "insufficient buying power"})]})
    with pytest.raises(BrokerError, match="403"):
        broker.submit_notional_order("SCHD", "buy", 10)
    assert len(session.calls) == 1


def test_bars_follow_pagination_and_sort():
    pages = [
        FakeResponse(200, {"bars": {"SPY": [{"t": "2024-01-03T05:00:00Z", "c": "470.1"}]}, "next_page_token": "p2"}),
        FakeResponse(200, {"bars": {"SPY": [{"t": "2024-01-02T05:00:00Z", "c": "469.0"}]}, "next_page_token": None}),
    ]
    broker, session = make_broker({("GET", "/v2/stocks/bars"): pages})
    bars = broker.get_daily_bars(["SPY"], date(2024, 1, 1), date(2024, 1, 5))
    assert [b.day for b in bars["SPY"]] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert session.calls[1][2]["page_token"] == "p2"
    assert session.calls[0][2]["adjustment"] == "all"


def test_latest_prices_missing_symbol():
    broker, _ = make_broker({("GET", "/v2/stocks/trades/latest"): [FakeResponse(200, {"trades": {"SPY": {"p": 500.0}}})]})
    assert broker.get_latest_prices(["SPY"]) == {"SPY": 500.0}
    broker, _ = make_broker({("GET", "/v2/stocks/trades/latest"): [FakeResponse(200, {"trades": {}})]})
    with pytest.raises(BrokerError, match="No recent price"):
        broker.get_latest_prices(["SPY"])


def test_dividends_and_equity_history():
    routes = {
        ("GET", "/v2/account/activities"): [FakeResponse(200, [{"symbol": "SCHD", "net_amount": "12.34", "date": "2024-03-25"}, {"symbol": "VTI", "net_amount": "0", "date": "2024-03-26"}])],
        ("GET", "/v2/account/portfolio/history"): [FakeResponse(200, {"equity": [None, 0, 100.0, 90.0]})],
    }
    broker, _ = make_broker(routes)
    divs = broker.get_dividends(date(2024, 1, 1))
    assert len(divs) == 1 and divs[0].amount == 12.34 and divs[0].day == date(2024, 3, 25)
    assert broker.get_equity_history() == [100.0, 90.0]
