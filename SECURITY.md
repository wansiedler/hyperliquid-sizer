# Security

This is a personal trading bot published as-is. Development happens on
`main`, so **only `main` is supported**. Fixes land there and nowhere else.

## Threat model, in one breath

- `bipboop` (gitignored) holds the Telegram bot token and the Hyperliquid
  **agent wallet** key. The agent wallet can trade but can never withdraw;
  the main wallet's key never touches this code. Revoke an agent in the
  Hyperliquid UI under Settings → API.
- The bot's username is public, so anyone can message it. Commands are
  answered only when `OWNER_ID` writes them in `TARGET_CHAT_ID`; any other
  chat, sender or bot is logged and dropped.
- The TradingView webhook secret is the only auth on that endpoint. The
  startup notice masks it; `/links` hands it out on request.
- `DRY_RUN=1` is the default: a fresh clone cannot place an order until the
  owner explicitly arms it.

## Reporting a vulnerability

Open a GitHub issue, or write to the repository owner directly if it is
sensitive. There is no bounty; there is gratitude.
