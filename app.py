from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv
import pandas as pd

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
PAPER_TRADING  = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT           = int(os.environ.get("PORT", 10000))
MARKET_OPEN    = dtime(9,  15)
MARKET_CLOSE   = dtime(15, 30)
FORCE_EXIT     = dtime(15, 12)

# ── ATR multipliers ───────────────────────────
ATR_SL_MULT    = 1.5
ATR_TP_MULT    = 2.0
CANDLE_TF      = "5m"       # 5-minute candles
ADX_TREND_MIN  = 20         # below this = consolidating

# ── Bot 1 state ───────────────────────────────
open_trades   = {}
closed_today  = []
traded_today  = set()

# ── Bot 2 state ───────────────────────────────
open_trades2  = {}
closed_today2 = []
traded_today2 = set()

# ── NSE cache ────────────────────────────────
_nse_cache      = {}
_nse_cache_time = 0
NSE_CACHE_TTL   = 60

# ── Signal Engine state ───────────────────────
_last_signals   = []          # list of latest scan results
_last_scan_time = "Never"

# ── Full Nifty 50 symbol list ─────────────────
NIFTY50 = [
    "RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK",
    "INFOSYS","SBIN","HINDUNILVR","ITC","KOTAKBANK",
    "LT","HCLTECH","AXISBANK","BAJFINANCE","ASIANPAINT",
    "MARUTI","SUNPHARMA","TITAN","ULTRACEMCO","ONGC",
    "NTPC","POWERGRID","WIPRO","TECHM","NESTLEIND",
    "BAJAJFINSV","ADANIENT","ADANIPORTS","JSWSTEEL","TATASTEEL",
    "HINDALCO","COALINDIA","BPCL","DRREDDY","CIPLA",
    "DIVISLAB","APOLLOHOSP","EICHERMOT","BAJAJ-AUTO","HEROMOTOCO",
    "M&M","TATAMOTORS","TATACONSUM","BRITANNIA","GRASIM",
    "INDUSINDBK","SBILIFE","HDFCLIFE","LTIM","UPL"
]

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def send(msg, token=None, chat_id=None):
    t = token   or BOT_TOKEN
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
#  SIGNAL ENGINE — 6 Rules
# ─────────────────────────────────────────────
def fetch_candles(symbol):
    """Fetch 5-min candles from Yahoo Finance. Returns DataFrame or None."""
    try:
        import yfinance as yf
        tk  = yf.Ticker(f"{symbol}.NS")
        df  = tk.history(period="2d", interval="5m")
        if df is None or len(df) < 60:
            return None
        df = df.rename(columns={
            "Open":"open","High":"high","Low":"low",
            "Close":"close","Volume":"volume"
        })
        return df[["open","high","low","close","volume"]].copy()
    except:
        return None

def calc_indicators(df):
    try:
        import ta as ta_lib
        df["ema13"] = ta_lib.trend.ema_indicator(df["close"], window=13)
        df["ema50"] = ta_lib.trend.ema_indicator(df["close"], window=50)
        df["atr"]   = ta_lib.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        df["adx"]   = ta_lib.trend.adx(df["high"], df["low"], df["close"], window=14)
        return df
    except:
        return None

def get_nifty_direction():
    """Returns 'UP', 'DOWN', or 'FLAT' based on Nifty 50 last price vs EMA13."""
    try:
        import yfinance as yf
        df = yf.Ticker("^NSEI").history(period="2d", interval="5m")
        if df is None or len(df) < 14:
            return "FLAT"
        closes = df["Close"]
        ema13  = closes.ewm(span=13, adjust=False).mean()
        last   = float(closes.iloc[-1])
        e13    = float(ema13.iloc[-1])
        if last > e13 * 1.001:
            return "UP"
        elif last < e13 * 0.999:
            return "DOWN"
        return "FLAT"
    except:
        return "FLAT"

