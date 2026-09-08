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
    """Long the strongest news-backed gappers with a volatility-scaled stop."""

    name = "rules"

    def propose(self, cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> GamePlan:
        dt = cfg.daytrade
        scored = []
        for c in candidates.values():
            if not c.headlines or c.gap_pct < 2.0:
                continue
            score = min(c.gap_pct, 15.0) / 15.0 * 0.6 + min(len(c.headlines), 3) / 3.0 * 0.4
            scored.append((score, c))
        scored.sort(key=lambda t: -t[0])
        picks = []
        for score, c in scored[: dt.max_picks]:
            stop = min(max(c.atr_pct * 0.75, dt.min_stop_pct), dt.max_stop_pct)
            picks.append(
                Pick(
                    symbol=c.symbol,
                    direction="long",
                    catalyst=c.headlines[0],
                    thesis=f"gapping {c.gap_pct:+.1f}% on news with ${c.avg_dollar_volume/1e6:.0f}M average daily volume",
                    confidence=round(0.4 + 0.4 * score, 2),
                    stop_pct=round(stop, 2),
                    target_pct=round(stop * 2.0, 2),
                )
            )
        summary = f"rules analyst: {len(candidates)} liquid candidates, {len(scored)} gapping on news"
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
                    "confidence": {"type": "number", "description": "0 to 1"},
                    "stop_pct": {"type": "number", "description": "Stop distance from entry in percent."},
                    "target_pct": {"type": "number", "description": "Target distance from entry in percent."},
                },
                "required": ["symbol", "direction", "catalyst", "thesis", "confidence", "stop_pct", "target_pct"],
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

Each morning you receive overnight headlines and a screened list of liquid US stocks with their gap, volatility and volume statistics. Your job is to pick the few names, if any, whose news gives a real reason to expect continued movement during today's session, and to say clearly why.

How the desk works, so your plan fits it:
- Entries are market orders a few minutes after the open. Every position has a stop-loss and a take-profit attached, and everything is closed before the bell. There is no overnight risk.
- Position size is computed by the desk from your stop distance, so the stop must be where the idea is wrong, not an arbitrary number. Targets must be at least the configured reward-to-risk multiple of the stop.
- Only symbols from the candidate list can be traded. Anything else is ignored.
- Not trading is a valid and common answer. A day with no clear catalysts should return an empty picks list.

Prefer catalysts that are new, specific and company-level: earnings beats or misses with guidance changes, FDA decisions, contract wins, M&A, analyst upgrades on real news. Treat generic market commentary, stale news, and already-exhausted moves as reasons to avoid. Be sceptical of small stocks with huge gaps and thin volume. Keep each thesis to two or three sentences and include what would invalidate it."""


def build_user_prompt(cfg: Config, day: date, candidates: dict[str, Candidate], news: list[NewsItem]) -> str:
    dt = cfg.daytrade
    lines = [f"Trading day: {day.isoformat()}", ""]
    lines.append(f"Desk limits: at most {dt.max_picks} positions; stop between {dt.min_stop_pct}% and {dt.max_stop_pct}%; "
                 f"target at least {dt.min_reward_risk}x the stop; shorting {'allowed' if dt.allow_short else 'NOT allowed'}.")
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
    lines.append("Return the game plan as JSON matching the schema. Use only candidate symbols.")
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
                        stop_pct=float(raw["stop_pct"]),
                        target_pct=float(raw["target_pct"]),
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
