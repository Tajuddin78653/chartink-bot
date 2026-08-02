# config.py — Your Trading Configuration
import os

# ── Telegram ───────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "")

# ── Capital & Risk ─────────────────────────────────
CAPITAL          = float(os.environ.get("CAPITAL", 20000))
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT", 1.0))   # 1%
SL_PERCENT       = float(os.environ.get("SL_PERCENT", 2.0))     # 2% fixed SL
MAX_TRADES       = int(os.environ.get("MAX_TRADES", 2))          # Max 2/day
CAPITAL_PER_TRADE = CAPITAL * 0.50                               # 50% per trade

# ── Trailing SL ────────────────────────────────────
TRAIL_PERCENT    = float(os.environ.get("TRAIL_PERCENT", 2.0))  # Trail by 2%

# ── Screener ───────────────────────────────────────
SCREENER_URL     = "https://chartink.com/screener/tazbul"
SCAN_MINS        = int(os.environ.get("SCAN_MINS", 5))

# ── Trading Hours (IST) ────────────────────────────
MARKET_OPEN      = (9,  15)
MARKET_CLOSE     = (15, 30)
TRADE_CUTOFF     = (14, 30)   # No new trades after 2:30 PM
FORCE_EXIT       = (15, 15)   # Force exit all at 3:15 PM

# ── Mode ───────────────────────────────────────────
PAPER_TRADING    = os.environ.get("PAPER_TRADING", "true").lower() == "true"

# ── Flask ──────────────────────────────────────────
PORT             = int(os.environ.get("PORT", 5000))
