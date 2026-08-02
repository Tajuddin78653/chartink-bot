# scanner.py — Chartink Screener Fetcher
import requests, re
from config import SCREENER_URL

last_stocks = set()


def fetch_screener():
    """Fetch stocks from Chartink screener."""
    try:
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        # Step 1: Get CSRF token
        page  = session.get(SCREENER_URL, headers=headers, timeout=15)
        match = re.search(r'meta name="csrf-token" content="(.+?)"', page.text)
        csrf  = match.group(1) if match else ""

        # Step 2: Extract scan clause
        clause = re.search(r'"scan_clause"\s*:\s*"(.+?)"', page.text)
        scan   = clause.group(1) if clause else ""

        if not scan:
            print("⚠️ Could not extract scan clause")
            return []

        # Step 3: Call screener API
        r = session.post(
            "https://chartink.com/screener/process",
            data={"scan_clause": scan},
            headers={
                **headers,
                "X-Csrf-Token"    : csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer"         : SCREENER_URL
            },
            timeout=15
        )
        data   = r.json()
        stocks = [item["nsecode"] for item in data.get("data", [])]
        print(f"📊 Screener result: {stocks}")
        return stocks

    except Exception as e:
        print(f"❌ Scanner error: {e}")
        return []


def get_new_stocks():
    """Return only NEW stocks not seen in last scan."""
    global last_stocks
    stocks     = fetch_screener()
    new_stocks = [s for s in stocks if s not in last_stocks]
    last_stocks = set(stocks)
    return new_stocks