def check_signal(symbol, market_dir):
    """
    Run all 6 rules for one symbol.
    Returns dict with keys: symbol, signal, reason, entry, sl, tp, atr
    signal: 'BUY' | 'SELL' | 'SKIP'
    """
    result = {"symbol": symbol, "signal": "SKIP", "reason": "", "entry": 0, "sl": 0, "tp": 0, "atr": 0}

    df = fetch_candles(symbol)
    if df is None:
        result["reason"] = "No candle data"
        return result

    df = calc_indicators(df)
    if df is None:
        result["reason"] = "Indicator error"
        return result

    df = df.dropna(subset=["ema13","ema50","atr","adx"])
    if len(df) < 3:
        result["reason"] = "Not enough data after dropna"
        return result

    # ── Latest 2 rows (current & previous candle) ──
    cur  = df.iloc[-1]
    prev = df.iloc[-2]

    ema13_cur  = float(cur["ema13"])
    ema50_cur  = float(cur["ema50"])
    ema13_prev = float(prev["ema13"])
    ema50_prev = float(prev["ema50"])
    atr        = float(cur["atr"])
    adx        = float(cur["adx"])
    close      = float(cur["close"])
    open_p     = float(cur["open"])
    high       = float(cur["high"])
    low        = float(cur["low"])

    result["atr"]   = round(atr, 2)
    result["entry"] = round(close, 2)

    # ── Rule 3: No consolidation (ADX > 20) ───────
    if adx < ADX_TREND_MIN:
        result["reason"] = f"Consolidating (ADX={adx:.1f}<{ADX_TREND_MIN})"
        return result

    # ── Rule 4: Candle quality ─────────────────────
    candle_range = high - low
    candle_body  = abs(close - open_p)
    if candle_range > 0:
        body_ratio   = candle_body / candle_range        # good if > 0.4
        upper_shadow = high - max(close, open_p)
        lower_shadow = min(close, open_p) - low
        shadow_ratio = (upper_shadow + lower_shadow) / candle_range  # good if < 0.5
        dist_ema13   = abs(close - ema13_cur) / ema13_cur * 100       # good if < 1%
        if body_ratio < 0.4:
            result["reason"] = f"Small body ratio ({body_ratio:.2f})"
            return result
        if shadow_ratio > 0.5:
            result["reason"] = f"Big shadow ({shadow_ratio:.2f})"
            return result
        if dist_ema13 > 1.0:
            result["reason"] = f"Far from EMA13 ({dist_ema13:.2f}%)"
            return result

    # ── Rule 1 & 2: EMA 13 crossover EMA 50 ──────
    crossed_up   = (ema13_prev <= ema50_prev) and (ema13_cur > ema50_cur)
    crossed_down = (ema13_prev >= ema50_prev) and (ema13_cur < ema50_cur)

    if not crossed_up and not crossed_down:
        result["reason"] = "No EMA crossover"
        return result

    # ── Rule 5: Trade with market direction ────────
    if crossed_up and market_dir == "DOWN":
        result["reason"] = f"BUY signal but Nifty is DOWN"
        return result
    if crossed_down and market_dir == "UP":
        result["reason"] = f"SELL signal but Nifty is UP"
        return result

    # ── Rule 6: ATR-based SL & TP ─────────────────
    if crossed_up:
        sl = round(close - ATR_SL_MULT * atr, 2)
        tp = round(close + ATR_TP_MULT * atr, 2)
        result["signal"] = "BUY"
        result["reason"] = f"EMA13 crossed above EMA50 | ADX={adx:.1f} | Nifty={market_dir}"
    else:
        sl = round(close + ATR_SL_MULT * atr, 2)
        tp = round(close - ATR_TP_MULT * atr, 2)
        result["signal"] = "SELL"
        result["reason"] = f"EMA13 crossed below EMA50 | ADX={adx:.1f} | Nifty={market_dir}"

    result["sl"] = sl
    result["tp"] = tp
    return result

def run_signal_scan():
    """Scan all Nifty 50 stocks. Updates _last_signals and _last_scan_time."""
    global _last_signals, _last_scan_time
    print(f"🔍 Signal scan started — {time_str()}")
    market_dir = get_nifty_direction()
    results    = []
    for symbol in NIFTY50:
        try:
            r = check_signal(symbol, market_dir)
            r["market_dir"] = market_dir
            results.append(r)
            # Auto-feed BUY signals into Bot2 paper trade engine
            if r["signal"] == "BUY":
                if symbol not in traded_today2 and symbol not in open_trades2:
                    trade = {
                        "symbol"      : symbol,
                        "entry"       : r["entry"],
                        "qty"         : max(1, int(BOT2_CAPITAL / r["entry"])),
                        "sl"          : r["sl"],
                        "tp"          : r["tp"],
                        "capital_used": round(max(1, int(BOT2_CAPITAL / r["entry"])) * r["entry"], 2),
                        "risk_amt"    : round(abs(r["entry"] - r["sl"]) * max(1, int(BOT2_CAPITAL / r["entry"])), 2),
                        "reward_amt"  : round(abs(r["tp"] - r["entry"]) * max(1, int(BOT2_CAPITAL / r["entry"])), 2),
                        "entry_time"  : time_str(),
                        "exit_time"   : None,
                    }
                    open_trades2[symbol] = trade
                    traded_today2.add(symbol)
                    send(
                        f"🤖 <b>SIGNAL ENGINE — BUY</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 Stock   : <b>{symbol}</b>\n"
                        f"💰 Entry   : ₹{r['entry']}\n"
                        f"🔴 SL      : ₹{r['sl']} (ATR×{ATR_SL_MULT})\n"
                        f"🟢 TP      : ₹{r['tp']} (ATR×{ATR_TP_MULT})\n"
                        f"📊 Reason  : {r['reason']}\n"
                        f"🌐 Nifty   : {market_dir}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🕐 {time_str()}",
                        token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID
                    )
        except Exception as e:
            results.append({"symbol": symbol, "signal": "SKIP", "reason": str(e),
                            "entry": 0, "sl": 0, "tp": 0, "atr": 0, "market_dir": market_dir})
    _last_signals   = results
    _last_scan_time = time_str()
    buys  = sum(1 for r in results if r["signal"] == "BUY")
    sells = sum(1 for r in results if r["signal"] == "SELL")
    print(f"✅ Scan done — BUY:{buys} SELL:{sells} SKIP:{len(results)-buys-sells}")

