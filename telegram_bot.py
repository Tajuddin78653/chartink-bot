# telegram_bot.py — All Telegram message functions
import requests
from config import BOT_TOKEN, CHAT_ID

def send(msg):
    """Send message to Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        if r.status_code == 200:
            print("✅ Telegram sent!")
        else:
            print(f"❌ Telegram error: {r.text}")
    except Exception as e:
        print(f"❌ Exception: {e}")


def send_entry(trade):
    """Send paper trade entry alert."""
    mode = "🧪 PAPER TRADE" if trade["paper"] else "⚡ LIVE TRADE"
    send(
        f"📝 <b>{mode} ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{trade['symbol']}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"📦 Quantity : {trade['qty']} shares\n"
        f"🔴 Stop Loss: ₹{trade['sl']} ({trade['sl_pct']}%)\n"
        f"🟢 Trailing : {trade['trail_pct']}% trail\n"
        f"💵 Capital  : ₹{trade['capital_used']}\n"
        f"⚠️ Risk     : ₹{trade['risk_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['entry_time']}"
    )


def send_trail_update(trade, new_sl, current_price, locked_profit):
    """Send trailing SL update alert."""
    send(
        f"🔄 <b>TRAILING SL UPDATE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock      : <b>{trade['symbol']}</b>\n"
        f"📈 Current    : ₹{current_price}\n"
        f"🔴 New SL     : ₹{new_sl}\n"
        f"✅ Locked P&L : ₹{locked_profit}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def send_exit(trade, exit_price, reason):
    """Send trade exit alert."""
    pnl      = round((exit_price - trade["entry"]) * trade["qty"], 2)
    pnl_icon = "💚" if pnl >= 0 else "❤️"
    result   = "PROFIT" if pnl >= 0 else "LOSS"
    mode     = "🧪 PAPER" if trade["paper"] else "⚡ LIVE"

    send(
        f"{'✅' if pnl >= 0 else '❌'} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{trade['symbol']}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"{pnl_icon} {result}   : ₹{abs(pnl)}\n"
        f"📝 Reason   : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['exit_time']}"
    )


def send_daily_summary(summary):
    """Send 3:30 PM daily P&L summary."""
    net      = summary["total_profit"] - summary["total_loss"]
    net_icon = "💚" if net >= 0 else "❤️"

    send(
        f"📋 <b>DAILY P&L SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date      : {summary['date']}\n"
        f"📊 Trades    : {summary['total_trades']}\n"
        f"✅ Winners   : {summary['winners']}\n"
        f"❌ Losers    : {summary['losers']}\n"
        f"💚 Profit    : ₹{summary['total_profit']}\n"
        f"❤️ Loss      : ₹{summary['total_loss']}\n"
        f"{net_icon} Net P&L  : ₹{net}\n"
        f"📈 Capital   : ₹{summary['capital']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode: {'Paper Trading' if summary['paper'] else 'Live Trading'}"
    )
