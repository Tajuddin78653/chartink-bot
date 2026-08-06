# app.py — Chartink Trading Bot
# Signal: tazbul | SL: 1% | TP: 1.5% | Capital: ₹10,000/trade

from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Config ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
CHAT_ID           = os.environ.get("CHAT_ID", "")
CAPITAL_PER_TRADE = 10000        # ₹10,000 per trade
SL_PERCENT        = 1.0          # 1% Stop Loss
TP_PERCENT        = 1.5          # 1.5% Take Profit
PAPER_TRADING     = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT              = int(os.environ.get("PORT", 10000))

# ── Timings ────────────────────────────────────────
MARKET_OPEN  = dtime(9,  15)
MARKET_CLOSE = dtime(15, 30)
FORCE_EXIT   = dtime(15, 12)

# ── In-memory store ────────────────────────────────
open_trades  = {}    # Currently open trades
closed_today = []    # Closed trades today
traded_today = set() # All stocks traded today (no repeat)

# ══════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        print("✅ Telegram sent!" if r.status_code == 200 else f"❌ {r.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")


# ══════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════

def now_ist():
    return datetime.now(IST).time()

def is_market_hours():
    return MARKET_OPEN <= now_ist() <= MARKET_CLOSE

def time_str():
    return datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")


# ══════════════════════════════════════════════════
#  PRICE FETCHER — Chartink → NSE → Yahoo
# ══════════════════════════════════════════════════

def get_price_nse(symbol):
    try:
        session = requests.Session()
        headers = {
            "User-Agent"     : "Mozilla/5.0",
            "Accept"         : "*/*",
            "Referer"        : "https://www.nseindia.com",
            "Accept-Language": "en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com",
            headers=headers, timeout=10)
        r = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            headers=headers, timeout=10)
        price = r.json()["priceInfo"]["lastPrice"]
        return round(float(price), 2)
    except:
        return None


def get_price_yahoo(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
        r   = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10)
        price = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(price), 2)
    except:
        return None


def get_price(symbol, chartink_price=None):
    # 1st: Use Chartink trigger price
    if chartink_price:
        try:
            p = round(float(chartink_price), 2)
            if p > 0:
                print(f"✅ Chartink price {symbol}: ₹{p}")
                return p
        except:
            pass
    # 2nd: NSE India
    price = get_price_nse(symbol)
    if price:
        print(f"✅ NSE price {symbol}: ₹{price}")
        return price
    # 3rd: Yahoo Finance
    price = get_price_yahoo(symbol)
    if price:
        print(f"✅ Yahoo price {symbol}: ₹{price}")
        return price
    print(f"❌ Price fetch failed: {symbol}")
    return None


# ══════════════════════════════════════════════════
#  TRADE CALCULATOR
# ══════════════════════════════════════════════════