# ─────────────────────────────────────────────
#  NSE DATA FETCHER
# ─────────────────────────────────────────────
def _nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept"         : "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer"        : "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com", timeout=10)
    except:
        pass
    return s

def fetch_nse_data():
    global _nse_cache, _nse_cache_time
    now_ts = time.time()
    if _nse_cache and (now_ts - _nse_cache_time) < NSE_CACHE_TTL:
        return _nse_cache
    result = {"preopen": [], "advances": 0, "declines": 0, "unchanged": 0,
              "sectors": [], "fetched_at": time_str(), "error": None}
    try:
        s = _nse_session()
        try:
            r   = s.get("https://www.nseindia.com/api/market-data-pre-open?key=NIFTY", timeout=12)
            raw = r.json().get("data", [])
            def pct(item):
                try: return abs(float(item.get("metadata", {}).get("pChange", 0)))
                except: return 0
            for item in sorted(raw, key=pct, reverse=True):
                m = item.get("metadata", {})
                result["preopen"].append({
                    "symbol" : m.get("symbol", ""),
                    "ltp"    : m.get("lastPrice", 0),
                    "change" : round(float(m.get("change", 0)), 2),
                    "pchange": round(float(m.get("pChange", 0)), 2),
                    "volume" : m.get("totalTradedVolume", 0),
                    "iep"    : m.get("iep", 0),
                })
        except Exception as e:
            result["error"] = f"Pre-open: {e}"
        try:
            r2  = s.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050", timeout=12)
            adv = dec = unc = 0
            for stock in r2.json().get("data", []):
                try:
                    chg = float(stock.get("pChange", 0))
                    if chg > 0: adv += 1
                    elif chg < 0: dec += 1
                    else: unc += 1
                except: pass
            result["advances"]  = adv
            result["declines"]  = dec
            result["unchanged"] = unc
        except: pass
        SECTORS = {
            "NIFTY IT"    : "NIFTY%20IT",
            "NIFTY BANK"  : "NIFTY%20BANK",
            "NIFTY AUTO"  : "NIFTY%20AUTO",
            "NIFTY PHARMA": "NIFTY%20PHARMA",
            "NIFTY FMCG"  : "NIFTY%20FMCG",
            "NIFTY METAL" : "NIFTY%20METAL",
            "NIFTY ENERGY": "NIFTY%20ENERGY",
            "NIFTY REALTY": "NIFTY%20REALTY",
        }
        sector_vols = []
        for name, key in SECTORS.items():
            try:
                rs  = s.get(f"https://www.nseindia.com/api/equity-stockIndices?index={key}", timeout=10)
                jd  = rs.json()
                vol = sum(int(st.get("totalTradedVolume", 0) or 0) for st in jd.get("data", []))
                pch = 0
                try: pch = round(float(jd.get("metadata", {}).get("pChange", 0)), 2)
                except: pass
                sector_vols.append({"name": name, "volume": vol, "pchange": pch})
            except: pass
        sector_vols.sort(key=lambda x: x["volume"], reverse=True)
        result["sectors"] = sector_vols
    except Exception as e:
        result["error"] = str(e)
    result["fetched_at"] = time_str()
    _nse_cache      = result
    _nse_cache_time = time.time()
    return result

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
        if not trade: continue
        price = get_price(symbol)
        if not price: continue
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
        if not trade: continue
        price = get_price(symbol)
        if not price: continue
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
#  MONITOR THREAD
# ─────────────────────────────────────────────
def run_monitor():
    eod_sent      = False
    last_scan_min = -1
    print("📈 Monitor started — every 1 min")
    while True:
        try:
            t       = now_ist()
            cur_min = datetime.now(IST).minute

            # Force exit
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

            # EOD
            if t >= MARKET_CLOSE and not eod_sent:
                send_eod()
                send_eod2()
                eod_sent = True
            if t < dtime(9, 0):
                eod_sent = False

            # Monitor positions
            if is_market_hours():
                if open_trades:  check_positions()
                if open_trades2: check_positions2()

            # Signal scan every 5 minutes during market hours
            if is_market_hours() and cur_min % 5 == 0 and cur_min != last_scan_min:
                last_scan_min = cur_min
                threading.Thread(target=run_signal_scan, daemon=True).start()

        except Exception as e:
            print(f"❌ Monitor error: {e}")
        time.sleep(60)

# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────
@app.route("/alert", methods=["POST"])
def receive_alert():
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
            results.append({"symbol": symbol, "status": "skip"}); continue
        price = get_price(symbol, prices[i] if i < len(prices) else None)
        if not price:
            send(f"❌ <b>Price fetch failed: {symbol}</b>")
            results.append({"symbol": symbol, "status": "price failed"}); continue
        open_trade(symbol, price)
        results.append({"symbol": symbol, "status": "entered", "price": price})
    return jsonify({"status": "processed", "results": results}), 200

