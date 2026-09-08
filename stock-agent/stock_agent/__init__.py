"""Rules-based stock investment agent built on the Alpaca brokerage API.

The agent keeps a portfolio of income-oriented ETFs at a target allocation,
reinvests idle cash and dividends, applies a trend-following risk-off filter,
and enforces hard risk limits before any order reaches the broker.
"""

__version__ = "0.1.0"
