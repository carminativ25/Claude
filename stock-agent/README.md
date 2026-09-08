# Stock Agent: news-driven day trading on Alpaca

An automated intraday trading desk in a small Python package. Every trading
day it:

1. **Reads the overnight news** and the pre-market screeners (top gainers,
   losers, most active) through the Alpaca data API.
2. **Screens for tradeable names**: price range, 20-day dollar volume,
   bid/ask spread, listed on a real exchange, not on your blocklist.
3. **Writes a game plan** by sending the headlines and candidate statistics
   to Claude, which returns a structured JSON plan: which stocks, why, where
   the stop goes, where the target is, and what to avoid. A deterministic
   rules-based analyst is used when no Anthropic key is configured or the
   model call fails.
4. **Opens positions shortly after the bell** with bracket orders: market
   entry plus an attached stop-loss and take-profit. Share count is derived
   from the stop distance so a stop-out costs a fixed fraction of equity.
5. **Monitors intraday**: if the account is down more than the daily loss
   limit, everything is flattened and trading stops for the day.
6. **Flattens before the close**, records realized P&L per symbol in a
   journal, and lets you review win rate, expectancy and profit factor over
   time.

> **Read this first.** Day trading is a negative-sum game after costs for
> most participants, and news-driven trades at the open are crowded and
> slippage-prone. This agent exists so you can *measure* whether the
> approach makes money on a paper account before risking real capital. It
> defaults to Alpaca paper trading, refuses live trading without two
> explicit opt-ins, and never trades without a stop. None of this is
> financial advice. Expect losing days and losing weeks.

## Setup

```bash
cd stock-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # paste your Alpaca PAPER keys and an Anthropic key
set -a; source .env; set +a
python -m stock_agent check
```

`check` validates the config, confirms broker access, reports whether the
account is under the $25k pattern-day-trader threshold, and says whether
the Claude analyst is configured.

## The daily cycle

| When (ET) | Command | What it does |
|-----------|---------|--------------|
| ~8:45 | `python -m stock_agent plan` | Scan news + screeners, ask Claude, save `journal/plans/YYYY-MM-DD.json` |
| 9:35 | `python -m stock_agent open --execute` | Size each pick and submit bracket orders |
| every 10 min | `python -m stock_agent monitor --execute` | Enforce the daily loss limit; flatten in the last 10 minutes |
| 15:52 | `python -m stock_agent close --execute` | Belt-and-braces flatten and journal the day's P&L |
| any time | `python -m stock_agent review --trades` | Performance statistics from the journal |

Without `--execute` every command is a dry run that prints what it would
do. Run the cycle that way for a few days before switching it on, then run
it on paper for at least a month and look at `review` before considering
real money.

### Example plan output

```
game plan for 2026-09-08 (claude:claude-opus-5)
Earnings-heavy morning; ACME raised full-year guidance and is gapping 8% on 3x volume...

LONG  ACME   conf 0.72  stop -2.50%  target +5.00%  (R:R 2.0)
      catalyst: Q3 beat, FY guidance raised 12%
      thesis:   Guidance raise on a large-cap with room above the prior high; invalidated if it loses the opening range.
skip  MEME   thin volume and no company-level news
plan saved to journal/plans/2026-09-08.json
```

## Risk rules

All of these are enforced in code, not left to the analyst:

| Rule | Default | Where |
|------|---------|-------|
| Max positions per day | 3 | `daytrade.max_picks` |
| Loss per trade if the stop hits | 0.5% of equity | `daytrade.risk_per_trade_pct` (sets share count) |
| Max size of one position | 20% of equity | `daytrade.max_position_pct` |
| Daily loss limit | 2% of previous-close equity | `daytrade.max_daily_loss_pct` (flattens and halts) |
| Stop distance bounds | 0.75% to 4% | `daytrade.min_stop_pct` / `max_stop_pct` |
| Minimum reward-to-risk | 1.5x | `daytrade.min_reward_risk` |
| Liquidity | $20M average daily dollar volume, spread under 0.3% | `daytrade.min_avg_dollar_volume` / `max_spread_pct` |
| Shorting | off | `daytrade.allow_short` |
| Pattern day trader rule | on | `daytrade.respect_pdt_rule` |
| Overnight exposure | none | `monitor` / `close` flatten before the bell |