@app.route("/alert2", methods=["POST"])
def receive_alert2():
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
            results.append({"symbol": symbol, "status": "skip"}); continue
        price = get_price(symbol, prices[i] if i < len(prices) else None)
        if not price:
            send(f"❌ <b>Price fetch failed: {symbol}</b>", token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID)
            results.append({"symbol": symbol, "status": "price failed"}); continue
        open_trade2(symbol, price)
        results.append({"symbol": symbol, "status": "entered", "price": price})
    return jsonify({"status": "processed", "results": results}), 200

@app.route("/scan", methods=["GET"])
def manual_scan():
    threading.Thread(target=run_signal_scan, daemon=True).start()
    return jsonify({"status": "scan started"}), 200

@app.route("/nse-data", methods=["GET"])
def nse_data_api():
    return jsonify(fetch_nse_data()), 200

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"      : "🟢 Running",
        "time_ist"    : time_str(),
        "market_open" : is_market_hours(),
        "last_scan"   : _last_scan_time,
        "signals"     : len([s for s in _last_signals if s["signal"] != "SKIP"]),
    }), 200

@app.route("/test", methods=["GET"])
def test():
    send(f"✅ <b>Bot 1 (tazbul) Working!</b>\n🕐 {time_str()}")
    send(f"✅ <b>Bot 2 (TazAmol-Test1) Working!</b>\n🕐 {time_str()}",
         token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID)
    return jsonify({"status": "both test messages sent"}), 200

@app.route("/report", methods=["GET"])
def report():
    send_eod(); send_eod2()
    return jsonify({"status": "both reports sent"}), 200

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "bot1_tazbul" : {"open_trades": list(open_trades.keys()), "closed_today": len(closed_today),
                         "net_pnl": round(sum(t["pnl"] for t in closed_today), 2)},
        "bot2_tazamol": {"open_trades": list(open_trades2.keys()), "closed_today": len(closed_today2),
                         "net_pnl": round(sum(t["pnl"] for t in closed_today2), 2)},
    }), 200

# ─────────────────────────────────────────────
#  DASHBOARD HELPERS
# ─────────────────────────────────────────────
def build_table_rows(trades_dict):
    rows = ""
    for sym, t in trades_dict.items():
        rows += f"""<tr><td><b>{sym}</b></td><td>&#8377;{t['entry']}</td>
          <td>{t['qty']}</td><td style="color:#ff4d4d;">&#8377;{t['sl']}</td>
          <td style="color:#00c896;">&#8377;{t['tp']}</td>
          <td>&#8377;{t['capital_used']}</td><td>{t['entry_time']}</td></tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No open positions</td></tr>'
    return rows

def build_closed_rows(today_closed):
    rows = ""
    for r in reversed(today_closed):
        pnl_val = float(r["pnl"]); color = "#00c896" if pnl_val >= 0 else "#ff4d4d"; sign = "+" if pnl_val >= 0 else ""
        rows += f"""<tr><td><b>{r['symbol']}</b></td><td>&#8377;{r['entry']}</td>
          <td>&#8377;{r['exit']}</td><td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r['reason']}</td><td>{r['exit_time']}</td></tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No closed trades today</td></tr>'
    return rows

def build_history_rows(history):
    rows = ""
    for r in reversed(history[-50:]):
        pnl_val = float(r["pnl"]); color = "#00c896" if pnl_val >= 0 else "#ff4d4d"; sign = "+" if pnl_val >= 0 else ""
        rows += f"""<tr><td>{r['date']}</td><td><b>{r['symbol']}</b></td>
          <td>&#8377;{r['entry']}</td><td>&#8377;{r['exit']}</td><td>{r['qty']}</td>
          <td style="color:{color};font-weight:700;">{sign}&#8377;{pnl_val}</td>
          <td>{r.get('reason','')}</td></tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No history yet</td></tr>'
    return rows

def load_csv(path):
    history = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                history.append(row)
    return history

def stats(today_closed):
    winners = [r for r in today_closed if r.get("result") == "WIN"]
    losers  = [r for r in today_closed if r.get("result") == "LOSS"]
    net_pnl = round(sum(float(r["pnl"]) for r in today_closed), 2)
    gp      = round(sum(float(r["pnl"]) for r in winners), 2)
    gl      = round(abs(sum(float(r["pnl"]) for r in losers)), 2)
    wr      = round((len(winners) / len(today_closed) * 100) if today_closed else 0, 1)
    return winners, losers, net_pnl, gp, gl, wr

def build_signal_rows(signals):
    buys  = [s for s in signals if s["signal"] == "BUY"]
    sells = [s for s in signals if s["signal"] == "SELL"]
    skips = [s for s in signals if s["signal"] == "SKIP"]
    rows  = ""
    for s in buys:
        rows += f"""<tr>
          <td><b style="color:#00c896;">&#9650; BUY</b></td>
          <td><b>{s['symbol']}</b></td>
          <td>&#8377;{s['entry']}</td>
          <td style="color:#ff4d4d;">&#8377;{s['sl']}</td>
          <td style="color:#00c896;">&#8377;{s['tp']}</td>
          <td>&#8377;{s['atr']}</td>
          <td style="font-size:11px;color:#8b949e;">{s['reason']}</td>
        </tr>"""
    for s in sells:
        rows += f"""<tr>
          <td><b style="color:#ff4d4d;">&#9660; SELL</b></td>
          <td><b>{s['symbol']}</b></td>
          <td>&#8377;{s['entry']}</td>
          <td style="color:#ff4d4d;">&#8377;{s['sl']}</td>
          <td style="color:#00c896;">&#8377;{s['tp']}</td>
          <td>&#8377;{s['atr']}</td>
          <td style="font-size:11px;color:#8b949e;">{s['reason']}</td>
        </tr>"""
    for s in skips[:10]:
        rows += f"""<tr style="opacity:0.45;">
          <td><b style="color:#8b949e;">&#8213; SKIP</b></td>
          <td>{s['symbol']}</td>
          <td>&#8377;{s['entry']}</td>
          <td>—</td><td>—</td><td>—</td>
          <td style="font-size:11px;color:#8b949e;">{s['reason']}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:24px;">No scan results yet — runs every 5 min during market hours, or click Scan Now</td></tr>'
    return rows, len(buys), len(sells), len(skips)

