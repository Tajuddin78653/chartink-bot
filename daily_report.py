# daily_report.py — Daily 3:30 PM Summary
from paper_trader    import get_daily_summary, reset_daily, open_trades, close_trade
from trailing_sl     import get_live_price
from telegram_bot    import send_daily_summary, send


def force_exit_all():
    """Force close all MIS positions at 3:15 PM."""
    if not open_trades:
        print("✅ No open trades to close")
        return

    send("⏰ <b>3:15 PM — Force closing all MIS positions!</b>")

    for symbol in list(open_trades.keys()):
        price = get_live_price(symbol)
        if price:
            close_trade(symbol, price, "MIS Force Exit 3:15 PM ⏰")
        else:
            trade = open_trades.get(symbol)
            if trade:
                close_trade(symbol, trade["entry"], "Force Exit (price unavailable)")


def send_eod_report():
    """Send end of day P&L summary."""
    summary = get_daily_summary()
    send_daily_summary(summary)
    reset_daily()
    print("📋 EOD report sent")