def calculate(```python
def calculate(symbol, price):
    sl_price     = round(price * (1 - SL_PERCENT / 100), 2)
    tp_price     = round(price * (1 + TP_PERCENT / 100), 2)
    sl_dist      = round(price - sl_price, 2)
    qty          = max(1, int(CAPITAL_PER_TRADE / price))
    risk_amt     = round(sl_dist * qty, 2)
    reward_amt   = round((tp_price - price) * qty, 2)
    return {
        "symbol"      : symbol,
        "entry"       : price,
        "qty"         : qty,
        "sl"          : sl_price,
        "tp"          : tp_price,
        "capital_used": round(qty * price, 2),
        "risk_amt"    : risk_amt,
        "reward_amt"  : reward_amt,
        "entry_time"  : time_str(),
        "exit_time"   : None,
        "paper"       : PAPER_TRADING,
    }


# ══════════════════════════════════════════════════
#  TRADE ACTIONS
# ══════════════════════════════════════════════════

def open_trade(symbol, price):
    if symbol in open_trades:
        print(f"⚠️ Already in trade: {symbol}")
        return
    if symbol in traded_today:
        print(f"⚠️ Already traded {symbol} today — skipping")
        return
    trade = calculate(symbol, price)
    open_trades[symbol] = trade
    traded_today.add(symbol)
    mode  = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"📝 <b>{mode} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock      : <b>{symbol}</b>\n"
        f"💰 Entry      : ₹{price}\n"
        f"📦 Quantity   : {trade['qty']} shares\n"
        f"🔴 Stop Loss  : ₹{trade['sl']} ({SL_PERCENT}%)\n"
        f"🟢 Take Profit: ₹{trade['tp']} ({TP_PERCENT}%)\n"
        f"💵 Capital    : ₹{trade['capital_used']}\n"
        f"⚠️ Risk       : ₹{trade['risk_amt']}\n"
        f"🎯 Reward     : ₹{trade['reward_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['entry_time']}"
    )
    print(f"📝 Opened: {symbol} @ ₹{price}")


def close_trade(symbol, exit_price, reason):
    if symbol not in open_trades:
        return
    trade              = open_trades.pop(symbol)
    now                = time_str()
    trade["exit_time"] = now
    pnl                = round((exit_price - trade["entry"]) * trade["qty"], 2)
    icon               = "✅" if pnl >= 0 else "❌"
    result             = "PROFIT" if pnl >= 0 else "LOSS"
    mode               = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"{icon} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock  : <b>{symbol}</b>\n"
        f"💰 Entry  : ₹{trade['entry']}\n"
        f"🚪 Exit   : ₹{exit_price}\n"
        f"📦 Qty    : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}: ₹{abs(pnl)}\n"
        f"📝 Reason : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now}"
    )
    closed_today.append({"symbol": symbol, "pnl": pnl})
    log_trade(trade, exit_price, pnl, reason)
    print(f"🚪 Closed: {symbol} @ ₹{exit_price} P&L: ₹{pnl}")


def log_trade(trade, exit_price, pnl, reason):
    f      = "logs/trades.csv"
    exists = os.path.exists(f)
    with open(f, "a", newline="") as fp:
        w = csv.writer(fp)
        if not exists:
            w.writerow([
                "date","symbol","entry","exit","qty",
                "sl","tp","risk","reward","pnl",
                "result","reason","entry_time","exit_time"
            ])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["tp"],
            trade["risk_amt"], trade["reward_amt"],
            pnl, "WIN" if pnl >= 0 else "LOSS",
            reason, trade["entry_time"], trade["exit_time"]
        ])


# ══════════════════════════════════════════════════
#  POSITION MONITOR — SL & TP
# ══════════════════════════════════════════════════

def check_positions():
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue
        print(f"📈 {symbol}: ₹{price} | SL:₹{trade['sl']} | TP:₹{trade['tp']}")
        if price >= trade["tp"]:
            close_trade(symbol, price, "🎯 Take Profit Hit")
            continue
        if price <= trade["sl"]:
            close_trade(symbol, price, "🔴 Stop Loss Hit")
            continue


# ══════════════════════════════════════════════════
#  EOD REPORT
# ══════════════════════════════════════════════════

def send_eod():
    winners  = [t for t in closed_today if t["pnl"] >= 0]
    losers   = [t for t in closed_today if t["pnl"] <  0]
    net      = round(sum(t["pnl"] for t in closed_today), 2)
    icon     = "💚" if net >= 0 else "❤️"
    send(
        f"📋 <b>DAILY P&L SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date        : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Total Trades: {len(closed_today)}\n"
        f"✅ Winners     : {len(winners)}\n"
        f"❌ Losers      : {len(losers)}\n"
        f"💚 Gross Profit: ₹{sum(t['pnl'] for t in winners):.2f}\n"
        f"❤️ Gross Loss  : ₹{abs(sum(t['pnl'] for t in losers)):.2f}\n"
        f"{icon} Net P&L    : ₹{net}\n"
        f"📈 Stocks      : {', '.join(t['symbol'] for t in closed_today)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    # Reset daily counters
    closed_today.clear()
    traded_today.clear()
    print("📋 EOD report sent — daily counters reset")


# ══════════════════════════════════════════════════
#  BACKGROUND MONITOR THREAD
# ══════════════════════════════════════════════════

def run_monitor():
    eod_sent = False
    print("📈 Monitor started — every 1 min")
    while True:
        try:
            t = now_ist()

            # Force exit at 3:12 PM
            if t >= FORCE_EXIT and open_trades:
                send("⏰ <b>3:12 PM — Force closing all MIS positions!</b>")
                for sym in list(open_trades.keys()):
                    p = get_price(sym) or open_trades[sym]["entry"]
                    close_trade(sym, p, "⏰ Force Exit 3:12 PM")

            # EOD report at 3:30 PM
            if t >= MARKET_CLOSE and not eod_sent:
                send_eod()
                eod_sent = True

            # Reset EOD flag next morning
            if t < dtime(9, 0):
                eod_sent = False

            # Monitor open positions
            if is_market_hours() and open_trades:
                check_positions()

        except Exception as e:
            print(f"❌ Monitor error: {e}")
        time.sleep(60)


# ══════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════

@app.route("/alert", methods=["POST"])
def receive_alert():
    """Chartink webhook — receives stocks & prices."""
    data    = request.json or request.form.to_dict()
    raw_stk = data.get("stocks", "")
    raw_prc = data.get("trigger_prices", "")

    stocks = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices = [p.strip() for p in raw_prc.split(",") if p.strip()]

    if not stocks:
        return jsonify({"status": "no stocks"}), 400

    print(f"📊 Alert received: {stocks}")

    results = []
    for i, symbol in enumerate(stocks):

        # Skip if already traded today
        if symbol in traded_today:
            print(f"⚠️ {symbol} already traded today — skip")
            results.append({"symbol": symbol, "status": "already traded"})
            continue

        # Skip if already in open trade
        if symbol in open_trades:
            print(f"⚠️ {symbol} already in open trade — skip")
            results.append({"symbol": symbol, "status": "already open"})
            continue

        # Get price — Chartink first
        chartink_price = prices[i] if i < len(prices) else None
        price          = get_price(symbol, chartink_price)

        if not price:
            send(f"❌ <b>Price fetch failed: {symbol}</b>")
            results.append({"symbol": symbol, "status": "price failed"})
            continue

        # Open trade
        open_trade(symbol, price)
        results.append({"symbol": symbol, "status": "entered", "price": price})

    return jsonify({"status": "processed", "results": results}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"          : "🟢 Running",
        "mode"            : "🧪 Paper" if PAPER_TRADING else "⚡ Live",
        "time_ist"        : time_str(),
        "market_open"     : is_market_hours(),
        "open_positions"  : list(open_trades.keys()),
        "total_open"      : len(open_trades),
        "traded_today"    : list(traded_today),
        "total_traded"    : len(traded_today),
        "closed_today"    : len(closed_today),
        "capital_per_trade": CAPITAL_PER_TRADE,
        "sl_percent"      : SL_PERCENT,
        "tp_percent"      : TP_PERCENT,
        "force_exit"      : "3:12 PM",
        "eod_report"      : "3:30 PM",
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot is Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode          : {'Paper' if PAPER_TRADING else 'Live'}\n"
        f"📡 Signal Source : Chartink tazbul\n"
        f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
        f"🔴 Stop Loss     : {SL_PERCENT}%\n"
        f"🟢 Take Profit   : {TP_PERCENT}%\n"
        f"🔁 Repeat Trade  : ❌ No (once per stock/day)\n"
        f"📊 Max Positions : Unlimited\n"
        f"🚪 Force Exit    : 3:12 PM\n"
        f"📋 EOD Report    : 3:30 PM\n"
        f"🕐 Time IST      : {time_str()}"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "open_trades" : list(open_trades.keys()),
        "traded_today": list(traded_today),
        "closed_today": [{"symbol": t["symbol"], "pnl": t["pnl"]}
                         for t in closed_today],
        "net_pnl"     : round(sum(t["pnl"] for t in closed_today), 2),
        "time_ist"    : time_str(),
    }), 200


@app.route("/report", methods=["GET"])
def report():
    """Manually trigger EOD report."""
    send_eod()
    return jsonify({"status": "report sent"}), 200


# ══════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════

print("🚀 Starting Chartink Bot...")
threading.Thread(target=run_monitor, daemon=True).start()

send(
    f"🟢 <b>Chartink Bot LIVE!</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"🧪 Mode          : {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
    f"📡 Signal Source : Chartink tazbul\n"
    f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
    f"🔴 Stop Loss     : {SL_PERCENT}%\n"
    f"🟢 Take Profit   : {TP_PERCENT}%\n"
    f"🔁 Repeat Trade  : ❌ No\n"
    f"📊 Max Positions : Unlimited\n"
    f"🚪 Force Exit    : 3:12 PM\n"
    f"📋 EOD Report    : 3:30 PM"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
