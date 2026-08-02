# trailing_sl.py — Trailing SL Monitor
import requests
from config       import TRAIL_PERCENT
from paper_trader import open_trades, close_trade
from telegram_bot import send_trail_update, send


def get_live_price(symbol):
    """Fetch live price from NSE India (free, no API key needed)."""
    try:
        url     = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
        headers = {"User-Agent": "Mozilla/5.0"}
        r       = requests.get(url, headers=headers, timeout=10)
        data    = r.json()
        price   = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(price), 2)
    except Exception as e:
        print(f"❌ Price fetch error for {symbol}: {e}")
        return None


def check_all_trades():
    """Check all open trades — update trailing SL or exit."""
    if not open_trades:
        return

    for symbol in list(open_trades.keys()):
        trade = open_trades[symbol]
        price = get_live_price(symbol)

        if not price:
            continue

        print(f"📈 {symbol}: ₹{price} | SL: ₹{trade['current_sl']}")

        # ── Check Stop Loss Hit ────────────────────
        if price <= trade["current_sl"]:
            print(f"🔴 SL hit for {symbol} @ ₹{price}")
            close_trade(symbol, price, "Stop Loss Hit 🔴")
            continue

        # ── Update Trailing SL ────────────────────
        if price > trade["highest_price"]:
            trade["highest_price"] = price

            # Calculate new trailing SL
            new_sl = round(price * (1 - TRAIL_PERCENT / 100), 2)

            if new_sl > trade["current_sl"]:
                old_sl  = trade["current_sl"]
                trade["current_sl"] = new_sl

                # Calculate locked profit
                locked  = round((new_sl - trade["entry"]) * trade["qty"], 2)

                print(f"🔄 Trail SL updated: {symbol} ₹{old_sl} → ₹{new_sl}")
                send_trail_update(trade, new_sl, price, locked)
