from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Bot 1 — tazbul screener ───────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
CHAT_ID           = os.environ.get("CHAT_ID", "")
CAPITAL_PER_TRADE = 10000
SL_PERCENT        = 1.0
TP_PERCENT        = 1.5

# ── Bot 2 — TazAmol-Test1 screener ───────────
BOT2_TOKEN        = "8030391810:AAFxJefvbNmdK97VZZQe2VJ9O1477U-Z8Ks"
BOT2_CHAT_ID      = "527293574"
BOT2_SL           = 1.0
BOT2_TP           = 1.0
BOT2_CAPITAL      = 10000

# ── Shared config ─────────────────────────────
PAPER_TRADING     = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT              = int(os.environ.get("PORT", 10000))
MARKET_OPEN       = dtime(9,  15)
MARKET_CLOSE      = dtime(15, 30)
FORCE_EXIT        = dtime(15, 12)

# ── Bot 1 state ───────────────────────────────
open_trades   = {}
closed_today  = []
traded_today  = set()

# ── Bot 2 state ───────────────────────────────
open_trades2  = {}
closed_today2 = []
traded_today2 = set()

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def send(msg, token=None, chat_id=None):
    t = token  or BOT_TOKEN
    c = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{t}/sendMessage"
    try:
        r = requests.post(url,
            data={"chat_id": c, "text": msg, "parse_mode": "HTML"},
            timeout=10)
        print("✅ Telegram sent!" if r.status_code == 200 else f"❌ {r.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

def now_ist():
    return datetime.now(IST).time()

def is_market_hours():
    return MARKET_OPEN <= now_ist() <= MARKET_CLOSE

def time_str():
    return datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")

def get_price_nse(symbol):
    try:
        session = requests.Session()
        headers = {
            "User-Agent"     : "Mozilla/5.0",
            "Accept"         : "*/*",
            "Referer"        : "https://www.nseindia.com",
            "Accept-Language": "en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        r = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            headers=headers, timeout=10)
        return round(float(r.json()["priceInfo"]["lastPrice"]), 2)
    except:
        return None

def get_price_yahoo(symbol):
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        return round(float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]), 2)
    except:
        return None

def get_price(symbol, chartink_price=None):
    if chartink_price:
        try:
            p = round(float(chartink_price), 2)
            if p > 0:
                return p
        except:
            pass
    price = get_price_nse(symbol)
    if price:
        return price
    price = get_price_yahoo(symbol)
    if price:
        return price
    return None

def calculate_trade(symbol, price, sl_pct, tp_pct, capital):
    sl_price   = round(price * (1 - sl_pct / 100), 2)
    tp_price   = round(price * (1 + tp_pct / 100), 2)
    sl_dist    = round(price - sl_price, 2)
    qty        = max(1, int(capital / price))
    risk_amt   = round(sl_dist * qty, 2)
    reward_amt = round((tp_price - price) * qty, 2)
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
    }

def log_trade(trade, exit_price, pnl, reason, logfile):
    exists = os.path.exists(logfile)
    with open(logfile, "a", newline="") as fp:
        w = csv.writer(fp)
        if not exists:
            w.writerow(["date","symbol","entry","exit","qty",
                        "sl","tp","risk","reward","pnl",
                        "result","reason","entry_time","exit_time"])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["tp"],
            trade["risk_amt"], trade["reward_amt"],
            pnl, "WIN" if pnl >= 0 else "LOSS",
            reason, trade["entry_time"], trade["exit_time"]
        ])

# ─────────────────────────────────────────────
#  BOT 1 — tazbul
# ─────────────────────────────────────────────
def open_trade(symbol, price):
    if symbol in open_trades or symbol in traded_today:
        return
    trade = calculate_trade(symbol, price, SL_PERCENT, TP_PERCENT, CAPITAL_PER_TRADE)
    open_trades[symbol] = trade
    traded_today.add(symbol)
    mode = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"📝 <b>{mode} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Screener   : tazbul\n"
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

