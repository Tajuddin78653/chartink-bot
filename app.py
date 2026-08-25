from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv, json

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
CHAT_ID           = os.environ.get("CHAT_ID", "")
CAPITAL_PER_TRADE = 10000
SL_PERCENT        = 1.0
TP_PERCENT        = 1.5
PAPER_TRADING     = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT              = int(os.environ.get("PORT", 10000))
BROKER            = os.environ.get("BROKER", "dhan")   # "dhan" (more brokers later)
MARKET_OPEN       = dtime(9,  15)
MARKET_CLOSE      = dtime(15, 30)
FORCE_EXIT        = dtime(15, 12)

# ─────────────────────────────────────────────
#  STATE STORE — Redis with in-memory fallback
#  Behaviour is 100% identical when Redis is
#  absent; existing paper/live flow is unchanged.
# ─────────────────────────────────────────────
class StateStore:
    """
    Thin wrapper around Redis for the three state buckets.
    Falls back silently to plain dicts/sets when Redis is
    not configured or unreachable — zero impact on trading.
    """
    _OPEN_KEY   = "chartink:open_trades"
    _CLOSED_KEY = "chartink:closed_today"
    _TRADED_KEY = "chartink:traded_today"

    def __init__(self):
        self._redis  = None
        self._mem_open   = {}
        self._mem_closed = []
        self._mem_traded = set()
        redis_url = os.environ.get("REDIS_URL", "")
        if redis_url:
            try:
                import redis as _redis
                client = _redis.from_url(redis_url, decode_responses=True, socket_timeout=3)
                client.ping()          # fail fast if unreachable
                self._redis = client
                print("✅ Redis connected — state will persist across restarts")
            except Exception as e:
                print(f"⚠️  Redis unavailable ({e}) — using in-memory state")

    # ── open_trades (dict) ───────────────────
    def get_open_trades(self):
        if self._redis:
            raw = self._redis.get(self._OPEN_KEY)
            return json.loads(raw) if raw else {}
        return self._mem_open

    def set_open_trades(self, data: dict):
        if self._redis:
            self._redis.set(self._OPEN_KEY, json.dumps(data))
        else:
            self._mem_open = data

    # ── closed_today (list) ──────────────────
    def get_closed_today(self):
        if self._redis:
            raw = self._redis.get(self._CLOSED_KEY)
            return json.loads(raw) if raw else []
        return self._mem_closed

    def set_closed_today(self, data: list):
        if self._redis:
            self._redis.set(self._CLOSED_KEY, json.dumps(data))
        else:
            self._mem_closed = data

    # ── traded_today (set) ───────────────────
    def get_traded_today(self):
        if self._redis:
            return set(self._redis.smembers(self._TRADED_KEY))
        return self._mem_traded

    def add_traded_today(self, symbol: str):
        if self._redis:
            self._redis.sadd(self._TRADED_KEY, symbol)
        else:
            self._mem_traded.add(symbol)

    def clear_traded_today(self):
        if self._redis:
            self._redis.delete(self._TRADED_KEY)
        else:
            self._mem_traded.clear()

    def clear_day(self):
        """Reset all daily counters — called at EOD."""
        self.set_closed_today([])
        self.clear_traded_today()


_state = StateStore()

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url,
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
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
                print(f"✅ Chartink price {symbol}: ₹{p}")
                return p
        except:
            pass
    price = get_price_nse(symbol)
    if price:
        print(f"✅ NSE price {symbol}: ₹{price}")
        return price
    price = get_price_yahoo(symbol)
    if price:
        print(f"✅ Yahoo price {symbol}: ₹{price}")
        return price
    print(f"❌ Price failed: {symbol}")
    return None

