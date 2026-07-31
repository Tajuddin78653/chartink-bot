from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import requests, pytz, os

app         = Flask(__name__)
IST         = pytz.timezone("Asia/Kolkata")
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "")
alert_count = 0

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def is_market_hours():
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 30)

def format_msg(stocks, prices, screener):
    now   = datetime.now(IST).strftime("%d %b %Y  %I:%M %p")
    lines = ""
    for i, s in enumerate(stocks):
        p = prices[i] if i < len(prices) else "—"
        lines += f"  📌 <b>{s}</b>  ₹{p}\n"
    return (
        f"🔔 <b>CHARTINK ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{screener}</b>\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{lines}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>DYOR before trading</i>"
    )

@app.route("/alert", methods=["POST"])
def alert():
    global alert_count
    if not is_market_hours():
        return jsonify({"status": "outside market hours"}), 200
    data     = request.json or request.form.to_dict()
    stocks   = [s.strip() for s in data.get("stocks","").split(",") if s.strip()]
    prices   = [p.strip() for p in data.get("trigger_prices","").split(",") if p.strip()]
    screener = data.get("scan_name", "Chartink Screener")
    if not stocks:
        return jsonify({"status": "no stocks"}), 400
    send_telegram(format_msg(stocks, prices, screener))
    alert_count += 1
    return jsonify({"status": "ok", "count": alert_count}), 200

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "🟢 Running", "alerts_today": alert_count}), 200

@app.route("/test", methods=["GET"])
def test():
    send_telegram(
        "✅ <b>Bot is Working!</b>\n"
        "☁️ Running FREE on Cloud\n"
        "📡 Waiting for Chartink alerts..."
    )
    return jsonify({"status": "test sent"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
