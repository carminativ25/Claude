import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import make_config
from stock_agent.broker import SimBroker
from stock_agent.daytrade import journal
from stock_agent.daytrade.analyst import ClaudeAnalyst, RulesAnalyst, build_user_prompt
from stock_agent.daytrade.plan import Candidate, GamePlan, Pick, load_plan, save_plan, size_position, validate_picks
from stock_agent.daytrade.scan import average_dollar_volume, average_true_range_pct, scan
from stock_agent.daytrade.session import close_day, monitor, open_day, plan_day
from stock_agent.models import Asset, Bar, Mover, NewsItem, Quote

TODAY = date(2026, 9, 8)


def dcfg(tmp_path, **overrides):
    dt = {"journal_dir": str(tmp_path / "journal")}
    dt.update(overrides)
    return make_config(daytrade=dt)


def history(price, days=30, vol=2_000_000, drift=0.0):
    out = []
    p = price
    for i in range(days):
        p *= 1 + drift
        out.append(Bar(TODAY - timedelta(days=days - i), close=round(p, 2), high=round(p * 1.02, 2), low=round(p * 0.98, 2), volume=vol))
    return out


def market_broker(cash=50_000):
    b = SimBroker(cash=cash)
    b.market_open = True
    b.gainers = [Mover("ACME", 52.0, 8.0), Mover("PENNY", 2.0, 40.0), Mover("THIN", 30.0, 6.0)]
    b.actives = [Mover("BIGCO", 0, 0, volume=9e6)]
    b.news = [
        NewsItem("ACME beats on earnings, raises guidance", "Strong quarter.", ("ACME",), "Benzinga", "2026-09-08T11:00:00Z"),
        NewsItem("ACME upgraded to Buy", "", ("ACME",), "Benzinga", "2026-09-08T10:30:00Z"),
        NewsItem("Markets await CPI", "", (), "Benzinga", "2026-09-08T09:00:00Z"),
        NewsItem("WIDE wins contract", "", ("WIDE",), "Benzinga", "2026-09-08T08:00:00Z"),
    ]
    b.bars = {
        "ACME": history(48.0),
        "PENNY": history(1.5),
        "THIN": history(28.0, vol=50_000),
        "BIGCO": history(200.0, vol=5_000_000),
        "WIDE": history(20.0),
    }
    b.quotes = {
        "ACME": Quote("ACME", 51.95, 52.05),
        "PENNY": Quote("PENNY", 1.99, 2.01),
        "THIN": Quote("THIN", 29.9, 30.1),
        "BIGCO": Quote("BIGCO", 200.0, 200.2),
        "WIDE": Quote("WIDE", 19.5, 20.5),
    }
    b.set_prices({"ACME": 52.0, "PENNY": 2.0, "THIN": 30.0, "BIGCO": 200.1, "WIDE": 20.0})
    b.last_equity = cash
    return b


# ---------------------------------------------------------------- scan
def test_atr_and_dollar_volume():
    bars = history(100.0)
    assert 3.5 < average_true_range_pct(bars) < 4.5   # 2% up + 2% down range each day
    assert average_dollar_volume(bars) == pytest.approx(100.0 * 2_000_000, rel=0.01)


def test_scan_filters(tmp_path):
    cfg = dcfg(tmp_path)
    res = scan(cfg, market_broker(), today=TODAY)
    assert set(res.candidates) == {"ACME", "BIGCO"}
    reasons = dict(res.dropped)
    assert "price" in reasons["PENNY"]
    assert "dollar volume" in reasons["THIN"]
    assert "spread" in reasons["WIDE"]
    acme = res.candidates["ACME"]
    assert acme.gap_pct == pytest.approx((52.0 - 48.0) / 48.0 * 100, abs=0.05)
    assert len(acme.headlines) == 2 and acme.source == "gainer"


def test_scan_drops_untradable_and_blocklisted(tmp_path):
    cfg = dcfg(tmp_path, blocklist=["BIGCO"])
    b = market_broker()
    b.assets["ACME"] = Asset("ACME", tradable=False, exchange="NASDAQ")
    res = scan(cfg, b, today=TODAY)
    assert res.candidates == {}
    assert ("ACME", "not tradable") in res.dropped
    assert ("BIGCO", "blocklisted") in res.dropped


