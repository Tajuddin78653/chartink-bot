# app.py — Chartink Paper Trading Bot (Final Fixed Version)
from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, re, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Config ─────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
CHAT_ID       = os.environ.get("CHAT_ID", "")
CAPITAL       = float(os.environ.get("CAPITAL", 20000))
RISK_PERCENT  = float(os.environ.get("RISK_PERCENT", 1.0))
SL_PERCENT    = float(os.environ.get("SL_PERCENT", 2.0))
TRAIL_PERCENT = float(os.environ.get("TRAIL_PERCENT", 2.0))
MAX_TRADES    = int(os.environ.get("MAX_TRADES", 2))
SCAN_MINS     = int(os.environ.get("SCAN_MINS", 5))
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT          = int(os.environ.get("PORT", 10000))

# ── In-memory store ────────────────────────────────
open_trades  = {}
closed_today = []
last_stocks  = set()

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
        if r.status_code == 200:
            print("✅ Telegram sent!")
        else:
            print(f"❌ Telegram error: {r.text}")
    except Exception as e:
        print(f"❌ Telegram exception: {e}")


# ══════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════

def now_ist():
    return datetime.now(IST).time()

def is_market_hours():
    return dtime(9, 15) <= now_ist() <= dtime(15, 30)

def can_enter():
    return dtime(9, 15) <= now_ist() <= dtime(14, 30)

def is_force_exit():
    return now_ist() >= dtime(15, 15)


# ══════════════════════════════════════════════════
#  PRICE FETCHER
# ══════════════════════════════════════════════════

def get_price(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
        r   = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        price = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(price), 2)
    except Exception as e:
        print(f"❌ Price error {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════
#  CHARTINK SCANNER
# ══════════════════════════════════════════════════

def fetch_screener():
    """Fetch stocks from Chartink screener with login."""
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/91.0 Safari/537.36"
        }

        # ── Step 1: Load login page → get CSRF ────
        login_page = session.get(
            "https://chartink.com/login",
            headers=headers,
            timeout=15
        )
        csrf_match = re.search(
            r'<meta name="csrf-token" content="(.+?)"',
            login_page.text
        )
        csrf = csrf_match.group(1) if csrf_match else ""
        print(f"🔑 Login CSRF: {csrf[:15]}..." if csrf else "⚠️ No login CSRF!")

        # ── Step 2: Login to Chartink ──────────────
        email    = os.environ.get("CHARTINK_EMAIL", "")
        password = os.environ.get("CHARTINK_PASSWORD", "")

        login_resp = session.post(
            "https://chartink.com/login",
            data={
                "_token"  : csrf,
                "email"   : email,
                "password": password
            },
            headers={
                **headers,
                "Referer"     : "https://chartink.com/login",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            timeout=15
        )

        # Check login success
        if "logout" in login_resp.text.lower() or login_resp.status_code == 200:
            print("✅ Chartink login successful!")
        else:
            print(f"⚠️ Login may have failed: {login_resp.status_code}")

        # ── Step 3: Load screener page ─────────────
        page = session.get(
            "https://chartink.com/screener/tazbul",
            headers={**headers, "Referer": "https://chartink.com/"},
            timeout=15
        )

        # ── Step 4: Get fresh CSRF token ───────────
        csrf_match2 = re.search(
            r'<meta name="csrf-token" content="(.+?)"',
            page.text
        )
        csrf2 = csrf_match2.group(1) if csrf_match2 else csrf
        print(f"🔑 Screener CSRF: {csrf2[:15]}...")

        # ── Step 5: Extract scan clause ────────────
        patterns = [
            r'"scan_clause"\s*:\s*"(.+?)"',
            r"'scan_clause'\s*:\s*'(.+?)'",
            r'scan_clause.*?value="(.+?)"',
            r'id="scan_clause"[^>]*value="([^"]*)"',
            r'name="scan_clause"[^>]*value="([^"]*)"'
        ]
        scan_clause = ""
        for pat in patterns:
            m = re.search(pat, page.text)
            if m:
                scan_clause = m.group(1)
                print(f"📋 Clause found: {scan_clause[:60]}...")
                break

        if not scan_clause:
            print("⚠️ Scan clause not found in page!")
            print(f"📄 Page title: {re.search(r'<title>(.+?)</title>', page.text, re.IGNORECASE)}")
            return []

        # ── Step 6: Call screener API ──────────────
        api_resp = session.post(
            "https://chartink.com/screener/process",
            data   ={"scan_clause": scan_clause},
            headers={
                **headers,
                "X-Csrf-Token"    : csrf2,
                "X-Requested-With": "XMLHttpRequest",
                "Referer"         : "https://chartink.com/screener/tazbul",
                "Content-Type"    : "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept"          : "application/json, text/javascript, */*"
            },
            timeout=15
        )

        print(f"📡 API: {api_resp.status_code}")
        json_data = api_resp.json()
        raw_list  = json_data.get("data", [])
        print(f"📊 Count: {len(raw_list)}")

        if raw_list:
            print(f"🔍 Keys: {list(raw_list[0].keys())}")

        # ── Step 7: Extract symbols ────────────────
        stocks = []
        for item in raw_list:
            sym = (
                item.get("nsecode") or
                item.get("bsecode") or
                item.get("symbol")  or
                item.get("stock_name", "")
            )
            if sym:
                stocks.append(sym.strip().upper())

        print(f"📈 Stocks: {stocks}")
        return stocks

    except Exception as e:
        print(f"❌ Screener error: {e}")
        return []


# ══════════════════════════════════════════════════
#  TRADE CALCULATOR
# ══════════════════════════════════════════════════

def calculate(symbol, price):
    risk_amt    = round(CAPITAL * (RISK_PERCENT / 100), 2)
    sl_price    = round(price * (1 - SL_PERCENT / 100), 2)
    sl_distance = round(price - sl_price, 2)
    qty         = max(1, int(risk_amt / sl_distance)) if sl_distance > 0 else 1
    cap_used    = round(qty * price, 2)
    max_cap     = CAPITAL * 0.5

    if cap_used > max_cap:
        qty      = max(1, int(max_cap / price))
        cap_used = round(qty * price, 2)

    sl_price = round(price * (1 - SL_PERCENT / 100), 2)
    risk_amt = round((price - sl_price) * qty, 2)

    return {
        "symbol"      : symbol,
        "entry"       : price,
        "qty"         : qty,
        "sl"          : sl_price,
        "current_sl"  : sl_price,
        "highest"     : price,
        "capital_used": cap_used,
        "risk_amt"    : risk_amt,
        "entry_time"  : datetime.now(IST).strftime("%d %b %Y %I:%M %p"),
        "exit_time"   : None,
        "paper"       : PAPER_TRADING
    }


# ══════════════════════════════════════════════════
#  TRADE ACTIONS
# ══════════════════════════════════════════════════

def open_trade(symbol, price):
    if symbol in open_trades:
        print(f"⚠️ Already in trade: {symbol}")
        return
    trade = calculate(symbol, price)
    open_trades[symbol] = trade
    mode = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"📝 <b>{mode} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{price}\n"
        f"📦 Quantity : {trade['qty']} shares\n"
        f"🔴 Stop Loss: ₹{trade['sl']} ({SL_PERCENT}%)\n"
        f"🟢 Trailing : {TRAIL_PERCENT}% trail\n"
        f"💵 Capital  : ₹{trade['capital_used']}\n"
        f"⚠️ Risk     : ₹{trade['risk_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['entry_time']}"
    )
    print(f"📝 Trade opened: {symbol} @ ₹{price}")


