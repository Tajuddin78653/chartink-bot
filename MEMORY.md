# 🧠 Chartink Bot — Project Memory
> **READ THIS FIRST before touching any code in this repo.**
> Last updated: 2025

---

## 📁 Repo
- **GitHub:** `Tajuddin78653/chartink-bot` (private)
- **Branch:** `main`
- **Deployed on:** Render (free tier — restarts frequently, wipes in-memory state)
- **Main file:** `app.py` (~2100 lines, single Flask file — do NOT replace with a smaller version)

---

## 🏗️ Architecture

```
app.py (single file)
├── Config & constants         (lines ~1–95)
├── State persistence          (file-based JSON → logs/state.json)
├── Broker charges             (calc_charges — Dhan intraday)
├── ATR helpers                (fetch_candles, calc_atr, calc_atr_sl)
├── Price fetchers             (NSE → Yahoo fallback)
├── Strategy engines           (13/50, Gap D/U, Supertrend+ADX, Pro Engine)
├── Bot 1 — tazbul             (open_trade, close_trade, check_positions, send_eod)
├── Bot 2 — TazAmol-Test1      (open_trade2, close_trade2, check_positions2, send_eod2)
├── Monitor thread             (run_monitor — every 60s)
├── Routes                     (/alert, /alert2, /dashboard, /prices, /close, /scan, ...)
└── Dashboard HTML             (tbl_open, tbl_closed, tbl_hist, tbl_signals, tbl_pro_signals...)
```

---

## 🤖 Bots

| Bot | Screener | Webhook | Token Env | Chat Env |
|-----|----------|---------|-----------|----------|
| **tazbul** (Bot 1) | tazbul Chartink screener | `POST /alert` | `BOT_TOKEN` | `CHAT_ID` |
| **TazAmol-Test1** (Bot 2) | TazAmol Chartink screener | `POST /alert2` | `BOT2_TOKEN` | `BOT2_CHAT_ID` |

---

## 📊 Dashboard — 4 Main Tabs
**URL:** `https://<render-app>.onrender.com/dashboard`

| Tab | ID | Content |
|-----|----|---------|
| 🏃 **Trading Bots** | `main-trading` | tazbul + TazAmol sub-tabs, open positions, closed today, history |
| 📊 **Strategies** | `main-signals` | 13/50 EMA, Gap D/U, Supertrend+ADX signal tables |
| 📈 **NSE Market** | `main-nse` | Nifty, breadth (advances/declines), sectors, pre-open |
| 🎯 **Pro Engine** | `main-pro` | 7-filter confluence strategy signals |

**Trading Bots tab — 3 sub-tabs per bot:**
- Open Positions → `tbl_open()` — columns: Stock, Entry, Qty, ATR SL 📐, TP/Trail 📈, Capital, Live Price, Unreal P&L, Time, Action
- Today's Trades → `tbl_closed()` — columns: Stock, Entry, Exit, Qty, Gross P&L, Charges 🏦, Net P&L, Reason, Time
- History → `tbl_hist()` — same columns as closed + Date

---

## ⚙️ Current Trading Logic (Bot 1 — tazbul)

| Setting | Value |
|---------|-------|
| Capital/trade | ₹10,000 |
| Stop Loss | **ATR(21) × 3 trailing** (points mode, ratchets up never down) |
| Take Profit | **0.05% first trigger**, then trailing TP at 0.05% steps |
| Trailing TP exit | Price retraces one 0.05% step from the locked trail_tp |
| Force exit | 3:12 PM IST |
| EOD report | 3:30 PM IST |
| Broker charges | Dhan intraday: brokerage + STT + exchange + SEBI + GST 18% |

---

## 🔧 State Persistence (CRITICAL)
- **Problem:** Render free tier restarts wipe in-memory state → repeated signals
- **Fix:** `logs/state.json` written atomically on every trade open/close/eod
- **Functions:** `_save_state()`, `_load_state()`, `_clear_state_if_new_day()`
- **Auto day-reset:** If `saved_date != today`, state file is deleted at startup → fresh day
- **State buckets saved:** `open_trades`, `open_trades2`, `closed_today`, `closed_today2`, `traded_today`, `traded_today2`

---

## 📦 Key Functions to Know

| Function | Purpose |
|----------|---------|
| `calc_charges(entry, exit, qty)` | Dhan intraday round-trip charges |
| `fetch_candles(symbol)` | 1-min Yahoo OHLC candles |
| `calc_atr_sl(candles, current_atr_sl)` | ATR trailing SL, ratchets up only |
| `calculate_trade(symbol, price, sl_pct, tp_pct, capital)` | Creates trade dict with ATR SL + trail fields |
| `open_trade / close_trade` | Bot 1 entry/exit + saves state |
| `tbl_open(d, bot_num)` | HTML rows for open positions table |
| `tbl_closed(rows)` | HTML rows for closed trades (Gross/Charges/Net) |
| `tbl_hist(rows)` | HTML rows for history (Gross/Charges/Net) |
| `stats(rows)` | Uses `net_pnl` with fallback to `pnl` for old CSV rows |

---

## 📝 CSV Schema (`logs/trades.csv`)
```
date, symbol, entry, exit, qty, sl, tp, risk, reward,
gross_pnl, charges, net_pnl, result, reason, entry_time, exit_time
```
> Old rows (before charges feature) only have `pnl` — all display code uses `.get("net_pnl", .get("pnl", 0))` fallback.

---

## 🚨 Rules Before Every Code Change
1. **Run `python -m py_compile app.py`** — must pass before push
2. **Never replace app.py with a shorter version** — the full 2100-line file has all 4 tabs
3. **Check GitHub commit history** if unsure which version is live: `GET /repos/Tajuddin78653/chartink-bot/commits`
4. **Read the dashboard route** before any UI change — `tbl_*()` functions build rows, HTML headers must match column count
5. **`_save_state()` must be called** after every mutation of `open_trades`, `traded_today`, `closed_today`

---

## 🌐 Render Environment Variables Required
```
BOT_TOKEN          = <tazbul telegram bot token>
CHAT_ID            = <tazbul telegram chat id>
BOT2_TOKEN         = <TazAmol bot token>
BOT2_CHAT_ID       = <TazAmol chat id>
PAPER_TRADING      = true   (set false for live)
BROKER             = dhan
PORT               = 10000
REDIS_URL          = (optional — file-based state used if absent)
```
