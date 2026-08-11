from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

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
MARKET_OPEN       = dtime(9,  15)
MARKET_CLOSE      = dtime(15, 30)
FORCE_EXIT        = dtime(15, 12)

open_trades  = {}
closed_today = []
traded_today = set()

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
    if symbol in open_trades or symbol in traded_today:
        print(f"⚠️ Skip {symbol} — already open or traded today")
        return
    trade = calculate(symbol, price)
    open_trades[symbol] = trade
    traded_today.add(symbol)
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
        f"📌 Stock  : <b>{symbol}</b>\n"
        f"💰 Entry  : ₹{trade['entry']}\n"
        f"🚪 Exit   : ₹{exit_price}\n"
        f"📦 Qty    : {trade['qty']} shares\n"
        f"{'💚' if pnl >= 0 else '❤️'} {result}: ₹{abs(pnl)}\n"
        f"📝 Reason : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {trade['exit_time']}"
    )
    closed_today.append({"symbol": symbol, "pnl": pnl})
    log_trade(trade, exit_price, pnl, reason)

def log_trade(trade, exit_price, pnl, reason):
    f      = "logs/trades.csv"
    exists = os.path.exists(f)
    with open(f, "a", newline="") as fp:
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

def check_positions():
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
    winners = [t for t in closed_today if t["pnl"] >= 0]
    losers  = [t for t in closed_today if t["pnl"] <  0]
    net     = round(sum(t["pnl"] for t in closed_today), 2)
    icon    = "💚" if net >= 0 else "❤️"
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
    closed_today.clear()
    traded_today.clear()
    print("📋 EOD done — counters reset")

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
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard", methods=["GET"])
def dashboard():
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
    net_pnl      = round(sum(float(r["pnl"]) for r in today_closed), 2)
    gross_profit = round(sum(float(r["pnl"]) for r in winners), 2)
    gross_loss   = round(abs(sum(float(r["pnl"]) for r in losers)), 2)
    win_rate     = round((len(winners) / total_trades * 100) if total_trades else 0, 1)
    open_count   = len(open_trades)
    mode_label   = "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading"
    pnl_color    = "#00c896" if net_pnl >= 0 else "#ff4d4d"
    mkt_status   = "🟢 Market Open" if is_market_hours() else "🔴 Market Closed"

    open_rows = ""
    for sym, t in open_trades.items():
        open_rows += f"""
        <tr>
          <td><b>{sym}</b></td>
          <td>&#8377;{t['entry']}</td>
          <td>{t['qty']}</td>
          <td style="color:#ff4d4d;">&#8377;{t['sl']}</td>
          <td style="color:#00c896;">&#8377;{t['tp']}</td>
          <td>&#8377;{t['capital_used']}</td>
          <td>{t['entry_time']}</td>
        </tr>"""
    if not open_rows:
        open_rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No open positions</td></tr>'

    closed_rows = ""
    for r in reversed(today_closed):
        pnl_val = float(r["pnl"])
        color   = "#00c896" if pnl_val >= 0 else "#ff4d4d"
        sign    = "+" if pnl_val >= 0 else ""
        closed_rows += f"""
        <tr>
          <td><b>{r['symbol']}</b></td>
          <td>&#8377;{r['entry']}</td>
          <td>&#8377;{r['exit']}</td>
          <td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r['reason']}</td>
          <td>{r['exit_time']}</td>
        </tr>"""
    if not closed_rows:
        closed_rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No closed trades today</td></tr>'

    history_rows = ""
    for r in reversed(history[-50:]):
        pnl_val = float(r["pnl"])
        color   = "#00c896" if pnl_val >= 0 else "#ff4d4d"
        sign    = "+" if pnl_val >= 0 else ""
        history_rows += f"""
        <tr>
          <td>{r['date']}</td>
          <td><b>{r['symbol']}</b></td>
          <td>&#8377;{r['entry']}</td>
          <td>&#8377;{r['exit']}</td>
          <td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r.get('reason','')}</td>
        </tr>"""
    if not history_rows:
        history_rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No trade history yet</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="30"/>
  <title>Chartink Bot Dashboard</title>
  <style>
    *            {{ box-sizing:border-box; margin:0; padding:0; }}
    body         {{ background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; }}
    .topbar      {{ background:#161b22; border-bottom:1px solid #30363d; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }}
    .topbar-left {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
    .bot-title   {{ font-size:1.1rem; font-weight:700; color:#58a6ff; }}
    .badge       {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600; }}
    .badge-gray  {{ background:#21262d; color:#8b949e; border:1px solid #30363d; }}
    .badge-mkt   {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; }}
    .topbar-right {{ text-align:right; font-size:12px; color:#8b949e; }}
    .container   {{ padding:20px; }}
    .stat-grid   {{ display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:16px; }}
    .stat-card   {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 10px; text-align:center; }}
    .stat-label  {{ font-size:11px; color:#8b949e; margin-bottom:6px; }}
    .stat-value  {{ font-size:1.7rem; font-weight:700; }}
    .pnl-grid    {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }}
    .tabs        {{ display:flex; gap:0; border-bottom:1px solid #30363d; margin-bottom:16px; }}
    .tab-btn     {{ background:none; border:none; border-bottom:3px solid transparent; color:#8b949e; padding:10px 20px; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit; transition:all .2s; white-space:nowrap; }}
    .tab-btn:hover  {{ color:#c9d1d9; }}
    .tab-btn.active {{ color:#58a6ff; border-bottom-color:#58a6ff; }}
    .tab-pane    {{ display:none; }}
    .tab-pane.active {{ display:block; }}
    .table-wrap  {{ background:#161b22; border:1px solid #30363d; border-radius:10px; overflow:hidden; overflow-x:auto; }}
    table        {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th           {{ background:#21262d; color:#8b949e; font-weight:500; padding:10px 14px; text-align:left; border-bottom:1px solid #30363d; white-space:nowrap; }}
    td           {{ padding:10px 14px; border-bottom:1px solid #21262d; vertical-align:middle; white-space:nowrap; }}
    tr:last-child td {{ border-bottom:none; }}
    tr:hover td  {{ background:#1c2128; }}
    .hint        {{ font-size:12px; color:#8b949e; margin-bottom:8px; }}
  </style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="bot-title">&#128202; Chartink Bot</span>
    <span class="badge badge-gray">{mode_label}</span>
    <span class="badge badge-mkt">{mkt_status}</span>
  </div>
  <div class="topbar-right">
    <div>&#128336; {time_str()}</div>
    <div>&#8635; Auto-refresh every 30s</div>
  </div>
</div>

<div class="container">

  <!-- Summary Stats -->
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value" style="color:#f0b429;">{open_count}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trades Today</div>
      <div class="stat-value" style="color:#58a6ff;">{total_trades}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Winners</div>
      <div class="stat-value" style="color:#00c896;">{len(winners)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Losers</div>
      <div class="stat-value" style="color:#ff4d4d;">{len(losers)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value" style="color:#a78bfa;">{win_rate}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Net P&amp;L</div>
      <div class="stat-value" style="color:{pnl_color};">&#8377;{net_pnl}</div>
    </div>
  </div>

  <!-- P&L Detail -->
  <div class="pnl-grid">
    <div class="stat-card">
      <div class="stat-label">Gross Profit</div>
      <div class="stat-value" style="color:#00c896;">&#8377;{gross_profit}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Gross Loss</div>
      <div class="stat-value" style="color:#ff4d4d;">&#8377;{gross_loss}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Capital / Trade</div>
      <div class="stat-value" style="color:#58a6ff;">&#8377;{CAPITAL_PER_TRADE}</div>
    </div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('open',this)">Open Positions ({open_count})</button>
    <button class="tab-btn" onclick="showTab('closed',this)">Today's Trades ({total_trades})</button>
    <button class="tab-btn" onclick="showTab('history',this)">Full History</button>
  </div>

  <!-- Open Positions -->
  <div id="tab-open" class="tab-pane active">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Stock</th><th>Entry Price</th><th>Qty</th>
            <th>Stop Loss</th><th>Take Profit</th>
            <th>Capital Used</th><th>Entry Time</th>
          </tr>
        </thead>
        <tbody>{open_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- Today's Trades -->
  <div id="tab-closed" class="tab-pane">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Stock</th><th>Entry Price</th><th>Exit Price</th>
            <th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Exit Time</th>
          </tr>
        </thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- Full History -->
  <div id="tab-history" class="tab-pane">
    <div class="hint">Showing last 50 trades</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Stock</th><th>Entry Price</th>
            <th>Exit Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th>
          </tr>
        </thead>
        <tbody>{history_rows}</tbody>
      </table>
    </div>
  </div>

</div>

<script>
function showTab(name, btn) {{
  document.querySelectorAll('.tab-pane').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab-btn').forEach(function(b)  {{ b.classList.remove('active'); }});
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}}
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
