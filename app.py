# app.py — Chartink Paper Trading Bot
# Entry: 9:16 AM | Bullish 9:15 candle | RR 1:2 | Max 10 positions

from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Config ─────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
CHAT_ID       = os.environ.get("CHAT_ID", "")
CAPITAL       = float(os.environ.get("CAPITAL", 20000))
CAPITAL_PER_TRADE = 10000          # Fixed ₹10,000 per trade
RISK_PERCENT  = float(os.environ.get("RISK_PERCENT", 1.0))
SL_PERCENT    = float(os.environ.get("SL_PERCENT", 2.0))
TARGET_PERCENT= SL_PERCENT * 2     # 1:2 Risk Reward = 4%
MAX_POSITIONS = 10                 # Max 10 positions
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT          = int(os.environ.get("PORT", 10000))

# ── Entry Window ───────────────────────────────────
ENTRY_START   = dtime(9, 16, 0)   # 9:16:00 AM
ENTRY_END     = dtime(9, 16, 59)  # 9:16:59 AM
FORCE_EXIT    = dtime(15, 12)     # 3:12 PM
MARKET_CLOSE  = dtime(15, 30)     # 3:30 PM

# ── In-memory store ────────────────────────────────
open_trades   = {}
closed_today  = []
pending_alerts= []   # Store alerts received before 9:16 AM

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

def is_entry_window():
    """Only allow entries at exactly 9:16 AM"""
    return ENTRY_START <= now_ist() <= ENTRY_END

def is_force_exit_time():
    return now_ist() >= FORCE_EXIT

def time_str():
    return datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")


# ══════════════════════════════════════════════════
#  PRICE FETCHER — NSE first → Yahoo fallback
# ══════════════════════════════════════════════════

def get_price_nse(symbol):
    """Fetch price from NSE India."""
    try:
        session  = requests.Session()
        headers  = {
            "User-Agent"    : "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept"        : "*/*",
            "Referer"       : "https://www.nseindia.com",
            "Accept-Language": "en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com",
            headers=headers, timeout=10)
        r     = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            headers=headers, timeout=10
        )
        price = r.json()["priceInfo"]["lastPrice"]
        return round(float(price), 2)
    except:
        return None


