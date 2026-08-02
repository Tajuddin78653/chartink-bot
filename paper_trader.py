# paper_trader.py — Paper Trading Engine
import csv, os
from datetime import datetime
import pytz

from config          import PAPER_TRADING, CAPITAL
from trade_calculator import calculate_trade
from telegram_bot    import send_entry, send_exit, send

IST      = pytz.timezone("Asia/Kolkata")
LOG_FILE = "logs/paper_trades.csv"
HEADERS  = ["date","symbol","entry","exit","qty","sl",
            "risk","pnl","result","entry_time","exit_time","reason"]

# ── In-memory trade store ──────────────────────────
open_trades  = {}   # symbol → trade dict
closed_today = []   # list of closed trades


def ensure_log():
    os.makedirs("logs", exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(HEADERS)


def open_trade(symbol, price):
    """Open a new paper trade."""
    if symbol in open_trades:
        print(f"⚠️ Already in trade: {symbol}")
        return None

    now   = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    trade = calculate_trade(symbol, price, now)

    open_trades[symbol] = trade
    send_entry(trade)
    print(f"📝 Paper trade opened: {symbol} @ ₹{price}")
    return trade


def close_trade(symbol, exit_price, reason="Manual"):
    """Close an open paper trade."""
    if symbol not in open_trades:
        return None

    trade            = open_trades.pop(symbol)
    now              = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    trade["exit_time"] = now
    trade["status"]  = "CLOSED"

    pnl = round((exit_price - trade["entry"]) * trade["qty"], 2)

    send_exit(trade, exit_price, reason)
    log_trade(trade, exit_price, pnl, reason)

    closed_today.append({**trade, "pnl": pnl, "exit_price": exit_price})
    print(f"🚪 Trade closed: {symbol} @ ₹{exit_price} | P&L: ₹{pnl}")
    return pnl


def log_trade(trade, exit_price, pnl, reason):
    """Save trade to CSV."""
    ensure_log()
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"],
            trade["entry"],
            exit_price,
            trade["qty"],
            trade["sl"],
            trade["risk_amt"],
            pnl,
            "WIN" if pnl >= 0 else "LOSS",
            trade["entry_time"],
            trade["exit_time"],
            reason
        ])


def get_open_trades():
    return open_trades


def get_daily_summary():
    """Build daily P&L summary."""
    winners = [t for t in closed_today if t["pnl"] >= 0]
    losers  = [t for t in closed_today if t["pnl"] <  0]
    return {
        "date"         : datetime.now(IST).strftime("%d %b %Y"),
        "total_trades" : len(closed_today),
        "winners"      : len(winners),
        "losers"       : len(losers),
        "total_profit" : round(sum(t["pnl"] for t in winners), 2),
        "total_loss"   : round(abs(sum(t["pnl"] for t in losers)), 2),
        "capital"      : CAPITAL,
        "paper"        : PAPER_TRADING
    }


def reset_daily():
    """Reset daily counters at market close."""
    global closed_today
    closed_today = []
    print("🔄 Daily counters reset")