def close_trade(symbol, exit_price, reason):
    if symbol not in open_trades:
        return
    trade              = open_trades.pop(symbol)
    trade["exit_time"] = time_str()
    pnl                = round((exit_price - trade["entry"]) * trade["qty"], 2)
    result             = "PROFIT" if pnl >= 0 else "LOSS"
    mode               = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"{'✅' if pnl >= 0 else '❌'} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Screener : tazbul\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}  : ₹{abs(pnl)}\n"
        f"📝 Reason   : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['exit_time']}"
    )
    closed_today.append({"symbol": symbol, "pnl": pnl})
    log_trade(trade, exit_price, pnl, reason, "logs/trades.csv")

def check_positions():
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue
        if price >= trade["tp"]:
            close_trade(symbol, price, "🎯 Take Profit Hit")
        elif price <= trade["sl"]:
            close_trade(symbol, price, "🔴 Stop Loss Hit")

def send_eod():
    winners = [t for t in closed_today if t["pnl"] >= 0]
    losers  = [t for t in closed_today if t["pnl"] <  0]
    net     = round(sum(t["pnl"] for t in closed_today), 2)
    icon    = "💚" if net >= 0 else "❤️"
    send(
        f"📋 <b>DAILY P&L — tazbul</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date        : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Total Trades: {len(closed_today)}\n"
        f"✅ Winners     : {len(winners)}\n"
        f"❌ Losers      : {len(losers)}\n"
        f"💚 Gross Profit: ₹{sum(t['pnl'] for t in winners):.2f}\n"
        f"❤️ Gross Loss  : ₹{abs(sum(t['pnl'] for t in losers)):.2f}\n"
        f"{icon} Net P&L    : ₹{net}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    closed_today.clear()
    traded_today.clear()

# ─────────────────────────────────────────────
#  BOT 2 — TazAmol-Test1
# ─────────────────────────────────────────────
def open_trade2(symbol, price):
    if symbol in open_trades2 or symbol in traded_today2:
        return
    trade = calculate_trade(symbol, price, BOT2_SL, BOT2_TP, BOT2_CAPITAL)
    open_trades2[symbol] = trade
    traded_today2.add(symbol)
    mode = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"📝 <b>{mode} TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Screener   : TazAmol-Test1\n"
        f"📌 Stock      : <b>{symbol}</b>\n"
        f"💰 Entry      : ₹{price}\n"
        f"📦 Quantity   : {trade['qty']} shares\n"
        f"🔴 Stop Loss  : ₹{trade['sl']} ({BOT2_SL}%)\n"
        f"🟢 Take Profit: ₹{trade['tp']} ({BOT2_TP}%)\n"
        f"💵 Capital    : ₹{trade['capital_used']}\n"
        f"⚠️ Risk       : ₹{trade['risk_amt']}\n"
        f"🎯 Reward     : ₹{trade['reward_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['entry_time']}",
        token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
    )

def close_trade2(symbol, exit_price, reason):
    if symbol not in open_trades2:
        return
    trade              = open_trades2.pop(symbol)
    trade["exit_time"] = time_str()
    pnl                = round((exit_price - trade["entry"]) * trade["qty"], 2)
    result             = "PROFIT" if pnl >= 0 else "LOSS"
    mode               = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"{'✅' if pnl >= 0 else '❌'} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Screener : TazAmol-Test1\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}  : ₹{abs(pnl)}\n"
        f"📝 Reason   : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['exit_time']}",
        token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
    )
    closed_today2.append({"symbol": symbol, "pnl": pnl})
    log_trade(trade, exit_price, pnl, reason, "logs/trades2.csv")

def check_positions2():
    for symbol in list(open_trades2.keys()):
        trade = open_trades2.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue
        if price >= trade["tp"]:
            close_trade2(symbol, price, "🎯 Take Profit Hit")
        elif price <= trade["sl"]:
            close_trade2(symbol, price, "🔴 Stop Loss Hit")

