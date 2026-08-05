# app.py — Chartink Paper Trading Bot
from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Config ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
CHAT_ID           = os.environ.get("CHAT_ID", "")
CAPITAL           = float(os.environ.get("CAPITAL", 100000))
CAPITAL_PER_TRADE = 10000
SL_PERCENT        = 1.0
TARGET_PERCENT    = 1.0
MAX_POSITIONS     = 10
PAPER_TRADING     = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT              = int(os.environ.get("PORT", 10000))

FORCE_EXIT        = dtime(15, 12)
MARKET_CLOSE      = dtime(15, 30)

# ── In-memory store ────────────────────────────────
open_trades  = {}
closed_today = []

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
    return dtime(9, 15) <= now_ist() <= MARKET_CLOSE

def can_enter():
    return dtime(9, 15) <= now_ist() <= dtime(14, 30)

def is_force_exit_time():
    return now_ist() >= FORCE_EXIT

def time_str():
    return datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")


# ══════════════════════════════════════════════════
#  PRICE FETCHER
# ══════════════════════════════════════════════════

def get_price_nse(symbol):
    try:
        session = requests.Session()
        headers = {
            "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept"         : "*/*",
            "Referer"        : "https://www.nseindia.com",
            "Accept-Language": "en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com",
            headers=headers, timeout=10)
        r     = session.get(
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
    if chartink_price and float(chartink_price) > 0:
        print(f"✅ Chartink price {symbol}: {chartink_price}")
        return round(float(chartink_price), 2)
    price = get_price_nse(symbol)
    if price:
        print(f"✅ NSE price {symbol}: {price}")
        return price
    price = get_price_yahoo(symbol)
    if price:
        print(f"✅ Yahoo price {symbol}: {price}")
        return price
    print(f"❌ Could not fetch price for {symbol}")
    return None


# ══════════════════════════════════════════════════
#  9:15 AM CANDLE ANALYSIS
# ══════════════════════════════════════════════════

def get_915_candle(symbol):
    try:
        url  = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
                f"?interval=1m&range=1d")
        r    = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10)
        data       = r.json()["chart"]["result"][0]
        timestamps = data["timestamp"]
        opens      = data["indicators"]["quote"][0]["open"]
        closes     = data["indicators"]["quote"][0]["close"]
        highs      = data["indicators"]["quote"][0]["high"]
        lows       = data["indicators"]["quote"][0]["low"]
        volumes    = data["indicators"]["quote"][0]["volume"]

        for i, ts in enumerate(timestamps):
            candle_time = datetime.fromtimestamp(ts, tz=IST).time()
            if candle_time.hour == 9 and candle_time.minute == 15:
                return {
                    "open"  : round(opens[i],  2) if opens[i]  else 0,
                    "high"  : round(highs[i],  2) if highs[i]  else 0,
                    "low"   : round(lows[i],   2) if lows[i]   else 0,
                    "close" : round(closes[i], 2) if closes[i] else 0,
                    "volume": volumes[i] if volumes[i] else 0,
                }
        print(f"⚠️ 9:15 candle not found for {symbol}")
        return None
    except Exception as e:
        print(f"❌ Candle error {symbol}: {e}")
        return None


def is_strong_bullish_candle(candle):
    if not candle:
        return False, "No candle data"
    o = candle["open"]
    c = candle["close"]
    h = candle["high"]
    l = candle["low"]
    if c <= o:
        return False, f"Bearish candle O:{o} C:{c}"
    rng = h - l
    if rng == 0:
        return False, "Zero range candle"
    body_pct  = ((c - o) / rng) * 100
    close_pos = ((c - l) / rng) * 100
    if body_pct < 50:
        return False, f"Weak body {body_pct:.1f}% need 50%"
    if close_pos < 70:
        return False, f"Close at {close_pos:.1f}% need 70%"
    return True, f"Strong bullish Body:{body_pct:.1f}% Close:{close_pos:.1f}%"


# ══════════════════════════════════════════════════
#  TRADE CALCULATOR
# ══════════════════════════════════════════════════

