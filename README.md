# Chartink Bot

Python + Flask Telegram trading bot with dual screener support.

## Bots
- **tazbul** — Bot 1 screener
- **TazAmol-Test1** — Bot 2 screener

## Setup
All sensitive values must be set as environment variables on Render — never hardcoded:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram token for Bot 1 (tazbul) |
| `CHAT_ID` | Telegram chat ID for Bot 1 |
| `BOT2_TOKEN` | Telegram token for Bot 2 (TazAmol) |
| `BOT2_CHAT_ID` | Telegram chat ID for Bot 2 |
| `PAPER_TRADING` | `true` for paper mode, `false` for live |

## Dashboard
Visit `/dashboard` to view live trading status, signals, and Pro Engine results.