def build_preopen_rows(preopen):
    if not preopen:
        return '<tr><td colspan="5" style="text-align:center;color:#8b949e;padding:24px;">No pre-open data</td></tr>'
    rows = ""
    for s in preopen:
        pct = float(s.get("pchange", 0)); col = "#00c896" if pct >= 0 else "#ff4d4d"; sign = "+" if pct >= 0 else ""
        try: vol_fmt = f"{int(s.get('volume',0)):,}"
        except: vol_fmt = str(s.get("volume", 0))
        rows += f"""<tr><td><b>{s['symbol']}</b></td>
          <td>&#8377;{s.get('iep', s.get('ltp', 0))}</td>
          <td>&#8377;{s.get('ltp', 0)}</td>
          <td style="color:{col};font-weight:700;">{sign}{pct}%</td>
          <td>{vol_fmt}</td></tr>"""
    return rows

def build_sector_rows(sectors):
    if not sectors:
        return '<tr><td colspan="3" style="text-align:center;color:#8b949e;padding:24px;">No sector data</td></tr>'
    rows = ""; max_vol = max((s["volume"] for s in sectors), default=1) or 1
    for s in sectors:
        pct = float(s.get("pchange", 0)); col = "#00c896" if pct >= 0 else "#ff4d4d"; sign = "+" if pct >= 0 else ""
        bar_w = int(s["volume"] / max_vol * 100)
        try: vol_fmt = f"{int(s['volume']):,}"
        except: vol_fmt = str(s["volume"])
        rows += f"""<tr><td><b>{s['name']}</b></td>
          <td><div style="background:#21262d;border-radius:4px;height:14px;width:160px;overflow:hidden;">
          <div style="background:#58a6ff;height:100%;width:{bar_w}%;"></div></div>
          <span style="font-size:11px;color:#8b949e;">{vol_fmt}</span></td>
          <td style="color:{col};font-weight:700;">{sign}{pct}%</td></tr>"""
    return rows

