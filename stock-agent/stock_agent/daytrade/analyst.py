"""Turn the morning scan into a game plan.

Two analysts share one interface:

* ClaudeAnalyst  - sends the headlines and candidate statistics to Claude and
                   asks for a structured JSON plan.
* RulesAnalyst   - a deterministic "gap and go" screen used when no Anthropic
                   credentials are available, and as the fallback if the model
                   call fails. It keeps the agent runnable end to end offline.

Whatever the analyst proposes is still validated and sized by plan.py; the
model never sets position size or bypasses the risk limits.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Protocol

from ..config import Config
from ..models import NewsItem
from .plan import Candidate, GamePlan, Pick

log = logging.getLogger(__name__)


class Analyst(Protocol):
    name: str

    def propose(self, cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> GamePlan: ...


# ---------------------------------------------------------------------------
# Rules-based fallback
# ---------------------------------------------------------------------------
class RulesAnalyst:
    """Watch the strongest gappers, preferring those with a headline behind the gap."""

    name = "rules"

    def propose(self, cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> GamePlan:
        dt = cfg.daytrade
        scored = []
        for c in candidates.values():
            if abs(c.gap_pct) < dt.min_gap_pct:
                continue
            if dt.min_gap_atr > 0 and (c.atr_pct <= 0 or abs(c.gap_pct) < dt.min_gap_atr * c.atr_pct):
                continue
            score = min(abs(c.gap_pct), 15.0) / 15.0 * 0.6 + min(len(c.headlines), 3) / 3.0 * 0.4
            scored.append((score, c))
        scored.sort(key=lambda t: -t[0])
        picks = []
        for score, c in scored[: dt.max_watchlist]:
            direction = "long" if c.gap_pct > 0 or not dt.allow_short else "short"
            if c.gap_pct < 0 and not dt.allow_short:
                continue
            picks.append(
                Pick(
                    symbol=c.symbol,
                    direction=direction,
                    catalyst=c.headlines[0] if c.headlines else "gap without a headline",
                    thesis=f"gapping {c.gap_pct:+.1f}% with ${c.avg_dollar_volume/1e6:.0f}M average daily volume; trade only on a range breakout",
                    confidence=round(0.4 + 0.4 * score, 2),
                )
            )
        summary = f"rules analyst: {len(candidates)} liquid candidates, {len(scored)} gapping at least {dt.min_gap_pct}%"
        return GamePlan(day=day.isoformat(), market_summary=summary, picks=picks, analyst=self.name)


# ---------------------------------------------------------------------------
# Claude analyst
# ---------------------------------------------------------------------------
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "market_summary": {"type": "string", "description": "Two or three sentences on the overnight news backdrop and what kind of day to expect."},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "direction": {"type": "string", "enum": ["long", "short"]},
                    "catalyst": {"type": "string", "description": "The specific news event driving the trade."},
                    "thesis": {"type": "string", "description": "Why the stock should move further today, and what would prove the idea wrong."},
                    "confidence": {"type": "number", "description": "0 to 1: how likely today's move is to continue rather than fade."},
                },
                "required": ["symbol", "direction", "catalyst", "thesis", "confidence"],
                "additionalProperties": False,
            },
        },
        "avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["symbol", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_summary", "picks", "avoid"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the pre-market analyst for a small, rules-bound intraday trading desk.

Each morning you receive overnight headlines and a screened list of liquid US stocks with their gap, volatility and volume statistics. Your job is to build the day's watchlist: the few names, if any, where the news gives a real reason for today's move to continue rather than fade, and to say clearly why.

How the desk works, so your watchlist fits it:
- You do not trigger trades. The desk waits for the opening range to form and only enters a watchlist name if price breaks out of that range on unusually high volume. Stops, targets and position size are all computed from the range. Everything is closed before the bell.
- Only symbols from the candidate list can be watched. Anything else is ignored.
- Use "direction" to say which way a breakout would be worth taking. Shorts are only used if the desk allows them.
- An empty watchlist is a valid and common answer. A day with no clear catalysts should return no picks.

Prefer catalysts that are new, specific and company-level: earnings with guidance changes, FDA decisions, contract wins, M&A, analyst moves on real news. Flag reasons a gap is likely to fade: dilutive offerings, lockup expiries, stale news, moves already exhausted overnight, small stocks with huge gaps and thin volume. Keep each thesis to two or three sentences."""