# ---------------------------------------------------------------- plan / sizing
def cands():
    return {
        "ACME": Candidate("ACME", 52.0, 48.0, 8.33, 1e8, 4.0, 0.1, ["beat"], "gainer"),
        "BIGCO": Candidate("BIGCO", 200.0, 199.0, 0.5, 1e9, 1.5, 0.05, [], "active"),
    }


def test_validate_picks_clamps_and_rejects(tmp_path):
    cfg = dcfg(tmp_path, max_picks=2)
    picks = [
        Pick("ACME", "long", "c", "t", 0.9, stop_pct=0.1, target_pct=0.1),     # stop too tight, target too close
        Pick("BIGCO", "short", "c", "t", 0.8, 2.0, 5.0),                        # shorting disabled
        Pick("NOPE", "long", "c", "t", 0.7, 2.0, 5.0),                          # not a candidate
        Pick("ACME", "long", "c", "t", 0.5, 2.0, 5.0),                          # duplicate
    ]
    ok, rejected = validate_picks(cfg, picks, cands())
    assert [p.symbol for p in ok] == ["ACME"]
    assert ok[0].stop_pct == 0.75 and ok[0].target_pct >= 0.75 * 1.5
    assert {r["symbol"] for r in rejected} == {"BIGCO", "NOPE"}


def test_validate_picks_respects_max_and_confidence_order(tmp_path):
    cfg = dcfg(tmp_path, max_picks=1)
    picks = [Pick("BIGCO", "long", "c", "t", 0.6, 1.0, 2.0), Pick("ACME", "long", "c", "t", 0.9, 1.0, 2.0)]
    ok, _ = validate_picks(cfg, picks, cands())
    assert [p.symbol for p in ok] == ["ACME"]


def test_size_position_by_risk(tmp_path):
    cfg = dcfg(tmp_path, risk_per_trade_pct=1.0, max_position_pct=50.0)
    pick = Pick("ACME", "long", "c", "t", 0.9, stop_pct=2.0, target_pct=4.0)
    sized = size_position(cfg, pick, entry=50.0, equity=10_000, cash=10_000)
    # risk $100, stop distance $1 -> 100 shares, $5000 = 50% cap
    assert sized.qty == 100 and sized.stop == 49.0 and sized.target == 52.0 and sized.risk_dollars == 100.0


def test_size_position_caps_by_weight_and_cash(tmp_path):
    cfg = dcfg(tmp_path, risk_per_trade_pct=5.0, max_position_pct=10.0)
    pick = Pick("ACME", "long", "c", "t", 0.9, 1.0, 2.0)
    assert size_position(cfg, pick, 50.0, 10_000, 10_000).qty == 20      # 10% of 10k / 50
    assert size_position(cfg, pick, 50.0, 10_000, 600).qty == 12         # cash-bound
    assert size_position(cfg, pick, 5_000.0, 10_000, 10_000) is None    # less than one share


def test_plan_round_trip(tmp_path):
    cfg = dcfg(tmp_path)
    plan = GamePlan(day="2026-09-08", market_summary="quiet", picks=[Pick("ACME", "long", "c", "t", 0.8, 1.0, 2.0)], analyst="rules")
    save_plan(cfg, plan)
    loaded = load_plan(cfg, "2026-09-08")
    assert loaded.picks[0].symbol == "ACME" and loaded.analyst == "rules"
    assert "ACME" in loaded.summary()


# ---------------------------------------------------------------- analysts
def test_rules_analyst_picks_news_gappers(tmp_path):
    cfg = dcfg(tmp_path)
    plan = RulesAnalyst().propose(cfg, TODAY, cands(), [])
    assert [p.symbol for p in plan.picks] == ["ACME"]      # BIGCO has no headlines
    p = plan.picks[0]
    assert p.stop_pct == 3.0 and p.target_pct == 6.0        # 0.75 * ATR 4%, 2:1


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.response


def fake_client(response=None, exc=None):
    msgs = FakeMessages(response, exc)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def text_response(payload, stop="end_turn", model="claude-opus-5"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))], model=model, stop_details=None)


