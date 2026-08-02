# trade_calculator.py — Calculate Qty, SL, Target
from config import (
    CAPITAL, RISK_PERCENT, SL_PERCENT,
    TRAIL_PERCENT, CAPITAL_PER_TRADE, PAPER_TRADING
)


def calculate_trade(symbol, entry_price, entry_time):
    """
    Calculate all trade parameters.

    Returns a trade dict with:
    - qty, sl, trail_pct, capital_used, risk_amt
    """
    # Risk amount in ₹
    risk_amt = round(CAPITAL * (RISK_PERCENT / 100), 2)   # ₹200

    # SL price (fixed %)
    sl_price = round(entry_price * (1 - SL_PERCENT / 100), 2)

    # SL distance per share
    sl_distance = round(entry_price - sl_price, 2)

    # Quantity based on risk
    qty = max(1, int(risk_amt / sl_distance))

    # Validate capital constraint
    capital_used = round(qty * entry_price, 2)
    if capital_used > CAPITAL_PER_TRADE:
        qty          = max(1, int(CAPITAL_PER_TRADE / entry_price))
        capital_used = round(qty * entry_price, 2)

    # Recalculate SL after qty adjustment
    sl_price     = round(entry_price * (1 - SL_PERCENT / 100), 2)
    risk_amt     = round((entry_price - sl_price) * qty, 2)

    return {
        "symbol"       : symbol,
        "entry"        : entry_price,
        "qty"          : qty,
        "sl"           : sl_price,
        "sl_pct"       : SL_PERCENT,
        "trail_pct"    : TRAIL_PERCENT,
        "capital_used" : capital_used,
        "risk_amt"     : risk_amt,
        "entry_time"   : entry_time,
        "exit_time"    : None,
        "current_sl"   : sl_price,
        "highest_price": entry_price,
        "paper"        : PAPER_TRADING,
        "status"       : "OPEN"
    }