def get_price_yahoo(symbol):
    """Fetch price from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
        r   = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        price = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(price), 2)
    except:
        return None


def get_price(symbol, chartink_price=None):
    """Smart price fetcher — Chartink → NSE → Yahoo"""
    # 1. Use Chartink trigger price if available
    if chartink_price and float(chartink_price) > 0:
        print(f"✅ Chartink price {symbol}: ₹{chartink_price}")
        return round(float(chartink_price), 2)

    # 2. Try NSE India
    price = get_price_nse(symbol)
    if price:
        print(f"✅ NSE price {symbol}: ₹{price}")
        return price

    # 3. Fallback Yahoo
    price = get_price_yahoo(symbol)
    if price:
        print(f"✅ Yahoo price {symbol}: ₹{price}")
        return price

    print(f"❌ Could not fetch price for {symbol}")
    return None


# ══════════════════════════════════════════════════
#  9:15 AM CANDLE ANALYSIS
# ══════════════════════════════════════════════════

def get_915_candle(symbol):
    """
    Fetch 9:15 AM 1-min candle from Yahoo Finance.
    Returns candle data: open, high, low, close, volume
    """
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
            f"?interval=1m&range=1d"
        )
        r    = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data      = r.json()["chart"]["result"][0]
        timestamps= data["timestamp"]
        opens     = data["indicators"]["quote"][0]["open"]
        closes    = data["indicators"]["quote"][0]["close"]
        highs     = data["indicators"]["quote"][0]["high"]
        lows      = data["indicators"]["quote"][0]["low"]
        volumes   = data["indicators"]["quote"][0]["volume"]

        # Find 9:15 AM candle (IST = UTC+5:30)
        for i, ts in enumerate(timestamps):
            candle_time = datetime.fromtimestamp(ts, tz=IST).time()
            if candle_time.hour == 9 and candle_time.minute == 15:
                candle = {
                    "open"  : round(opens[i],   2) if opens[i]   else 0,
                    "high"  : round(highs[i],   2) if highs[i]   else 0,
                    "low"   : round(lows[i],    2) if lows[i]    else 0,
                    "close" : round(closes[i],  2) if closes[i]  else 0,
                    "volume": volumes[i] if volumes[i] else 0,
                }
                print(f"📊 9:15 candle {symbol}: {candle}")
                return candle

        print(f"⚠️ 9:15 candle not found for {symbol}")
        return None

    except Exception as e:
        print(f"❌ Candle fetch error {symbol}: {e}")
        return None


def is_strong_bullish_candle(candle):
    """
    Check if 9:15 AM candle is STRONG BULLISH:
    1. Close > Open (green candle)
    2. Body >= 50% of total range (strong body)
    3. Close in upper 70% of range (closed near high)
    """
    if not candle:
        return False, "No candle data"

    open_p  = candle["open"]
    close_p = candle["close"]
    high_p  = candle["high"]
    low_p   = candle["low"]

    # Check 1: Must be green candle
    if close_p <= open_p:
        return False, f"Bearish candle (O:{open_p} C:{close_p})"

    candle_range = high_p - low_p
    if candle_range == 0:
        return False, "Zero range candle"

    body_size    = close_p - open_p
    body_pct     = (body_size / candle_range) * 100
    close_pos    = ((close_p - low_p) / candle_range) * 100

    # Check 2: Body >= 50% of range
    if body_pct < 50:
        return False, f"Weak body {body_pct:.1f}% (need 50%+)"

    # Check 3: Close in upper 70% of range
    if close_pos < 70:
        return False, f"Close position {close_pos:.1f}% (need 70%+)"

    return True, (
        f"✅ Strong bullish! "
        f"Body:{body_pct:.1f}% "
        f"Close:{close_pos:.1f}%"
    )


# ══════════════════════════════════════════════════
#  TRADE CALCULATOR — RR 1:2
# ══════════════════════════════════════════════════

def calculate(symbol, price):
    sl_price     = round(price * (1 - SL_PERCENT / 100), 2)
    target_price = round(price * (1 + TARGET_PERCENT / 100), 2)
    sl_distance  = round(price - sl_price, 2)
    qty          = max(1, int(CAPITAL_PER_TRADE / price))
    risk_amt     = round(sl_distance * qty, 2)
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
        print(f"⚠️ Already in trade: {symbol}")
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
        f"📌 Stock     : <b>{symbol}</b>\n"
        f"💰 Entry     : ₹{price}\n"
        f"📦 Quantity  : {trade['qty']} shares\n"
        f"🔴 Stop Loss : ₹{trade['sl']} ({SL_PERCENT}%)\n"
        f"🟢 Target    : ₹{trade['target']} ({TARGET_PERCENT}%)\n"
        f"📊 R:R       : 1:2\n"
        f"💵 Capital   : ₹{trade['capital_used']}\n"
        f"⚠️ Risk      : ₹{trade['risk_amt']}\n"
        f"🎯 Reward    : ₹{trade['reward_amt']}\n"
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
    rr_actual          = round(abs(pnl) / trade["risk_amt"], 2) if trade["risk_amt"] > 0 else 0

    send(
        f"{icon} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}  : ₹{abs(pnl)}\n"
        f"📊 R:R      : 1:{rr_actual}\n"
        f"📝 Reason   : {reason}\n"
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
                "sl","target","risk","reward","pnl",
                "result","reason","entry_time","exit_time"
            ])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["target"],
            trade["risk_amt"], trade["reward_amt"],
            pnl, "WIN" if pnl >= 0 else "LOSS",
            reason, trade["entry_time"], trade["exit_time"]
        ])


# ══════════════════════════════════════════════════
#  POSITION MONITOR — SL & TARGET
# ══════════════════════════════════════════════════

def check_positions():
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue

        print(f"📈 {symbol}: ₹{price} | SL:₹{trade['current_sl']} | Target:₹{trade['target']}")

        # Check Target Hit (1:2 RR)
        if price >= trade["target"]:
            close_trade(symbol, price, "🎯 Target Hit (1:2 RR)")
            continue

        # Check Stop Loss Hit
        if price <= trade["current_sl"]:
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
        f"📅 Date       : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Trades     : {len(closed_today)}\n"
        f"✅ Winners    : {len(winners)}\n"
        f"❌ Losers     : {len(losers)}\n"
        f"💚 Profit     : ₹{sum(t['pnl'] for t in winners if t['pnl']>0):.2f}\n"
        f"❤️ Loss       : ₹{abs(sum(t['pnl'] for t in losers if t['pnl']<0)):.2f}\n"
        f"{icon} Net P&L   : ₹{net}\n"
        f"💰 Capital    : ₹{CAPITAL}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    closed_today.clear()
    pending_alerts.clear()
    print("📋 EOD report sent")


# ══════════════════════════════════════════════════
#  BACKGROUND MONITOR THREAD
# ══════════════════════════════════════════════════

def run_monitor():
    eod_sent = False
    print("📈 Position monitor started — every 1 min")

    while True:
        try:
            t = now_ist()

            # ── Force exit at 3:12 PM ──────────────
            if t >= FORCE_EXIT and open_trades:
                send("⏰ <b>3:12 PM — Force closing all MIS positions!</b>")
                for sym in list(open_trades.keys()):
                    p = get_price(sym) or open_trades[sym]["entry"]
                    close_trade(sym, p, "⏰ Force Exit 3:12 PM")

            # ── EOD report at 3:30 PM ──────────────
            if t >= MARKET_CLOSE and not eod_sent:
                send_eod()
                eod_sent = True

            # ── Reset next morning ─────────────────
            if t < dtime(9, 0):
                eod_sent = False

            # ── Monitor positions ──────────────────
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
    """Main webhook — Chartink posts here."""
    data     = request.json or request.form.to_dict()
    raw_stk  = data.get("stocks", "")
    raw_prc  = data.get("trigger_prices", "")
    screener = data.get("scan_name", "tazbul")

    stocks = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices = [p.strip() for p in raw_prc.split(",") if p.strip()]

    if not stocks:
        return jsonify({"status": "no stocks"}), 400

    t = now_ist()
    print(f"📊 Alert received at {t}: {stocks}")

    # ── Check if alert is at 9:15-9:16 AM window ─
    if t < dtime(9, 15):
        send(
            f"⏰ <b>Alert received too early!</b>\n"
            f"🕐 Time: {time_str()}\n"
            f"📌 Stocks: {', '.join(stocks)}\n"
            f"⚠️ Entry only at 9:16 AM"
        )
        return jsonify({"status": "too early"}), 200

    if t > dtime(9, 16, 59):
        send(
            f"⏰ <b>Alert received after entry window!</b>\n"
            f"🕐 Time: {time_str()}\n"
            f"📌 Stocks: {', '.join(stocks)}\n"
            f"⚠️ Entry window: 9:16 AM only"
        )
        return jsonify({"status": "outside entry window"}), 200

    # ── Process each stock at 9:16 AM ─────────────
    results = []
    for i, symbol in enumerate(stocks):

        # Check max positions
        if len(open_trades) >= MAX_POSITIONS:
            send(f"🚫 <b>Max {MAX_POSITIONS} positions reached!</b>")
            break

        # Check already in trade
        if symbol in open_trades:
            print(f"⚠️ Already in {symbol}")
            continue

        # ── CONDITION: Check 9:15 AM candle ───────
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
            results.append({"symbol": symbol, "status": "rejected", "reason": candle_info})
            continue

        # ── Get entry price ────────────────────────
        chartink_price = prices[i] if i < len(prices) else None
        price          = get_price(symbol, chartink_price)

        if not price:
            send(f"❌ <b>Could not fetch price for {symbol}</b>")
            continue

        # ── Open trade ─────────────────────────────
        open_trade(symbol, price, candle_info)
        results.append({"symbol": symbol, "status": "entered", "price": price})

    return jsonify({"status": "processed", "results": results}), 200


@app.route("/", methods=["GET"])
def home():
    t = now_ist()
    return jsonify({
        "status"        : "🟢 Running",
        "mode"          : "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading",
        "time_ist"      : time_str(),
        "market_open"   : is_market_hours(),
        "entry_window"  : is_entry_window(),
        "open_positions": list(open_trades.keys()),
        "total_positions": len(open_trades),
        "max_positions" : MAX_POSITIONS,
        "trades_today"  : len(closed_today) + len(open_trades),
        "capital_per_trade": CAPITAL_PER_TRADE,
        "sl_percent"    : SL_PERCENT,
        "target_percent": TARGET_PERCENT,
        "rr_ratio"      : "1:2",
        "force_exit"    : "3:12 PM",
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot is Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode          : {'Paper Trading' if PAPER_TRADING else 'Live Trading'}\n"
        f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
        f"📊 Max Positions : {MAX_POSITIONS}\n"
        f"🔴 Stop Loss     : {SL_PERCENT}%\n"
        f"🟢 Target        : {TARGET_PERCENT}% (1:2 RR)\n"
        f"⏰ Entry Window  : 9:16 AM only\n"
        f"📈 Entry Filter  : Strong bullish 9:15 candle\n"
        f"🚪 Force Exit    : 3:12 PM\n"
        f"🕐 Current Time  : {time_str()}"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "open_trades" : open_trades,
        "closed_today": [{"symbol": t["symbol"], "pnl": t["pnl"]}
                         for t in closed_today],
        "summary"     : {
            "total"  : len(closed_today) + len(open_trades),
            "open"   : len(open_trades),
            "closed" : len(closed_today),
            "net_pnl": round(sum(t["pnl"] for t in closed_today), 2)
        }
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
    f"🧪 Mode          : {'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}\n"
    f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
    f"📊 Max Positions : {MAX_POSITIONS}\n"
    f"🔴 Stop Loss     : {SL_PERCENT}%\n"
    f"🟢 Target        : {TARGET_PERCENT}% (1:2 RR)\n"
    f"⏰ Entry Window  : 9:16 AM ONLY\n"
    f"📈 Entry Filter  : Strong bullish 9:15 candle\n"
    f"🚪 Force Exit    : 3:12 PM\n"
    f"⏰ Market Hours  : 9:15 AM – 3:30 PM IST"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