def test_claude_analyst_parses_structured_plan(tmp_path):
    cfg = dcfg(tmp_path)
    payload = {
        "market_summary": "Earnings-driven morning.",
        "picks": [{"symbol": "acme", "direction": "long", "catalyst": "beat", "thesis": "guidance up", "confidence": 0.7, "stop_pct": 2.0, "target_pct": 4.0}],
        "avoid": [{"symbol": "BIGCO", "reason": "no catalyst"}],
    }
    client, msgs = fake_client(text_response(payload))
    plan = ClaudeAnalyst(client=client).propose(cfg, TODAY, cands(), [])
    assert plan.picks[0].symbol == "ACME" and plan.analyst == "claude:claude-opus-5"
    assert plan.avoid[0]["symbol"] == "BIGCO"
    kw = msgs.calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert "ACME" in kw["messages"][0]["content"]


def test_claude_analyst_falls_back_on_refusal_and_errors(tmp_path):
    import anthropic
    import httpx2 as httpx

    cfg = dcfg(tmp_path)
    refused = SimpleNamespace(stop_reason="refusal", content=[], model="x", stop_details=SimpleNamespace(category="cyber"))
    client, _ = fake_client(refused)
    plan = ClaudeAnalyst(client=client).propose(cfg, TODAY, cands(), [])
    assert plan.analyst.startswith("rules") and "fallback" in plan.market_summary
    assert [p.symbol for p in plan.picks] == ["ACME"]

    client, _ = fake_client(exc=anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
    plan = ClaudeAnalyst(client=client).propose(cfg, TODAY, cands(), [])
    assert "connection error" in plan.market_summary

    client, _ = fake_client(SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")], model="x", stop_details=None))
    plan = ClaudeAnalyst(client=client).propose(cfg, TODAY, cands(), [])
    assert "could not parse" in plan.market_summary


def test_prompt_mentions_limits_and_headlines(tmp_path):
    cfg = dcfg(tmp_path, max_picks=2)
    news = [NewsItem("ACME beats", "big", ("ACME",), "src", "2026-09-08T11:00:00Z")]
    text = build_user_prompt(cfg, TODAY, cands(), news)
    assert "at most 2 positions" in text and "ACME beats" in text and "NOT allowed" in text


# ---------------------------------------------------------------- session
def test_plan_day_end_to_end(tmp_path):
    cfg = dcfg(tmp_path)
    plan, report = plan_day(cfg, market_broker(), RulesAnalyst(), today=TODAY)
    assert [p.symbol for p in plan.picks] == ["ACME"]
    assert load_plan(cfg, TODAY) is not None
    assert "plan saved" in report.summary()
    events = journal.load_day(cfg, TODAY)["events"]
    assert events[0]["kind"] == "plan" and events[0]["picks"] == ["ACME"]


def test_open_day_dry_run_then_execute(tmp_path):
    cfg = dcfg(tmp_path, risk_per_trade_pct=1.0)
    broker = market_broker(cash=50_000)
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    report = open_day(cfg, broker, dry_run=True, today=TODAY)
    assert report.halted_reason is None and broker.brackets == []
    assert any(line.startswith("BUY  ACME") for line in report.lines)

    report = open_day(cfg, broker, dry_run=False, today=TODAY)
    assert len(report.orders) == 1
    br = broker.brackets[0]
    # entry at ask 52.05, stop 3% -> 1.5615 per share, risk $500 -> 320 shares, capped by 20% of equity (10k / 52.05 = 192)
    assert br["symbol"] == "ACME" and br["qty"] == 192
    assert br["stop_loss"] == pytest.approx(52.05 * 0.97, abs=0.01)
    assert br["take_profit"] == pytest.approx(52.05 * 1.06, abs=0.01)
    # second call must not double up
    again = open_day(cfg, broker, dry_run=False, today=TODAY)
    assert again.orders == [] and any("already has a position" in line for line in again.lines)


def test_open_day_requires_plan_and_open_market(tmp_path):
    cfg = dcfg(tmp_path)
    broker = market_broker()
    assert "no game plan" in open_day(cfg, broker, dry_run=False, today=TODAY).halted_reason
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    broker.market_open = False
    assert "market is closed" in open_day(cfg, broker, dry_run=False, today=TODAY).halted_reason
    assert broker.brackets == []


def test_open_day_pdt_rule_limits_small_accounts(tmp_path):
    cfg = dcfg(tmp_path)
    broker = market_broker(cash=10_000)
    broker.daytrade_count = 3
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    report = open_day(cfg, broker, dry_run=False, today=TODAY)
    assert broker.brackets == [] and any("PDT rule" in w for w in report.warnings)


def test_open_day_blocked_after_loss_limit(tmp_path):
    cfg = dcfg(tmp_path, max_daily_loss_pct=2.0)
    broker = market_broker(cash=50_000)
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    broker.last_equity = 52_000  # down 3.8% already
    report = open_day(cfg, broker, dry_run=False, today=TODAY)
    assert "already down" in report.halted_reason and broker.brackets == []


def test_monitor_flattens_on_loss_limit_and_halts_reentry(tmp_path):
    cfg = dcfg(tmp_path, max_daily_loss_pct=2.0)
    broker = market_broker(cash=50_000)
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    open_day(cfg, broker, dry_run=False, today=TODAY)
    broker.set_prices({"ACME": 45.0})  # position tanks
    broker.last_equity = 50_000
    report = monitor(cfg, broker, dry_run=False, today=TODAY)
    assert "loss limit" in report.halted_reason
    assert broker.get_positions() == []
    kinds = [e["kind"] for e in journal.load_day(cfg, TODAY)["events"]]
    assert "halt" in kinds and "close" in kinds
    # the halt sticks for the rest of the day
    assert "halted earlier today" in open_day(cfg, broker, dry_run=False, today=TODAY).halted_reason


def test_monitor_flattens_before_close_only(tmp_path):
    cfg = dcfg(tmp_path, flatten_minutes_before_close=10)
    broker = market_broker(cash=50_000)
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    open_day(cfg, broker, dry_run=False, today=TODAY)
    close_at = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
    broker.next_close = close_at.isoformat()
    broker.clock_timestamp = (close_at - timedelta(hours=2)).isoformat()
    monitor(cfg, broker, dry_run=False, today=TODAY)
    assert len(broker.get_positions()) == 1
    broker.clock_timestamp = (close_at - timedelta(minutes=8)).isoformat()
    report = monitor(cfg, broker, dry_run=False, today=TODAY)
    assert broker.get_positions() == [] and "end of day" in report.summary()


def test_close_day_records_pnl_and_review(tmp_path):
    cfg = dcfg(tmp_path)
    broker = market_broker(cash=50_000)
    plan_day(cfg, broker, RulesAnalyst(), today=TODAY)
    open_day(cfg, broker, dry_run=False, today=TODAY)
    broker.set_prices({"ACME": 54.0})
    report = close_day(cfg, broker, dry_run=False, today=TODAY)
    assert broker.get_positions() == []
    qty = broker.brackets[0]["qty"]
    expected = qty * (54.0 - 52.0)  # SimBroker fills at last price (52.0), not the ask
    close_event = [e for e in journal.load_day(cfg, TODAY)["events"] if e["kind"] == "close"][-1]
    assert close_event["pnl_by_symbol"]["ACME"] == pytest.approx(expected)
    assert f"{expected:+,.2f}" in report.summary()
    stats, rows = journal.review(cfg)
    assert stats.days == 1 and stats.trades == 1 and stats.wins == 1
    assert stats.total_pnl == pytest.approx(expected) and stats.profit_factor is None
    assert any("ACME" in r for r in rows)


def test_daytrade_config_validation():
    with pytest.raises(Exception, match="risk_per_trade_pct"):
        make_config(daytrade={"risk_per_trade_pct": 10})
    with pytest.raises(Exception, match="analyst_effort"):
        make_config(daytrade={"analyst_effort": "turbo"})
    cfg = make_config(daytrade={"blocklist": ["tsla"]})
    assert cfg.daytrade.blocklist == ("TSLA",)