The analyst can only choose from the screened candidates, cannot set
position size, and its stops and targets are clamped into the configured
bounds. If Claude proposes a symbol that failed the screen, it is dropped
and the reason is recorded in the plan.

**Pattern day trader rule.** US margin accounts under $25,000 may make at
most three day trades in five business days. The agent reads the broker's
day-trade counter and caps new entries accordingly. With a small account
this means one or two trades a week, not three a day. Alpaca paper accounts
start at $100k, so paper results will not reflect this constraint unless
you reset the paper balance.

## The Claude analyst

The morning call uses the Anthropic SDK with structured JSON output, so the
plan always matches the schema in `stock_agent/daytrade/analyst.py`.
Adaptive thinking is on, effort is configurable (`daytrade.analyst_effort`),
and the request opts into Anthropic's server-side refusal fallback so a
declined request is re-run on a fallback model automatically. Set
`ANTHROPIC_API_KEY` to enable it; the model defaults to `claude-opus-5` and
one plan costs a few cents.

The system prompt tells the model how the desk works (bracket orders, fixed
risk, flat by close), asks for company-level catalysts, and makes "no
trades today" an explicitly acceptable answer. You can read and edit it in
the same file.

## Journal and review

`journal/YYYY-MM-DD.json` records the plan, each entry with its stop and
target, any halt, and the close with realized P&L per symbol (computed
from the broker's fill records, so it matches the account). `review`
aggregates every day:

```
trading days      22   (halted by loss limit: 1)
trades            41
win rate          46%
total P&L         $612.40
per trade         $14.94
best / worst      $310.00 / -$248.50
profit factor     1.31
```

A profit factor under 1.0 or a negative per-trade expectancy after a few
weeks of paper trading is the answer to "does this make money".

## Running it on a schedule

**cron on any always-on machine** (recommended for the intraday steps;
times shown for US Eastern):

```
45 8  * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent plan >> logs/agent.log 2>&1
35 9  * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent open --execute >> logs/agent.log 2>&1
*/10 9-15 * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent monitor --execute >> logs/agent.log 2>&1
52 15 * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent close --execute >> logs/agent.log 2>&1
```

**GitHub Actions**: `.github/workflows/stock-agent.yml` (repo root) runs
`plan`, `open`, `monitor` and `close` on a weekday schedule using the
repository secrets `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ANTHROPIC_API_KEY` and optionally `ALERT_WEBHOOK_URL`. Scheduled Actions
can start several minutes late, which matters for the open and the close,
so treat it as a way to try the cycle, not as production plumbing. The
workflow runs dry unless the `execute` input is set on a manual trigger or
you change the schedule steps. Note that the journal is not persisted
between Actions runs; it is uploaded as an artifact instead.

## Going live

1. `export STOCK_AGENT_LIVE_TRADING=I_UNDERSTAND_THE_RISKS`
2. add `--live` to every command: `python -m stock_agent --live open --execute`

Neither alone is enough. Before that, run at least a month on paper, keep
`risk_per_trade_pct` and `max_daily_loss_pct` small, and know that fills
at the open will be worse than the paper account suggests.

## Long-term ETF portfolio mode

The original dividend/index strategy is still included under
`python -m stock_agent portfolio run|status|income|backtest`. It uses the
`targets`, `trading`, `regime`, `risk` and `backtest` sections of
`config.yaml` and is a far lower-risk way to compound money than day
trading. Both modes share the same broker account, so do not run both on
the same account at once.

## Tests

```bash
python -m pytest
```

Everything runs against an in-memory broker simulator and a fake Claude
client. No network, keys or money involved.

## Layout

```
stock_agent/
  daytrade/
    scan.py       news + screeners -> screened candidates (ATR, dollar volume, spread)
    analyst.py    ClaudeAnalyst (structured JSON plan) and RulesAnalyst fallback
    plan.py       GamePlan/Pick, validation and clamping, risk-based sizing
    session.py    plan / open / monitor / close commands
    journal.py    per-day journal and the review statistics
  broker.py       AlpacaBroker (REST) and SimBroker (in-memory)
  config.py       config.yaml + credentials, validation
  models.py       dataclasses
  agent.py, strategy.py, risk.py, backtest.py, reporting.py   ETF portfolio mode
  cli.py          command line entry point
tests/            pytest suite
config.yaml       all settings
```