def send_eod2():
    winners = [t for t in closed_today2 if t["pnl"] >= 0]
    losers  = [t for t in closed_today2 if t["pnl"] <  0]
    net     = round(sum(t["pnl"] for t in closed_today2), 2)
    icon    = "💚" if net >= 0 else "❤️"
    send(
        f"📋 <b>DAILY P&L — TazAmol-Test1</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date        : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Total Trades: {len(closed_today2)}\n"
        f"✅ Winners     : {len(winners)}\n"
        f"❌ Losers      : {len(losers)}\n"
        f"💚 Gross Profit: ₹{sum(t['pnl'] for t in winners):.2f}\n"
        f"❤️ Gross Loss  : ₹{abs(sum(t['pnl'] for t in losers)):.2f}\n"
        f"{icon} Net P&L    : ₹{net}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}",
        token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
    )
    closed_today2.clear()
    traded_today2.clear()

# ─────────────────────────────────────────────
#  MONITOR — runs both bots
# ─────────────────────────────────────────────
def run_monitor():
    eod_sent = False
    print("📈 Monitor started — every 1 min")
    while True:
        try:
            t = now_ist()
            # Force exit both bots
            if t >= FORCE_EXIT:
                if open_trades:
                    send("⏰ <b>3:12 PM — Force closing tazbul positions!</b>")
                    for sym in list(open_trades.keys()):
                        p = get_price(sym) or open_trades[sym]["entry"]
                        close_trade(sym, p, "⏰ Force Exit 3:12 PM")
                if open_trades2:
                    send("⏰ <b>3:12 PM — Force closing TazAmol-Test1 positions!</b>",
                         token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID)
                    for sym in list(open_trades2.keys()):
                        p = get_price(sym) or open_trades2[sym]["entry"]
                        close_trade2(sym, p, "⏰ Force Exit 3:12 PM")
            # EOD report both bots
            if t >= MARKET_CLOSE and not eod_sent:
                send_eod()
                send_eod2()
                eod_sent = True
            if t < dtime(9, 0):
                eod_sent = False
            # Monitor positions
            if is_market_hours():
                if open_trades:
                    check_positions()
                if open_trades2:
                    check_positions2()
        except Exception as e:
            print(f"❌ Monitor error: {e}")
        time.sleep(60)

# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────
@app.route("/alert", methods=["POST"])
def receive_alert():
    """Webhook for tazbul screener"""
    data    = request.json or request.form.to_dict()
    raw_stk = data.get("stocks", "")
    raw_prc = data.get("trigger_prices", "")
    stocks  = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices  = [p.strip() for p in raw_prc.split(",") if p.strip()]
    if not stocks:
        return jsonify({"status": "no stocks"}), 400
    results = []
    for i, symbol in enumerate(stocks):
        if symbol in traded_today or symbol in open_trades:
            results.append({"symbol": symbol, "status": "skip"})
            continue
        chartink_price = prices[i] if i < len(prices) else None
        price          = get_price(symbol, chartink_price)
        if not price:
            send(f"❌ <b>Price fetch failed: {symbol}</b>")
            results.append({"symbol": symbol, "status": "price failed"})
            continue
        open_trade(symbol, price)
        results.append({"symbol": symbol, "status": "entered", "price": price})
    return jsonify({"status": "processed", "results": results}), 200

@app.route("/alert2", methods=["POST"])
def receive_alert2():
    """Webhook for TazAmol-Test1 screener"""
    data    = request.json or request.form.to_dict()
    raw_stk = data.get("stocks", "")
    raw_prc = data.get("trigger_prices", "")
    stocks  = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices  = [p.strip() for p in raw_prc.split(",") if p.strip()]
    if not stocks:
        return jsonify({"status": "no stocks"}), 400
    results = []
    for i, symbol in enumerate(stocks):
        if symbol in traded_today2 or symbol in open_trades2:
            results.append({"symbol": symbol, "status": "skip"})
            continue
        chartink_price = prices[i] if i < len(prices) else None
        price          = get_price(symbol, chartink_price)
        if not price:
            send(f"❌ <b>Price fetch failed: {symbol}</b>",
                 token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID)
            results.append({"symbol": symbol, "status": "price failed"})
            continue
        open_trade2(symbol, price)
        results.append({"symbol": symbol, "status": "entered", "price": price})
    return jsonify({"status": "processed", "results": results}), 200

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"      : "🟢 Running",
        "time_ist"    : time_str(),
        "market_open" : is_market_hours(),
        "bot1_tazbul" : {
            "open"   : list(open_trades.keys()),
            "closed" : len(closed_today),
        },
        "bot2_tazamol": {
            "open"   : list(open_trades2.keys()),
            "closed" : len(closed_today2),
        },
    }), 200