# ─────────────────────────────────────────────
#  BROKER CHARGES — Dhan Equity Intraday (MIS)
#  Both entry + exit orders are counted.
#  Dhan: ₹20 per order OR 0.03% of turnover,
#        whichever is LOWER — applied twice.
#  Plus STT (0.025% on sell side), exchange txn
#  charge (0.00345%), SEBI (₹10/crore), GST 18%.
# ─────────────────────────────────────────────
def calc_charges(entry_price, exit_price, qty):
    """Return total Dhan intraday charges for one round-trip trade."""
    buy_turnover  = entry_price * qty
    sell_turnover = exit_price  * qty
    total_turnover = buy_turnover + sell_turnover

    # Brokerage: min(₹20, 0.03% of turnover) per order — 2 orders (buy + sell)
    brok_entry = min(20.0, buy_turnover  * 0.0003)
    brok_exit  = min(20.0, sell_turnover * 0.0003)
    brokerage  = round(brok_entry + brok_exit, 2)

    # STT: 0.025% on sell-side turnover (intraday)
    stt = round(sell_turnover * 0.00025, 2)

    # Exchange transaction charge: 0.00345% of total turnover (NSE)
    exc = round(total_turnover * 0.0000345, 2)

    # SEBI charges: ₹10 per crore of total turnover
    sebi = round(total_turnover * 0.000001, 2)

    # Subtotal before GST
    sub = brokerage + stt + exc + sebi

    # GST: 18% on (brokerage + exc + sebi)  — NOT on STT
    gst = round((brokerage + exc + sebi) * 0.18, 2)

    total = round(sub + gst, 2)
    return total

def calculate(symbol, price):
    sl_price   = round(price * (1 - SL_PERCENT / 100), 2)
    tp_price   = round(price * (1 + TP_PERCENT / 100), 2)
    sl_dist    = round(price - sl_price, 2)
    qty        = max(1, int(CAPITAL_PER_TRADE / price))
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
        "paper"       : PAPER_TRADING,
    }

def open_trade(symbol, price):
    open_trades  = _state.get_open_trades()
    traded_today = _state.get_traded_today()
    if symbol in open_trades or symbol in traded_today:
        print(f"⚠️ Skip {symbol} — already open or traded today")
        return
    trade = calculate(symbol, price)
    open_trades[symbol] = trade
    _state.set_open_trades(open_trades)
    _state.add_traded_today(symbol)
    mode = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
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
    open_trades = _state.get_open_trades()
    if symbol not in open_trades:
        return
    trade = open_trades.pop(symbol)
    _state.set_open_trades(open_trades)
    trade["exit_time"] = time_str()
    gross_pnl = round((exit_price - trade["entry"]) * trade["qty"], 2)
    charges   = calc_charges(trade["entry"], exit_price, trade["qty"])
    net_pnl   = round(gross_pnl - charges, 2)
    result    = "PROFIT" if net_pnl >= 0 else "LOSS"
    mode      = "🧪 PAPER" if PAPER_TRADING else "⚡ LIVE"
    send(
        f"{'✅' if net_pnl >= 0 else '❌'} <b>{mode} EXIT — {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Stock    : <b>{symbol}</b>\n"
        f"💰 Entry    : ₹{trade['entry']}\n"
        f"🚪 Exit     : ₹{exit_price}\n"
        f"📦 Qty      : {trade['qty']} shares\n"
        f"💹 Gross P&L: {'+'if gross_pnl>=0 else ''}₹{gross_pnl}\n"
        f"🏦 Charges  : -₹{charges} (Dhan)\n"
        f"{'💚' if net_pnl >= 0 else '❤️'} Net P&L  : {'+'if net_pnl>=0 else ''}₹{net_pnl}\n"
        f"📝 Reason   : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['exit_time']}"
    )
    closed_today = _state.get_closed_today()
    closed_today.append({"symbol": symbol, "pnl": net_pnl, "charges": charges, "gross_pnl": gross_pnl})
    _state.set_closed_today(closed_today)
    log_trade(trade, exit_price, gross_pnl, charges, net_pnl, reason)

