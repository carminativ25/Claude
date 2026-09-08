"""Per-day trade journal and the performance review built from it."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import Config
from ..models import Fill


def journal_path(cfg: Config, day: date | str) -> Path:
    return Path(cfg.daytrade.journal_dir) / f"{day}.json"


def load_day(cfg: Config, day: date | str) -> dict:
    path = journal_path(cfg, day)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"day": str(day), "events": []}


def record(cfg: Config, day: date | str, kind: str, payload: dict) -> None:
    data = load_day(cfg, day)
    data["events"].append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **payload})
    path = journal_path(cfg, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def realized_pnl_by_symbol(fills: list[Fill]) -> dict[str, float]:
    """Net cash per symbol from the day's fills. Positions are flat by the close, so this is realized P&L."""
    pnl: dict[str, float] = defaultdict(float)
    for f in fills:
        signed = f.qty * f.price
        pnl[f.symbol] += signed if f.side.startswith("sell") else -signed
    return dict(pnl)


@dataclass
class ReviewStats:
    days: int
    trades: int
    wins: int
    total_pnl: float
    best: float
    worst: float
    gross_profit: float
    gross_loss: float
    halted_days: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        return self.total_pnl / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float | None:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else None

    def summary(self) -> str:
        pf = "n/a" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        return "\n".join(
            [
                f"trading days      {self.days}   (halted by loss limit: {self.halted_days})",
                f"trades            {self.trades}",
                f"win rate          {self.win_rate:.0%}",
                f"total P&L         ${self.total_pnl:,.2f}",
                f"per trade         ${self.expectancy:,.2f}",
                f"best / worst      ${self.best:,.2f} / ${self.worst:,.2f}",
                f"profit factor     {pf}",
            ]
        )


def review(cfg: Config) -> tuple[ReviewStats, list[str]]:
    root = Path(cfg.daytrade.journal_dir)
    rows: list[str] = []
    days = trades = wins = halted = 0
    total = best = worst = gross_p = gross_l = 0.0
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        close_events = [e for e in data.get("events", []) if e.get("kind") == "close"]
        if not close_events:
            continue
        days += 1
        if any(e.get("kind") == "halt" for e in data["events"]):
            halted += 1
        by_symbol = close_events[-1].get("pnl_by_symbol", {})
        day_total = 0.0
        for sym, pnl in by_symbol.items():
            trades += 1
            total += pnl
            day_total += pnl
            if pnl > 0:
                wins += 1
                gross_p += pnl
            else:
                gross_l += -pnl
            best, worst = max(best, pnl), min(worst, pnl)
            rows.append(f"{data['day']}  {sym:6} {pnl:>+10,.2f}")
        rows.append(f"{data['day']}  {'TOTAL':6} {day_total:>+10,.2f}")
    return ReviewStats(days, trades, wins, total, best, worst, gross_p, gross_l, halted), rows