def calculate(symbol, price):
    sl_price     = round(price * (1 - SL_PERCENT / 100), 2)
    target_price = round(price * (1 + TARGET_PERCENT / 100), 2)
    sl_dist      = round(price - sl_price, 2)
    qty          = max(1, int(CAPITAL_PER_TRADE / price))
    risk_amt     = round(sl_dist * qty, 2)
    reward_amt   = round((target_price - price) * qty, 2)
    return {
        "symbol"      : symbol,
        "entry"       : price,
        "qty"         : qty,
        "sl"          : sl_price,
        "target"      : target_price,
        "current_sl"  : sl_price,
        "highest"     : price,
        "capital_used": round(qty * price, 2),
        "risk_amt"    : risk_amt,
        "reward_amt"  : reward_amt,
        "entry_time"  : time_str(),
        "exit_time"   : None,
        "paper"       : PAPER_TRADING,
        "status"      : "OPEN"
    }


# ══════════════════════════════════════════════════
#  TRADE ACTIONS
# ══════════════════════════════════════════════════

def open_trade(symbol, price, candle_info=""):
    if symbol in open_trades:
        return
    if len(open_trades) >= MAX_POSITIONS:
        send(f"🚫 <b>Max {MAX_POSITIONS} positions reached!</b>")
        return
    trade = calculate(symbol, price)
    open_trades[symbol] = trade
    mode  = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"📝 <b>{mode} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock      : <b>{symbol}</b>\n"
        f"💰 Entry      : ₹{price}\n"
        f"📦 Quantity   : {trade['qty']} shares\n"
        f"🔴 Stop Loss  : ₹{trade['sl']} ({SL_PERCENT}%)\n"
        f"🟢 Target     : ₹{trade['target']} ({TARGET_PERCENT}%)\n"
        f"📊 R:R        : 1:1\n"
        f"💵 Capital    : ₹{trade['capital_used']}\n"
        f"⚠️ Risk       : ₹{trade['risk_amt']}\n"
        f"🎯 Reward     : ₹{trade['reward_amt']}\n"
        f"📈 9:15 Candle: {candle_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['entry_time']}"
    )
    print(f"📝 Trade opened: {symbol} @ ₹{price}")


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
            w.writerow(["date","symbol","entry","exit","qty",
                        "sl","target","risk","reward","pnl",
                        "result","reason","entry_time","exit_time"])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["target"],
            trade["risk_amt"], trade["reward_amt"],
            pnl, "WIN" if pnl >= 0 else "LOSS",
            reason, trade["entry_time"], trade["exit_time"]
        ])


# ══════════════════════════════════════════════════
#  POSITION MONITOR
# ══════════════════════════════════════════════════

def check_positions():
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue
        print(f"📈 {symbol}: ₹{price} SL:₹{trade['sl']} T:₹{trade['target']}")
        if price >= trade["target"]:
            close_trade(symbol, price, "🎯 Target Hit 1:1")
            continue
        if price <= trade["sl"]:
            close_trade(symbol, price, "🔴 Stop Loss Hit")
            continue


# ══════════════════════════════════════════════════
#  EOD REPORT
# ══════════════════════════════════════════════════

