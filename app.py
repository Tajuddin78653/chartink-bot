from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import pytz, os, time, threading, requests, csv

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
os.makedirs("logs", exist_ok=True)

# ── Bot 1 — tazbul ────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
CHAT_ID           = os.environ.get("CHAT_ID", "")
CAPITAL_PER_TRADE = 10000
SL_PERCENT        = 1.0
TP_PERCENT        = 1.5

# ── Bot 2 — TazAmol-Test1 ─────────────────────
BOT2_TOKEN        = "8030391810:AAFxJefvbNmdK97VZZQe2VJ9O1477U-Z8Ks"
BOT2_CHAT_ID      = "527293574"
BOT2_SL           = 1.0
BOT2_TP           = 1.0
BOT2_CAPITAL      = 10000

# ── Shared ────────────────────────────────────
PAPER_TRADING  = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PORT           = int(os.environ.get("PORT", 10000))
MARKET_OPEN    = dtime(9,  15)
MARKET_CLOSE   = dtime(15, 30)
FORCE_EXIT     = dtime(15, 12)
ATR_SL_MULT    = 1.5
ATR_TP_MULT    = 2.0
ADX_TREND_MIN  = 20

# ── State ─────────────────────────────────────
open_trades   = {}; closed_today  = []; traded_today  = set()
open_trades2  = {}; closed_today2 = []; traded_today2 = set()

# ── NSE cache ─────────────────────────────────
_nse_cache = {}; _nse_cache_time = 0; NSE_CACHE_TTL = 120

# ── Signal Engine ─────────────────────────────
_last_signals = []; _last_scan_time = "Never"

# ── Nifty 50 ──────────────────────────────────
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
SECTORS_YAHOO = {
    "NIFTY BANK":"^NSEBANK","NIFTY IT":"^CNXIT",
    "NIFTY AUTO":"^CNXAUTO","NIFTY PHARMA":"^CNXPHARMA",
    "NIFTY FMCG":"^CNXFMCG","NIFTY METAL":"^CNXMETAL",
    "NIFTY ENERGY":"^CNXENERGY","NIFTY REALTY":"^CNXREALTY",
}

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def send(msg, token=None, chat_id=None):
    t = token or BOT_TOKEN; c = chat_id or CHAT_ID
    try:
        r = requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
            data={"chat_id":c,"text":msg,"parse_mode":"HTML"}, timeout=10)
        print("✅ TG sent!" if r.status_code==200 else f"❌ {r.text}")
    except Exception as e: print(f"❌ TG: {e}")

def now_ist(): return datetime.now(IST).time()
def is_market_hours(): return MARKET_OPEN <= now_ist() <= MARKET_CLOSE
def time_str(): return datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")

def get_price_yahoo(symbol):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS",
            headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        return round(float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]),2)
    except: return None

def get_price_nse(symbol):
    try:
        s = requests.Session()
        h = {"User-Agent":"Mozilla/5.0","Accept":"*/*",
             "Referer":"https://www.nseindia.com","Accept-Language":"en-US,en;q=0.9"}
        s.get("https://www.nseindia.com", headers=h, timeout=10)
        r = s.get(f"https://www.nseindia.com/api/quote-equity?symbol={symbol}", headers=h, timeout=10)
        return round(float(r.json()["priceInfo"]["lastPrice"]),2)
    except: return None

def get_price(symbol, chartink_price=None):
    if chartink_price:
        try:
            p = round(float(chartink_price),2)
            if p > 0: return p
        except: pass
    return get_price_nse(symbol) or get_price_yahoo(symbol)

def calculate_trade(symbol, price, sl_pct, tp_pct, capital):
    sl  = round(price*(1-sl_pct/100),2); tp = round(price*(1+tp_pct/100),2)
    qty = max(1,int(capital/price))
    return {"symbol":symbol,"entry":price,"qty":qty,"sl":sl,"tp":tp,
            "capital_used":round(qty*price,2),
            "risk_amt":round((price-sl)*qty,2),
            "reward_amt":round((tp-price)*qty,2),
            "entry_time":time_str(),"exit_time":None}

def log_trade(trade, exit_price, pnl, reason, logfile):
    exists = os.path.exists(logfile)
    with open(logfile,"a",newline="") as fp:
        w = csv.writer(fp)
        if not exists:
            w.writerow(["date","symbol","entry","exit","qty","sl","tp",
                        "risk","reward","pnl","result","reason","entry_time","exit_time"])
        w.writerow([datetime.now(IST).strftime("%Y-%m-%d"),
            trade["symbol"],trade["entry"],exit_price,trade["qty"],
            trade["sl"],trade["tp"],trade["risk_amt"],trade["reward_amt"],
            pnl,"WIN" if pnl>=0 else "LOSS",reason,trade["entry_time"],trade["exit_time"]])

# ─────────────────────────────────────────────
#  LIVE PRICES API
# ─────────────────────────────────────────────
@app.route("/prices", methods=["GET"])
def prices_api():
    """Returns live price + unrealised PnL for all open positions (both bots)."""
    result = {}
    for sym, t in open_trades.items():
        price = get_price(sym)
        if price:
            unreal = round((price - t["entry"]) * t["qty"], 2)
            pct    = round((price - t["entry"]) / t["entry"] * 100, 2)
            result[sym] = {"price":price, "unrealised_pnl":unreal, "pct":pct, "bot":"1"}
    for sym, t in open_trades2.items():
        price = get_price(sym)
        if price:
            unreal = round((price - t["entry"]) * t["qty"], 2)
            pct    = round((price - t["entry"]) / t["entry"] * 100, 2)
            result[sym+"__2"] = {"price":price, "unrealised_pnl":unreal, "pct":pct, "bot":"2"}
    return jsonify(result), 200

# ─────────────────────────────────────────────
#  MANUAL CLOSE API
# ─────────────────────────────────────────────
@app.route("/close/<bot>/<symbol>", methods=["POST"])
def manual_close(bot, symbol):
    symbol = symbol.upper()
    if bot == "1":
        if symbol not in open_trades:
            return jsonify({"status":"not found"}), 404
        price = get_price(symbol) or open_trades[symbol]["entry"]
        close_trade(symbol, price, "🖱️ Manual Close")
        return jsonify({"status":"closed","symbol":symbol,"price":price}), 200
    elif bot == "2":
        if symbol not in open_trades2:
            return jsonify({"status":"not found"}), 404
        price = get_price(symbol) or open_trades2[symbol]["entry"]
        close_trade2(symbol, price, "🖱️ Manual Close")
        return jsonify({"status":"closed","symbol":symbol,"price":price}), 200
    return jsonify({"status":"invalid bot"}), 400