@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot 1 (tazbul) Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode  : {'Paper' if PAPER_TRADING else 'Live'}\n"
        f"📡 Signal: Chartink tazbul\n"
        f"💰 Capital: ₹{CAPITAL_PER_TRADE}\n"
        f"🔴 SL    : {SL_PERCENT}%\n"
        f"🟢 TP    : {TP_PERCENT}%\n"
        f"🕐 Time  : {time_str()}"
    )
    send(
        f"✅ <b>Bot 2 (TazAmol-Test1) Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode  : {'Paper' if PAPER_TRADING else 'Live'}\n"
        f"📡 Signal: TazAmol-Test1\n"
        f"💰 Capital: ₹{BOT2_CAPITAL}\n"
        f"🔴 SL    : {BOT2_SL}%\n"
        f"🟢 TP    : {BOT2_TP}%\n"
        f"🕐 Time  : {time_str()}",
        token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
    )
    return jsonify({"status": "both test messages sent"}), 200

@app.route("/report", methods=["GET"])
def report():
    send_eod()
    send_eod2()
    return jsonify({"status": "both reports sent"}), 200

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "bot1_tazbul" : {
            "open_trades" : list(open_trades.keys()),
            "traded_today": list(traded_today),
            "closed_today": len(closed_today),
            "net_pnl"     : round(sum(t["pnl"] for t in closed_today), 2),
        },
        "bot2_tazamol": {
            "open_trades" : list(open_trades2.keys()),
            "traded_today": list(traded_today2),
            "closed_today": len(closed_today2),
            "net_pnl"     : round(sum(t["pnl"] for t in closed_today2), 2),
        },
    }), 200

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
def build_table_rows(trades_dict):
    rows = ""
    for sym, t in trades_dict.items():
        rows += f"""<tr>
          <td><b>{sym}</b></td>
          <td>&#8377;{t['entry']}</td>
          <td>{t['qty']}</td>
          <td style="color:#ff4d4d;">&#8377;{t['sl']}</td>
          <td style="color:#00c896;">&#8377;{t['tp']}</td>
          <td>&#8377;{t['capital_used']}</td>
          <td>{t['entry_time']}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No open positions</td></tr>'
    return rows

def build_closed_rows(today_closed):
    rows = ""
    for r in reversed(today_closed):
        pnl_val = float(r["pnl"])
        color   = "#00c896" if pnl_val >= 0 else "#ff4d4d"
        sign    = "+" if pnl_val >= 0 else ""
        rows += f"""<tr>
          <td><b>{r['symbol']}</b></td>
          <td>&#8377;{r['entry']}</td>
          <td>&#8377;{r['exit']}</td>
          <td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r['reason']}</td>
          <td>{r['exit_time']}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No closed trades today</td></tr>'
    return rows