def log_trade(trade, exit_price, gross_pnl, charges, net_pnl, reason):
    f      = "logs/trades.csv"
    exists = os.path.exists(f)
    with open(f, "a", newline="") as fp:
        w = csv.writer(fp)
        if not exists:
            w.writerow(["date","symbol","entry","exit","qty",
                        "sl","tp","risk","reward","gross_pnl",
                        "charges","net_pnl",
                        "result","reason","entry_time","exit_time"])
        w.writerow([
            datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"], trade["entry"], exit_price,
            trade["qty"], trade["sl"], trade["tp"],
            trade["risk_amt"], trade["reward_amt"],
            gross_pnl, charges, net_pnl,
            "WIN" if net_pnl >= 0 else "LOSS",
            reason, trade["entry_time"], trade["exit_time"]
        ])

def check_positions():
    open_trades = _state.get_open_trades()
    for symbol in list(open_trades.keys()):
        trade = open_trades.get(symbol)
        if not trade:
            continue
        price = get_price(symbol)
        if not price:
            continue
        print(f"📈 {symbol}: ₹{price} SL:₹{trade['sl']} TP:₹{trade['tp']}")
        if price >= trade["tp"]:
            close_trade(symbol, price, "🎯 Take Profit Hit")
        elif price <= trade["sl"]:
            close_trade(symbol, price, "🔴 Stop Loss Hit")

def send_eod():
    closed_today = _state.get_closed_today()
    winners      = [t for t in closed_today if t["pnl"] >= 0]
    losers       = [t for t in closed_today if t["pnl"] <  0]
    gross        = round(sum(t.get("gross_pnl", t["pnl"]) for t in closed_today), 2)
    total_chg    = round(sum(t.get("charges", 0) for t in closed_today), 2)
    net          = round(sum(t["pnl"] for t in closed_today), 2)
    icon         = "💚" if net >= 0 else "❤️"
    gross_win    = round(sum(t.get("gross_pnl", t["pnl"]) for t in winners), 2)
    gross_loss   = round(abs(sum(t.get("gross_pnl", t["pnl"]) for t in losers)), 2)
    send(
        f"📋 <b>DAILY P&L SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Date         : {datetime.now(IST).strftime('%d %b %Y')}\n"
        f"📊 Total Trades : {len(closed_today)}\n"
        f"✅ Winners      : {len(winners)}\n"
        f"❌ Losers       : {len(losers)}\n"
        f"💚 Gross Profit : ₹{gross_win:.2f}\n"
        f"❤️ Gross Loss   : ₹{gross_loss:.2f}\n"
        f"💹 Gross P&L    : {'+'if gross>=0 else ''}₹{gross:.2f}\n"
        f"🏦 Total Charges: -₹{total_chg:.2f} (Dhan)\n"
        f"{icon} Net P&L     : {'+'if net>=0 else ''}₹{net:.2f}\n"
        f"📈 Stocks       : {', '.join(t['symbol'] for t in closed_today)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 {'Paper Trading' if PAPER_TRADING else 'Live Trading'}"
    )
    _state.clear_day()
    print("📋 EOD done — counters reset")

def run_monitor():
    eod_sent = False
    print("📈 Monitor started — every 1 min")
    while True:
        try:
            t = now_ist()
            open_trades = _state.get_open_trades()
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
            if is_market_hours() and _state.get_open_trades():
                check_positions()
        except Exception as e:
            print(f"❌ Monitor error: {e}")
        time.sleep(60)

@app.route("/alert", methods=["POST"])
def receive_alert():
    data    = request.json or request.form.to_dict()
    raw_stk = data.get("stocks", "")
    raw_prc = data.get("trigger_prices", "")
    stocks  = [s.strip().upper() for s in raw_stk.split(",") if s.strip()]
    prices  = [p.strip() for p in raw_prc.split(",") if p.strip()]
    if not stocks:
        return jsonify({"status": "no stocks"}), 400
    print(f"📊 Alert: {stocks}")
    open_trades  = _state.get_open_trades()
    traded_today = _state.get_traded_today()
    results = []
    for i, symbol in enumerate(stocks):
        if symbol in traded_today:
            results.append({"symbol": symbol, "status": "already traded"})
            continue
        if symbol in open_trades:
            results.append({"symbol": symbol, "status": "already open"})
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