# ─────────────────────────────────────────────
#  NSE MARKET DATA — Yahoo Finance
# ─────────────────────────────────────────────
def fetch_nse_data():
    global _nse_cache, _nse_cache_time
    if _nse_cache and (time.time()-_nse_cache_time) < NSE_CACHE_TTL:
        return _nse_cache
    result = {"preopen":[],"advances":0,"declines":0,"unchanged":0,
              "sectors":[],"fetched_at":time_str(),"error":None,
              "nifty_ltp":0,"nifty_chg":0}
    try:
        import yfinance as yf
        try:
            nh = yf.Ticker("^NSEI").history(period="2d",interval="1d")
            if len(nh)>=2:
                result["nifty_ltp"] = round(float(nh["Close"].iloc[-1]),2)
                prev = float(nh["Close"].iloc[-2])
                result["nifty_chg"] = round((result["nifty_ltp"]-prev)/prev*100,2)
        except: pass
        adv=dec=unc=0; stock_list=[]
        for sym in NIFTY50:
            try:
                h = yf.Ticker(f"{sym}.NS").history(period="2d",interval="1d")
                if len(h)<2: continue
                ltp=round(float(h["Close"].iloc[-1]),2)
                prev=round(float(h["Close"].iloc[-2]),2)
                chg=round(ltp-prev,2); pchg=round((chg/prev)*100,2) if prev else 0
                vol=int(h["Volume"].iloc[-1])
                if pchg>0: adv+=1
                elif pchg<0: dec+=1
                else: unc+=1
                stock_list.append({"symbol":sym,"ltp":ltp,"iep":ltp,"change":chg,"pchange":pchg,"volume":vol})
            except: pass
        stock_list.sort(key=lambda x:abs(x["pchange"]),reverse=True)
        result["preopen"]=stock_list; result["advances"]=adv
        result["declines"]=dec; result["unchanged"]=unc
        sector_list=[]
        for name,ticker in SECTORS_YAHOO.items():
            try:
                h=yf.Ticker(ticker).history(period="2d",interval="1d")
                if len(h)<2: continue
                ltp=float(h["Close"].iloc[-1]); prev=float(h["Close"].iloc[-2])
                pchg=round((ltp-prev)/prev*100,2) if prev else 0
                vol=int(h["Volume"].iloc[-1])
                sector_list.append({"name":name,"pchange":pchg,"volume":vol})
            except: pass
        sector_list.sort(key=lambda x:x["volume"],reverse=True)
        result["sectors"]=sector_list
    except Exception as e: result["error"]=str(e)
    result["fetched_at"]=time_str()
    _nse_cache=result; _nse_cache_time=time.time()
    return result

