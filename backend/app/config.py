"""Central configuration for shopalbi.

Everything a serious operator might want to tweak lives here and can be
overridden with environment variables (see .env.example). Defaults target the
EU server with a Premium account, which is our setup.

Values that encode *game rules* (taxes, which locations exist, what the Black
Market buys) carry a comment saying how they were verified. Do not change those
on a hunch — re-verify first.
"""

from __future__ import annotations

import os
from pathlib import Path

VERSION = "5.0.0"


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


def _get_bool(name: str, default: bool) -> bool:
    return _get(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


# --- Paths -----------------------------------------------------------------

# repo layout: <root>/backend/app/config.py -> parents[2] == <root>
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(_get("SHOPALBI_DATA_DIR", str(REPO_ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(_get("SHOPALBI_DB_PATH", str(DATA_DIR / "shopalbi.db")))
CATALOG_CACHE = Path(_get("SHOPALBI_CATALOG_CACHE", str(DATA_DIR / "items.json")))
FRONTEND_DIR = Path(_get("SHOPALBI_FRONTEND_DIR", str(REPO_ROOT / "frontend")))

# Where the rotating log file is written. In Docker this is bind-mounted to
# /root/logs on the host so logs are readable without `docker compose logs`.
LOG_DIR = Path(_get("SHOPALBI_LOG_DIR", str(DATA_DIR)))
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# --- Albion Online Data Project API ----------------------------------------

# Server region host. EU by default. West = Americas, East = Asia.
API_HOST = _get("SHOPALBI_API_HOST", "https://europe.albion-online-data.com")

CATALOG_URL = _get(
    "SHOPALBI_CATALOG_URL",
    "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
)

# Rate limits published by AODP: 180/min, 300/5min. Stay comfortably under.
RATE_PER_MIN = _get_int("SHOPALBI_RATE_PER_MIN", 165)

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

# Cities excluded as buy sources. Brecilien is a Faerie Realm hub, not a Royal
# city, and getting out of it toward Caerleon is slow and awkward. It stays in
# the average-price table for reference but never in flips/plans/ranking.
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

# Equipment slot tokens the Black Market trades. Verified empirically against
# live BM buy orders + BM trade history (see docs/HANDOFF.md §5.7):
#   BAG / CAPE / CAPEITEM (faction capes DO get BM buy orders, checked T5 FW cape)
#   MAIN / 2H / OFF / HEAD / ARMOR / SHOES
# BACKPACK (gatherer backpacks) and *_GATHERER_* gear return buy=0 with zero BM
# history — mobs never drop gathering gear, so the BM never buys it.
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
# Substrings that disqualify an item id even if the slot token matches.
EXCLUDE_ID_TOKENS = ("_TOOL", "NONTRADABLE", "_GATHERER_", "_DEBUG", "TUTORIAL")


# --- Profit math ------------------------------------------------------------
#
# Verified against https://wiki.albiononline.com/wiki/Marketplace and
# https://wiki.albiononline.com/wiki/Margin :
#
#   * Sales tax    — 8% of the sale price, reduced to 4% with Premium.
#                    Charged when a sale COMPLETES. Always applies.
#   * Setup fee    — 2.5% of the order value, charged when you CREATE or UPDATE
#                    a buy/sell order. It does NOT apply when you instantly buy
#                    from an existing sell order, nor when you instantly sell
#                    into an existing buy order.
#
# The canonical Black Market flip is instant on both legs:
#     buy from a city sell order (no fee) -> carry -> "Sell" into a BM buy order
# so the only deduction is the sales tax:  net = 1 - SALES_TAX = 0.96.
#
# The previous version subtracted the setup fee here too, which understated
# every flip's profit by 2.5% of revenue. Both legs are now switchable because
# the alternative strategies are real:
#   buy_mode  = "instant" -> pay city sell_price_min, no fee              (default)
#             = "order"   -> place your own buy order at buy_price_max,
#                            cost = price * (1 + SETUP_FEE), you wait
#   sell_mode = "instant" -> sell into the BM buy order, tax only         (default)
#             = "order"   -> park a sell order on the BM, tax + setup fee,
#                            fills only once BM demand climbs to your ask
SALES_TAX = _get_float("SHOPALBI_SALES_TAX", 0.04)     # 0.04 premium, 0.08 without
SETUP_FEE = _get_float("SHOPALBI_SETUP_FEE", 0.025)    # flat for everyone
DEFAULT_BUY_MODE = _get("SHOPALBI_BUY_MODE", "instant")
DEFAULT_SELL_MODE = _get("SHOPALBI_SELL_MODE", "instant")


def net_factor(sell_mode: str = DEFAULT_SELL_MODE, sales_tax: float | None = None) -> float:
    """Fraction of the Black Market price you actually keep."""
    tax = SALES_TAX if sales_tax is None else sales_tax
    return 1.0 - tax - (SETUP_FEE if sell_mode == "order" else 0.0)


def cost_factor(buy_mode: str = DEFAULT_BUY_MODE) -> float:
    """Multiplier on the city price you actually pay."""
    return 1.0 + (SETUP_FEE if buy_mode == "order" else 0.0)


# --- Transport risk ---------------------------------------------------------
#
# Caerleon sits in a red-zone cluster and the Caerleon Realmgate was removed,
# so every Royal -> Caerleon run is overland through forced-PvP territory.
# Community consensus (albioncodex transport guide, AO forums) puts the gank
# rate at roughly 5-15% per trip during prime time. Expected value of a run is
#     EV = gross_profit - gank_rate * load_value
# because a gank costs you the whole load, not just the margin.
#
# Buying inside Caerleon itself has ZERO transport risk (you walk to the BM),
# which is exactly why Caerleon prices are structurally higher.
DEFAULT_GANK_RATE = _get_float("SHOPALBI_GANK_RATE", 0.08)
CITY_RISK_MULTIPLIER = {
    "Caerleon": 0.0,        # same city as the Black Market, no red zone at all
    "Bridgewatch": 1.0,
    "Fort Sterling": 1.0,
    "Lymhurst": 1.0,
    "Martlock": 1.0,
    "Thetford": 1.0,
    "Brecilien": 1.4,       # awkward exit toward Caerleon (excluded by default anyway)
}
# Round trip wall-clock estimate per city, used for profit-per-hour. Rough but
# consistent; the point is ranking cities, not a stopwatch.
LOAD_OVERHEAD_HOURS = _get_float("SHOPALBI_LOAD_OVERHEAD_HOURS", 0.35)
CITY_TRIP_HOURS = {
    "Caerleon": 0.10,
    "Bridgewatch": 0.55,
    "Fort Sterling": 0.55,
    "Lymhurst": 0.55,
    "Martlock": 0.55,
    "Thetford": 0.55,
    "Brecilien": 0.80,
}


# --- Data sanity guards -----------------------------------------------------
#
# AODP is crowd-sourced, so individual quotes can be junk: a 158-silver buy
# order on a T5 shield, a 499,999-silver troll sell order on an item that
# normally trades at 9k. Both directions break the math, so every quote is
# sanity-checked against the item's own volume-weighted history.
#
# A BM bid far above the historical average is the dangerous one: it makes a
# flip look amazing, you haul the load, and the order is gone on arrival.
BM_MAX_VS_VWAP = _get_float("SHOPALBI_BM_MAX_VS_VWAP", 2.2)    # flag/drop bids above this x VWAP
BM_MIN_VS_VWAP = _get_float("SHOPALBI_BM_MIN_VS_VWAP", 0.25)   # ignore degenerate lowball bids
CITY_MAX_VS_VWAP = _get_float("SHOPALBI_CITY_MAX_VS_VWAP", 3.0)  # ignore troll-priced sell orders
# Hard-drop spiked bids instead of only flagging them.
DROP_SPIKED_BIDS = _get_bool("SHOPALBI_DROP_SPIKED_BIDS", False)

# Freshness ceilings (hours). Albion Free Market drops BM buy orders after one
# hour without a new report; we are a bit more permissive but score age heavily.
FLIP_MAX_AGE_HOURS = _get_float("SHOPALBI_FLIP_MAX_AGE_HOURS", 8.0)
BM_MAX_AGE_HOURS = _get_float("SHOPALBI_BM_MAX_AGE_HOURS", 3.0)

# Reference daily BM volume that counts as "fully liquid". Real numbers are far
# higher than the old value of 20 (T5_BAG clears ~4,300 units/day), so the score
# is log-scaled between these bounds instead of saturating instantly.
LIQUIDITY_FLOOR = _get_float("SHOPALBI_LIQUIDITY_FLOOR", 3.0)
LIQUIDITY_REF = _get_float("SHOPALBI_LIQUIDITY_REF", 400.0)

# Default flip filters (frontend can override via query params).
DEFAULT_MIN_PROFIT = _get_int("SHOPALBI_MIN_PROFIT", 1000)
DEFAULT_MIN_PROFIT_PCT = _get_float("SHOPALBI_MIN_PROFIT_PCT", 0.0)
DEFAULT_MIN_BM_DAILY_VOLUME = _get_float("SHOPALBI_MIN_BM_DAILY_VOLUME", 1.0)


# --- Budget recommender -----------------------------------------------------

# Max qty per item when we have NO live order-book depth for it (warmup or a
# gap in the feed). Prevents "buy 300x T4" suggestions that cannot be filled.
RECOMMEND_NODEPTH_CAP = _get_int("SHOPALBI_RECOMMEND_NODEPTH_CAP", 8)
# Also cap no-depth quantities by a share of one day's BM absorption.
RECOMMEND_VOLUME_CAPTURE = _get_float("SHOPALBI_RECOMMEND_VOLUME_CAPTURE", 0.15)
# Don't sink the whole budget into one item.
RECOMMEND_MAX_ITEM_SHARE = _get_float("SHOPALBI_RECOMMEND_MAX_ITEM_SHARE", 0.30)
RECOMMEND_MAX_ITEMS = _get_int("SHOPALBI_RECOMMEND_MAX_ITEMS", 40)


# --- Live order-book feed (AODP public NATS) --------------------------------
# Subscribing needs no game client — it's a plain TCP stream anyone can consume
# from a VPS. Each message is one market order carrying price + Amount + side,
# i.e. real order-book depth that the REST API does not expose.
# Regions: :4222 Americas, :24222 Asia, :34222 Europe.
NATS_ENABLE = _get_bool("SHOPALBI_NATS_ENABLE", True)
NATS_URL = _get(
    "SHOPALBI_NATS_URL",
    "nats://public:thenewalbiondata@nats.albion-online-data.com:34222",
)
# `marketorders.deduped` is one order per message; `.bulk` is the same data
# batched into arrays. Subscribing to both raises coverage and is safe because
# orders are upserted by their unique Id. Never subscribe to `.ingest`: those
# messages carry raw prices scaled by 10000 and include duplicates.
NATS_TOPICS = [
    t.strip() for t in
    _get("SHOPALBI_NATS_TOPICS", "marketorders.deduped,marketorders.deduped.bulk").split(",")
    if t.strip()
]

# How long a live order stays usable. The feed only carries markets that players
# actually opened in game, at roughly a handful of orders per second worldwide,
# so a 30-minute window leaves the book almost always empty. Several hours with
# a visible age is far more useful than nothing.
ORDER_MAX_AGE_MINUTES = _get_int("SHOPALBI_ORDER_MAX_AGE_MINUTES", 360)
# BM buy orders decay fast in practice; keep a tighter window for them.
BM_ORDER_MAX_AGE_MINUTES = _get_int("SHOPALBI_BM_ORDER_MAX_AGE_MINUTES", 90)
ORDER_FLUSH_SECONDS = _get_float("SHOPALBI_ORDER_FLUSH_SECONDS", 3.0)
ORDER_FLUSH_MAX = _get_int("SHOPALBI_ORDER_FLUSH_MAX", 2000)

# AODP numeric LocationId -> our location name.
#
# VERIFIED 2026-07-29 by querying the REST API one id at a time
# (/api/v2/stats/prices/T4_BAG.json?locations=<id>) and reading back the `city`
# field, then cross-checked against the live NATS feed:
#
#     3003 -> Black Market      <-- NOT Caerleon
#     3005 -> Caerleon          <-- NOT the Black Market
#
# The feed confirms it: location 3003 carries `request` orders whose Expires is
# in year 3025 (the BM's NPC buy orders never expire) alongside player `offer`
# orders, while 3005 is near-silent. The previous release had these two swapped,
# which silently pointed the whole depth engine at the wrong market.
NATS_LOCATION_IDS = {
    7: "Thetford",
    1002: "Lymhurst",
    2004: "Bridgewatch",
    3003: BLACK_MARKET,
    3005: "Caerleon",
    3008: "Martlock",
    4002: "Fort Sterling",
    5003: "Brecilien",
}
# An order whose Expires is this far out is a system/NPC order, not a player's
# (player sell orders live at most 30 days). BM buy orders come back as year 3025.
NPC_EXPIRY_YEAR = _get_int("SHOPALBI_NPC_EXPIRY_YEAR", 2100)


# --- Scheduling -------------------------------------------------------------

CURRENT_REFRESH_MINUTES = _get_int("SHOPALBI_CURRENT_REFRESH_MINUTES", 10)
HISTORY_REFRESH_HOURS = _get_int("SHOPALBI_HISTORY_REFRESH_HOURS", 6)
HISTORY_DAYS = _get_int("SHOPALBI_HISTORY_DAYS", 31)
CATALOG_MAX_AGE_DAYS = _get_int("SHOPALBI_CATALOG_MAX_AGE_DAYS", 7)
REFRESH_ON_START = _get_bool("SHOPALBI_REFRESH_ON_START", True)


# --- Localization / web -----------------------------------------------------

PRIMARY_LANG = _get("SHOPALBI_LANG", "RU-RU")
FALLBACK_LANG = "EN-US"

HOST = _get("SHOPALBI_HOST", "0.0.0.0")
PORT = _get_int("SHOPALBI_PORT", 8000)

BASIC_AUTH_USER = _get("SHOPALBI_AUTH_USER", "")
BASIC_AUTH_PASS = _get("SHOPALBI_AUTH_PASS", "")


# Windows offered on the stats tabs, in days. `day` means the last full day.
STAT_WINDOWS = {
    "day": 1,
    "3d": 3,
    "week": 7,
    "month": 30,
}
WINDOW_LABELS = {"day": "день", "3d": "3 дня", "week": "неделя", "month": "месяц"}
