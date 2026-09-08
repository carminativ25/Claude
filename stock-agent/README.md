# Stock Agent: opening range breakouts on stocks in play

An automated intraday trading desk in a small Python package, built to be
**measured before it is trusted**. The rules that trade are the rules that
backtest, and it runs on an Alpaca paper account by default.

## How it trades

1. **Pre-market (`plan`).** Pull overnight headlines and the gainers, losers
   and most-active screeners. Filter to liquid, tradeable names. Send the
   headlines and statistics to Claude, which returns a ranked **watchlist**
   with a catalyst and thesis for each name and a list to avoid. A
   deterministic rules analyst (largest gaps with a headline) is used when
   no Anthropic key is set or the model call fails.
2. **The open (`trade --loop`).** The agent does *not* buy at the bell. It
   waits for the first 15 minutes to form an opening range, then polls every
   minute. A watchlist name is entered only if:
   - price closes above the range high (below the low for shorts),
   - volume so far is at least 2x what an average day would have traded by
     that time,
   - price is on the right side of VWAP,
   - the range (which becomes the stop distance) is neither noise nor too
     wide, and the entry window (first 90 minutes) is still open.

   Entries are bracket orders: market in, stop at the range boundary, target
   at 2x the risk. Share count is set so a stop-out loses a fixed fraction of
   equity. Most watchlist names never break out, and that is the point.
3. **Intraday.** If the account is down more than the daily loss limit,
   everything is flattened and trading halts for the day. In the last ten
   minutes it flattens whatever is left. Nothing is held overnight.
4. **After the close (`close`, `review`).** Realized P&L per symbol is
   journaled from the broker's fills; `review` reports win rate, expectancy
   and profit factor over all days.

> **Read this first.** Day trading is negative-sum after costs for most
> participants. Breakout strategies have thin edges that slippage can erase.
> This agent exists so you can find out whether these rules make money on
> your account with paper money, and it will tell you honestly when they
> don't. Nothing here is financial advice.

## Setup

```bash
cd stock-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # Alpaca PAPER keys, and an Anthropic key for the analyst
set -a; source .env; set +a
python -m stock_agent check
```

## Step 1: backtest the rules

```bash
python -m stock_agent backtest --start 2026-03-01 --trades trades.csv --verbose-days
```

This downloads daily bars for the universe (a built-in list of ~80 liquid
names, or `daytrade.universe`), builds each day's watchlist mechanically
(largest opening gaps that pass the liquidity screen), fetches that day's
minute bars for those names, and replays the session through the exact
`evaluate_breakout()` used live. Fills happen at the next bar's open plus
slippage; within a bar the stop is checked before the target, so a bar that
touches both counts as a loss. Minute bars are cached under `cache/` so you
can re-run with different settings quickly.

```
period            2026-03-02 -> 2026-08-29  (126 trading days, 604 symbol-days watched)
trades            188   (stop 97, target 61, eod 27, loss-limit 3)
win rate          38%
total P&L         $1,412.50  (+14.13% on $10,000)
per trade         $7.51   avg R +0.14
profit factor     1.19
max drawdown      6.80%
loss-limit days   3
```

What to look for: a profit factor comfortably above 1 and a positive
average R after slippage, across at least a few months. Then change one
setting at a time (`range_minutes`, `min_relative_volume`, `reward_risk`)
and see whether the result is stable or whether you are just fitting noise.
The backtest does not replay the news feed, so it measures the mechanical
core; live, the analyst narrows the watchlist further.

**Data caveat.** The free IEX feed reports only IEX's share of volume
(a few percent of the market). Relative volume and dollar-volume filters are
computed consistently from that feed, so the ratios still work, but they are
noisier than with the consolidated tape. A paid Alpaca data plan with
`daytrade.data_feed: sip` fixes this.

## Step 2: paper trade it

| When (ET) | Command | What it does |
|-----------|---------|--------------|
| ~8:45 | `python -m stock_agent plan` | News + screeners -> Claude -> `journal/plans/YYYY-MM-DD.json` |
| 9:30 | `python -m stock_agent trade --loop --execute` | Poll for breakouts until 11:00, then enforce the loss limit and flatten before the close |
| 15:52 | `python -m stock_agent close --execute` | Belt-and-braces flatten and journal the day |
| any time | `python -m stock_agent review --trades` | Performance from the journal |

`trade` without `--loop` does a single pass (useful from cron every minute);
`monitor` is the loss-limit and end-of-day check on its own. Without
`--execute` everything is a dry run that prints what it would do. Run a
month on paper and compare `review` with the backtest. If they disagree
badly, slippage or the news filter is the difference, and both are worth
knowing before real money.

