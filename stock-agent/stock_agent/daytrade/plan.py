"""Game-plan data structures, validation, and position sizing."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from ..config import Config


@dataclass
class Candidate:
    """A stock that passed the liquidity screen, with the numbers the analyst needs."""

    symbol: str
    price: float
    prev_close: float
    gap_pct: float               # pre-market / current price vs previous close
    avg_dollar_volume: float     # 20-day average
    atr_pct: float               # 14-day average true range as % of price
    spread_pct: float
    headlines: list[str] = field(default_factory=list)
    source: str = ""             # gainer / loser / active / news


@dataclass
class Pick:
    """A watchlist entry. Entries, stops and targets come from price action, not from here."""

    symbol: str
    direction: str               # long | short
    catalyst: str
    thesis: str
    confidence: float            # 0..1


@dataclass
class GamePlan:
    day: str
    market_summary: str
    picks: list[Pick]
    avoid: list[dict] = field(default_factory=list)
    analyst: str = "rules"
    rejected: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "GamePlan":
        raw = json.loads(text)
        picks = [Pick(**p) for p in raw.get("picks", [])]
        return cls(
            day=raw["day"],
            market_summary=raw.get("market_summary", ""),
            picks=picks,
            avoid=raw.get("avoid", []),
            analyst=raw.get("analyst", "rules"),
            rejected=raw.get("rejected", []),
        )

    def summary(self) -> str:
        lines = [f"game plan for {self.day} ({self.analyst})", self.market_summary.strip(), ""]
        if not self.picks:
            lines.append("empty watchlist: nothing to trade today")
        for p in self.picks:
            lines.append(f"{p.direction.upper():5} {p.symbol:6} conf {p.confidence:.2f}")
            lines.append(f"      catalyst: {p.catalyst}")
            lines.append(f"      thesis:   {p.thesis}")
        for r in self.rejected:
            lines.append(f"skip  {r.get('symbol', '?'):6} {r.get('reason', '')}")
        return "\n".join(lines)


def validate_picks(cfg: Config, picks: list[Pick], candidates: dict[str, Candidate]) -> tuple[list[Pick], list[dict]]:
    """Keep only rule-abiding watchlist entries, best confidence first, up to max_watchlist."""
    dt = cfg.daytrade
    ok: list[Pick] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    for p in sorted(picks, key=lambda x: -x.confidence):
        sym = p.symbol.upper()
        if sym in seen:
            continue
        seen.add(sym)
        if sym not in candidates:
            rejected.append({"symbol": sym, "reason": "not in the screened candidate list"})
            continue
        if sym in dt.blocklist:
            rejected.append({"symbol": sym, "reason": "blocklisted"})
            continue
        if p.direction not in ("long", "short"):
            rejected.append({"symbol": sym, "reason": f"bad direction {p.direction!r}"})
            continue
        if p.direction == "short" and not dt.allow_short:
            rejected.append({"symbol": sym, "reason": "shorting is disabled"})
            continue
        if not 0 <= p.confidence <= 1:
            rejected.append({"symbol": sym, "reason": "confidence must be between 0 and 1"})
            continue
        ok.append(Pick(sym, p.direction, p.catalyst, p.thesis, p.confidence))
        if len(ok) >= dt.max_watchlist:
            break
    return ok, rejected


@dataclass(frozen=True)
class Sized:
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    risk_dollars: float


def size_position(cfg: Config, symbol: str, side: str, entry: float, stop: float, target: float, equity: float, cash: float) -> Sized | None:
    """Shares so that a stop-out loses at most risk_per_trade_pct of equity, within the position and cash caps."""
    dt = cfg.daytrade
    stop_dist = abs(entry - stop)
    if entry <= 0 or equity <= 0 or stop_dist <= 0:
        return None
    risk_budget = equity * dt.risk_per_trade_pct / 100.0
    by_risk = risk_budget / stop_dist
    by_weight = equity * dt.max_position_pct / 100.0 / entry
    by_cash = cash / entry
    qty = int(math.floor(min(by_risk, by_weight, by_cash)))
    if qty < 1:
        return None
    return Sized(symbol, side, qty, round(entry, 2), round(stop, 2), round(target, 2), round(qty * stop_dist, 2))


def plan_path(cfg: Config, day: date | str) -> Path:
    return Path(cfg.daytrade.journal_dir) / "plans" / f"{day}.json"


def save_plan(cfg: Config, plan: GamePlan) -> Path:
    path = plan_path(cfg, plan.day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.to_json(), encoding="utf-8")
    return path


def load_plan(cfg: Config, day: date | str) -> GamePlan | None:
    path = plan_path(cfg, day)
    if not path.exists():
        return None
    return GamePlan.from_json(path.read_text(encoding="utf-8"))
