# app.py — Chartink Auto Scraper + Telegram Bot
# Scrapes your screener every 5 mins — NO webhook needed!

from flask import Flask, request, jsonify
from datetime import datetime, time as dtime
import requests, pytz, os, time, threading

app         = Flask(__name__)
IST         = pytz.timezone("Asia/Kolkata")
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "")
SCAN_MINS   = int(os.environ.get("SCAN_MINS", 5))  # Check every 5 mins

# ── Track previously seen stocks to avoid duplicate alerts ──
last_stocks = set()

# ══════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════

def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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
        print(f"❌ Exception: {e}")


# ══════════════════════════════════════════════════
#  CHARTINK SCRAPER
# ══════════════════════════════════════════════════

def fetch_chartink_screener():
    """Fetch stocks from Chartink screener tazbul."""
    try:
        session = requests.Session()

        # Step 1: Get CSRF token from screener page
        page = session.get(
            "https://chartink.com/screener/tazbul",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        # Extract CSRF token
        csrf_token = ""
        for line in page.text.split("\n"):
            if "csrf-token" in line and "content=" in line:
                csrf_token = line.split('content="')[1].split('"')[0]
                break

        if not csrf_token:
            # Try meta tag extraction
            import re
            match = re.search(r'meta name="csrf-token" content="(.+?)"', page.text)
            if match:
                csrf_token = match.group(1)

        print(f"CSRF Token: {csrf_token[:20]}..." if csrf_token else "No CSRF found")

        # Step 2: Call Chartink screener API
        headers = {
            "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Csrf-Token"    : csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer"         : "https://chartink.com/screener/tazbul",
            "Content-Type"    : "application/x-www-form-urlencoded"
        }

        response = session.post(
            "https://chartink.com/screener/process",
            data={"scan_clause": get_scan_clause(page.text)},
            headers=headers,
            timeout=15
        )

        data = response.json()
        stocks = [item["nsecode"] for item in data.get("data", [])]
        print(f"📊 Stocks found: {stocks}")
        return stocks

    except Exception as e:
        print(f"❌ Scraper error: {e}")
        return []


def get_scan_clause(page_html):
    """Extract scan clause from screener page HTML."""
    import re
    # Try to extract scan_clause from page
    match = re.search(r'scan_clause\s*[=:]\s*["\'](.+?)["\']', page_html)
    if match:
        return match.group(1)
    # Fallback — return empty
    return ""


# ══════════════════════════════════════════════════
#  MARKET HOURS CHECK
# ══════════════════════════════════════════════════

def is_market_hours():
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 30)


# ══════════════════════════════════════════════════
#  FORMAT MESSAGE
# ══════════════════════════════════════════════════

def format_msg(stocks, screener="tazbul"):
    now   = datetime.now(IST).strftime("%d %b %Y  %I:%M %p")
    lines = "".join(f"  📌 <b>{s}</b>\n" for s in stocks)
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


# ══════════════════════════════════════════════════
#  BACKGROUND SCANNER THREAD
# ══════════════════════════════════════════════════

def run_scanner():
    """Background thread — scans Chartink every SCAN_MINS minutes."""
    global last_stocks
    print(f"🔍 Scanner started — checking every {SCAN_MINS} mins during market hours")

    while True:
        if is_market_hours():
            print(f"🔍 Scanning Chartink... [{datetime.now(IST).strftime('%I:%M %p')}]")
            stocks = fetch_chartink_screener()

            if stocks:
                # Find NEW stocks not seen in last scan
                new_stocks = [s for s in stocks if s not in last_stocks]

                if new_stocks:
                    print(f"🆕 New stocks: {new_stocks}")
                    send_telegram(format_msg(new_stocks))
                else:
                    print("ℹ️ No new stocks since last scan")

                last_stocks = set(stocks)
            else:
                print("📭 No stocks found in screener")
        else:
            print(f"⏰ Market closed — waiting... [{datetime.now(IST).strftime('%I:%M %p')}]")
            last_stocks = set()  # Reset at end of day

        time.sleep(SCAN_MINS * 60)


# ══════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"        : "🟢 Running",
        "scanner"       : f"Every {SCAN_MINS} mins",
        "market_open"   : is_market_hours(),
        "last_stocks"   : list(last_stocks)
    }), 200


@app.route("/test", methods=["GET"])
def test():
    send_telegram(
        "✅ <b>Bot is Working!</b>\n"
        "☁️ Running FREE on Cloud\n"
        f"🔍 Scanning tazbul every {SCAN_MINS} mins\n"
        "📡 Waiting for market hours (9:15 AM - 3:30 PM IST)"
    )
    return jsonify({"status": "test sent"}), 200


@app.route("/scan-now", methods=["GET"])
def scan_now():
    """Manually trigger a scan right now."""
    stocks = fetch_chartink_screener()
    if stocks:
        send_telegram(format_msg(stocks))
        return jsonify({"status": "alert sent", "stocks": stocks}), 200
    return jsonify({"status": "no stocks found"}), 200


# ══════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    # Start background scanner thread
    scanner = threading.Thread(target=run_scanner, daemon=True)
    scanner.start()

    send_telegram(
        "🟢 <b>Chartink Bot is LIVE!</b>\n"
        f"🔍 Auto-scanning <b>tazbul</b> every {SCAN_MINS} mins\n"
        "⏰ Active: 9:15 AM – 3:30 PM IST\n"
        "☁️ Running FREE on Render.com"
    )

    app.run(host="0.0.0.0", port=port)
