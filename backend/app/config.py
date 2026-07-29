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

# Premium sales tax when selling instantly into an existing buy order.
# Premium = 4%, non-Premium = 8%. No setup fee applies to instant sells.
SALES_TAX = _get_float("SHOPALBI_SALES_TAX", 0.04)
# Optional extra fee if you instead place your own buy/sell orders (0 for instant).
SETUP_FEE = _get_float("SHOPALBI_SETUP_FEE", 0.0)

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


# --- Full order-book depth (captured client-side) ---------------------------

# Shared secret the capture agent must present on POST /api/ingest/orders.
# Empty => ingest is disabled (fail closed): the endpoint returns 503 and no
# external process can write into the depth store.
INGEST_TOKEN = _get("SHOPALBI_INGEST_TOKEN", "")

# Captured order books older than this (hours) are treated as stale: excluded
# from depth reads and eligible for pruning. Depth is only as fresh as the last
# time a market window was actually opened in-game, so keep this generous.
DEPTH_MAX_AGE_HOURS = _get_float("SHOPALBI_DEPTH_MAX_AGE_HOURS", 24.0)

# Hard retention: orders older than this (hours) are deleted on ingest so the
# table cannot grow without bound for markets no one visits anymore.
DEPTH_PRUNE_AGE_HOURS = _get_float("SHOPALBI_DEPTH_PRUNE_AGE_HOURS", 168.0)

# Max books accepted in a single ingest POST (protects against a runaway agent).
INGEST_MAX_BOOKS = _get_int("SHOPALBI_INGEST_MAX_BOOKS", 5000)