### cron

```
45 8  * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent plan >> logs/agent.log 2>&1
30 9  * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent trade --loop --execute >> logs/agent.log 2>&1
52 15 * * 1-5  cd /path/to/stock-agent && . .env && python -m stock_agent close --execute >> logs/agent.log 2>&1
```

### GitHub Actions

`.github/workflows/stock-agent.yml` (repo root) runs `plan` pre-market, a
`trade --loop` session job at the open, and `close` before the bell, using
the repository secrets `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ANTHROPIC_API_KEY` and optionally `ALERT_WEBHOOK_URL`. Everything is a dry
run until you set `EXECUTE` to `true` in the workflow's `env` block (or pass
the `execute` input on a manual trigger). Scheduled Actions can start a few
minutes late and jobs are capped at six hours, so the session job may end
before the close; the separate `close` job and the end-of-day flatten in the
loop cover that. It is fine for a paper test and not the right tool for
real money.

## Risk rules

| Rule | Default | Setting |
|------|---------|---------|
| Positions per day | 3 | `max_picks` |
| Loss per trade if stopped | 0.5% of equity | `risk_per_trade_pct` |
| Max position size | 20% of equity | `max_position_pct` |
| Daily loss limit | 2% (flatten + halt) | `max_daily_loss_pct` |
| Stop distance | range height, 0.75% to 4% | `min_stop_pct` / `max_stop_pct` |
| Target | 2x risk | `reward_risk` |
| Relative volume to enter | 2x | `min_relative_volume` |
| Entry window | first 90 minutes | `entry_window_minutes` |
| Liquidity screen | $20M/day, spread < 0.3% | `min_avg_dollar_volume` / `max_spread_pct` |
| Shorting | off | `allow_short` |
| Pattern day trader rule | on | `respect_pdt_rule` |
| Overnight exposure | none | flatten before close |

All of these are enforced in code. The analyst only ranks the watchlist; it
cannot trigger an entry, set a stop, or size a position.

**Pattern day trader rule.** Margin accounts under $25,000 may make at most
three day trades in five business days. The agent reads the broker's
counter and caps entries accordingly. Alpaca paper accounts start at $100k,
so paper results will not show this constraint unless you reset the paper
balance to what you would actually fund.

## The Claude analyst

The morning call uses the Anthropic SDK with structured JSON output
(schema in `stock_agent/daytrade/analyst.py`), adaptive thinking, a
configurable effort level, and Anthropic's server-side refusal fallback.
Model defaults to `claude-opus-5`; one plan costs a few cents. The system
prompt explains the breakout mechanics so the model ranks names by "will
this move continue" and flags reasons a gap is likely to fade (offerings,
lockups, stale news).

## Going live

1. `export STOCK_AGENT_LIVE_TRADING=I_UNDERSTAND_THE_RISKS`
2. add `--live` to every command

Neither alone is enough. Before that: a backtest with a profit factor above
1 after slippage, a month of paper trading that agrees with it, and small
`risk_per_trade_pct` and `max_daily_loss_pct` settings.

## Long-term ETF portfolio mode

The original dividend/index strategy is still included under
`python -m stock_agent portfolio run|status|income|backtest`. It uses the
`targets`, `trading`, `regime`, `risk` and `backtest` sections of
`config.yaml` and has far stronger evidence behind it than any intraday
approach. Do not run both modes on the same account at once.

## Tests

```bash
python -m pytest
```

Runs against an in-memory broker simulator, synthetic minute-bar sessions
and a fake Claude client. No network, keys or money involved.

## Layout

```
stock_agent/
  daytrade/
    orb.py        opening range, VWAP, relative volume, evaluate_breakout()  (shared live/backtest)
    scan.py       news + screeners -> screened candidates
    analyst.py    ClaudeAnalyst (structured JSON watchlist) and RulesAnalyst fallback
    plan.py       GamePlan/Pick, watchlist validation, risk-based sizing
    session.py    plan / trade / trade --loop / monitor / close
    backtest.py   minute-bar replay of the breakout rules
    journal.py    per-day journal and review statistics
  broker.py       AlpacaBroker (REST) and SimBroker (in-memory)
  config.py       config.yaml + credentials, validation
  models.py       dataclasses
  agent.py, strategy.py, risk.py, backtest.py, reporting.py   ETF portfolio mode
  cli.py          command line entry point
tests/            pytest suite
config.yaml       all settings
```
