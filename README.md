# Hyperliquid-sizer

[![CI](https://github.com/wansiedler/hyperliquid-sizer/actions/workflows/ci.yml/badge.svg)](https://github.com/wansiedler/hyperliquid-sizer/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wansiedler/hyperliquid-sizer/actions/workflows/codeql.yml/badge.svg)](https://github.com/wansiedler/hyperliquid-sizer/actions/workflows/codeql.yml)
[![coverage: 100%](https://img.shields.io/badge/coverage-100%25_branches-brightgreen)](#tests)
[![python](https://img.shields.io/badge/python-3.12_%7C_3.13_%7C_3.14-blue)](https://github.com/wansiedler/hyperliquid-sizer/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy: checked](https://img.shields.io/badge/mypy-checked-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A risk-manager watchdog bot for the owner's Hyperliquid account** — the
[bybit-tv-sizer](https://github.com/wansiedler/bybit-tv-sizer) ported to the
Hyperliquid DEX. Same rules, same Telegram voice, same journal.

The watchdog polls the account and enforces:

1. A position with no stop-loss trigger → reduce-only market close.
2. A position leveraged above `MAX_LEVERAGE` → market close, leverage reset
   to 1x.
3. An entry order with no stop attached anywhere (no trigger child, no
   account stop, no position stop) → cancelled.
4. **The sizer**: every resting entry order is resized so that stopping out
   costs exactly `RISK_PCT` of the account — the maker entry fee and the
   stop's taker close budgeted in:
   `qty = equity·RISK_PCT / (|entry−stop| + entry·MAKER_FEE + stop·TAKER_FEE)`.
5. **The trimmer**: a market entry that filled oversized for its stop is cut
   back down with a reduce-only market order, every taker fee on the way
   (the entry, the cut itself, the stop) inside the same budget.

Every entry and exit lands in Telegram with a chart (entry, TP, SL and
breakeven lines, TradingView-style zones), a fee-and-funding-aware PnL
report, and a row in the Google Sheets trade journal. `/positions`,
`/statistics`, `/close`, `/stopall`, `/lev1` answer in the chat.

## TradingView: alerts yes, orders no

TradingView charts Hyperliquid ([data and charting only](https://www.tradingview.com/blog/en/trade-xyz-and-hyperliquid-59210/)
— it is not an order-routing broker there), so:

- **Entries** are drawn on Hyperliquid's own chart — their UI is a
  TradingView chart with order placement, TP/SL included. The sizer picks
  the order up within a couple of seconds and rewrites its quantity.
- **Alerts** drawn on any TradingView chart still arrive: point the alert's
  webhook at this bot (`TV_WEBHOOK_SECRET`/`TV_PORT`, a tunnel in front)
  and every crossing lands in the chat, spoken on the Nest if configured.

## Auth: an agent wallet, never the main key

The bot watches by address only (`HL_ACCOUNT`). Trading needs `HL_SECRET` —
an **agent wallet** generated on Hyperliquid under Settings → API: it signs
orders on behalf of the account but **can never withdraw**. `DRY_RUN=1`
(the default) logs every order the bot would send instead of sending it.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp bipboop.example bipboop   # fill in: bot token, chat id, HL_ACCOUNT
.venv/bin/python relay.py --check
.venv/bin/python relay.py
```

Or in Docker:

```bash
docker compose up -d --build
```

## Tests

```bash
.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report
```

100% branch coverage is the CI floor — anything less fails the build.

## Honest limitations

- Hyperliquid's fills API serves the most recent ~2000 fills: a position
  older than that window gets a taker-fee estimate instead of its real
  entry fees, and `/statistics` reaches only as far as the window does.
- The close notice labels стоп/тейк/руками by which level the exit landed
  nearest (0.2% tolerance) — the fills carry no order provenance.
- Funding accrual starts from the position's first fill inside the window.