@app.route("/", methods=["GET"])
def home():
    open_trades  = _state.get_open_trades()
    traded_today = _state.get_traded_today()
    closed_today = _state.get_closed_today()
    return jsonify({
        "status"           : "🟢 Running",
        "mode"             : "🧪 Paper" if PAPER_TRADING else "⚡ Live",
        "time_ist"         : time_str(),
        "market_open"      : is_market_hours(),
        "open_positions"   : list(open_trades.keys()),
        "total_open"       : len(open_trades),
        "traded_today"     : list(traded_today),
        "closed_today"     : len(closed_today),
        "capital_per_trade": CAPITAL_PER_TRADE,
        "sl_percent"       : SL_PERCENT,
        "tp_percent"       : TP_PERCENT,
        "force_exit"       : "3:12 PM",
        "eod_report"       : "3:30 PM",
    }), 200

@app.route("/test", methods=["GET"])
def test():
    send(
        f"✅ <b>Bot is Working!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Mode          : {'Paper' if PAPER_TRADING else 'Live'}\n"
        f"📡 Signal        : Chartink tazbul\n"
        f"💰 Capital/Trade : ₹{CAPITAL_PER_TRADE}\n"
        f"🔴 Stop Loss     : {SL_PERCENT}%\n"
        f"🟢 Take Profit   : {TP_PERCENT}%\n"
        f"🔁 Repeat Trade  : ❌ No\n"
        f"📊 Max Positions : Unlimited\n"
        f"🚪 Force Exit    : 3:12 PM\n"
        f"📋 EOD Report    : 3:30 PM\n"
        f"🕐 Time IST      : {time_str()}"
    )
    return jsonify({"status": "test sent"}), 200

@app.route("/status", methods=["GET"])
def status():
    open_trades  = _state.get_open_trades()
    traded_today = _state.get_traded_today()
    closed_today = _state.get_closed_today()
    return jsonify({
        "open_trades" : list(open_trades.keys()),
        "traded_today": list(traded_today),
        "closed_today": [{"symbol": t["symbol"], "pnl": t["pnl"]}
                         for t in closed_today],
        "net_pnl"     : round(sum(t["pnl"] for t in closed_today), 2),
    }), 200

@app.route("/report", methods=["GET"])
def report():
    send_eod()
    return jsonify({"status": "report sent"}), 200

# ─────────────────────────────────────────────
#  LIVE PRICES — used by dashboard JS
# ─────────────────────────────────────────────
@app.route("/prices", methods=["GET"])
def prices():
    """Return current market price for every open position."""
    open_trades = _state.get_open_trades()
    result = {}
    for sym, t in open_trades.items():
        price = get_price(sym)
        if price:
            unreal = round((price - t["entry"]) * t["qty"], 2)
            result[sym] = {"price": price, "unrealised_pnl": unreal}
    return jsonify(result), 200