def close_trade(symbol, exit_price, reason):
    if symbol not in open_trades:
        return
    trade             = open_trades.pop(symbol)
    now               = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    trade["exit_time"] = now
    pnl               = round((exit_price - trade["entry"]) * trade["qty"], 2)
    icon              = "✅" if pnl >= 0 else "❌"
    result            = "PROFIT" if pnl >= 0 else "LOSS"
    mode              = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"

    send(
        f"{icon} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}  : ₹{abs(pnl)}\n"
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
            w.writerow(["date","symbol","entry","exit",
                        "qty","sl","risk","pnl","result","reason"])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["risk_amt"],
            pnl, "WIN" if pnl >= 0 else "LOSS", reason
        ])


# ══════════════════════════════════════════════════
#  TRAILING SL MONITOR
# ══════════════════════════════════════════════════

def check_trails():
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue

        print(f"📈 {symbol}: ₹{price} | SL: ₹{trade['current_sl']}")

        # SL hit
        if price <= trade["current_sl"]:
            close_trade(symbol, price, "🔴 Stop Loss Hit")
            continue

        # Update trailing SL
        if price > trade["highest"]:
            trade["highest"] = price
            new_sl = round(price * (1 - TRAIL_PERCENT / 100), 2)
            if new_sl > trade["current_sl"]:
                locked             = round((new_sl - trade["entry"]) * trade["qty"], 2)
                trade["current_sl"] = new_sl
                send(
                    f"🔄 <b>TRAILING SL UPDATE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 Stock     : <b>{symbol}</b>\n"
                    f"📈 Price     : ₹{price}\n"
                    f"🔴 New SL    : ₹{new_sl}\n"
                    f"✅ Locked P&L: ₹{locked}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )


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
        f"📅 Date      : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Trades    : {len(closed_today)}\n"
        f"✅ Winners   : {len(winners)}\n"
        f"❌ Losers    : {len(losers)}\n"
        f"💚 Profit    : ₹{sum(t['pnl'] for t in winners):.2f}\n"
        f"❤️ Loss      : ₹{abs(sum(t['pnl'] for t in losers)):.2f}\n"
        f"{icon} Net P&L  : ₹{net}\n"
        f"💰 Capital   : ₹{CAPITAL}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    closed_today.clear()
    print("📋 EOD report sent")


# ══════════════════════════════════════════════════
#  BACKGROUND SCANNER
# ══════════════════════════════════════════════════

def run_scanner():
    global last_stocks
    eod_sent = False
    print(f"🔍 Scanner thread started — every {SCAN_MINS} mins")

    while True:
        try:
            t = now_ist()

            # Force exit at 3:15 PM
            if is_force_exit() and open_trades:
                send("⏰ <b>3:15 PM — Force closing all MIS positions!</b>")
                for sym in list(open_trades.keys()):
                    p = get_price(sym) or open_trades[sym]["entry"]
                    close_trade(sym, p, "⏰ MIS Force Exit 3:15 PM")

            # EOD report at 3:30 PM
            if t >= dtime(15, 30) and not eod_sent:
                send_eod()
                eod_sent  = True
                last_stocks = set()

            # Reset next morning
            if t < dtime(9, 0):
                eod_sent = False

            # Scan for new stocks during entry window
            if can_enter():
                print(f"🔍 Scanning... [{datetime.now(IST).strftime('%I:%M %p')}]")
                stocks     = fetch_screener()
                new_stocks = [s for s in stocks if s not in last_stocks]
                last_stocks = set(stocks)

                for sym in new_stocks:
                    total = len(closed_today) + len(open_trades)
                    if total >= MAX_TRADES:
                        send(f"🚫 <b>Max {MAX_TRADES} trades reached today!</b>")
                        break
                    price = get_price(sym)
                    if price:
                        open_trade(sym, price)
                    else:
                        print(f"⚠️ Could not get price for {sym}")

            elif is_market_hours():
                print(f"⏰ Past entry cutoff — monitoring only")
            else:
                print(f"💤 Market closed — waiting [{datetime.now(IST).strftime('%I:%M %p')}]")

        except Exception as e:
            print(f"❌ Scanner loop error: {e}")

        time.sleep(SCAN_MINS * 60)


# ══════════════════════════════════════════════════
#  TRAILING SL MONITOR THREAD
# ══════════════════════════════════════════════════

def run_trail_monitor():
    print("📈 Trailing SL monitor started — every 1 min")
    while True:
        try:
            if is_market_hours() and open_trades:
                check_trails()
        except Exception as e:
            print(f"❌ Trail monitor error: {e}")
        time.sleep(60)


# ══════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"      : "🟢 Running",
        "mode"        : "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading",
        "market_open" : is_market_hours(),
        "can_enter"   : can_enter(),
        "open_trades" : list(open_trades.keys()),
        "trades_today": len(closed_today) + len(open_trades),
        "max_trades"  : MAX_TRADES,
        "time_ist"    : datetime.now(IST).strftime("%I:%M %p")
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot is Working!</b>\n"
        f"🧪 Mode      : {'Paper Trading' if PAPER_TRADING else 'Live Trading'}\n"
        f"💰 Capital   : ₹{CAPITAL}\n"
        f"⚠️ Risk      : {RISK_PERCENT}% = ₹{CAPITAL * RISK_PERCENT / 100}\n"
        f"🔴 SL        : {SL_PERCENT}% fixed\n"
        f"🟢 Trailing  : {TRAIL_PERCENT}%\n"
        f"📊 Max Trades: {MAX_TRADES}/day\n"
        f"🔍 Scanning  : tazbul every {SCAN_MINS} mins\n"
        f"🕐 Time IST  : {datetime.now(IST).strftime('%I:%M %p')}"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/scan-now", methods=["GET"])
def scan_now():
    """Manually trigger a scan right now — ignores market hours."""
    print("🔍 Manual scan triggered!")
    stocks = fetch_screener()
    if stocks:
        send(
            f"🔍 <b>MANUAL SCAN RESULT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Screener : tazbul\n"
            f"📈 Stocks   : {len(stocks)} found\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(f"  📌 <b>{s}</b>" for s in stocks)
        )
        return jsonify({"status": "found", "stocks": stocks}), 200
    send("📭 <b>Manual Scan:</b> No stocks found in tazbul screener")
    return jsonify({"status": "no stocks"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "open_trades" : list(open_trades.keys()),
        "closed_today": [t["symbol"] for t in closed_today],
        "trades_today": len(closed_today) + len(open_trades),
        "time_ist"    : datetime.now(IST).strftime("%I:%M %p")
    }), 200


@app.route("/report", methods=["GET"])
def report():
    send_eod()
    return jsonify({"status": "report sent"}), 200


# ══════════════════════════════════════════════════
#  START THREADS ON MODULE LOAD
# ══════════════════════════════════════════════════

print("🚀 Starting Chartink Bot...")
threading.Thread(target=run_scanner,       daemon=True).start()
threading.Thread(target=run_trail_monitor, daemon=True).start()
send(
    f"🟢 <b>Chartink Bot LIVE!</b>\n"
    f"🧪 Mode     : {'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}\n"
    f"💰 Capital  : ₹{CAPITAL}\n"
    f"⚠️ Risk     : {RISK_PERCENT}% = ₹{CAPITAL * RISK_PERCENT / 100}/trade\n"
    f"🔴 SL       : {SL_PERCENT}% fixed\n"
    f"🟢 Trailing : {TRAIL_PERCENT}%\n"
    f"📊 Max      : {MAX_TRADES} trades/day\n"
    f"🔍 Scanner  : every {SCAN_MINS} mins\n"
    f"⏰ Hours    : 9:15 AM – 3:30 PM IST"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
