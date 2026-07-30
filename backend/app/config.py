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

VERSION = "5.1.2"


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

# SQLite tuning. A full price refresh is only ~25-40 requests, so the AODP rate
# limit is never the bottleneck — disk I/O on the history table is. Give SQLite
# a real page cache and memory-mapped reads if the box has RAM to spare.
SQLITE_CACHE_MB = _get_int("SHOPALBI_SQLITE_CACHE_MB", 64)
SQLITE_MMAP_MB = _get_int("SHOPALBI_SQLITE_MMAP_MB", 256)


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
# Wording matches the Russian game client, so the label can be read straight
# off the screen and found in the in-game quality tabs.
QUALITY_NAMES = {
    1: "Обычное",
    2: "Хорошее",
    3: "Выдающееся",
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


# Both factors are rounded so the value used in the arithmetic is EXACTLY the one
# reported by the API and shown in the UI. Left raw, 1 - 0.04 - 0.025 evaluates to
# 0.9349999999999999 while the interface says 0.935, and that invisible gap flips
# the occasional rounding — making a displayed profit impossible to reproduce from
# the displayed inputs.
_FACTOR_DP = 6


def net_factor(sell_mode: str = DEFAULT_SELL_MODE, sales_tax: float | None = None) -> float:
    """Fraction of the Black Market price you actually keep."""
    tax = SALES_TAX if sales_tax is None else sales_tax
    return round(1.0 - tax - (SETUP_FEE if sell_mode == "order" else 0.0), _FACTOR_DP)


def cost_factor(buy_mode: str = DEFAULT_BUY_MODE) -> float:
    """Multiplier on the city price you actually pay."""
    return round(1.0 + (SETUP_FEE if buy_mode == "order" else 0.0), _FACTOR_DP)


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

# Quantities when we have NO live order-book depth for an item (feed warmup or
# a market nobody has opened in game recently).
#
# The real bound is what the Black Market absorbs per day: suggesting more than
# a fraction of that is fiction no matter how much silver you hold. The absolute
# ceiling is only a backstop for absurd cases.
#
# It used to be 8, which *overrode* the volume figure — a T5 bag clearing ~4,300
# units/day still got capped at 8, so a 10M budget could only ever place ~2M.
# The volume-derived bound has to be the thing that binds, not the backstop.
RECOMMEND_NODEPTH_CAP = _get_int("SHOPALBI_RECOMMEND_NODEPTH_CAP", 60)
RECOMMEND_VOLUME_CAPTURE = _get_float("SHOPALBI_RECOMMEND_VOLUME_CAPTURE", 0.15)
# Without live depth we know the price of the cheapest lot but not its size, so
# buying N units at that exact price is optimistic. For quantities above this,
# the expected fill price falls back to the city's own volume-weighted average,
# which is what you actually end up paying when you clear several lots.
RECOMMEND_TRUST_MIN_PRICE_QTY = _get_int("SHOPALBI_RECOMMEND_TRUST_MIN_PRICE_QTY", 3)
# Don't sink the whole budget into one item.
RECOMMEND_MAX_ITEM_SHARE = _get_float("SHOPALBI_RECOMMEND_MAX_ITEM_SHARE", 0.30)
RECOMMEND_MAX_ITEMS = _get_int("SHOPALBI_RECOMMEND_MAX_ITEMS", 80)


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
# ONE topic only. Measured over 100 s of live traffic: `marketorders.deduped`
# (one order per message) and `marketorders.deduped.bulk` (the same orders
# batched into arrays) delivered 465 unique order ids each, with 100% overlap and
# zero ids unique to either side. Subscribing to both therefore doubles parsing
# work for no extra coverage, and — worse — double-counts every order, which made
# the feed log look like it was losing half the data when it was not.
#
# Never subscribe to `marketorders.ingest`: it carries duplicates and prices
# scaled by 10000 (7160000 where deduped says 716).
NATS_TOPICS = [
    t.strip() for t in
    _get("SHOPALBI_NATS_TOPICS", "marketorders.deduped").split(",")
    if t.strip()
]

# How long a live order stays usable. The feed only carries markets that players
# actually opened in game, at roughly a handful of orders per second worldwide,
# so a 30-minute window leaves the book almost always empty. A wide window with
# an age discount beats no data at all.
ORDER_MAX_AGE_MINUTES = _get_int("SHOPALBI_ORDER_MAX_AGE_MINUTES", 720)
# BM buy orders move with PvE demand and decay faster; keep a tighter window.
BM_ORDER_MAX_AGE_MINUTES = _get_int("SHOPALBI_BM_ORDER_MAX_AGE_MINUTES", 120)

# Age discount on live depth. A wider retention window raises coverage but an
# hours-old order may already be gone, and over-promising quantity is the exact
# failure mode we are trying to kill ("buy 300x T4" when 12 exist). So an order
# counts in full while it is fresh, then its usable amount decays linearly down
# to ORDER_STALE_TRUST at the age limit. Quantities stay conservative as the
# window widens instead of becoming fiction.
ORDER_TRUST_FRESH_MINUTES = _get_int("SHOPALBI_ORDER_TRUST_FRESH_MINUTES", 30)
ORDER_STALE_TRUST = _get_float("SHOPALBI_ORDER_STALE_TRUST", 0.5)
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

# A full price refresh costs ~25-40 API calls out of a 180/min budget, so a
# short interval is cheap. History is heavier (one call per item batch per
# window span) but still nowhere near the limit.
CURRENT_REFRESH_MINUTES = _get_int("SHOPALBI_CURRENT_REFRESH_MINUTES", 5)
HISTORY_REFRESH_HOURS = _get_int("SHOPALBI_HISTORY_REFRESH_HOURS", 4)
# AODP serves at least 180 days of daily buckets (verified). More history means
# a far more stable VWAP for illiquid T7/T8 gear, which is exactly what the
# spike guard depends on — many items only trade a handful of days per month.
HISTORY_DAYS = _get_int("SHOPALBI_HISTORY_DAYS", 90)
CATALOG_MAX_AGE_DAYS = _get_int("SHOPALBI_CATALOG_MAX_AGE_DAYS", 7)
REFRESH_ON_START = _get_bool("SHOPALBI_REFRESH_ON_START", True)


# --- Localization / web -----------------------------------------------------

PRIMARY_LANG = _get("SHOPALBI_LANG", "RU-RU")
FALLBACK_LANG = "EN-US"

HOST = _get("SHOPALBI_HOST", "0.0.0.0")
PORT = _get_int("SHOPALBI_PORT", 8000)

BASIC_AUTH_USER = _get("SHOPALBI_AUTH_USER", "")
BASIC_AUTH_PASS = _get("SHOPALBI_AUTH_PASS", "")


# Windows offered on the stats tabs, in days. Each covers the last N *complete*
# UTC days; today's partial bucket is excluded. `quarter` exists because thin
# T7/T8 items trade only a few days a month — a 30-day window leaves their VWAP
# too noisy to judge whether a live bid is a spike.
STAT_WINDOWS = {
    "day": 1,
    "3d": 3,
    "week": 7,
    "month": 30,
    "quarter": 90,
}
WINDOW_LABELS = {
    "day": "День", "3d": "3 дня", "week": "Неделя", "month": "Месяц", "quarter": "90 дней",
}