def build_adv_dec_html(adv, dec, unc):
    total = adv + dec + unc or 1
    aw = int(adv / total * 100); dw = int(dec / total * 100); uw = 100 - aw - dw
    return f"""<div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:16px;">
      <div class="stat-card" style="flex:1;min-width:120px;"><div class="stat-label">&#9650; Advances</div><div class="stat-value" style="color:#00c896;">{adv}</div></div>
      <div class="stat-card" style="flex:1;min-width:120px;"><div class="stat-label">&#9660; Declines</div><div class="stat-value" style="color:#ff4d4d;">{dec}</div></div>
      <div class="stat-card" style="flex:1;min-width:120px;"><div class="stat-label">&#8213; Unchanged</div><div class="stat-value" style="color:#8b949e;">{unc}</div></div>
      <div style="flex:3;min-width:200px;">
        <div style="font-size:11px;color:#8b949e;margin-bottom:6px;">Market Breadth — Nifty 50</div>
        <div style="display:flex;border-radius:6px;overflow:hidden;height:20px;">
          <div style="width:{aw}%;background:#00c896;"></div>
          <div style="width:{uw}%;background:#8b949e;"></div>
          <div style="width:{dw}%;background:#ff4d4d;"></div>
        </div>
        <div style="display:flex;gap:16px;font-size:11px;color:#8b949e;margin-top:4px;">
          <span style="color:#00c896;">&#9650; {aw}% Adv</span>
          <span>&#8213; {uw}% Unch</span>
          <span style="color:#ff4d4d;">&#9660; {dw}% Dec</span>
        </div>
      </div></div>"""

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard", methods=["GET"])
def dashboard():
    today_str  = datetime.now(IST).strftime("%Y-%m-%d")
    mode_label = "🧪 Paper Trading" if PAPER_TRADING else "⚡ Live Trading"
    mkt_status = "🟢 Market Open" if is_market_hours() else "🔴 Market Closed"

    hist1  = load_csv("logs/trades.csv"); today1 = [r for r in hist1 if r.get("date") == today_str]
    w1,l1,net1,gp1,gl1,wr1 = stats(today1)
    hist2  = load_csv("logs/trades2.csv"); today2 = [r for r in hist2 if r.get("date") == today_str]
    w2,l2,net2,gp2,gl2,wr2 = stats(today2)

    nse       = fetch_nse_data()
    preopen_r = build_preopen_rows(nse["preopen"])
    sector_r  = build_sector_rows(nse["sectors"])
    adv_dec_h = build_adv_dec_html(nse["advances"], nse["declines"], nse["unchanged"])
    nse_err   = nse.get("error") or ""

    sig_rows, n_buy, n_sell, n_skip = build_signal_rows(_last_signals)
    pnl_color1 = "#00c896" if net1 >= 0 else "#ff4d4d"
    pnl_color2 = "#00c896" if net2 >= 0 else "#ff4d4d"

    CSS = """
    *{box-sizing:border-box;margin:0;padding:0;}
    body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;font-size:14px;}
    .topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;}
    .topbar-left{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
    .bot-title{font-size:1.1rem;font-weight:700;color:#58a6ff;}
    .badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;background:#21262d;color:#8b949e;border:1px solid #30363d;}
    .topbar-right{text-align:right;font-size:12px;color:#8b949e;}
    .container{padding:20px;}
    .main-tabs{display:flex;gap:0;border-bottom:2px solid #30363d;margin-bottom:20px;}
    .main-tab-btn{background:none;border:none;border-bottom:3px solid transparent;color:#8b949e;padding:12px 24px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;white-space:nowrap;}
    .main-tab-btn:hover{color:#c9d1d9;}
    .main-tab-btn.active{color:#58a6ff;border-bottom-color:#58a6ff;}
    .main-tab-pane{display:none;}
    .main-tab-pane.active{display:block;}
    .screener-tabs{display:flex;gap:8px;margin-bottom:20px;}
    .screener-btn{background:#161b22;border:2px solid #30363d;border-radius:10px;color:#8b949e;padding:10px 24px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;}
    .screener-btn:hover{border-color:#58a6ff;color:#c9d1d9;}
    .screener-btn.active{border-color:#58a6ff;color:#58a6ff;background:#1c2128;}
    .screener-panel{display:none;}
    .screener-panel.active{display:block;}
    .stat-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px;}
    .stat-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 10px;text-align:center;}
    .stat-label{font-size:11px;color:#8b949e;margin-bottom:6px;}
    .stat-value{font-size:1.7rem;font-weight:700;}
    .pnl-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px;}
    .tabs{display:flex;gap:0;border-bottom:1px solid #30363d;margin-bottom:16px;}
    .tab-btn{background:none;border:none;border-bottom:3px solid transparent;color:#8b949e;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .2s;white-space:nowrap;}
    .tab-btn:hover{color:#c9d1d9;}
    .tab-btn.active{color:#58a6ff;border-bottom-color:#58a6ff;}
    .tab-pane{display:none;}
    .tab-pane.active{display:block;}
    .table-wrap{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden;overflow-x:auto;}
    table{width:100%;border-collapse:collapse;font-size:13px;}
    th{background:#21262d;color:#8b949e;font-weight:500;padding:10px 14px;text-align:left;border-bottom:1px solid #30363d;white-space:nowrap;}
    td{padding:10px 14px;border-bottom:1px solid #21262d;vertical-align:middle;white-space:nowrap;}
    tr:last-child td{border-bottom:none;}
    tr:hover td{background:#1c2128;}
    .hint{font-size:12px;color:#8b949e;margin-bottom:8px;}
    .info-bar{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 14px;margin-bottom:16px;font-size:12px;color:#8b949e;display:flex;gap:20px;flex-wrap:wrap;}
    .info-bar span{color:#c9d1d9;font-weight:600;}
    .nse-section-title{font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin:20px 0 10px;}
    .nse-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
    .error-bar{background:#2d1a1a;border:1px solid #6e2020;border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#ff4d4d;}
    .scan-bar{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px;margin-bottom:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
    .scan-btn{background:#238636;border:none;border-radius:6px;color:#fff;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;}
    .scan-btn:hover{background:#2ea043;}
    .sig-stat{font-size:13px;}
    @media(max-width:700px){.stat-grid{grid-template-columns:repeat(3,1fr);}.nse-grid{grid-template-columns:1fr;}}
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta http-equiv="refresh" content="30"/>
  <title>Chartink Bot Dashboard</title>
  <style>{CSS}</style>
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
    <div>&#8635; Auto-refresh 30s</div>
  </div>
</div>

<div class="container">
  <div class="main-tabs">
    <button class="main-tab-btn active" onclick="showMainTab('trading',this)">&#127939; Trading Bots</button>
    <button class="main-tab-btn"        onclick="showMainTab('signals',this)">&#129302; Signal Engine &nbsp;<span style="background:#238636;color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;">{n_buy}B {n_sell}S</span></button>
    <button class="main-tab-btn"        onclick="showMainTab('nse',this)">&#128200; NSE Market</button>
  </div>

  <!-- ══ TRADING BOTS ══════════════════════════ -->
  <div id="main-trading" class="main-tab-pane active">
    <div class="screener-tabs">
      <button class="screener-btn active" onclick="showScreener('s1',this)">&#128209; tazbul &nbsp;|&nbsp; Open:{len(open_trades)} &nbsp;|&nbsp; P&amp;L:&#8377;{net1}</button>
      <button class="screener-btn"        onclick="showScreener('s2',this)">&#128209; TazAmol-Test1 &nbsp;|&nbsp; Open:{len(open_trades2)} &nbsp;|&nbsp; P&amp;L:&#8377;{net2}</button>
    </div>
    <div id="s1" class="screener-panel active">
      <div class="info-bar">Screener:<span>tazbul</span>&nbsp;|&nbsp;SL:<span>{SL_PERCENT}%</span>&nbsp;|&nbsp;TP:<span>{TP_PERCENT}%</span>&nbsp;|&nbsp;Capital:<span>&#8377;{CAPITAL_PER_TRADE}</span>&nbsp;|&nbsp;Webhook:<span>/alert</span></div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Open</div><div class="stat-value" style="color:#f0b429;">{len(open_trades)}</div></div>
        <div class="stat-card"><div class="stat-label">Trades Today</div><div class="stat-value" style="color:#58a6ff;">{len(today1)}</div></div>
        <div class="stat-card"><div class="stat-label">Winners</div><div class="stat-value" style="color:#00c896;">{len(w1)}</div></div>
        <div class="stat-card"><div class="stat-label">Losers</div><div class="stat-value" style="color:#ff4d4d;">{len(l1)}</div></div>
        <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:#a78bfa;">{wr1}%</div></div>
        <div class="stat-card"><div class="stat-label">Net P&amp;L</div><div class="stat-value" style="color:{pnl_color1};">&#8377;{net1}</div></div>
      </div>
      <div class="pnl-grid">
        <div class="stat-card"><div class="stat-label">Gross Profit</div><div class="stat-value" style="color:#00c896;">&#8377;{gp1}</div></div>
        <div class="stat-card"><div class="stat-label">Gross Loss</div><div class="stat-value" style="color:#ff4d4d;">&#8377;{gl1}</div></div>
        <div class="stat-card"><div class="stat-label">Capital/Trade</div><div class="stat-value" style="color:#58a6ff;">&#8377;{CAPITAL_PER_TRADE}</div></div>
      </div>
      <div class="tabs">
        <button class="tab-btn active" onclick="showTab('s1','open',this)">Open ({len(open_trades)})</button>
        <button class="tab-btn" onclick="showTab('s1','closed',this)">Today ({len(today1)})</button>
        <button class="tab-btn" onclick="showTab('s1','history',this)">History</button>
      </div>
      <div id="s1-open" class="tab-pane active"><div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry</th><th>Qty</th><th>SL</th><th>TP</th><th>Capital</th><th>Time</th></tr></thead>
        <tbody>{build_table_rows(open_trades)}</tbody></table></div></div>
      <div id="s1-closed" class="tab-pane"><div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Time</th></tr></thead>
        <tbody>{build_closed_rows(today1)}</tbody></table></div></div>
      <div id="s1-history" class="tab-pane"><div class="hint">Last 50 trades</div><div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
        <tbody>{build_history_rows(hist1)}</tbody></table></div></div>
    </div>
    <div id="s2" class="screener-panel">
      <div class="info-bar">Screener:<span>TazAmol-Test1</span>&nbsp;|&nbsp;SL:<span>{BOT2_SL}%</span>&nbsp;|&nbsp;TP:<span>{BOT2_TP}%</span>&nbsp;|&nbsp;Capital:<span>&#8377;{BOT2_CAPITAL}</span>&nbsp;|&nbsp;Webhook:<span>/alert2</span></div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Open</div><div class="stat-value" style="color:#f0b429;">{len(open_trades2)}</div></div>
        <div class="stat-card"><div class="stat-label">Trades Today</div><div class="stat-value" style="color:#58a6ff;">{len(today2)}</div></div>
        <div class="stat-card"><div class="stat-label">Winners</div><div class="stat-value" style="color:#00c896;">{len(w2)}</div></div>
        <div class="stat-card"><div class="stat-label">Losers</div><div class="stat-value" style="color:#ff4d4d;">{len(l2)}</div></div>
        <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:#a78bfa;">{wr2}%</div></div>
        <div class="stat-card"><div class="stat-label">Net P&amp;L</div><div class="stat-value" style="color:{pnl_color2};">&#8377;{net2}</div></div>
      </div>
      <div class="pnl-grid">
        <div class="stat-card"><div class="stat-label">Gross Profit</div><div class="stat-value" style="color:#00c896;">&#8377;{gp2}</div></div>
        <div class="stat-card"><div class="stat-label">Gross Loss</div><div class="stat-value" style="color:#ff4d4d;">&#8377;{gl2}</div></div>
        <div class="stat-card"><div class="stat-label">Capital/Trade</div><div class="stat-value" style="color:#58a6ff;">&#8377;{BOT2_CAPITAL}</div></div>
      </div>
      <div class="tabs">
        <button class="tab-btn active" onclick="showTab('s2','open',this)">Open ({len(open_trades2)})</button>
        <button class="tab-btn" onclick="showTab('s2','closed',this)">Today ({len(today2)})</button>
        <button class="tab-btn" onclick="showTab('s2','history',this)">History</button>
      </div>
      <div id="s2-open" class="tab-pane active"><div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry</th><th>Qty</th><th>SL</th><th>TP</th><th>Capital</th><th>Time</th></tr></thead>
        <tbody>{build_table_rows(open_trades2)}</tbody></table></div></div>
      <div id="s2-closed" class="tab-pane"><div class="table-wrap"><table>
        <thead><tr><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Time</th></tr></thead>
        <tbody>{build_closed_rows(today2)}</tbody></table></div></div>
      <div id="s2-history" class="tab-pane"><div class="hint">Last 50 trades</div><div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
        <tbody>{build_history_rows(hist2)}</tbody></table></div></div>
    </div>
  </div>

  <!-- ══ SIGNAL ENGINE ═════════════════════════ -->
  <div id="main-signals" class="main-tab-pane">
    <div class="scan-bar">
      <button class="scan-btn" onclick="triggerScan()">&#9654; Scan Now</button>
      <span class="sig-stat">Last scan: <b>{_last_scan_time}</b></span>
      <span class="sig-stat">&#9650; BUY: <b style="color:#00c896;">{n_buy}</b></span>
      <span class="sig-stat">&#9660; SELL: <b style="color:#ff4d4d;">{n_sell}</b></span>
      <span class="sig-stat">&#8213; Skip: <b style="color:#8b949e;">{n_skip}</b></span>
      <span class="sig-stat" style="color:#8b949e;font-size:12px;">EMA13/50 | ADX&gt;20 | Candle Quality | Market Dir | ATR SL/TP | 5-min candles</span>
    </div>
    <div class="info-bar">
      Universe: <span>All Nifty 50 stocks</span> &nbsp;|&nbsp;
      Timeframe: <span>5-min</span> &nbsp;|&nbsp;
      SL: <span>ATR × {ATR_SL_MULT}</span> &nbsp;|&nbsp;
      TP: <span>ATR × {ATR_TP_MULT}</span> &nbsp;|&nbsp;
      Auto-scan: <span>Every 5 min (market hours)</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Signal</th><th>Symbol</th><th>Entry &#8377;</th><th>SL &#8377;</th><th>TP &#8377;</th><th>ATR</th><th>Reason</th></tr></thead>
      <tbody>{sig_rows}</tbody>
    </table></div>
  </div>

  <!-- ══ NSE MARKET ════════════════════════════ -->
  <div id="main-nse" class="main-tab-pane">
    <div class="info-bar">Source:<span>NSE India</span>&nbsp;|&nbsp;Cache:<span>60s</span>&nbsp;|&nbsp;Fetched:<span>{nse.get('fetched_at','')}</span></div>
    {"<div class='error-bar'>&#9888; " + nse_err + "</div>" if nse_err else ""}
    <div class="nse-section-title">&#128200; Market Breadth — Nifty 50</div>
    {adv_dec_h}
    <div class="nse-grid">
      <div>
        <div class="nse-section-title">&#9728; Pre-Open Movers (Nifty 50)</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Symbol</th><th>IEP &#8377;</th><th>LTP &#8377;</th><th>Change %</th><th>Volume</th></tr></thead>
          <tbody>{preopen_r}</tbody>
        </table></div>
      </div>
      <div>
        <div class="nse-section-title">&#127970; Sector Activity</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Sector</th><th>Volume</th><th>Change %</th></tr></thead>
          <tbody>{sector_r}</tbody>
        </table></div>
      </div>
    </div>
  </div>
</div>

<script>
function showMainTab(id,btn){{
  document.querySelectorAll('.main-tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.main-tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('main-'+id).classList.add('active');
  btn.classList.add('active');
}}
function showScreener(id,btn){{
  document.querySelectorAll('.screener-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.screener-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
function showTab(s,name,btn){{
  document.querySelectorAll('#'+s+' .tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('#'+s+' .tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(s+'-'+name).classList.add('active');
  btn.classList.add('active');
}}
function triggerScan(){{
  fetch('/scan').then(()=>setTimeout(()=>location.reload(),8000));
  document.querySelector('.scan-btn').textContent='⏳ Scanning...';
}}
</script>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────

print("🚀 Starting Chartink Bot (Dual Screener + Signal Engine)...")
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