def build_history_rows(history):
    rows = ""
    for r in reversed(history[-50:]):
        pnl_val = float(r["pnl"])
        color   = "#00c896" if pnl_val >= 0 else "#ff4d4d"
        sign    = "+" if pnl_val >= 0 else ""
        rows += f"""<tr>
          <td>{r['date']}</td>
          <td><b>{r['symbol']}</b></td>
          <td>&#8377;{r['entry']}</td>
          <td>&#8377;{r['exit']}</td>
          <td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r.get('reason','')}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No trade history yet</td></tr>'
    return rows

def load_csv(path):
    history = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                history.append(row)
    return history

def stats(today_closed):
    winners      = [r for r in today_closed if r.get("result") == "WIN"]
    losers       = [r for r in today_closed if r.get("result") == "LOSS"]
    net_pnl      = round(sum(float(r["pnl"]) for r in today_closed), 2)
    gross_profit = round(sum(float(r["pnl"]) for r in winners), 2)
    gross_loss   = round(abs(sum(float(r["pnl"]) for r in losers)), 2)
    win_rate     = round((len(winners) / len(today_closed) * 100) if today_closed else 0, 1)
    return winners, losers, net_pnl, gross_profit, gross_loss, win_rate

@app.route("/dashboard", methods=["GET"])
def dashboard():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    mode_label = "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading"
    mkt_status = "🟢 Market Open" if is_market_hours() else "🔴 Market Closed"

    # ── Bot 1 data ────────────────────────────
    hist1         = load_csv("logs/trades.csv")
    today1        = [r for r in hist1 if r.get("date") == today_str]
    w1,l1,net1,gp1,gl1,wr1 = stats(today1)
    open_rows1    = build_table_rows(open_trades)
    closed_rows1  = build_closed_rows(today1)
    history_rows1 = build_history_rows(hist1)

    # ── Bot 2 data ────────────────────────────
    hist2         = load_csv("logs/trades2.csv")
    today2        = [r for r in hist2 if r.get("date") == today_str]
    w2,l2,net2,gp2,gl2,wr2 = stats(today2)
    open_rows2    = build_table_rows(open_trades2)
    closed_rows2  = build_closed_rows(today2)
    history_rows2 = build_history_rows(hist2)

    pnl_color1 = "#00c896" if net1 >= 0 else "#ff4d4d"
    pnl_color2 = "#00c896" if net2 >= 0 else "#ff4d4d"

    HTML_STYLE = """
    * { box-sizing:border-box; margin:0; padding:0; }
    body { background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; }
    .topbar { background:#161b22; border-bottom:1px solid #30363d; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
    .topbar-left { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .bot-title { font-size:1.1rem; font-weight:700; color:#58a6ff; }
    .badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600; background:#21262d; color:#8b949e; border:1px solid #30363d; }
    .topbar-right { text-align:right; font-size:12px; color:#8b949e; }
    .container { padding:20px; }
    .screener-tabs { display:flex; gap:8px; margin-bottom:20px; }
    .screener-btn { background:#161b22; border:2px solid #30363d; border-radius:10px; color:#8b949e; padding:10px 24px; font-size:14px; font-weight:700; cursor:pointer; font-family:inherit; transition:all .2s; }
    .screener-btn:hover { border-color:#58a6ff; color:#c9d1d9; }
    .screener-btn.active { border-color:#58a6ff; color:#58a6ff; background:#1c2128; }
    .screener-panel { display:none; }
    .screener-panel.active { display:block; }
    .stat-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:16px; }
    .stat-card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 10px; text-align:center; }
    .stat-label { font-size:11px; color:#8b949e; margin-bottom:6px; }
    .stat-value { font-size:1.7rem; font-weight:700; }
    .pnl-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }
    .tabs { display:flex; gap:0; border-bottom:1px solid #30363d; margin-bottom:16px; }
    .tab-btn { background:none; border:none; border-bottom:3px solid transparent; color:#8b949e; padding:10px 20px; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit; transition:all .2s; white-space:nowrap; }
    .tab-btn:hover { color:#c9d1d9; }
    .tab-btn.active { color:#58a6ff; border-bottom-color:#58a6ff; }
    .tab-pane { display:none; }
    .tab-pane.active { display:block; }
    .table-wrap { background:#161b22; border:1px solid #30363d; border-radius:10px; overflow:hidden; overflow-x:auto; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th { background:#21262d; color:#8b949e; font-weight:500; padding:10px 14px; text-align:left; border-bottom:1px solid #30363d; white-space:nowrap; }
    td { padding:10px 14px; border-bottom:1px solid #21262d; vertical-align:middle; white-space:nowrap; }
    tr:last-child td { border-bottom:none; }
    tr:hover td { background:#1c2128; }
    .hint { font-size:12px; color:#8b949e; margin-bottom:8px; }
    .info-bar { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:8px 14px; margin-bottom:16px; font-size:12px; color:#8b949e; display:flex; gap:20px; flex-wrap:wrap; }
    .info-bar span { color:#c9d1d9; font-weight:600; }
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="30"/>
  <title>Chartink Bot Dashboard</title>
  <style>{HTML_STYLE}</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="bot-title">&#128202; Chartink Bot Dashboard</span>
    <span class="badge">{mode_label}</span>
    <span class="badge">{mkt_status}</span>
  </div>
  <div class="topbar-right">
    <div>&#128336; {time_str()}</div>
    <div>&#8635; Auto-refresh every 30s</div>
  </div>
</div>

<div class="container">

  <!-- Screener Selector -->
  <div class="screener-tabs">
    <button class="screener-btn active" onclick="showScreener('s1',this)">
      &#128209; tazbul &nbsp;|&nbsp; Open: {len(open_trades)} &nbsp;|&nbsp; P&amp;L: &#8377;{net1}
    </button>
    <button class="screener-btn" onclick="showScreener('s2',this)">
      &#128209; TazAmol-Test1 &nbsp;|&nbsp; Open: {len(open_trades2)} &nbsp;|&nbsp; P&amp;L: &#8377;{net2}
    </button>
  </div>

  <!-- ── SCREENER 1 — tazbul ─────────────── -->
  <div id="s1" class="screener-panel active">
    <div class="info-bar">
      Screener: <span>tazbul</span> &nbsp;|&nbsp;
      SL: <span>{SL_PERCENT}%</span> &nbsp;|&nbsp;
      TP: <span>{TP_PERCENT}%</span> &nbsp;|&nbsp;
      Capital/Trade: <span>&#8377;{CAPITAL_PER_TRADE}</span> &nbsp;|&nbsp;
      Webhook: <span>/alert</span>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-label">Open Positions</div><div class="stat-value" style="color:#f0b429;">{len(open_trades)}</div></div>
      <div class="stat-card"><div class="stat-label">Trades Today</div><div class="stat-value" style="color:#58a6ff;">{len(today1)}</div></div>
      <div class="stat-card"><div class="stat-label">Winners</div><div class="stat-value" style="color:#00c896;">{len(w1)}</div></div>
      <div class="stat-card"><div class="stat-label">Losers</div><div class="stat-value" style="color:#ff4d4d;">{len(l1)}</div></div>
      <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:#a78bfa;">{wr1}%</div></div>
      <div class="stat-card"><div class="stat-label">Net P&amp;L</div><div class="stat-value" style="color:{pnl_color1};">&#8377;{net1}</div></div>
    </div>
    <div class="pnl-grid">
      <div class="stat-card"><div class="stat-label">Gross Profit</div><div class="stat-value" style="color:#00c896;">&#8377;{gp1}</div></div>
      <div class="stat-card"><div class="stat-label">Gross Loss</div><div class="stat-value" style="color:#ff4d4d;">&#8377;{gl1}</div></div>
      <div class="stat-card"><div class="stat-label">Capital / Trade</div><div class="stat-value" style="color:#58a6ff;">&#8377;{CAPITAL_PER_TRADE}</div></div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('s1','open',this)">Open Positions ({len(open_trades)})</button>
      <button class="tab-btn" onclick="showTab('s1','closed',this)">Today's Trades ({len(today1)})</button>
      <button class="tab-btn" onclick="showTab('s1','history',this)">Full History</button>
    </div>
    <div id="s1-open" class="tab-pane active">
      <div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry Price</th><th>Qty</th><th>Stop Loss</th><th>Take Profit</th><th>Capital Used</th><th>Entry Time</th></tr></thead>
        <tbody>{open_rows1}</tbody>
      </table></div>
    </div>
    <div id="s1-closed" class="tab-pane">
      <div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry Price</th><th>Exit Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Exit Time</th></tr></thead>
        <tbody>{closed_rows1}</tbody>
      </table></div>
    </div>
    <div id="s1-history" class="tab-pane">
      <div class="hint">Showing last 50 trades</div>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Stock</th><th>Entry Price</th><th>Exit Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
        <tbody>{history_rows1}</tbody>
      </table></div>
    </div>
  </div>

  <!-- ── SCREENER 2 — TazAmol-Test1 ─────── -->
  <div id="s2" class="screener-panel">
    <div class="info-bar">
      Screener: <span>TazAmol-Test1</span> &nbsp;|&nbsp;
      SL: <span>{BOT2_SL}%</span> &nbsp;|&nbsp;
      TP: <span>{BOT2_TP}%</span> &nbsp;|&nbsp;
      Capital/Trade: <span>&#8377;{BOT2_CAPITAL}</span> &nbsp;|&nbsp;
      Webhook: <span>/alert2</span>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-label">Open Positions</div><div class="stat-value" style="color:#f0b429;">{len(open_trades2)}</div></div>
      <div class="stat-card"><div class="stat-label">Trades Today</div><div class="stat-value" style="color:#58a6ff;">{len(today2)}</div></div>
      <div class="stat-card"><div class="stat-label">Winners</div><div class="stat-value" style="color:#00c896;">{len(w2)}</div></div>
      <div class="stat-card"><div class="stat-label">Losers</div><div class="stat-value" style="color:#ff4d4d;">{len(l2)}</div></div>
      <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:#a78bfa;">{wr2}%</div></div>
      <div class="stat-card"><div class="stat-label">Net P&amp;L</div><div class="stat-value" style="color:{pnl_color2};">&#8377;{net2}</div></div>
    </div>
    <div class="pnl-grid">
      <div class="stat-card"><div class="stat-label">Gross Profit</div><div class="stat-value" style="color:#00c896;">&#8377;{gp2}</div></div>
      <div class="stat-card"><div class="stat-label">Gross Loss</div><div class="stat-value" style="color:#ff4d4d;">&#8377;{gl2}</div></div>
      <div class="stat-card"><div class="stat-label">Capital / Trade</div><div class="stat-value" style="color:#58a6ff;">&#8377;{BOT2_CAPITAL}</div></div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('s2','open',this)">Open Positions ({len(open_trades2)})</button>
      <button class="tab-btn" onclick="showTab('s2','closed',this)">Today's Trades ({len(today2)})</button>
      <button class="tab-btn" onclick="showTab('s2','history',this)">Full History</button>
    </div>
    <div id="s2-open" class="tab-pane active">
      <div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry Price</th><th>Qty</th><th>Stop Loss</th><th>Take Profit</th><th>Capital Used</th><th>Entry Time</th></tr></thead>
        <tbody>{open_rows2}</tbody>
      </table></div>
    </div>
    <div id="s2-closed" class="tab-pane">
      <div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry Price</th><th>Exit Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Exit Time</th></tr></thead>
        <tbody>{closed_rows2}</tbody>
      </table></div>
    </div>
    <div id="s2-history" class="tab-pane">
      <div class="hint">Showing last 50 trades</div>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Stock</th><th>Entry Price</th><th>Exit Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
        <tbody>{history_rows2}</tbody>
      </table></div>
    </div>
  </div>

</div>

<script>
function showScreener(id, btn) {{
  document.querySelectorAll('.screener-panel').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('.screener-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
function showTab(screener, name, btn) {{
  var prefix = screener + '-';
  document.querySelectorAll('#' + screener + ' .tab-pane').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('#' + screener + ' .tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.getElementById(prefix + name).classList.add('active');
  btn.classList.add('active');
}}
</script>

</body>
</html>"""
    return html

# ─────────────────────────────────────────────

print("🚀 Starting Chartink Bot (Dual Screener)...")
threading.Thread(target=run_monitor, daemon=True).start()
send(
    f"🟢 <b>Bot 1 LIVE — tazbul</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"🧪 Mode    : {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
    f"💰 Capital : ₹{CAPITAL_PER_TRADE}\n"
    f"🔴 SL      : {SL_PERCENT}%\n"
    f"🟢 TP      : {TP_PERCENT}%\n"
    f"📡 Webhook : /alert"
)
send(
    f"🟢 <b>Bot 2 LIVE — TazAmol-Test1</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"🧪 Mode    : {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
    f"💰 Capital : ₹{BOT2_CAPITAL}\n"
    f"🔴 SL      : {BOT2_SL}%\n"
    f"🟢 TP      : {BOT2_TP}%\n"
    f"📡 Webhook : /alert2",
    token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
