# Stock Investment Agent

A small, transparent, rules-based agent that runs an income-oriented ETF
portfolio through the [Alpaca](https://alpaca.markets) brokerage API.

It is designed for the "set it up once, let it run" use case:

- holds a fixed target allocation of diversified dividend / index / bond ETFs,
- reinvests every dollar of idle cash and every dividend automatically,
- shifts toward bonds when the market trend turns down (200-day moving average),
- rebalances only when a holding drifts well past its target (low turnover),
- refuses to do anything outside a set of hard risk limits,
- reports what it did (console, CSV trade log, optional Slack/Discord webhook),
- can be backtested on real daily prices before you trust it with money.

> **Read this first.** No software can guarantee passive income. Stocks and
> ETFs go down as well as up, dividends get cut, and a strategy that looked
> good in a backtest can lose money going forward. This agent is a tool for
> executing *your* plan consistently, not a source of returns. Run it on a
> paper account for a while, read the code, and only then decide whether to
> point it at real money. Nothing here is financial advice.

## How it decides

Every run does the same five steps:

1. **Observe.** Pull account, positions, open orders, market clock, the last
   year of portfolio equity, and the benchmark's recent daily closes.
2. **Regime.** If the benchmark (SPY by default) closes below its 200-day
   simple moving average the agent is "risk-off": equity targets are scaled
   down (50% by default) and the freed weight moves into the defensive asset
   (BND). Above the average, the normal targets apply.
3. **Plan.** Compare each holding's value with its target. Any holding more
   than `drift_threshold_pct` *above* target is sold down to target. All cash
   above the cash buffer (plus sale proceeds) is spent on the most underweight
   holdings first, then spread by target weight. Dividends land as cash, so
   this step is also the dividend reinvestment.
4. **Risk limits.** Every planned trade passes through hard caps: per-order
   value, total value per run, maximum position weight, only symbols in your
   allocation, no symbol with an open order, and a cash budget so buys can
   never exceed what the sells actually free up. If the account is more than
   `max_drawdown_pct` below its 1-year peak, buying pauses (sells still run
   so the risk-off shift can complete).
5. **Act and report.** Sells are submitted first and the agent waits for them
   to fill before submitting buys. Market orders in dollar amounts
   (fractional shares) are used so small accounts work fine. A summary is
   printed and, if configured, posted to your webhook.

The run is stateless: everything it needs comes from the broker, so it can
run from cron, a GitHub Actions schedule, or by hand.

## Setup

```bash
cd stock-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env         # then paste your Alpaca PAPER keys into .env
set -a; source .env; set +a  # export them into the shell
python -m stock_agent check  # validates config and credentials
```

Get API keys from the Alpaca dashboard. Start with the paper-trading keys;
the agent uses the paper endpoint unless you explicitly enable live trading
(see below).

## Usage

```bash
python -m stock_agent run              # dry run: prints what it would do
python -m stock_agent run --execute    # submit orders on the paper account
python -m stock_agent status           # holdings vs. targets
python -m stock_agent income --days 365   # dividend income report
python -m stock_agent backtest --start 2018-01-01 --curve equity.csv
```

`run` writes every submitted order to `logs/trades.csv`.

### Backtesting

`backtest` downloads split- and dividend-adjusted daily bars for your
allocation and benchmark, then simulates the strategy with the initial
deposit and monthly contribution from `config.yaml`. It reports final value,
annualized internal rate of return, and maximum drawdown next to a plain
buy-and-hold of the benchmark with identical contributions. Fills happen at
the daily close with no commissions or slippage, so treat the numbers as an
upper bound. Free Alpaca data starts around 2016.

### Running on a schedule

Two options are included:

- **GitHub Actions**: `.github/workflows/stock-agent.yml` (repo root) runs a
  dry run every weekday shortly after the US market opens. Add
  `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (and optionally
  `ALERT_WEBHOOK_URL`) as repository secrets, then flip the `execute` input
  when triggering it by hand, or change the scheduled step to
  `--execute` once you are happy with the dry runs.
- **cron** on any machine:
  `35 9 * * 1-5 cd /path/to/stock-agent && . .env && python -m stock_agent run --execute >> logs/agent.log 2>&1`
  (adjust for your time zone; the agent skips the run when the market is closed).

Running once a day is plenty for this strategy.

## Configuration

Everything lives in `config.yaml`. The shipped allocation is an example:

| Symbol | Weight | Role |
|--------|--------|------|
| SCHD | 30% | US dividend-growth stocks |
| VYM | 15% | US high-dividend-yield stocks |
| VTI | 25% | Total US market |
| JEPI | 12% | Covered-call income (monthly payouts) |
| BND | 15% | US bonds, also the risk-off destination |
| cash | 3% | Buffer |

Key knobs:

| Setting | Meaning |
|---------|---------|
| `trading.cash_buffer_pct` | Cash kept uninvested |
| `trading.drift_threshold_pct` | How far above target a holding must be before it is sold |
| `trading.allow_sells` | `false` turns the agent into a buy-only accumulator |
| `regime.*` | Benchmark, moving-average length, how much equity to cut in risk-off |
| `risk.max_order_value` / `max_daily_trade_value` | Hard dollar caps per order and per run |
| `risk.max_position_weight` | No buy may push a holding above this weight |
| `risk.max_drawdown_pct` | Pause buying after a loss this large from the 1-year peak |
| `backtest.*` | Initial deposit, monthly contribution, evaluation cadence |

The loader rejects configurations that cannot work, for example weights that
do not sum to 100%, or a risk-off shift that would put the defensive asset
above the position cap.

## Going live

Live trading needs two deliberate steps, so it cannot happen by accident:

1. `export STOCK_AGENT_LIVE_TRADING=I_UNDERSTAND_THE_RISKS`
2. pass `--live` on the command line: `python -m stock_agent --live run --execute`

Before that, run the paper account for at least a few weeks, check
`status` and `income` regularly, and read through `stock_agent/risk.py` so
you know exactly what the caps are. Keep `max_order_value` and
`max_daily_trade_value` small at first. Money moving *into* the brokerage
account (your monthly contribution) is still up to you: the agent invests
whatever cash it finds.

## Tests

```bash
python -m pytest
```

The suite runs entirely against an in-memory broker simulator; no network
or API keys are needed.

## Layout

```
stock_agent/
  config.py     YAML config + credentials, validation
  models.py     dataclasses (Account, Position, Trade, RunReport, ...)
  broker.py     AlpacaBroker (REST) and SimBroker (in-memory)
  strategy.py   regime filter, target weights, trade planning
  risk.py       preflight checks, drawdown, hard order limits
  agent.py      one run: observe -> plan -> limit -> execute -> report
  backtest.py   historical simulation with contributions
  reporting.py  status and dividend income reports
  cli.py        command line entry point
tests/          pytest suite (no network)
config.yaml     strategy and risk settings
```