def build_user_prompt(cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> str:
    dt = cfg.daytrade
    lines = [f"Trading day: {day.isoformat()}", ""]
    lines.append(f"Desk limits: watchlist of at most {dt.max_watchlist} names, at most {dt.max_picks} positions taken; "
                 f"entries only on a {dt.range_minutes}-minute opening range breakout with relative volume above {dt.min_relative_volume}x; "
                 f"shorting {'allowed' if dt.allow_short else 'NOT allowed'}.")
    lines.append("")
    lines.append("## Overnight headlines (newest first)")
    for n in news[:50]:
        syms = ",".join(n.symbols) if n.symbols else "-"
        summary = f" | {n.summary[:200]}" if n.summary else ""
        lines.append(f"- [{n.created_at[:16]}] ({syms}) {n.headline}{summary}")
    lines.append("")
    lines.append("## Screened candidates (all pass liquidity and spread filters)")
    lines.append("symbol | source | price | gap% vs prev close | 20d avg $ volume | ATR% | headlines")
    for c in sorted(candidates.values(), key=lambda c: -abs(c.gap_pct)):
        heads = " || ".join(c.headlines[:3]) if c.headlines else "-"
        lines.append(f"{c.symbol} | {c.source} | {c.price:.2f} | {c.gap_pct:+.2f}% | ${c.avg_dollar_volume/1e6:.0f}M | {c.atr_pct:.2f}% | {heads}")
    lines.append("")
    lines.append("Return the watchlist as JSON matching the schema. Use only candidate symbols.")
    return "\n".join(lines)


class ClaudeAnalyst:
    """Asks Claude for a structured game plan; falls back to the rules analyst on failure."""

    name = "claude"

    def __init__(self, client=None, model: str | None = None, effort: str | None = None):
        self._client = client
        self.model = model
        self.effort = effort

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def propose(self, cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> GamePlan:
        import anthropic

        model = self.model or cfg.daytrade.analyst_model
        effort = self.effort or cfg.daytrade.analyst_effort
        prompt = build_user_prompt(cfg, day, candidates, news)
        try:
            client = self._get_client()
            response = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            return self._fallback(cfg, day, candidates, news, f"rate limited: {exc}")
        except anthropic.APIStatusError as exc:
            return self._fallback(cfg, day, candidates, news, f"API error {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            return self._fallback(cfg, day, candidates, news, f"connection error: {exc}")

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            return self._fallback(cfg, day, candidates, news, f"model declined the request ({getattr(detail, 'category', None)})")
        if response.stop_reason == "max_tokens":
            return self._fallback(cfg, day, candidates, news, "response truncated")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._fallback(cfg, day, candidates, news, f"could not parse plan JSON: {exc}")

        picks = []
        for raw in data.get("picks", []):
            try:
                picks.append(
                    Pick(
                        symbol=str(raw["symbol"]).upper(),
                        direction=str(raw["direction"]),
                        catalyst=str(raw["catalyst"]),
                        thesis=str(raw["thesis"]),
                        confidence=float(raw["confidence"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed pick %r: %s", raw, exc)
        served = getattr(response, "model", model)
        return GamePlan(
            day=day.isoformat(),
            market_summary=str(data.get("market_summary", "")),
            picks=picks,
            avoid=[{"symbol": str(a.get("symbol", "")).upper(), "reason": str(a.get("reason", ""))} for a in data.get("avoid", [])],
            analyst=f"claude:{served}",
        )

    def _fallback(self, cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem], why: str) -> GamePlan:
        log.warning("claude analyst unavailable (%s); using rules analyst", why)
        plan = RulesAnalyst().propose(cfg, day, candidates, news)
        plan.market_summary = f"[claude fallback: {why}] " + plan.market_summary
        plan.analyst = "rules (claude fallback)"
        return plan


def default_analyst(cfg: Config, use_claude: bool) -> Analyst:
    return ClaudeAnalyst() if use_claude else RulesAnalyst()
