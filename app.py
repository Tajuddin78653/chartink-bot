# app.py — Main Flask Server + Background Threads
from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading

from config       import PORT, SCAN_MINS, MARKET_OPEN, MARKET_CLOSE, TRADE_CUTOFF, FORCE_EXIT, MAX_TRADES, PAPER_TRADING
from scanner      import get_new_stocks
from paper_trader import open_trades, closed_today, open_trade, get_daily_summary
from trailing_sl  import check_all_trades, get_live_price
from daily_report import force_exit_all, send_eod_report
from telegram_bot import send

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ══════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════

def now_time():
    return datetime.now(IST).time()

def is_between(h1, m1, h2, m2):
    return dtime(h1, m1) <= now_time() <= dtime(h2, m2)

def is_market_hours():
    return is_between(*MARKET_OPEN, *MARKET_CLOSE)

def can_enter_trade():
    return is_between(*MARKET_OPEN, *TRADE_CUTOFF)

def is_force_exit_time():
    t = now_time()
    return t >= dtime(*FORCE_EXIT)


# ══════════════════════════════════════════════════
#  BACKGROUND — SCANNER THREAD
# ══════════════════════════════════════════════════

def run_scanner():
    """Scan Chartink every SCAN_MINS minutes."""
    print(f"🔍 Scanner started — every {SCAN_MINS} mins")
    eod_sent = False

    while True:
        t = now_time()

        # ── Force Exit at 3:15 PM ─────────────────
        if t >= dtime(*FORCE_EXIT) and open_trades:
            force_exit_all()

        # ── EOD Report at 3:30 PM ─────────────────
        if t >= dtime(*MARKET_CLOSE) and not eod_sent:
            send_eod_report()
            eod_sent = True

        # ── Reset EOD flag next morning ───────────
        if t < dtime(9, 0):
            eod_sent = False

        # ── Scan for new stocks ───────────────────
        if can_enter_trade():
            new_stocks = get_new_stocks()

            for symbol in new_stocks:
                trades_today = len(closed_today) + len(open_trades)
                if trades_today >= MAX_TRADES:
                    print(f"🚫 Max trades ({MAX_TRADES}) reached")
                    send(f"🚫 <b>Max {MAX_TRADES} trades reached for today!</b>")
                    break

                price = get_live_price(symbol)
                if price:
                    open_trade(symbol, price)

        elif is_market_hours():
            print(f"⏰ Past entry cutoff — monitoring only")

        time.sleep(SCAN_MINS * 60)


# ══════════════════════════════════════════════════
#  BACKGROUND — TRAILING SL MONITOR
# ══════════════════════════════════════════════════

def run_trail_monitor():
    """Check trailing SL every 1 minute during market hours."""
    print("📈 Trailing SL monitor started — every 1 min")
    while True:
        if is_market_hours() and open_trades:
            check_all_trades()
        time.sleep(60)


# ══════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"       : "🟢 Running",
        "mode"         : "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading",
        "market_open"  : is_market_hours(),
        "open_trades"  : list(open_trades.keys()),
        "trades_today" : len(closed_today) + len(open_trades),
        "max_trades"   : MAX_TRADES
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send(
        "✅ <b>Bot is Working!</b>\n"
        f"🧪 Mode: {'Paper Trading' if PAPER_TRADING else 'Live Trading'}\n"
        f"💰 Capital: ₹{20000}\n"
        f"⚠️ Risk/Trade: 1% = ₹200\n"
        f"🔴 SL: 2% fixed\n"
        f"🟢 Target: Trailing 2%\n"
        f"📊 Max Trades: {MAX_TRADES}/day\n"
        "📡 Scanning: tazbul screener"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/status", methods=["GET"])
def status():
    summary = get_daily_summary()
    return jsonify({
        "open_trades" : list(open_trades.keys()),
        "summary"     : summary
    }), 200


@app.route("/report", methods=["GET"])
def report():
    """Manually trigger daily report."""
    send_eod_report()
    return jsonify({"status": "report sent"}), 200


# ══════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    mode = "🧪 PAPER TRADING" if PAPER_TRADING else "⚡ LIVE TRADING"

    # Start background threads
    threading.Thread(target=run_scanner,      daemon=True).start()
    threading.Thread(target=run_trail_monitor, daemon=True).start()

    send(
        f"🟢 <b>Chartink Bot LIVE — {mode}</b>\n"
        f"💰 Capital  : ₹20,000\n"
        f"⚠️ Risk     : 1% = ₹200/trade\n"
        f"🔴 SL       : 2% fixed\n"
        f"🟢 Target   : 2% trailing\n"
        f"📊 Max      : {MAX_TRADES} trades/day\n"
        f"🔍 Scanner  : every {SCAN_MINS} mins\n"
        f"⏰ Hours    : 9:15 AM – 3:30 PM IST"
    )

    app.run(host="0.0.0.0", port=PORT)