# ─────────────────────────────────────────────
#  SIGNAL ENGINE — 6 Rules
# ─────────────────────────────────────────────
def fetch_candles(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(f"{symbol}.NS").history(period="5d",interval="5m")
        if df is None or len(df)<60: return None
        df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
        return df[["open","high","low","close","volume"]].copy()
    except: return None

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_atr(df, window=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr = (h-l).combine((h-c.shift()).abs(),max).combine((l-c.shift()).abs(),max)
    return tr.rolling(window).mean()

def calc_adx(df, window=14):
    try:
        h,l,c = df["high"],df["low"],df["close"]
        up=h-h.shift(); down=l.shift()-l
        pdm=up.where((up>down)&(up>0),0.0); ndm=down.where((down>up)&(down>0),0.0)
        tr=(h-l).combine((h-c.shift()).abs(),max).combine((l-c.shift()).abs(),max)
        atr=tr.rolling(window).mean()
        pdi=100*pdm.rolling(window).mean()/atr; ndi=100*ndm.rolling(window).mean()/atr
        dx=(100*(pdi-ndi).abs()/(pdi+ndi)).replace([float("inf"),float("nan")],0)
        return dx.rolling(window).mean()
    except: return None

def get_nifty_direction():
    try:
        import yfinance as yf
        df = yf.Ticker("^NSEI").history(period="2d",interval="5m")
        if df is None or len(df)<14: return "FLAT"
        closes=df["Close"]; ema13=closes.ewm(span=13,adjust=False).mean()
        last=float(closes.iloc[-1]); e13=float(ema13.iloc[-1])
        if last>e13*1.001: return "UP"
        elif last<e13*0.999: return "DOWN"
        return "FLAT"
    except: return "FLAT"

def check_signal(symbol, market_dir):
    res={"symbol":symbol,"signal":"SKIP","reason":"","entry":0,"sl":0,"tp":0,"atr":0}
    df=fetch_candles(symbol)
    if df is None: res["reason"]="No candle data"; return res
    df["ema13"]=calc_ema(df["close"],13); df["ema50"]=calc_ema(df["close"],50)
    df["atr"]=calc_atr(df)
    adx_s=calc_adx(df); df["adx"]=adx_s if adx_s is not None else 25.0
    df=df.dropna(subset=["ema13","ema50","atr","adx"])
    if len(df)<3: res["reason"]="Not enough data"; return res
    cur=df.iloc[-1]; prev=df.iloc[-2]
    e13c=float(cur["ema13"]); e50c=float(cur["ema50"])
    e13p=float(prev["ema13"]); e50p=float(prev["ema50"])
    atr=float(cur["atr"]); adx=float(cur["adx"])
    close=float(cur["close"]); opn=float(cur["open"])
    high=float(cur["high"]); low=float(cur["low"])
    res["atr"]=round(atr,2); res["entry"]=round(close,2)
    if adx<ADX_TREND_MIN: res["reason"]=f"Consolidating ADX={adx:.1f}"; return res
    rng=high-low
    if rng>0:
        body=abs(close-opn)/rng; shadow=((high-max(close,opn))+(min(close,opn)-low))/rng
        dist=abs(close-e13c)/e13c*100
        if body<0.4: res["reason"]=f"Small body {body:.2f}"; return res
        if shadow>0.5: res["reason"]=f"Big shadow {shadow:.2f}"; return res
        if dist>1.0: res["reason"]=f"Far EMA13 {dist:.2f}%"; return res
    crossed_up=(e13p<=e50p)and(e13c>e50c); crossed_down=(e13p>=e50p)and(e13c<e50c)
    if not crossed_up and not crossed_down: res["reason"]="No EMA crossover"; return res
    if crossed_up and market_dir=="DOWN": res["reason"]="BUY but Nifty DOWN"; return res
    if crossed_down and market_dir=="UP": res["reason"]="SELL but Nifty UP"; return res
    if crossed_up:
        res["signal"]="BUY"; res["sl"]=round(close-ATR_SL_MULT*atr,2)
        res["tp"]=round(close+ATR_TP_MULT*atr,2)
        res["reason"]=f"EMA13>EMA50 | ADX={adx:.1f} | Nifty={market_dir}"
    else:
        res["signal"]="SELL"; res["sl"]=round(close+ATR_SL_MULT*atr,2)
        res["tp"]=round(close-ATR_TP_MULT*atr,2)
        res["reason"]=f"EMA13<EMA50 | ADX={adx:.1f} | Nifty={market_dir}"
    return res

def run_signal_scan():
    global _last_signals, _last_scan_time
    print(f"🔍 Scan {time_str()}")
    market_dir=get_nifty_direction(); results=[]
    for symbol in NIFTY50:
        try:
            r=check_signal(symbol,market_dir); r["market_dir"]=market_dir; results.append(r)
            if r["signal"]=="BUY" and symbol not in traded_today2 and symbol not in open_trades2:
                qty=max(1,int(BOT2_CAPITAL/r["entry"]))
                trade={"symbol":symbol,"entry":r["entry"],"qty":qty,
                       "sl":r["sl"],"tp":r["tp"],
                       "capital_used":round(qty*r["entry"],2),
                       "risk_amt":round(abs(r["entry"]-r["sl"])*qty,2),
                       "reward_amt":round(abs(r["tp"]-r["entry"])*qty,2),
                       "entry_time":time_str(),"exit_time":None}
                open_trades2[symbol]=trade; traded_today2.add(symbol)
                send(f"🤖 <b>SIGNAL ENGINE — BUY</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━\n"
                     f"📌 Stock  : <b>{symbol}</b>\n"
                     f"💰 Entry  : ₹{r['entry']}\n"
                     f"🔴 SL     : ₹{r['sl']}\n"
                     f"🟢 TP     : ₹{r['tp']}\n"
                     f"📊 Reason : {r['reason']}\n"
                     f"🕐 {time_str()}",
                     token=BOT2_TOKEN, chat_id=BOT2_CHAT_ID)
        except Exception as e:
            results.append({"symbol":symbol,"signal":"SKIP","reason":str(e),
                            "entry":0,"sl":0,"tp":0,"atr":0,"market_dir":market_dir})
    _last_signals=results; _last_scan_time=time_str()
    b=sum(1 for r in results if r["signal"]=="BUY")
    s=sum(1 for r in results if r["signal"]=="SELL")
    print(f"✅ Scan done BUY:{b} SELL:{s} SKIP:{len(results)-b-s}")

# ─────────────────────────────────────────────
#  BOT 1 — tazbul
# ─────────────────────────────────────────────
def open_trade(symbol, price):
    if symbol in open_trades or symbol in traded_today: return
    trade=calculate_trade(symbol,price,SL_PERCENT,TP_PERCENT,CAPITAL_PER_TRADE)
    open_trades[symbol]=trade; traded_today.add(symbol)
    send(f"📝 <b>{'🧪 PAPER' if PAPER_TRADING else '⚡ LIVE'} ENTRY — tazbul</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📌 Stock : <b>{symbol}</b>\n"
         f"💰 Entry : ₹{price}\n"
         f"📦 Qty   : {trade['qty']}\n"
         f"🔴 SL    : ₹{trade['sl']}\n"
         f"🟢 TP    : ₹{trade['tp']}\n"
         f"💵 Cap   : ₹{trade['capital_used']}\n"
         f"🕐 {trade['entry_time']}")

def close_trade(symbol, exit_price, reason):
    if symbol not in open_trades: return
    trade=open_trades.pop(symbol); trade["exit_time"]=time_str()
    pnl=round((exit_price-trade["entry"])*trade["qty"],2)
    send(f"{'✅' if pnl>=0 else '❌'} <b>{'🧪 PAPER' if PAPER_TRADING else '⚡ LIVE'} EXIT — tazbul</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📌 Stock : <b>{symbol}</b>\n"
         f"💰 Entry : ₹{trade['entry']} → ₹{exit_price}\n"
         f"{'💚' if pnl>=0 else '❤️'} P&L  : ₹{abs(pnl)}\n"
         f"📝 {reason}\n🕐 {trade['exit_time']}")
    closed_today.append({"symbol":symbol,"pnl":pnl})
    log_trade(trade,exit_price,pnl,reason,"logs/trades.csv")

def check_positions():
    for sym in list(open_trades):
        t=open_trades.get(sym)
        if not t: continue
        p=get_price(sym)
        if not p: continue
        if p>=t["tp"]: close_trade(sym,p,"🎯 Take Profit")
        elif p<=t["sl"]: close_trade(sym,p,"🔴 Stop Loss")

def send_eod():
    w=[t for t in closed_today if t["pnl"]>=0]; l=[t for t in closed_today if t["pnl"]<0]
    net=round(sum(t["pnl"] for t in closed_today),2)
    send(f"📋 <b>EOD — tazbul</b>\n"
         f"📅 {datetime.now(IST).strftime('%d %b %Y')}\n"
         f"📊 Trades:{len(closed_today)} ✅{len(w)} ❌{len(l)}\n"
         f"{'💚' if net>=0 else '❤️'} Net: ₹{net}")
    closed_today.clear(); traded_today.clear()

# ─────────────────────────────────────────────
#  BOT 2 — TazAmol-Test1
# ─────────────────────────────────────────────
def open_trade2(symbol, price):
    if symbol in open_trades2 or symbol in traded_today2: return
    trade=calculate_trade(symbol,price,BOT2_SL,BOT2_TP,BOT2_CAPITAL)
    open_trades2[symbol]=trade; traded_today2.add(symbol)
    send(f"📝 <b>{'🧪 PAPER' if PAPER_TRADING else '⚡ LIVE'} ENTRY — TazAmol</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📌 Stock : <b>{symbol}</b>\n"
         f"💰 Entry : ₹{price}\n"
         f"📦 Qty   : {trade['qty']}\n"
         f"🔴 SL    : ₹{trade['sl']}\n"
         f"🟢 TP    : ₹{trade['tp']}\n"
         f"💵 Cap   : ₹{trade['capital_used']}\n"
         f"🕐 {trade['entry_time']}",
         token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)

def close_trade2(symbol, exit_price, reason):
    if symbol not in open_trades2: return
    trade=open_trades2.pop(symbol); trade["exit_time"]=time_str()
    pnl=round((exit_price-trade["entry"])*trade["qty"],2)
    send(f"{'✅' if pnl>=0 else '❌'} <b>{'🧪 PAPER' if PAPER_TRADING else '⚡ LIVE'} EXIT — TazAmol</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n"
         f"📌 Stock : <b>{symbol}</b>\n"
         f"💰 Entry : ₹{trade['entry']} → ₹{exit_price}\n"
         f"{'💚' if pnl>=0 else '❤️'} P&L  : ₹{abs(pnl)}\n"
         f"📝 {reason}\n🕐 {trade['exit_time']}",
         token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)
    closed_today2.append({"symbol":symbol,"pnl":pnl})
    log_trade(trade,exit_price,pnl,reason,"logs/trades2.csv")

def check_positions2():
    for sym in list(open_trades2):
        t=open_trades2.get(sym)
        if not t: continue
        p=get_price(sym)
        if not p: continue
        if p>=t["tp"]: close_trade2(sym,p,"🎯 Take Profit")
        elif p<=t["sl"]: close_trade2(sym,p,"🔴 Stop Loss")

def send_eod2():
    w=[t for t in closed_today2 if t["pnl"]>=0]; l=[t for t in closed_today2 if t["pnl"]<0]
    net=round(sum(t["pnl"] for t in closed_today2),2)
    send(f"📋 <b>EOD — TazAmol-Test1</b>\n"
         f"📅 {datetime.now(IST).strftime('%d %b %Y')}\n"
         f"📊 Trades:{len(closed_today2)} ✅{len(w)} ❌{len(l)}\n"
         f"{'💚' if net>=0 else '❤️'} Net: ₹{net}",
         token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)
    closed_today2.clear(); traded_today2.clear()

# ─────────────────────────────────────────────
#  MONITOR
# ─────────────────────────────────────────────
def run_monitor():
    eod_sent=False; last_scan_min=-1
    print("📈 Monitor started")
    while True:
        try:
            t=now_ist(); cur_min=datetime.now(IST).minute
            if t>=FORCE_EXIT:
                if open_trades:
                    send("⏰ <b>Force closing tazbul!</b>")
                    for s in list(open_trades): close_trade(s,get_price(s) or open_trades[s]["entry"],"⏰ Force Exit")
                if open_trades2:
                    send("⏰ <b>Force closing TazAmol!</b>",token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)
                    for s in list(open_trades2): close_trade2(s,get_price(s) or open_trades2[s]["entry"],"⏰ Force Exit")
            if t>=MARKET_CLOSE and not eod_sent:
                send_eod(); send_eod2(); eod_sent=True
            if t<dtime(9,0): eod_sent=False
            if is_market_hours():
                if open_trades: check_positions()
                if open_trades2: check_positions2()
                if cur_min%5==0 and cur_min!=last_scan_min:
                    last_scan_min=cur_min
                    threading.Thread(target=run_signal_scan,daemon=True).start()
        except Exception as e: print(f"❌ Monitor: {e}")
        time.sleep(60)

# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────
def parse_alert_data():
    """Parse Chartink webhook — handles JSON, form-data, and raw body."""
    # Try JSON first (silent=True + force=True handles any Content-Type)
    data = request.get_json(force=True, silent=True)
    if data:
        return data
    # Try form data (application/x-www-form-urlencoded)
    if request.form:
        return request.form.to_dict()
    # Try raw body as form
    try:
        from urllib.parse import parse_qs
        raw = request.data.decode("utf-8")
        parsed = parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}
    except: pass
    return {}

def parse_stocks_prices(data):
    """Chartink sends: stocks='RELIANCE, TCS' trigger_prices='2450.50 3200.00' (space-separated prices)"""
    raw_stocks = data.get("stocks","")
    raw_prices = data.get("trigger_prices","")
    # stocks: comma or comma+space separated
    stocks = [s.strip().upper() for s in raw_stocks.replace(","," ").split() if s.strip()]
    # prices: space separated OR comma separated
    prices = [p.strip() for p in raw_prices.replace(","," ").split() if p.strip()]
    return stocks, prices

@app.route("/alert",methods=["POST"])
def receive_alert():
    data = parse_alert_data()
    print(f"📥 /alert data: {data}")
    stocks, prices = parse_stocks_prices(data)
    if not stocks: return jsonify({"status":"no stocks","received":str(data)}),400
    results=[]
    for i,sym in enumerate(stocks):
        if sym in traded_today or sym in open_trades:
            results.append({"symbol":sym,"status":"skip"}); continue
        price=get_price(sym,prices[i] if i<len(prices) else None)
        if not price:
            send(f"❌ Price failed: {sym}"); results.append({"symbol":sym,"status":"price failed"}); continue
        open_trade(sym,price); results.append({"symbol":sym,"status":"entered","price":price})
    return jsonify({"status":"processed","results":results}),200

@app.route("/alert2",methods=["POST"])
def receive_alert2():
    data = parse_alert_data()
    print(f"📥 /alert2 data: {data}")
    stocks, prices = parse_stocks_prices(data)
    if not stocks: return jsonify({"status":"no stocks","received":str(data)}),400
    results=[]
    for i,sym in enumerate(stocks):
        if sym in traded_today2 or sym in open_trades2:
            results.append({"symbol":sym,"status":"skip"}); continue
        price=get_price(sym,prices[i] if i<len(prices) else None)
        if not price:
            send(f"❌ Price failed: {sym}",token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)
            results.append({"symbol":sym,"status":"price failed"}); continue
        open_trade2(sym,price); results.append({"symbol":sym,"status":"entered","price":price})
    return jsonify({"status":"processed","results":results}),200

@app.route("/scan",methods=["GET"])
def manual_scan():
    threading.Thread(target=run_signal_scan,daemon=True).start()
    return jsonify({"status":"scan started"}),200

@app.route("/nse-data",methods=["GET"])
def nse_data_api():
    return jsonify(fetch_nse_data()),200

@app.route("/",methods=["GET"])
def home():
    return jsonify({"status":"🟢 Running","time_ist":time_str(),
                    "market_open":is_market_hours(),"last_scan":_last_scan_time}),200

@app.route("/test",methods=["GET"])
def test():
    send(f"✅ Bot1 tazbul OK 🕐{time_str()}")
    send(f"✅ Bot2 TazAmol-Test1 OK 🕐{time_str()}",token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)
    return jsonify({"status":"sent"}),200

@app.route("/report",methods=["GET"])
def report():
    send_eod(); send_eod2(); return jsonify({"status":"sent"}),200

@app.route("/status",methods=["GET"])
def status():
    return jsonify({
        "bot1":{"open":list(open_trades),"closed":len(closed_today),"pnl":round(sum(t["pnl"] for t in closed_today),2)},
        "bot2":{"open":list(open_trades2),"closed":len(closed_today2),"pnl":round(sum(t["pnl"] for t in closed_today2),2)},
    }),200

# ─────────────────────────────────────────────
#  DASHBOARD HELPERS
# ─────────────────────────────────────────────
def load_csv(path):
    if not os.path.exists(path): return []
    with open(path,newline="") as f: return list(csv.DictReader(f))

def stats(rows):
    w=[r for r in rows if r.get("result")=="WIN"]; l=[r for r in rows if r.get("result")=="LOSS"]
    net=round(sum(float(r["pnl"]) for r in rows),2)
    gp=round(sum(float(r["pnl"]) for r in w),2)
    gl=round(abs(sum(float(r["pnl"]) for r in l)),2)
    wr=round(len(w)/len(rows)*100 if rows else 0,1)
    return w,l,net,gp,gl,wr

def tbl_open(d, bot_num):
    """Build open positions table with live price + unrealised PnL + close button."""
    if not d:
        return '<tr><td colspan="9" style="text-align:center;color:#8b949e;padding:20px;">No open positions</td></tr>'
    rows=""
    for s,t in d.items():
        key = s if bot_num=="1" else s+"__2"
        rows += (f'<tr id="row-{bot_num}-{s}">'
                 f'<td><b>{s}</b></td>'
                 f'<td>&#8377;{t["entry"]}</td>'
                 f'<td>{t["qty"]}</td>'
                 f'<td style="color:#ff4d4d;">&#8377;{t["sl"]}</td>'
                 f'<td style="color:#00c896;">&#8377;{t["tp"]}</td>'
                 f'<td>&#8377;{t["capital_used"]}</td>'
                 f'<td id="ltp-{bot_num}-{s}" style="font-weight:700;">—</td>'
                 f'<td id="upnl-{bot_num}-{s}">—</td>'
                 f'<td>{t["entry_time"]}</td>'
                 f'<td><button onclick="closePos(\'{bot_num}\',\'{s}\',this)" '
                 f'style="background:#da3633;border:none;border-radius:5px;color:#fff;'
                 f'padding:3px 10px;font-size:12px;cursor:pointer;">Close</button></td>'
                 f'</tr>')
    return rows

def tbl_closed(rows):
    if not rows: return '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:20px;">No closed trades today</td></tr>'
    out=""
    for r in reversed(rows):
        v=float(r["pnl"]); c="#00c896" if v>=0 else "#ff4d4d"; sg="+" if v>=0 else ""
        out+=f'<tr><td><b>{r["symbol"]}</b></td><td>&#8377;{r["entry"]}</td><td>&#8377;{r["exit"]}</td><td>{r["qty"]}</td><td style="color:{c};font-weight:700;">{sg}&#8377;{v}</td><td>{r["reason"]}</td><td>{r["exit_time"]}</td></tr>'
    return out

def tbl_hist(rows):
    if not rows: return '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:20px;">No history yet</td></tr>'
    out=""
    for r in reversed(rows[-50:]):
        v=float(r["pnl"]); c="#00c896" if v>=0 else "#ff4d4d"; sg="+" if v>=0 else ""
        out+=f'<tr><td>{r["date"]}</td><td><b>{r["symbol"]}</b></td><td>&#8377;{r["entry"]}</td><td>&#8377;{r["exit"]}</td><td>{r["qty"]}</td><td style="color:{c};font-weight:700;">{sg}&#8377;{v}</td><td>{r.get("reason","")}</td></tr>'
    return out

def tbl_signals(sigs):
    buys=[s for s in sigs if s["signal"]=="BUY"]
    sells=[s for s in sigs if s["signal"]=="SELL"]
    skips=[s for s in sigs if s["signal"]=="SKIP"]
    out=""
    for s in buys:
        out+=f'<tr><td><b style="color:#00c896;">&#9650; BUY</b></td><td><b>{s["symbol"]}</b></td><td>&#8377;{s["entry"]}</td><td style="color:#ff4d4d;">&#8377;{s["sl"]}</td><td style="color:#00c896;">&#8377;{s["tp"]}</td><td>&#8377;{s["atr"]}</td><td style="font-size:11px;color:#8b949e;">{s["reason"]}</td></tr>'
    for s in sells:
        out+=f'<tr><td><b style="color:#ff4d4d;">&#9660; SELL</b></td><td><b>{s["symbol"]}</b></td><td>&#8377;{s["entry"]}</td><td style="color:#ff4d4d;">&#8377;{s["sl"]}</td><td style="color:#00c896;">&#8377;{s["tp"]}</td><td>&#8377;{s["atr"]}</td><td style="font-size:11px;color:#8b949e;">{s["reason"]}</td></tr>'
    for s in skips[:15]:
        out+=f'<tr style="opacity:0.4;"><td><b style="color:#8b949e;">&#8213; SKIP</b></td><td>{s["symbol"]}</td><td>&#8377;{s["entry"]}</td><td>&#8212;</td><td>&#8212;</td><td>&#8212;</td><td style="font-size:11px;color:#8b949e;">{s["reason"]}</td></tr>'
    if not out:
        out='<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:20px;">No scan results — click Scan Now or wait for auto-scan every 5 min</td></tr>'
    return out,len(buys),len(sells),len(skips)

def tbl_preopen(stocks):
    if not stocks: return '<tr><td colspan="5" style="text-align:center;color:#8b949e;padding:20px;">No data</td></tr>'
    out=""
    for s in stocks:
        p=float(s.get("pchange",0)); c="#00c896" if p>=0 else "#ff4d4d"; sg="+" if p>=0 else ""
        try: vol=f"{int(s.get('volume',0)):,}"
        except: vol="—"
        out+=f'<tr><td><b>{s["symbol"]}</b></td><td>&#8377;{s.get("ltp",0)}</td><td style="color:{c};font-weight:700;">{sg}{p}%</td><td style="color:{c};">{"&#9650;" if p>=0 else "&#9660;"} &#8377;{abs(float(s.get("change",0)))}</td><td>{vol}</td></tr>'
    return out

def tbl_sectors(sectors):
    if not sectors: return '<tr><td colspan="3" style="text-align:center;color:#8b949e;padding:20px;">No data</td></tr>'
    mx=max((s["volume"] for s in sectors),default=1) or 1; out=""
    for s in sectors:
        p=float(s.get("pchange",0)); c="#00c896" if p>=0 else "#ff4d4d"; sg="+" if p>=0 else ""
        bw=int(s["volume"]/mx*100)
        try: vol=f"{int(s['volume']):,}"
        except: vol="—"
        out+=f'<tr><td><b>{s["name"]}</b></td><td><div style="background:#21262d;border-radius:4px;height:12px;width:140px;overflow:hidden;"><div style="background:#58a6ff;height:100%;width:{bw}%;"></div></div><span style="font-size:11px;color:#8b949e;">{vol}</span></td><td style="color:{c};font-weight:700;">{sg}{p}%</td></tr>'
    return out

def adv_dec_html(adv,dec,unc):
    tot=adv+dec+unc or 1; aw=int(adv/tot*100); dw=int(dec/tot*100); uw=100-aw-dw
    return (f'<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:16px;">'
            f'<div class="sc"><div class="sl">&#9650; Advances</div><div class="sv" style="color:#00c896;">{adv}</div></div>'
            f'<div class="sc"><div class="sl">&#9660; Declines</div><div class="sv" style="color:#ff4d4d;">{dec}</div></div>'
            f'<div class="sc"><div class="sl">&#8213; Unchanged</div><div class="sv" style="color:#8b949e;">{unc}</div></div>'
            f'<div style="flex:3;min-width:180px;"><div style="font-size:11px;color:#8b949e;margin-bottom:4px;">Nifty 50 Breadth</div>'
            f'<div style="display:flex;border-radius:6px;overflow:hidden;height:18px;">'
            f'<div style="width:{aw}%;background:#00c896;"></div>'
            f'<div style="width:{uw}%;background:#8b949e;"></div>'
            f'<div style="width:{dw}%;background:#ff4d4d;"></div></div>'
            f'<div style="display:flex;gap:12px;font-size:11px;color:#8b949e;margin-top:3px;">'
            f'<span style="color:#00c896;">&#9650;{aw}%</span><span>&#8213;{uw}%</span>'
            f'<span style="color:#ff4d4d;">&#9660;{dw}%</span></div></div></div>')

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard",methods=["GET"])
def dashboard():
    today_str=datetime.now(IST).strftime("%Y-%m-%d")
    mode_label="🧪 Paper" if PAPER_TRADING else "⚡ Live"
    mkt_status="🟢 Open" if is_market_hours() else "🔴 Closed"

    h1=load_csv("logs/trades.csv"); t1=[r for r in h1 if r.get("date")==today_str]
    h2=load_csv("logs/trades2.csv"); t2=[r for r in h2 if r.get("date")==today_str]
    w1,l1,net1,gp1,gl1,wr1=stats(t1); w2,l2,net2,gp2,gl2,wr2=stats(t2)

    nse=fetch_nse_data()
    sig_rows,nb,ns,nsk=tbl_signals(_last_signals)
    pc1="#00c896" if net1>=0 else "#ff4d4d"; pc2="#00c896" if net2>=0 else "#ff4d4d"
    nifty_c="#00c896" if nse.get("nifty_chg",0)>=0 else "#ff4d4d"
    nifty_sg="+" if nse.get("nifty_chg",0)>=0 else ""

    CSS="""
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;font-size:14px;}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;}
.bot-title{font-size:1.1rem;font-weight:700;color:#58a6ff;}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;background:#21262d;color:#8b949e;border:1px solid #30363d;margin-left:6px;}
.tr{text-align:right;font-size:12px;color:#8b949e;}
.con{padding:20px;}
.mt{display:flex;gap:0;border-bottom:2px solid #30363d;margin-bottom:20px;}
.mtb{background:none;border:none;border-bottom:3px solid transparent;color:#8b949e;padding:12px 22px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;white-space:nowrap;}
.mtb:hover{color:#c9d1d9;} .mtb.active{color:#58a6ff;border-bottom-color:#58a6ff;}
.mtp{display:none;} .mtp.active{display:block;}
.stabs{display:flex;gap:8px;margin-bottom:18px;}
.sb{background:#161b22;border:2px solid #30363d;border-radius:10px;color:#8b949e;padding:10px 20px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;}
.sb:hover{border-color:#58a6ff;color:#c9d1d9;} .sb.active{border-color:#58a6ff;color:#58a6ff;background:#1c2128;}
.sp{display:none;} .sp.active{display:block;}
.sg{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px;}
.pg{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px;}
.sc{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 10px;text-align:center;}
.sl{font-size:11px;color:#8b949e;margin-bottom:5px;} .sv{font-size:1.6rem;font-weight:700;}
.tabs{display:flex;gap:0;border-bottom:1px solid #30363d;margin-bottom:14px;}
.tb{background:none;border:none;border-bottom:3px solid transparent;color:#8b949e;padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .2s;white-space:nowrap;}
.tb:hover{color:#c9d1d9;} .tb.active{color:#58a6ff;border-bottom-color:#58a6ff;}
.tp{display:none;} .tp.active{display:block;}
.tw{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden;overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{background:#21262d;color:#8b949e;font-weight:500;padding:9px 13px;text-align:left;border-bottom:1px solid #30363d;white-space:nowrap;}
td{padding:9px 13px;border-bottom:1px solid #21262d;vertical-align:middle;white-space:nowrap;}
tr:last-child td{border-bottom:none;} tr:hover td{background:#1c2128;}
.ib{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 13px;margin-bottom:14px;font-size:12px;color:#8b949e;display:flex;gap:18px;flex-wrap:wrap;}
.ib span{color:#c9d1d9;font-weight:600;}
.scanbar{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.scanbtn{background:#238636;border:none;border-radius:6px;color:#fff;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;}
.scanbtn:hover{background:#2ea043;}
.ng{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
.nt{font-size:12px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin:16px 0 8px;}
.nifty-bar{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px;margin-bottom:16px;display:flex;align-items:center;gap:20px;flex-wrap:wrap;}
.ltp-status{font-size:11px;color:#8b949e;margin-bottom:10px;}
@media(max-width:700px){{.sg{{grid-template-columns:repeat(3,1fr);}}.ng{{grid-template-columns:1fr;}}}}
"""

    html=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Chartink Bot Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="topbar">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    <span class="bot-title">&#128202; Chartink Bot</span>
    <span class="badge">{mode_label}</span>
    <span class="badge">{mkt_status}</span>
    <span class="badge" style="color:{nifty_c};">Nifty &#8377;{nse.get('nifty_ltp',0)} {nifty_sg}{nse.get('nifty_chg',0)}%</span>
  </div>
  <div class="tr"><div>&#128336; {time_str()}</div><div id="ltp-ts" class="ltp-status">&#8635; Fetching live prices...</div></div>
</div>
<div class="con">
<div class="mt">
  <button class="mtb active" onclick="showMain('trading',this)">&#127939; Trading Bots</button>
  <button class="mtb" onclick="showMain('signals',this)">&#129302; Signal Engine <span style="background:#238636;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px;">{nb}B {ns}S</span></button>
  <button class="mtb" onclick="showMain('nse',this)">&#128200; NSE Market</button>
</div>

<!-- TRADING BOTS -->
<div id="main-trading" class="mtp active">
  <div class="stabs">
    <button class="sb active" onclick="showS('s1',this)">&#128209; tazbul | Open:{len(open_trades)} | P&amp;L:&#8377;{net1}</button>
    <button class="sb" onclick="showS('s2',this)">&#128209; TazAmol-Test1 | Open:{len(open_trades2)} | P&amp;L:&#8377;{net2}</button>
  </div>
  <div id="s1" class="sp active">
    <div class="ib">Screener:<span>tazbul</span> SL:<span>{SL_PERCENT}%</span> TP:<span>{TP_PERCENT}%</span> Capital:<span>&#8377;{CAPITAL_PER_TRADE}</span> Webhook:<span>/alert</span></div>
    <div class="sg">
      <div class="sc"><div class="sl">Open</div><div class="sv" style="color:#f0b429;">{len(open_trades)}</div></div>
      <div class="sc"><div class="sl">Today</div><div class="sv" style="color:#58a6ff;">{len(t1)}</div></div>
      <div class="sc"><div class="sl">Winners</div><div class="sv" style="color:#00c896;">{len(w1)}</div></div>
      <div class="sc"><div class="sl">Losers</div><div class="sv" style="color:#ff4d4d;">{len(l1)}</div></div>
      <div class="sc"><div class="sl">Win Rate</div><div class="sv" style="color:#a78bfa;">{wr1}%</div></div>
      <div class="sc"><div class="sl">Net P&amp;L</div><div class="sv" style="color:{pc1};">&#8377;{net1}</div></div>
    </div>
    <div class="pg">
      <div class="sc"><div class="sl">Gross Profit</div><div class="sv" style="color:#00c896;">&#8377;{gp1}</div></div>
      <div class="sc"><div class="sl">Gross Loss</div><div class="sv" style="color:#ff4d4d;">&#8377;{gl1}</div></div>
      <div class="sc"><div class="sl">Capital/Trade</div><div class="sv" style="color:#58a6ff;">&#8377;{CAPITAL_PER_TRADE}</div></div>
    </div>
    <div class="tabs">
      <button class="tb active" onclick="showT('s1','open',this)">Open ({len(open_trades)})</button>
      <button class="tb" onclick="showT('s1','closed',this)">Today ({len(t1)})</button>
      <button class="tb" onclick="showT('s1','hist',this)">History</button>
    </div>
    <div id="s1-open" class="tp active"><div class="tw"><table>
      <thead><tr><th>Stock</th><th>Entry</th><th>Qty</th><th>SL</th><th>TP</th><th>Capital</th><th>Live Price</th><th>Unreal P&amp;L</th><th>Time</th><th>Action</th></tr></thead>
      <tbody id="open-tbody-1">{tbl_open(open_trades,"1")}</tbody></table></div></div>
    <div id="s1-closed" class="tp"><div class="tw"><table>
      <thead><tr><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Time</th></tr></thead>
      <tbody>{tbl_closed(t1)}</tbody></table></div></div>
    <div id="s1-hist" class="tp"><div class="tw"><table>
      <thead><tr><th>Date</th><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
      <tbody>{tbl_hist(h1)}</tbody></table></div></div>
  </div>
  <div id="s2" class="sp">
    <div class="ib">Screener:<span>TazAmol-Test1</span> SL:<span>{BOT2_SL}%</span> TP:<span>{BOT2_TP}%</span> Capital:<span>&#8377;{BOT2_CAPITAL}</span> Webhook:<span>/alert2</span></div>
    <div class="sg">
      <div class="sc"><div class="sl">Open</div><div class="sv" style="color:#f0b429;">{len(open_trades2)}</div></div>
      <div class="sc"><div class="sl">Today</div><div class="sv" style="color:#58a6ff;">{len(t2)}</div></div>
      <div class="sc"><div class="sl">Winners</div><div class="sv" style="color:#00c896;">{len(w2)}</div></div>
      <div class="sc"><div class="sl">Losers</div><div class="sv" style="color:#ff4d4d;">{len(l2)}</div></div>
      <div class="sc"><div class="sl">Win Rate</div><div class="sv" style="color:#a78bfa;">{wr2}%</div></div>
      <div class="sc"><div class="sl">Net P&amp;L</div><div class="sv" style="color:{pc2};">&#8377;{net2}</div></div>
    </div>
    <div class="pg">
      <div class="sc"><div class="sl">Gross Profit</div><div class="sv" style="color:#00c896;">&#8377;{gp2}</div></div>
      <div class="sc"><div class="sl">Gross Loss</div><div class="sv" style="color:#ff4d4d;">&#8377;{gl2}</div></div>
      <div class="sc"><div class="sl">Capital/Trade</div><div class="sv" style="color:#58a6ff;">&#8377;{BOT2_CAPITAL}</div></div>
    </div>
    <div class="tabs">
      <button class="tb active" onclick="showT('s2','open',this)">Open ({len(open_trades2)})</button>
      <button class="tb" onclick="showT('s2','closed',this)">Today ({len(t2)})</button>
      <button class="tb" onclick="showT('s2','hist',this)">History</button>
    </div>
    <div id="s2-open" class="tp active"><div class="tw"><table>
      <thead><tr><th>Stock</th><th>Entry</th><th>Qty</th><th>SL</th><th>TP</th><th>Capital</th><th>Live Price</th><th>Unreal P&amp;L</th><th>Time</th><th>Action</th></tr></thead>
      <tbody id="open-tbody-2">{tbl_open(open_trades2,"2")}</tbody></table></div></div>
    <div id="s2-closed" class="tp"><div class="tw"><table>
      <thead><tr><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Time</th></tr></thead>
      <tbody>{tbl_closed(t2)}</tbody></table></div></div>
    <div id="s2-hist" class="tp"><div class="tw"><table>
      <thead><tr><th>Date</th><th>Stock</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
      <tbody>{tbl_hist(h2)}</tbody></table></div></div>
  </div>
</div>

<!-- SIGNAL ENGINE -->
<div id="main-signals" class="mtp">
  <div class="scanbar">
    <button class="scanbtn" onclick="doScan(this)">&#9654; Scan Now</button>
    <span style="font-size:13px;">Last: <b>{_last_scan_time}</b></span>
    <span style="font-size:13px;">&#9650; BUY:<b style="color:#00c896;">{nb}</b></span>
    <span style="font-size:13px;">&#9660; SELL:<b style="color:#ff4d4d;">{ns}</b></span>
    <span style="font-size:13px;">&#8213; Skip:<b style="color:#8b949e;">{nsk}</b></span>
  </div>
  <div class="ib">Universe:<span>All Nifty 50</span> TF:<span>5-min</span> SL:<span>ATR&#215;{ATR_SL_MULT}</span> TP:<span>ATR&#215;{ATR_TP_MULT}</span> ADX:<span>&gt;{ADX_TREND_MIN}</span> Auto:<span>Every 5min</span></div>
  <div class="tw"><table>
    <thead><tr><th>Signal</th><th>Symbol</th><th>Entry &#8377;</th><th>SL &#8377;</th><th>TP &#8377;</th><th>ATR</th><th>Reason</th></tr></thead>
    <tbody>{sig_rows}</tbody>
  </table></div>
</div>

<!-- NSE MARKET -->
<div id="main-nse" class="mtp">
  <div class="nifty-bar">
    <span style="font-size:13px;font-weight:700;">&#128200; Nifty 50</span>
    <span style="font-size:1.4rem;font-weight:700;">&#8377;{nse.get('nifty_ltp',0)}</span>
    <span style="font-size:1rem;font-weight:700;color:{nifty_c};">{nifty_sg}{nse.get('nifty_chg',0)}%</span>
    <span style="font-size:12px;color:#8b949e;">Yahoo Finance | {nse.get('fetched_at','')}</span>
  </div>
  {"<div style='background:#2d1a1a;border:1px solid #6e2020;border-radius:8px;padding:8px 13px;margin-bottom:10px;font-size:12px;color:#ff4d4d;'>&#9888; "+nse.get('error','')+"</div>" if nse.get('error') else ""}
  <div class="nt">&#128200; Market Breadth</div>
  {adv_dec_html(nse.get('advances',0),nse.get('declines',0),nse.get('unchanged',0))}
  <div class="ng">
    <div>
      <div class="nt">&#9728; Nifty 50 — Top Movers</div>
      <div class="tw"><table>
        <thead><tr><th>Symbol</th><th>LTP &#8377;</th><th>Change %</th><th>Change &#8377;</th><th>Volume</th></tr></thead>
        <tbody>{tbl_preopen(nse.get('preopen',[]))}</tbody>
      </table></div>
    </div>
    <div>
      <div class="nt">&#127970; Sector Performance</div>
      <div class="tw"><table>
        <thead><tr><th>Sector</th><th>Volume</th><th>Change %</th></tr></thead>
        <tbody>{tbl_sectors(nse.get('sectors',[]))}</tbody>
      </table></div>
    </div>
  </div>
</div>

<!-- CHARTINK GUIDE -->
</div>
<script>
// ── Tab navigation with hash persistence ──────
function showMain(id,btn){{
  document.querySelectorAll('.mtp').forEach(function(p){{p.classList.remove('active');}});
  document.querySelectorAll('.mtb').forEach(function(b){{b.classList.remove('active');}});
  document.getElementById('main-'+id).classList.add('active');
  btn.classList.add('active');
  window.location.hash='tab-'+id;
}}
function showS(id,btn){{
  document.querySelectorAll('.sp').forEach(function(p){{p.classList.remove('active');}});
  document.querySelectorAll('.sb').forEach(function(b){{b.classList.remove('active');}});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  var h=window.location.hash.split('|')[0];
  window.location.hash=h+'|screener-'+id;
}}
function showT(s,name,btn){{
  document.querySelectorAll('#'+s+' .tp').forEach(function(p){{p.classList.remove('active');}});
  document.querySelectorAll('#'+s+' .tb').forEach(function(b){{b.classList.remove('active');}});
  document.getElementById(s+'-'+name).classList.add('active');
  btn.classList.add('active');
}}
function doScan(btn){{
  btn.textContent='⏳ Scanning...'; btn.disabled=true;
  fetch('/scan').then(function(){{setTimeout(function(){{location.reload();}},10000);}});
}}

// ── Restore tab from hash on page load ────────
(function(){{
  var hash=window.location.hash||''; var parts=hash.split('|');
  var mainTab='trading';
  parts.forEach(function(p){{if(p.indexOf('tab-')===0) mainTab=p.replace('tab-','');}});
  var mainBtn=document.querySelector('.mtb[onclick*="'+mainTab+'"]');
  if(mainBtn){{
    document.querySelectorAll('.mtp').forEach(function(p){{p.classList.remove('active');}});
    document.querySelectorAll('.mtb').forEach(function(b){{b.classList.remove('active');}});
    document.getElementById('main-'+mainTab).classList.add('active');
    mainBtn.classList.add('active');
  }}
  parts.forEach(function(p){{
    if(p.indexOf('screener-')===0){{
      var sid=p.replace('screener-','');
      var sBtn=document.querySelector('.sb[onclick*="'+sid+'"]');
      if(sBtn){{
        document.querySelectorAll('.sp').forEach(function(x){{x.classList.remove('active');}});
        document.querySelectorAll('.sb').forEach(function(x){{x.classList.remove('active');}});
        document.getElementById(sid).classList.add('active');
        sBtn.classList.add('active');
      }}
    }}
  }});
}})();

// ── Live Price Fetcher (every 15s) ────────────
function fetchPrices(){{
  fetch('/prices')
    .then(function(r){{return r.json();}})
    .then(function(data){{
      for(var key in data){{
        var info = data[key];
        var bot  = info.bot;
        var sym  = key.replace('__2','');
        var ltpEl  = document.getElementById('ltp-'+bot+'-'+sym);
        var upnlEl = document.getElementById('upnl-'+bot+'-'+sym);
        if(ltpEl) ltpEl.textContent = '&#8377;'+info.price;
        if(upnlEl){{
          var pnl = info.unrealised_pnl;
          var pct = info.pct;
          var col = pnl>=0?'#00c896':'#ff4d4d';
          var sg  = pnl>=0?'+':'';
          upnlEl.innerHTML = '<span style="color:'+col+';font-weight:700;">'+sg+'&#8377;'+pnl+' ('+sg+pct+'%)</span>';
        }}
      }}
      var ts = new Date().toLocaleTimeString('en-IN');
      document.getElementById('ltp-ts').textContent = '&#8635; Prices updated '+ts;
    }})
    .catch(function(){{
      document.getElementById('ltp-ts').textContent = '&#9888; Price fetch failed';
    }});
}}
fetchPrices();
setInterval(fetchPrices, 15000);

// ── Manual Close ──────────────────────────────
function closePos(bot, sym, btn){{
  if(!confirm('Close '+sym+' position?')) return;
  btn.disabled=true; btn.textContent='...';
  fetch('/close/'+bot+'/'+sym, {{method:'POST'}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.status==='closed'){{
        var row=document.getElementById('row-'+bot+'-'+sym);
        if(row) row.remove();
        alert('✅ '+sym+' closed @ &#8377;'+d.price);
      }} else {{
        alert('❌ Error: '+JSON.stringify(d));
        btn.disabled=false; btn.textContent='Close';
      }}
    }})
    .catch(function(){{btn.disabled=false; btn.textContent='Close';}});
}}
</script>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────
print("🚀 Starting Chartink Bot (Dual Screener + Signal Engine)...")
threading.Thread(target=run_monitor,daemon=True).start()
send(f"🟢 <b>Bot1 tazbul LIVE</b>\n💰 ₹{CAPITAL_PER_TRADE} SL:{SL_PERCENT}% TP:{TP_PERCENT}%")
send(f"🟢 <b>Bot2 TazAmol-Test1 LIVE</b>\n💰 ₹{BOT2_CAPITAL} SL:{BOT2_SL}% TP:{BOT2_TP}%",
     token=BOT2_TOKEN,chat_id=BOT2_CHAT_ID)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=PORT)