# ─────────────────────────────────────────────
#  MANUAL CLOSE — dashboard close button
# ─────────────────────────────────────────────
@app.route("/close/<symbol>", methods=["POST"])
def manual_close(symbol):
    symbol = symbol.upper()
    open_trades = _state.get_open_trades()
    if symbol not in open_trades:
        return jsonify({"status": "not found"}), 404
    price = get_price(symbol) or open_trades[symbol]["entry"]
    close_trade(symbol, price, "🖱️ Manual Close (Dashboard)")
    return jsonify({"status": "closed", "symbol": symbol, "price": price}), 200

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard", methods=["GET"])
def dashboard():
    open_trades  = _state.get_open_trades()
    closed_today = _state.get_closed_today()
    history  = []
    csv_path = "logs/trades.csv"
    if os.path.exists(csv_path):
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                history.append(row)

    today_str    = datetime.now(IST).strftime("%Y-%m-%d")
    today_closed = [r for r in history if r.get("date") == today_str]

    total_trades = len(today_closed)
    winners      = [r for r in today_closed if r.get("result") == "WIN"]
    losers       = [r for r in today_closed if r.get("result") == "LOSS"]
    # net_pnl uses net_pnl column if present, else pnl (backwards compat)
    net_pnl      = round(sum(float(r.get("net_pnl", r["pnl"])) for r in today_closed), 2)
    gross_profit = round(sum(float(r.get("gross_pnl", r["pnl"])) for r in winners), 2)
    gross_loss   = round(abs(sum(float(r.get("gross_pnl", r["pnl"])) for r in losers)), 2)
    win_rate     = round((len(winners) / total_trades * 100) if total_trades else 0, 1)
    open_count   = len(open_trades)
    mode_label   = "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading"
    pnl_color    = "#00c896" if net_pnl >= 0 else "#ff4d4d"
    mkt_status   = "🟢 Market Open" if is_market_hours() else "🔴 Market Closed"

    # ── daily P&L chart data (last 14 trading days) ──
    from collections import defaultdict
    day_pnl = defaultdict(float)
    for r in history:
        day_pnl[r["date"]] += float(r["pnl"])
    sorted_days  = sorted(day_pnl.keys())[-14:]
    chart_labels = json.dumps(sorted_days)
    chart_values = json.dumps([round(day_pnl[d], 2) for d in sorted_days])
    chart_colors = json.dumps(["#00c896" if day_pnl[d] >= 0 else "#ff4d4d" for d in sorted_days])

    # ── open positions table rows ──
    open_rows = ""
    for sym, t in open_trades.items():
        open_rows += f"""
        <tr id="row-{sym}">
          <td><b>{sym}</b></td>
          <td>₹{t['entry']}</td>
          <td>{t['qty']}</td>
          <td class="text-danger">₹{t['sl']}</td>
          <td class="text-success">₹{t['tp']}</td>
          <td>₹{t['capital_used']}</td>
          <td id="ltp-{sym}"><span class="text-muted">fetching…</span></td>
          <td id="upnl-{sym}"><span class="text-muted">—</span></td>
          <td>{t['entry_time']}</td>
          <td>
            <button class="btn btn-sm btn-outline-danger close-btn" data-sym="{sym}"
              onclick="closePosition('{sym}',this)">Close</button>
          </td>
        </tr>"""
    if not open_rows:
        open_rows = '<tr><td colspan="10" class="text-center text-muted">No open positions</td></tr>'

    # ── today closed rows ──
    closed_rows = ""
    for r in reversed(today_closed):
        gross_val   = float(r.get("gross_pnl", r["pnl"]))
        charges_val = float(r.get("charges", 0))
        net_val     = float(r.get("net_pnl", r["pnl"]))
        badge       = 'success' if net_val >= 0 else 'danger'
        gross_sign  = "+" if gross_val >= 0 else ""
        net_sign    = "+" if net_val >= 0 else ""
        closed_rows += f"""
        <tr>
          <td><b>{r['symbol']}</b></td>
          <td>₹{r['entry']}</td>
          <td>₹{r['exit']}</td>
          <td>{r['qty']}</td>
          <td>{gross_sign}₹{gross_val}</td>
          <td class="text-warning">-₹{charges_val}</td>
          <td><span class="badge bg-{badge}">{net_sign}₹{net_val}</span></td>
          <td>{r['reason']}</td>
          <td>{r['exit_time']}</td>
        </tr>"""
    if not closed_rows:
        closed_rows = '<tr><td colspan="9" class="text-center text-muted">No closed trades today</td></tr>'

    # ── full history rows (all trades, paginated client-side) ──
    history_rows = ""
    for r in reversed(history):
        gross_val   = float(r.get("gross_pnl", r["pnl"]))
        charges_val = float(r.get("charges", 0))
        net_val     = float(r.get("net_pnl", r["pnl"]))
        badge       = 'success' if net_val >= 0 else 'danger'
        gross_sign  = "+" if gross_val >= 0 else ""
        net_sign    = "+" if net_val >= 0 else ""
        history_rows += f"""
        <tr class="hist-row" data-date="{r['date']}" data-sym="{r['symbol'].upper()}">
          <td>{r['date']}</td>
          <td><b>{r['symbol']}</b></td>
          <td>₹{r['entry']}</td>
          <td>₹{r['exit']}</td>
          <td>{r['qty']}</td>
          <td>{gross_sign}₹{gross_val}</td>
          <td class="text-warning">-₹{charges_val}</td>
          <td><span class="badge bg-{badge}">{net_sign}₹{net_val}</span></td>
          <td>{r.get('reason','')}</td>
        </tr>"""
    if not history_rows:
        history_rows = '<tr class="hist-row"><td colspan="9" class="text-center text-muted">No trade history yet</td></tr>'

    total_hist = len(history)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Chartink Bot Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body        {{ background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',sans-serif; }}
    .card       {{ background:#161b22; border:1px solid #30363d; border-radius:12px; }}
    .stat-val   {{ font-size:2rem; font-weight:700; }}
    th          {{ color:#8b949e; font-weight:500; border-color:#30363d !important; }}
    td          {{ border-color:#30363d !important; vertical-align:middle; }}
    .badge      {{ font-size:.8rem; padding:.4em .7em; }}
    .nav-tabs .nav-link        {{ color:#8b949e; border-color:#30363d; }}
    .nav-tabs .nav-link.active {{ color:#fff; background:#161b22; border-bottom-color:#161b22; }}
    .top-bar    {{ background:#161b22; border-bottom:1px solid #30363d; padding:12px 20px; }}
    .refresh-note {{ font-size:.75rem; color:#8b949e; }}
    input.form-control {{ background:#0d1117; border-color:#30363d; color:#c9d1d9; }}
    input.form-control:focus {{ background:#0d1117; border-color:#58a6ff; color:#c9d1d9; box-shadow:none; }}
    .page-btn   {{ cursor:pointer; padding:2px 10px; border-radius:4px; border:1px solid #30363d;
                   background:#161b22; color:#c9d1d9; font-size:.8rem; }}
    .page-btn.active {{ background:#58a6ff; color:#000; border-color:#58a6ff; }}
    .close-btn  {{ padding:2px 10px; font-size:.75rem; }}
  </style>
</head>
<body>
<div class="top-bar d-flex justify-content-between align-items-center flex-wrap gap-2">
  <div>
    <span style="font-size:1.2rem;font-weight:700;color:#58a6ff;">📊 Chartink Bot</span>
    <span class="ms-3 badge bg-secondary">{mode_label}</span>
    <span class="ms-2 badge bg-dark border">{mkt_status}</span>
  </div>
  <div class="text-end">
    <div style="color:#c9d1d9;">🕐 {time_str()}</div>
    <div class="refresh-note" id="ltp-status">⟳ Fetching live prices…</div>
  </div>
</div>

<div class="container-fluid py-4 px-3 px-md-4">

  {{# ── Stat cards ── #}}
  <div class="row g-3 mb-4">
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Open Positions</div><div class="stat-val text-warning">{open_count}</div></div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Trades Today</div><div class="stat-val text-info">{total_trades}</div></div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Winners</div><div class="stat-val text-success">{len(winners)}</div></div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Losers</div><div class="stat-val text-danger">{len(losers)}</div></div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Win Rate</div><div class="stat-val" style="color:#a78bfa;">{win_rate}%</div></div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center"><div class="text-muted small">Net P&L</div><div class="stat-val" style="color:{pnl_color};">₹{net_pnl}</div></div></div>
  </div>
  <div class="row g-3 mb-4">
    <div class="col-md-4"><div class="card p-3 text-center"><div class="text-muted small">Gross Profit</div><div class="stat-val text-success">₹{gross_profit}</div></div></div>
    <div class="col-md-4"><div class="card p-3 text-center"><div class="text-muted small">Gross Loss</div><div class="stat-val text-danger">₹{gross_loss}</div></div></div>
    <div class="col-md-4"><div class="card p-3 text-center"><div class="text-muted small">Capital / Trade</div><div class="stat-val text-info">₹{CAPITAL_PER_TRADE}</div></div></div>
  </div>

  {{# ── Daily P&L Chart ── #}}
  <div class="card p-3 mb-4">
    <div class="text-muted small mb-2">📈 Daily Net P&L — Last 14 Trading Days</div>
    <canvas id="pnlChart" height="80"></canvas>
  </div>

  {{# ── Tabs ── #}}
  <ul class="nav nav-tabs" id="dashTabs">
    <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-open">🟡 Open Positions <span class="badge bg-warning text-dark ms-1">{open_count}</span></button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-closed">📋 Today's Trades <span class="badge bg-secondary ms-1">{total_trades}</span></button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-history">📁 Full History <span class="badge bg-secondary ms-1">{total_hist}</span></button></li>
  </ul>

  <div class="tab-content mt-3">

    {{# ── Tab 1: Open Positions ── #}}
    <div class="tab-pane fade show active" id="tab-open">
      <div class="card"><div class="table-responsive">
        <table class="table table-dark table-hover mb-0">
          <thead><tr>
            <th>Stock</th><th>Entry</th><th>Qty</th>
            <th>Stop Loss</th><th>Take Profit</th><th>Capital</th>
            <th>Live Price</th><th>Unrealised P&L</th>
            <th>Entry Time</th><th>Action</th>
          </tr></thead>
          <tbody id="open-tbody">{open_rows}</tbody>
        </table>
      </div></div>
    </div>

    {{# ── Tab 2: Today Closed ── #}}
    <div class="tab-pane fade" id="tab-closed">
      <div class="card"><div class="table-responsive">
        <table class="table table-dark table-hover mb-0">
          <thead><tr>
            <th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th>
            <th>Gross P&L</th>
            <th title="Dhan intraday charges">Charges 🏦</th>
            <th>Net P&L</th>
            <th>Reason</th><th>Exit Time</th>
          </tr></thead>
          <tbody>{closed_rows}</tbody>
        </table>
      </div></div>
    </div>

    {{# ── Tab 3: Full History ── #}}
    <div class="tab-pane fade" id="tab-history">
      <div class="row g-2 mb-3">
        <div class="col-sm-4">
          <input type="date" id="filter-date" class="form-control form-control-sm"
            placeholder="Filter by date" oninput="applyFilters()"/>
        </div>
        <div class="col-sm-4">
          <input type="text" id="filter-sym" class="form-control form-control-sm"
            placeholder="Search stock symbol…" oninput="applyFilters()"/>
        </div>
        <div class="col-sm-4 d-flex align-items-center">
          <span class="text-muted small" id="hist-count">Showing {total_hist} of {total_hist} trades</span>
        </div>
      </div>
      <div class="card"><div class="table-responsive">
        <table class="table table-dark table-hover mb-0" id="hist-table">
          <thead><tr>
            <th>Date</th><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th>
            <th>Gross P&L</th>
            <th title="Dhan intraday charges">Charges 🏦</th>
            <th>Net P&L</th>
            <th>Reason</th>
          </tr></thead>
          <tbody id="hist-tbody">{history_rows}</tbody>
        </table>
      </div></div>
      <div class="d-flex gap-2 mt-3 flex-wrap align-items-center" id="pagination"></div>
    </div>

  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── Daily P&L Chart ──────────────────────────────────
const ctx = document.getElementById('pnlChart').getContext('2d');
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [{{
      label: 'Net P&L (₹)',
      data: {chart_values},
      backgroundColor: {chart_colors},
      borderRadius: 4,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: ctx => '₹' + ctx.parsed.y }} }}
    }},
    scales: {{
      x: {{ ticks: {{ color:'#8b949e' }}, grid: {{ color:'#21262d' }} }},
      y: {{ ticks: {{ color:'#8b949e', callback: v => '₹'+v }}, grid: {{ color:'#21262d' }} }}
    }}
  }}
}});

// ── Live Price Fetcher ───────────────────────────────
function fetchPrices() {{
  fetch('/prices')
    .then(r => r.json())
    .then(data => {{
      for (const [sym, info] of Object.entries(data)) {{
        const ltpEl  = document.getElementById('ltp-'  + sym);
        const upnlEl = document.getElementById('upnl-' + sym);
        if (ltpEl)  ltpEl.textContent  = '₹' + info.price;
        if (upnlEl) {{
          const pnl = info.unrealised_pnl;
          upnlEl.innerHTML = `<span style="color:${{pnl>=0?'#00c896':'#ff4d4d'}}">
            ${{pnl>=0?'+':''}}₹${{pnl}}</span>`;
        }}
      }}
      document.getElementById('ltp-status').textContent =
        '⟳ Prices updated ' + new Date().toLocaleTimeString('en-IN');
    }})
    .catch(() => {{
      document.getElementById('ltp-status').textContent = '⚠️ Price fetch failed';
    }});
}}
fetchPrices();
setInterval(fetchPrices, 30000);

// ── Manual Close ─────────────────────────────────────
function closePosition(sym, btn) {{
  if (!confirm('Close position: ' + sym + '?')) return;
  btn.disabled = true;
  btn.textContent = '…';
  fetch('/close/' + sym, {{ method: 'POST' }})
    .then(r => r.json())
    .then(d => {{
      if (d.status === 'closed') {{
        const row = document.getElementById('row-' + sym);
        if (row) row.remove();
        alert('✅ ' + sym + ' closed @ ₹' + d.price);
      }} else {{
        alert('❌ Could not close: ' + JSON.stringify(d));
        btn.disabled = false; btn.textContent = 'Close';
      }}
    }})
    .catch(() => {{ btn.disabled=false; btn.textContent='Close'; }});
}}

// ── History Filter + Pagination ──────────────────────
const PAGE_SIZE = 25;
let currentPage = 1;

function getVisibleRows() {{
  const dateVal = document.getElementById('filter-date').value.trim();
  const symVal  = document.getElementById('filter-sym').value.trim().toUpperCase();
  const rows    = Array.from(document.querySelectorAll('#hist-tbody .hist-row'));
  return rows.filter(r => {{
    const dOk = !dateVal || r.dataset.date === dateVal;
    const sOk = !symVal  || r.dataset.sym.includes(symVal);
    return dOk && sOk;
  }});
}}

function applyFilters() {{
  currentPage = 1;
  renderPage();
}}

function renderPage() {{
  const visible = getVisibleRows();
  const total   = visible.length;
  const start   = (currentPage - 1) * PAGE_SIZE;
  const end     = start + PAGE_SIZE;

  // hide/show rows
  document.querySelectorAll('#hist-tbody .hist-row').forEach(r => r.style.display = 'none');
  visible.slice(start, end).forEach(r => r.style.display = '');

  // count label
  document.getElementById('hist-count').textContent =
    `Showing ${{Math.min(end, total)}} of ${{total}} trades`;

  // pagination buttons
  const pages  = Math.ceil(total / PAGE_SIZE) || 1;
  const pg     = document.getElementById('pagination');
  pg.innerHTML = '';
  for (let i = 1; i <= pages; i++) {{
    const b = document.createElement('button');
    b.className = 'page-btn' + (i === currentPage ? ' active' : '');
    b.textContent = i;
    b.onclick = () => {{ currentPage = i; renderPage(); }};
    pg.appendChild(b);
  }}
}}

renderPage();
</script>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────

print("🚀 Starting Chartink Bot...")
threading.Thread(target=run_monitor, daemon=True).start()
send(
    f"🟢 <b>Chartink Bot LIVE!</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"🧪 Mode          : {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
    f"📡 Signal        : Chartink tazbul\n"
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
