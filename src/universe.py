"""Stock universes (NSE symbols, without the .NS Yahoo suffix)."""
from __future__ import annotations

# ---- Large cap: Nifty 50 -------------------------------------------------- #
NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
    "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
    "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN",
    "TRENT", "ULTRACEMCO", "WIPRO", "JIOFIN", "DIVISLAB",
]

# ---- Large cap: Nifty Next 50 (rest of Nifty 100) ------------------------- #
NIFTY_NEXT_50 = [
    "ABB", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "DMART",
    "BAJAJHLDNG", "BANKBARODA", "BERGEPAINT", "BOSCHLTD", "BPCL",
    "BRITANNIA", "CANBK", "CHOLAFIN", "COLPAL", "DABUR",
    "DLF", "GAIL", "GODREJCP", "HAVELLS", "HAL",
    "ICICIGI", "ICICIPRULI", "IOC", "INDIGO", "NAUKRI",
    "INDUSTOWER", "IRFC", "JINDALSTEL", "JSWENERGY", "LICI",
    "LTIM", "MARICO", "MOTHERSON", "MUTHOOTFIN", "PIDILITIND",
    "PFC", "PNB", "RECLTD", "SIEMENS", "SRF",
    "TVSMOTOR", "TATAPOWER", "TORNTPHARM", "UNITDSPR", "VEDL",
    "VBL", "ETERNAL", "ZYDUSLIFE", "GODREJPROP", "CGPOWER",
]

# ---- Mid cap (liquid, intraday-friendly) ---------------------------------- #
NIFTY_MIDCAP = [
    "PERSISTENT", "COFORGE", "MPHASIS", "OFSS", "LTTS",
    "POLYCAB", "CUMMINSIND", "ASHOKLEY", "TIINDIA", "BALKRISIND",
    "MRF", "ESCORTS", "BHARATFORG", "SAIL", "NMDC",
    "HINDPETRO", "IDEA", "YESBANK", "IDFCFIRSTB", "FEDERALBNK",
    "BANDHANBNK", "AUBANK", "INDHOTEL", "PAGEIND", "UPL",
    "AUROPHARMA", "LUPIN", "ALKEM", "BIOCON", "GLENMARK",
    "LAURUSLABS", "ABCAPITAL", "BHEL", "OIL", "PETRONET",
    "GUJGASLTD", "CONCOR", "SUPREMEIND", "ASTRAL", "DIXON",
    "KPITTECH", "TATACOMM", "TATAELXSI", "MAXHEALTH", "FORTIS",
    "APLAPOLLO", "NYKAA", "POLICYBZR", "DELHIVERY", "PAYTM",
]

# ---- Small cap (liquid enough for intraday) ------------------------------- #
NIFTY_SMALLCAP = [
    "RBLBANK", "SOUTHBANK", "IRCON", "RVNL", "NBCC",
    "HUDCO", "MAZDOCK", "COCHINSHIP", "IEX", "CDSL",
    "BSE", "ANGELONE", "KFINTECH", "CAMS", "MANAPPURAM",
    "IIFL", "CHAMBLFERT", "GNFC", "DEEPAKNTR", "AARTIIND",
    "TATACHEM", "NATIONALUM", "RATNAMANI", "KEC", "RAILTEL",
    "RITES", "HFCL", "TANLA", "ZENSARTECH", "BIRLASOFT",
    "SONACOMS", "EXIDEIND", "BATAINDIA", "RELAXO", "VGUARD",
    "CROMPTON", "BLUEDART", "JBCHEPHARM", "CESC", "GESHIP",
    "FINCABLES", "KAJARIACER", "CYIENT", "SUNTV", "GMDCLTD",
]


def get_universe(cfg: dict) -> list[str]:
    """Resolve the configured preset into a de-duplicated symbol list.

    Presets: nifty50, nifty100, midcap, smallcap, broad (all of the above),
             custom (uses universe.custom_symbols).
    """
    preset = cfg.get("universe", {}).get("preset", "broad").lower()
    if preset == "custom":
        return list(dict.fromkeys(cfg["universe"]["custom_symbols"]))
    if preset == "nifty50":
        return list(NIFTY_50)
    if preset == "nifty100":
        return _dedup(NIFTY_50 + NIFTY_NEXT_50)
    if preset == "midcap":
        return list(NIFTY_MIDCAP)
    if preset == "smallcap":
        return list(NIFTY_SMALLCAP)
    # broad = large + mid + small (spans all market caps)
    return _dedup(NIFTY_50 + NIFTY_NEXT_50 + NIFTY_MIDCAP + NIFTY_SMALLCAP)


def _dedup(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def to_yahoo(symbol: str) -> str:
    """Convert an NSE symbol to its Yahoo Finance ticker."""
    return f"{symbol}.NS"
