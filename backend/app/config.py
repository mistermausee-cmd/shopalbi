"""Central configuration for shopalbi.

Everything that a strong operator might want to tweak lives here and can be
overridden with environment variables (see .env.example). Sane defaults are
chosen for the EU server with a Premium account, which is our target setup.
"""

from __future__ import annotations

import os
from pathlib import Path


def _get(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


# --- Paths -----------------------------------------------------------------

# repo layout: <root>/backend/app/config.py -> parents[2] == <root>
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(_get("SHOPALBI_DATA_DIR", str(REPO_ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(_get("SHOPALBI_DB_PATH", str(DATA_DIR / "shopalbi.db")))
CATALOG_CACHE = Path(_get("SHOPALBI_CATALOG_CACHE", str(DATA_DIR / "items.json")))
FRONTEND_DIR = Path(_get("SHOPALBI_FRONTEND_DIR", str(REPO_ROOT / "frontend")))

# Where the rotating log file is written. Defaults to the data dir; in Docker
# this is bind-mounted to /root/logs on the host so logs are readable there.
LOG_DIR = Path(_get("SHOPALBI_LOG_DIR", str(DATA_DIR)))
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# --- Albion Online Data Project API ----------------------------------------

# Server region host. EU by default. West = Americas, East = Asia.
API_HOST = _get("SHOPALBI_API_HOST", "https://europe.albion-online-data.com")

# Where the item catalog with localized names comes from.
CATALOG_URL = _get(
    "SHOPALBI_CATALOG_URL",
    "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
)

# Rate limits published by AODP: 180/min, 300/5min. Stay comfortably under.
RATE_PER_MIN = _get_int("SHOPALBI_RATE_PER_MIN", 170)

# The API enforces a 4096 char URL limit. We build batches under this ceiling.
MAX_URL_LEN = _get_int("SHOPALBI_MAX_URL_LEN", 3900)

HTTP_TIMEOUT = _get_float("SHOPALBI_HTTP_TIMEOUT", 60.0)
HTTP_RETRIES = _get_int("SHOPALBI_HTTP_RETRIES", 4)


# --- Market model -----------------------------------------------------------

ROYAL_CITIES = [
    "Bridgewatch",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
    "Caerleon",
    "Brecilien",
]
BLACK_MARKET = "Black Market"
ALL_LOCATIONS = ROYAL_CITIES + [BLACK_MARKET]

# Cities excluded as buy sources (too far / hard to reach Caerleon, e.g.
# Brecilien). They still appear in the average-price table for reference, but
# are not used for flips, the city ranking, or budget recommendations.
EXCLUDED_BUY_CITIES = {
    c.strip() for c in _get("SHOPALBI_EXCLUDED_BUY_CITIES", "Brecilien").split(",") if c.strip()
}
BUY_CITIES = [c for c in ROYAL_CITIES if c not in EXCLUDED_BUY_CITIES]

QUALITIES = [1, 2, 3, 4, 5]
QUALITY_NAMES = {
    1: "Обычное",
    2: "Хорошее",
    3: "Незаурядное",
    4: "Отличное",
    5: "Шедевральное",
}

# Item scope: T4-T8 combat equipment only (what the Black Market actually buys).
TIERS = [int(t) for t in _get("SHOPALBI_TIERS", "4,5,6,7,8").split(",") if t.strip()]

# Equipment slot tokens the Black Market trades: weapons, off-hands, armor,
# capes and bags. Gathering tools (_TOOL) are excluded — the BM does not buy them.
EQUIP_SLOT_TOKENS = {
    "MAIN": "weapon",
    "2H": "weapon",
    "OFF": "offhand",
    "HEAD": "armor",
    "ARMOR": "armor",
    "SHOES": "armor",
    "CAPE": "cape",
    "CAPEITEM": "cape",
    "BAG": "bag",
}
CATEGORY_NAMES = {
    "weapon": "Оружие",
    "offhand": "Оффхенд",
    "armor": "Броня",
    "cape": "Плащ",
    "bag": "Сумка",
}


# --- Profit math ------------------------------------------------------------

# Black Market sale deductions.
#  * SALES_TAX  — tax on a completed sale: 4% with Premium, 8% without.
#  * SETUP_FEE  — order setup fee, 2.5% of the lot value, charged per listing.
# Both are subtracted from the sale proceeds, so net = price * (1 - tax - fee).
SALES_TAX = _get_float("SHOPALBI_SALES_TAX", 0.04)
SETUP_FEE = _get_float("SHOPALBI_SETUP_FEE", 0.025)

# Budget recommender tuning.
#  * SLIPPAGE — how fast your average buy price rises as you clear cheap lots
#    (the API only exposes the single lowest price, so we model depth). At a
#    quantity equal to one full day of Black Market volume, the average price
#    is raised by this fraction.
#  * VOLUME_CAPTURE — the share of a day's Black Market demand you assume you
#    can realistically offload for one item (caps the recommended quantity).
RECOMMEND_SLIPPAGE = _get_float("SHOPALBI_RECOMMEND_SLIPPAGE", 0.10)
RECOMMEND_VOLUME_CAPTURE = _get_float("SHOPALBI_RECOMMEND_VOLUME_CAPTURE", 0.5)
# Hard cap on recommended quantity per item when we have NO live order-book
# depth (warmup / thin feed). Prevents suggesting quantities that can't exist.
RECOMMEND_NODEPTH_CAP = _get_int("SHOPALBI_RECOMMEND_NODEPTH_CAP", 10)


# --- Live order-book feed (AODP public NATS) --------------------------------
# Subscribing needs no game client — it's a plain TCP stream anyone can consume.
# Each message is one market order carrying price + Amount + side, i.e. real
# order-book depth. Regions: :4222 Americas, :24222 Asia, :34222 Europe.
NATS_ENABLE = _get("SHOPALBI_NATS_ENABLE", "true").lower() in ("1", "true", "yes")
NATS_URL = _get(
    "SHOPALBI_NATS_URL",
    "nats://public:thenewalbiondata@nats.albion-online-data.com:34222",
)
NATS_TOPIC = _get("SHOPALBI_NATS_TOPIC", "marketorders.deduped")

# Live orders older than this (by last-seen or past their Expires) are dropped.
ORDER_MAX_AGE_MINUTES = _get_int("SHOPALBI_ORDER_MAX_AGE_MINUTES", 30)
# Batch flushing of incoming orders to SQLite.
ORDER_FLUSH_SECONDS = _get_float("SHOPALBI_ORDER_FLUSH_SECONDS", 3.0)
ORDER_FLUSH_MAX = _get_int("SHOPALBI_ORDER_FLUSH_MAX", 400)

# AODP numeric LocationId -> our city name. Verified against the REST API and
# the live feed: 3003 is Caerleon's normal market (offer-heavy), 3005 is the
# Black Market (NPC buy orders).
NATS_LOCATION_IDS = {
    7: "Thetford",
    1002: "Lymhurst",
    2004: "Bridgewatch",
    3003: "Caerleon",
    3005: BLACK_MARKET,
    3008: "Martlock",
    4002: "Fort Sterling",
    5003: "Brecilien",
}

# Freshness ceilings (hours). Prices older than this are treated as untrustworthy.
FLIP_MAX_AGE_HOURS = _get_float("SHOPALBI_FLIP_MAX_AGE_HOURS", 12.0)

# Default flip filters (frontend can override via query params).
DEFAULT_MIN_PROFIT = _get_int("SHOPALBI_MIN_PROFIT", 1000)
DEFAULT_MIN_BM_DAILY_VOLUME = _get_float("SHOPALBI_MIN_BM_DAILY_VOLUME", 1.0)


# --- Scheduling -------------------------------------------------------------

CURRENT_REFRESH_MINUTES = _get_int("SHOPALBI_CURRENT_REFRESH_MINUTES", 10)
HISTORY_REFRESH_HOURS = _get_int("SHOPALBI_HISTORY_REFRESH_HOURS", 12)
HISTORY_DAYS = _get_int("SHOPALBI_HISTORY_DAYS", 31)

# Refresh the item catalog from source at most this often (days).
CATALOG_MAX_AGE_DAYS = _get_int("SHOPALBI_CATALOG_MAX_AGE_DAYS", 7)

# Run a data refresh immediately on startup (handy for a fresh VPS).
REFRESH_ON_START = _get("SHOPALBI_REFRESH_ON_START", "true").lower() in ("1", "true", "yes")


# --- Localization -----------------------------------------------------------

PRIMARY_LANG = _get("SHOPALBI_LANG", "RU-RU")
FALLBACK_LANG = "EN-US"


# --- Web server -------------------------------------------------------------

HOST = _get("SHOPALBI_HOST", "0.0.0.0")
PORT = _get_int("SHOPALBI_PORT", 8000)

# Optional HTTP Basic auth. Leave empty to keep the site open (default).
BASIC_AUTH_USER = _get("SHOPALBI_AUTH_USER", "")
BASIC_AUTH_PASS = _get("SHOPALBI_AUTH_PASS", "")


# Windows offered on the stats tabs, in days.
STAT_WINDOWS = {
    "day": 1,
    "3d": 3,
    "week": 7,
    "month": 30,
}