def send_eod():
    winners = [t for t in closed_today if t["pnl"] >= 0]
    losers  = [t for t in closed_today if t["pnl"] <  0]
    net     = round(sum(t["pnl"] for t in closed_today), 2)
    icon    = "💚" if net >= 0 else "❤️"
    send(
        f"📋 <b>DAILY P&L SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date     : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Trades   : {len(closed_today)}\n"
        f"✅ Winners  : {len(winners)}\n"
        f"❌ Losers   : {len(losers)}\n"
        f"💚 Profit   : ₹{sum(t['pnl'] for t in winners):.2f}\n"
        f"❤️ Loss     : ₹{abs(sum(t['pnl'] for t in losers)):.2f}\n"
        f"{icon} Net P&L : ₹{net}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    closed_today.clear()
    print("📋 EOD report sent")


# ══════════════════════════════════════════════════
#  BACKGROUND MONITOR
# ══════════════════════════════════════════════════

def run_monitor():
    eod_sent = False
    print("📈 Monitor started — every 1 min")
    while True:
        try:
            t = now_ist()
            if t >= FORCE_EXIT and open_trades:
                send("⏰ <b>3:12 PM — Force closing all MIS positions!</b>")
                for sym in list(open_trades.keys()):
                    p = get_price(sym) or open_trades[sym]["entry"]
                    close_trade(sym, p, "⏰ Force Exit 3:12 PM")
            if t >= MARKET_CLOSE and not eod_sent:
                send_eod()
                eod_sent = True
            if t < dtime(9, 0):
                eod_sent = False
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
    data     = request.json or request.form.to_dict()
    raw_stk  = data.get("stocks", "")
    raw_prc  = data.get("trigger_prices", "")

    stocks = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices = [p.strip() for p in raw_prc.split(",") if p.strip()]

    if not stocks:
        return jsonify({"status": "no stocks"}), 400

    t = now_ist()
    if not can_enter():
        print(f"⏰ Outside entry hours: {t}")
        return jsonify({"status": "outside entry hours"}), 200

    results = []
    for i, symbol in enumerate(stocks):
        if len(open_trades) >= MAX_POSITIONS:
            send(f"🚫 <b>Max {MAX_POSITIONS} positions reached!</b>")
            break
        if symbol in open_trades:
            continue

        # Check 9:15 candle
        candle = get_915_candle(symbol)
        is_bullish, candle_info = is_strong_bullish_candle(candle)

        if not is_bullish:
            send(
                f"❌ <b>Entry REJECTED — {symbol}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 9:15 Candle: {candle_info}\n"
                f"⚠️ Need strong bullish candle\n"
                f"🕐 {time_str()}"
            )
            results.append({"symbol": symbol, "status": "rejected"})
            continue

        chartink_price = prices[i] if i < len(prices) else None
        price          = get_price(symbol, chartink_price)

        if not price:
            send(f"❌ Could not fetch price for {symbol}")
            continue

        open_trade(symbol, price, candle_info)
        results.append({"symbol": symbol, "status": "entered", "price": price})

    return jsonify({"status": "processed", "results": results}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"           : "🟢 Running",
        "mode"             : "🧪 Paper" if PAPER_TRADING else "⚡ Live",
        "time_ist"         : time_str(),
        "market_open"      : is_market_hours(),
        "can_enter"        : can_enter(),
        "open_positions"   : list(open_trades.keys()),
        "total_positions"  : len(open_trades),
        "max_positions"    : MAX_POSITIONS,
        "trades_today"     : len(closed_today) + len(open_trades),
        "capital_per_trade": CAPITAL_PER_TRADE,
        "sl_percent"       : SL_PERCENT,
        "target_percent"   : TARGET_PERCENT,
        "rr_ratio"         : "1:1",
        "force_exit"       : "3:12 PM",
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot is Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode          : {'Paper' if PAPER_TRADING else 'Live'}\n"
        f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
        f"📊 Max Positions : {MAX_POSITIONS}\n"
        f"🔴 Stop Loss     : {SL_PERCENT}%\n"
        f"🟢 Target        : {TARGET_PERCENT}% (1:1 RR)\n"
        f"⏰ Entry Hours   : 9:15 AM - 2:30 PM\n"
        f"📈 Entry Filter  : 9:15 candle strong bullish\n"
        f"🚪 Force Exit    : 3:12 PM\n"
        f"🕐 Time IST      : {time_str()}"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "open_trades" : list(open_trades.keys()),
        "closed_today": [{"symbol": t["symbol"], "pnl": t["pnl"]}
                         for t in closed_today],
        "net_pnl"     : round(sum(t["pnl"] for t in closed_today), 2)
    }), 200


@app.route("/report", methods=["GET"])
def report():
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
    f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
    f"📊 Max Positions : {MAX_POSITIONS}\n"
    f"🔴 Stop Loss     : {SL_PERCENT}%\n"
    f"🟢 Target        : {TARGET_PERCENT}% (1:1 RR)\n"
    f"⏰ Entry Hours   : 9:15 AM - 2:30 PM\n"
    f"📈 Entry Filter  : 9:15 candle strong bullish\n"
    f"🚪 Force Exit    : 3:12 PM"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
