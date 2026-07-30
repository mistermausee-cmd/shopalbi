"""The analytical core.

Two jobs:
  1. Refresh — pull the catalog, current prices and daily history into SQLite,
     then rebuild the derived tables (`agg`, `bm_offer`).
  2. Serve — turn stored data into flips, a city ranking, a budget plan and the
     per-city average-price table.

=============================================================================
PROFIT MODEL
=============================================================================
The canonical Black Market flip is instant on both legs:

    buy from a city sell order  ->  carry to Caerleon  ->  "Sell" into a BM bid

Neither leg creates a market order, so the 2.5% setup fee does not apply; the
only deduction is the sales tax (4% with Premium). Verified against the Albion
wiki Marketplace/Margin pages.

    cost    = city_sell_price_min * cost_mult      # cost_mult = 1.0 (instant buy)
    revenue = bm_bid * net                         # net = 1 - 0.04 = 0.96
    profit  = revenue - cost
    roi     = profit / cost

Both legs are switchable (`buy_mode` / `sell_mode`) because the alternatives are
real strategies: placing your own buy order in the city, or parking a sell order
on the Black Market. Those DO pay the setup fee.

THE QUALITY LADDER. A buy order accepts items of its own quality *or higher*,
and the Black Market prices each quality independently — so it frequently pays
more for a lower quality than a higher one. The reachable bid for an item you
hold at quality Q is therefore max(bid[q] for q <= Q), precomputed into
`bm_offer`. Matching quality strictly 1:1 (the old behaviour) throws away a
large share of the genuinely profitable flips.

TRANSPORT RISK. Caerleon sits in a red-zone cluster; a gank costs the whole
load, not the margin. Expected value of one unit, relative to not trading:

    EV = (1 - p) * revenue - cost          (equivalently: profit - p * revenue)

so the break-even gank rate is p* = 1 - cost/revenue. Buying inside Caerleon
carries p = 0, which is precisely why its prices are structurally higher.
=============================================================================
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config
from .aodp_client import AodpClient
from .catalog import build_items, tier_label
from .storage import Storage

log = logging.getLogger("shopalbi.engine")

# Live units on both sides of the book that count as "comfortable depth".
_DEPTH_REF = 25.0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def liquidity_score(daily: float) -> float:
    """Log-scaled 0..1 liquidity.

    Real Black Market throughput spans four orders of magnitude (a niche T8
    off-hand does single digits per day; T5_BAG clears ~4,300), so a linear
    scale against a small reference — the old code divided by 20 — saturates at
    1.0 for almost everything and carries no information.
    """
    floor, ref = config.LIQUIDITY_FLOOR, config.LIQUIDITY_REF
    if daily <= 0:
        return 0.0
    return _clamp(math.log1p(daily / floor) / math.log1p(ref / floor))


def stability_score(bid: float, vwap: float) -> float:
    """How consistent the current bid is with the item's own traded history.

    A bid at or below the volume-weighted average is trustworthy (1.0). A bid
    far above it is the dangerous case — it looks like free silver, and it is
    usually gone by the time you finish the ride — so the score decays to 0 as
    the bid approaches BM_MAX_VS_VWAP x VWAP.
    """
    if vwap <= 0:
        return 0.35          # no history to judge against: mildly sceptical
    ratio = bid / vwap
    if ratio <= 1.0:
        return 1.0
    span = max(1e-6, config.BM_MAX_VS_VWAP - 1.0)
    return _clamp(1.0 - (ratio - 1.0) / span)


def competition_score(bid: float, ask: float) -> float:
    """Pressure from other players' sell orders parked on the Black Market.

    The BM also carries player `offer` rows. When the cheapest of those sits at
    or below the NPC bid, those sellers get filled before you do. An ask a
    comfortable margin above the bid means nobody is queued in front of you.
    """
    if ask <= 0 or bid <= 0:
        return 1.0           # nothing parked in front of us
    return _clamp((ask - bid) / (0.10 * bid))


def freshness_score(buy_age_h: float, bm_age_h: float) -> float:
    """Weakest-link freshness: a fresh city price cannot rescue a stale BM bid."""
    f_buy = 1.0 - (buy_age_h / max(1e-6, config.FLIP_MAX_AGE_HOURS))
    f_bm = 1.0 - (bm_age_h / max(1e-6, config.BM_MAX_AGE_HOURS))
    return _clamp(min(f_buy, f_bm))


# Reliability weights. They sum to 1.0; `depth` is neutral (0.5) when the live
# order book has nothing for that item rather than punishing it to zero.
_W_FRESH, _W_LIQ, _W_STAB, _W_DEPTH, _W_COMP = 0.30, 0.22, 0.25, 0.13, 0.10


def reliability(fresh: float, liq: float, stab: float, depth: float | None, comp: float) -> int:
    d = 0.5 if depth is None else depth
    return int(round(100.0 * (
        _W_FRESH * fresh + _W_LIQ * liq + _W_STAB * stab + _W_DEPTH * d + _W_COMP * comp
    )))


def depth_trust(age_minutes: float, max_minutes: float) -> float:
    """How much of a live order's amount we are willing to count on.

    Full credit while the order is fresh, then a linear decay to
    ORDER_STALE_TRUST at the retention limit. This is what lets the retention
    window be wide (coverage) without the planner promising quantities that
    have already been bought by someone else (accuracy).
    """
    fresh = config.ORDER_TRUST_FRESH_MINUTES
    if age_minutes <= fresh:
        return 1.0
    span = max(1e-6, max_minutes - fresh)
    k = _clamp((age_minutes - fresh) / span)
    return 1.0 - k * (1.0 - config.ORDER_STALE_TRUST)


def _usable(amount: int, age_minutes: float, max_minutes: float) -> int:
    """Discounted amount, never rounding a real order down to nothing."""
    if amount <= 0:
        return 0
    return max(1, int(amount * depth_trust(age_minutes, max_minutes)))


def city_gank_rate(city: str, base: float) -> float:
    return _clamp(base * config.CITY_RISK_MULTIPLIER.get(city, 1.0), 0.0, 0.95)


# --------------------------------------------------------------------------
# composite ranking
# --------------------------------------------------------------------------
#
# Sorting by one column answers "what is the biggest X". Picking a position to
# actually trade usually means several things at once — a good margin AND real
# turnover AND a short sell-through. RANK_KEYS declares, for each criterion, how
# to read it and which direction is "better"; `composite_rank` then scores rows
# on the combination.
#
# Percentile rank, not min-max: these distributions are extremely skewed (a
# single 500k-profit outlier next to thousands of 2k rows), and min-max
# normalisation would collapse everything except the outlier to ~0.
RANK_KEYS: dict[str, tuple[str, bool]] = {
    # key                (field,               higher_is_better)
    "profit":            ("profit",            True),
    "profit_pct":        ("profit_pct",        True),
    "ev_unit":           ("ev_unit",           True),
    "ev_pct":            ("ev_pct",            True),
    "opportunity":       ("opportunity",       True),
    "throughput":        ("throughput",        True),
    "reliability":       ("reliability",       True),
    "bm_volume":         ("bm_daily_volume",   True),
    "available":         ("available",         True),
    "profit_vwap":       ("profit_vwap",       True),
    "bm_trend":          ("bm_trend_pct",      True),
    "absorb":            ("est_absorb_h",      False),   # sooner is better
    "buy_price":         ("buy_price",         False),   # cheaper is better
}


def _percentiles(values: list[float], higher_is_better: bool) -> list[float]:
    """Percentile position of each value in 0..1, ties sharing the average rank."""
    n = len(values)
    if n <= 1:
        return [1.0] * n
    # Best first, so the leading block gets the 1.0 end of the scale: descending
    # when a bigger number is better, ascending when a smaller one is.
    order = sorted(range(n), key=lambda i: values[i], reverse=higher_is_better)
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # average rank for the tied block, mapped so that "best" -> 1.0
        avg_rank = (i + j) / 2.0
        score = 1.0 - avg_rank / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = score
        i = j + 1
    return out


def composite_rank(rows: list[dict], keys: list[str]) -> list[str]:
    """Annotate rows with `rank_score` (0..100) over the chosen criteria.

    Missing values (a None sell-through, unknown live depth) get the worst
    percentile for that criterion rather than being dropped, so a row is never
    promoted just because we know less about it.

    Returns the criteria actually used.
    """
    used = [k for k in keys if k in RANK_KEYS]
    if not rows or not used:
        for r in rows:
            r.pop("rank_score", None)
            r.pop("rank_parts", None)
        return []

    per_key: dict[str, list[float]] = {}
    for k in used:
        field, higher = RANK_KEYS[k]
        raw = [r.get(field) for r in rows]
        worst = min((v for v in raw if v is not None), default=0.0) if higher else \
            max((v for v in raw if v is not None), default=0.0)
        vals = [float(worst if v is None else v) for v in raw]
        per_key[k] = _percentiles(vals, higher)

    for i, r in enumerate(rows):
        r["rank_parts"] = {k: round(per_key[k][i] * 100) for k in used}
        # Two scores: the exact mean drives the ordering, the rounded one is what
        # gets displayed. Sorting on the rounded value would leave every row
        # inside a shared integer score in arbitrary order — with a few thousand
        # candidates that is ~20 rows per point, enough to look broken.
        exact = sum(per_key[k][i] for k in used) / len(used)
        r["rank_exact"] = round(exact * 100, 4)
        r["rank_score"] = round(exact * 100)
    return used


def _sort_and_slice(rows: list[dict], sort: str, direction: str, keys: dict, limit: int) -> list[dict]:
    keyfn = keys.get(sort) or keys[next(iter(keys))]
    rows.sort(key=keyfn, reverse=(direction != "asc"))
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------
# refresh (write path)
# --------------------------------------------------------------------------

def ensure_catalog(storage: Storage, force: bool = False) -> int:
    if not force and not storage.catalog_needs_rebuild():
        return storage.item_count()
    items = build_items(force=force)
    n = storage.replace_items(items)
    storage.purge_orphans()
    storage.set_meta("catalog_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("catalog_count", str(n))
    log.info("catalog stored: %d items", n)
    return n


def ensure_derived(storage: Storage) -> None:
    """Rebuild `agg`/`bm_offer` from stored raw data if they are missing.

    Raw tables (`history`, `current_prices`) survive an upgrade, but the derived
    ones get dropped whenever the data model changes. Recomputing them at startup
    is a couple of SQL statements and avoids an empty-looking site while the
    first full refresh runs.
    """
    try:
        if storage.table_empty("agg") and not storage.table_empty("history"):
            log.info("derived tables missing after upgrade: rebuilding aggregates")
            storage.rebuild_aggregates()
        if storage.table_empty("bm_offer") and not storage.table_empty("current_prices"):
            log.info("derived tables missing after upgrade: rebuilding Black Market offers")
            storage.rebuild_bm_offers()
    except Exception:
        log.exception("could not rebuild derived tables; the next refresh will fix it")


def refresh_current(storage: Storage, client: AodpClient) -> int:
    item_ids = storage.all_item_ids()
    if not item_ids:
        ensure_catalog(storage)
        item_ids = storage.all_item_ids()
    log.info("refreshing current prices for %d items", len(item_ids))
    t0 = time.monotonic()
    rows = client.fetch_prices(item_ids, config.ALL_LOCATIONS, config.QUALITIES)
    n = storage.upsert_current_prices(rows)
    storage.rebuild_bm_offers()
    storage.set_meta("current_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("current_rows", str(n))
    log.info("current prices stored: %d rows in %.1fs", n, time.monotonic() - t0)
    return n


def refresh_history(storage: Storage, client: AodpClient) -> int:
    item_ids = storage.all_item_ids()
    if not item_ids:
        ensure_catalog(storage)
        item_ids = storage.all_item_ids()
    log.info("refreshing %d-day history for %d items", config.HISTORY_DAYS, len(item_ids))
    t0 = time.monotonic()
    series = client.fetch_history(
        item_ids, config.ALL_LOCATIONS, config.QUALITIES, config.HISTORY_DAYS, time_scale=24
    )
    n = storage.upsert_history(series)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=config.HISTORY_DAYS + 3)).isoformat()
    storage.prune_history(cutoff)
    storage.rebuild_aggregates()
    storage.vacuum_analyze()
    storage.set_meta("history_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("history_rows", str(n))
    log.info("history stored: %d buckets in %.1fs", n, time.monotonic() - t0)
    return n


# --------------------------------------------------------------------------
# read path
# --------------------------------------------------------------------------

class Analytics:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._lock = threading.Lock()
        self._flip_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._cache_ttl = 40.0

    def invalidate(self) -> None:
        with self._lock:
            self._flip_cache.clear()

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        s = self.storage
        min_seen, _ = self._book_window(config.ORDER_MAX_AGE_MINUTES)
        book = s.order_book_stats(min_seen)
        return {
            "version": config.VERSION,
            "items": s.item_count(),
            "catalog_refreshed_at": s.get_meta("catalog_refreshed_at"),
            "current_refreshed_at": s.get_meta("current_refreshed_at"),
            "current_rows": int(s.get_meta("current_rows", "0")),
            "history_refreshed_at": s.get_meta("history_refreshed_at"),
            "history_rows": int(s.get_meta("history_rows", "0")),
            "orderbook": book,
            # Session counters (reset on restart). `accepted` and `written`
            # should stay close; a lasting gap means orders are being dropped.
            "orderbook_accepted": int(s.get_meta("orderbook_accepted", "0")),
            "orderbook_written": int(s.get_meta("orderbook_written", "0")),
            "orderbook_updated_at": s.get_meta("orderbook_updated_at"),
            "refresh_running": s.get_meta("refresh_running", "0") == "1",
            # Which calendar days each window actually averages.
            "agg_ranges": {w: s.get_meta(f"agg_range_{w}") for w in config.STAT_WINDOWS},
            "server": config.API_HOST,
            "sales_tax": config.SALES_TAX,
            "setup_fee": config.SETUP_FEE,
            "gank_rate": config.DEFAULT_GANK_RATE,
            "windows": config.STAT_WINDOWS,
        }

    # -- live order book ----------------------------------------------------

    def _book_window(self, minutes: int) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        return (now - timedelta(minutes=minutes)).isoformat(), now.isoformat()

    def _bm_requests(self) -> dict[str, list[list]]:
        """Live Black Market buy orders per item: [price, amount, quality, age_h].

        Grouped by item (not by quality) because the quality ladder lets one
        order be filled from several held qualities; keeping a single shared
        pool per item is what stops us double-counting the same NPC order.
        """
        cap = config.BM_ORDER_MAX_AGE_MINUTES
        min_seen, now = self._book_window(cap)
        rows = self.storage.live_book("request", min_seen, now, city=config.BLACK_MARKET)
        out: dict[str, list[list]] = defaultdict(list)
        now_dt = datetime.now(timezone.utc)
        for r in rows:
            seen = _parse_iso(r["seen_at"])
            age_min = (now_dt - seen).total_seconds() / 60.0 if seen else cap
            amount = _usable(r["amount"], age_min, cap)
            if amount <= 0:
                continue
            out[r["item_id"]].append([r["price"], amount, r["quality"], age_min / 60.0])
        for lst in out.values():
            lst.sort(key=lambda x: -x[0])
        return out

    def _city_offers(self, city: str | None = None) -> dict[tuple, list[list]]:
        """Live sell offers per (city, item): [price, amount, quality, age_h]."""
        cap = config.ORDER_MAX_AGE_MINUTES
        min_seen, now = self._book_window(cap)
        rows = self.storage.live_book("offer", min_seen, now, city=city)
        out: dict[tuple, list[list]] = defaultdict(list)
        now_dt = datetime.now(timezone.utc)
        for r in rows:
            if r["city"] == config.BLACK_MARKET:
                continue
            seen = _parse_iso(r["seen_at"])
            age_min = (now_dt - seen).total_seconds() / 60.0 if seen else cap
            amount = _usable(r["amount"], age_min, cap)
            if amount <= 0:
                continue
            out[(r["city"], r["item_id"])].append(
                [r["price"], amount, r["quality"], age_min / 60.0]
            )
        for lst in out.values():
            lst.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _depth_units(offers: list[list], requests: list[list], quality: int) -> tuple[int, int, float]:
        """(city units at this quality, BM units reachable from it, newest age h).

        Amounts are already age-discounted by `_usable`, so these are the
        quantities we are prepared to stand behind, not the raw book totals.
        """
        city_units = sum(a for _p, a, q, _age in offers if q == quality)
        bm_units = sum(a for _p, a, q, _age in requests if q <= quality)
        ages = [age for _p, _a, q, age in offers if q == quality]
        ages += [age for _p, _a, q, age in requests if q <= quality]
        return city_units, bm_units, (min(ages) if ages else 0.0)

    # -- flip finder --------------------------------------------------------

    def _candidates(
        self,
        window: str,
        buy_mode: str,
        sell_mode: str,
        min_profit: int,
        min_profit_pct: float,
        min_bm_volume: float,
        gank_rate: float,
        category: str | None,
        tier: int | None,
        enchant: int | None,
        quality: int | None,
        search: str | None,
        buy_cities: list[str] | None = None,
        use_depth: bool = True,
    ) -> list[dict]:
        cities = buy_cities or list(config.BUY_CITIES)
        cache_key = (window, buy_mode, sell_mode, min_profit, min_profit_pct, min_bm_volume,
                     round(gank_rate, 4), category, tier, enchant, quality, search or "",
                     tuple(cities), use_depth)
        with self._lock:
            hit = self._flip_cache.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < self._cache_ttl:
                return hit[1]

        net = config.net_factor(sell_mode)
        cost_mult = config.cost_factor(buy_mode)

        rows = self.storage.flip_rows(
            window=window, net=net, cost_mult=cost_mult,
            min_profit=min_profit, min_profit_pct=min_profit_pct,
            min_bm_daily=min_bm_volume, buy_cities=cities,
            category=category, tier=tier, enchant=enchant, quality=quality, search=search,
        )

        bm_req = self._bm_requests() if use_depth else {}
        offers = self._city_offers() if use_depth else {}

        out: list[dict] = []
        for r in rows:
            # Every derived number is computed from the values we actually DISPLAY,
            # rounded exactly as the UI shows them. Scores used to be built from
            # full-precision inputs while the row carried rounded ones, so the
            # reliability on screen could not be reproduced from the figures next
            # to it — which makes the tool impossible to audit. Cost is also
            # multiplied in Python rather than trusting SQLite's float, so the
            # result does not depend on which engine did the arithmetic.
            buy_price = int(r["buy_price_raw"])
            bm_price = int(r["bm_price"])
            cost = buy_price * cost_mult
            if cost <= 0:
                continue
            revenue = bm_price * net
            profit = round(revenue - cost)

            buy_age = round(float(r["buy_age_h"]), 1)
            bm_age = round(float(r["bm_age_h"]), 1)
            bm_daily = round(float(r["bm_daily"]), 1)
            bm_vwap = int(r["bm_vwap"])
            bm_ask = int(r["bm_ask"])

            fresh = freshness_score(buy_age, bm_age)
            liq = liquidity_score(bm_daily)
            stab = stability_score(bm_price, bm_vwap)
            comp = competition_score(bm_price, bm_ask)

            qty_city = qty_bm = None
            depth = None
            depth_age = None
            if use_depth:
                o = offers.get((r["buy_city"], r["item_id"]))
                q_req = bm_req.get(r["item_id"])
                if o or q_req:
                    qty_city, qty_bm, depth_age = self._depth_units(
                        o or [], q_req or [], r["quality"]
                    )
                    depth = _clamp(min(qty_city, qty_bm) / _DEPTH_REF)
            score = reliability(fresh, liq, stab, depth, comp)

            p = city_gank_rate(r["buy_city"], gank_rate)
            ev_unit = round((1.0 - p) * revenue - cost)
            breakeven_p = 1.0 - (cost / revenue) if revenue > 0 else 0.0

            # window-average profit: the same flip priced off the BM's own
            # volume-weighted average instead of the momentary bid. If this is
            # much worse than the live number, the live number is a spike.
            profit_vwap = round(bm_vwap * net - cost)
            trend = 0.0
            if bm_vwap and r["bm_vwap_day"]:
                trend = (float(r["bm_vwap_day"]) / bm_vwap - 1.0) * 100.0

            available = None
            if qty_city is not None and qty_bm is not None:
                available = min(qty_city, qty_bm)

            out.append({
                "item_id": r["item_id"],
                "name": r["name_disp"],
                "tier": r["tier"],
                "enchant": r["enchant"],
                "tier_ench": tier_label(r["tier"], r["enchant"]),
                "quality": r["quality"],
                "quality_label": config.QUALITY_NAMES.get(r["quality"], str(r["quality"])),
                "category": r["category"],
                "category_label": config.CATEGORY_NAMES.get(r["category"], r["category"]),
                "buy_city": r["buy_city"],
                "buy_price": buy_price,
                "buy_cost": round(cost),
                "buy_age_h": buy_age,
                "bm_price": bm_price,
                "bm_quality": r["bm_quality"],
                "bm_quality_label": config.QUALITY_NAMES.get(r["bm_quality"], str(r["bm_quality"])),
                "quality_upsell": r["bm_quality"] < r["quality"],
                "bm_age_h": bm_age,
                "bm_vwap": bm_vwap,
                "bm_ask": bm_ask,
                "bm_daily_volume": bm_daily,
                "bm_days": r["bm_days"],
                "city_vwap": r["city_vwap"],
                "bm_trend_pct": round(trend, 1),
                # Percentages come from the ROUNDED silver figures shown beside
                # them, so `profit / buy_price` on screen reproduces them exactly.
                "profit": profit,
                "profit_pct": round(profit / cost * 100.0, 1),
                "profit_vwap": profit_vwap,
                "profit_pct_vwap": round(profit_vwap / cost * 100.0, 1),
                "ev_unit": ev_unit,
                "ev_pct": round(ev_unit / cost * 100.0, 1),
                "gank_rate": round(p * 100.0, 1),
                "breakeven_gank_pct": round(breakeven_p * 100.0, 1),
                "spike": bm_vwap > 0 and bm_price > config.BM_MAX_VS_VWAP * bm_vwap,
                "avail_city": qty_city,
                "avail_bm": qty_bm,
                "available": available,
                "depth_age_h": None if depth_age is None else round(depth_age, 1),
                "est_absorb_h": round(24.0 / bm_daily, 2) if bm_daily > 0 else None,
                "reliability": score,
                # Headline composite: expected profit per unit after transport
                # risk, discounted by how much we trust the numbers.
                "opportunity": round(ev_unit * score / 100.0),
                # Daily opportunity: what the item is worth if you work it all
                # day, bounded by what the Black Market can actually absorb.
                "throughput": round(round(ev_unit * score / 100.0) * min(bm_daily, 200.0)),
            })

        with self._lock:
            self._flip_cache[cache_key] = (time.monotonic(), out)
            if len(self._flip_cache) > 24:
                oldest = min(self._flip_cache, key=lambda k: self._flip_cache[k][0])
                self._flip_cache.pop(oldest, None)
        return out

    _FLIP_KEYS = {
        "opportunity": lambda r: r["opportunity"],
        "throughput": lambda r: r["throughput"],
        "profit": lambda r: r["profit"],
        "profit_pct": lambda r: r["profit_pct"],
        "ev_unit": lambda r: r["ev_unit"],
        "ev_pct": lambda r: r["ev_pct"],
        "profit_vwap": lambda r: r["profit_vwap"],
        "reliability": lambda r: r["reliability"],
        "bm_volume": lambda r: r["bm_daily_volume"],
        "bm_trend": lambda r: r["bm_trend_pct"],
        "buy_price": lambda r: r["buy_price"],
        "bm_price": lambda r: r["bm_price"],
        "available": lambda r: (-1 if r["available"] is None else r["available"]),
        "absorb": lambda r: (r["est_absorb_h"] if r["est_absorb_h"] is not None else float("inf")),
        "buy_city": lambda r: r["buy_city"],
        "name": lambda r: r["name"].lower(),
        "tier": lambda r: (r["tier"], r["enchant"]),
        "quality": lambda r: r["quality"],
    }

    def flips(
        self,
        window: str = "week",
        buy_mode: str | None = None,
        sell_mode: str | None = None,
        min_profit: int | None = None,
        min_profit_pct: float | None = None,
        min_bm_volume: float | None = None,
        gank_rate: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        enchant: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        buy_city: str | None = None,
        sort: str = "opportunity",
        direction: str = "desc",
        limit: int = 300,
        rank: list[str] | None = None,
    ) -> dict:
        buy_mode = buy_mode or config.DEFAULT_BUY_MODE
        sell_mode = sell_mode or config.DEFAULT_SELL_MODE
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_profit_pct = config.DEFAULT_MIN_PROFIT_PCT if min_profit_pct is None else min_profit_pct
        min_bm_volume = config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        gank = config.DEFAULT_GANK_RATE if gank_rate is None else gank_rate

        # A city that is not a permitted buy source must never leak in through a
        # query parameter — otherwise `?buy_city=Brecilien` quietly bypasses the
        # exclusion policy that the rest of the app enforces.
        if buy_city and buy_city not in config.BUY_CITIES:
            return {
                "window": window, "window_days": config.STAT_WINDOWS.get(window, 7),
                "window_range": self.storage.get_meta(f"agg_range_{window}"),
                "buy_mode": buy_mode, "sell_mode": sell_mode,
                "net": config.net_factor(sell_mode), "cost_mult": config.cost_factor(buy_mode),
                "sales_tax": config.SALES_TAX, "setup_fee": config.SETUP_FEE,
                "gank_rate": gank, "min_profit": min_profit,
                "min_profit_pct": min_profit_pct, "min_bm_volume": min_bm_volume,
                "buy_city": buy_city, "sort": sort, "direction": direction,
                "total": 0, "rows": [],
                "note": f"{buy_city} не входит в список городов закупки",
            }

        cands = self._candidates(
            window, buy_mode, sell_mode, min_profit, min_profit_pct, min_bm_volume,
            gank, category, tier, enchant, quality, search,
            buy_cities=[buy_city] if buy_city else None,
        )
        if buy_city:
            rows = list(cands)
        else:
            # collapse to the single best source city per (item, quality)
            best: dict[tuple, dict] = {}
            for c in cands:
                k = (c["item_id"], c["quality"])
                cur = best.get(k)
                if cur is None or c["opportunity"] > cur["opportunity"]:
                    best[k] = c
            rows = list(best.values())

        if not quality:
            rows = self._merge_identical_qualities(rows)

        total = len(rows)
        # Composite ranking is applied to the FULL candidate set before paging, so
        # the top of the list is the best combination overall — not just the best
        # combination among whatever a single-column sort happened to surface.
        used_rank = composite_rank(rows, rank or [])
        if used_rank:
            rows.sort(key=lambda r: r["rank_exact"], reverse=(direction != "asc"))
            page = rows[:limit] if limit else rows
        else:
            page = _sort_and_slice(rows, sort, direction, self._FLIP_KEYS, limit)
        return {
            "window": window,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "window_range": self.storage.get_meta(f"agg_range_{window}"),
            "buy_mode": buy_mode,
            "sell_mode": sell_mode,
            "net": config.net_factor(sell_mode),
            "cost_mult": config.cost_factor(buy_mode),
            "sales_tax": config.SALES_TAX,
            "setup_fee": config.SETUP_FEE,
            "gank_rate": gank,
            "min_profit": min_profit,
            "min_profit_pct": min_profit_pct,
            "min_bm_volume": min_bm_volume,
            "buy_city": buy_city,
            "sort": sort,
            "direction": direction,
            "rank": used_rank,
            "rank_available": list(RANK_KEYS),
            "total": total,
            "rows": page,
        }

    @staticmethod
    def _merge_identical_qualities(rows: list[dict]) -> list[dict]:
        """Fold rows that describe the exact same trade at different qualities.

        When a city's cheapest listing sits at the same price for q3 and q4, and
        both sell into the same Black Market order, the two rows carry identical
        numbers and only pad the table. We keep the lowest quality (cheaper and
        more plentiful in practice) and list the others in `also_qualities`, so
        nothing is hidden — you can still see that q4 works at the same price.
        """
        merged: dict[tuple, dict] = {}
        for r in rows:
            key = (r["item_id"], r["buy_city"], r["buy_price"], r["bm_price"], r["bm_quality"])
            cur = merged.get(key)
            if cur is None:
                r = dict(r)
                r["also_qualities"] = []
                merged[key] = r
                continue
            lo, hi = (r, cur) if r["quality"] < cur["quality"] else (cur, r)
            if lo is not cur:
                lo = dict(lo)
                lo["also_qualities"] = cur["also_qualities"]
                merged[key] = lo
            if hi["quality"] not in lo["also_qualities"]:
                lo["also_qualities"].append(hi["quality"])
            lo["also_qualities"].sort()
        return list(merged.values())

    # -- city ranking -------------------------------------------------------

    def city_ranking(self, top_n: int = 100, **kw) -> dict:
        kw.pop("buy_city", None)
        kw.pop("sort", None)
        kw.pop("direction", None)
        kw.pop("limit", None)
        window = kw.pop("window", "week")
        buy_mode = kw.pop("buy_mode", None) or config.DEFAULT_BUY_MODE
        sell_mode = kw.pop("sell_mode", None) or config.DEFAULT_SELL_MODE
        min_profit = kw.pop("min_profit", None)
        min_profit_pct = kw.pop("min_profit_pct", None)
        min_bm_volume = kw.pop("min_bm_volume", None)
        gank = kw.pop("gank_rate", None)
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_profit_pct = config.DEFAULT_MIN_PROFIT_PCT if min_profit_pct is None else min_profit_pct
        min_bm_volume = config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        gank = config.DEFAULT_GANK_RATE if gank is None else gank

        cands = self._candidates(
            window, buy_mode, sell_mode, min_profit, min_profit_pct, min_bm_volume, gank,
            kw.get("category"), kw.get("tier"), kw.get("enchant"),
            kw.get("quality"), kw.get("search"),
        )
        buckets: dict[str, list[dict]] = defaultdict(list)
        for c in cands:
            buckets[c["buy_city"]].append(c)

        out: list[dict] = []
        for city, lst in buckets.items():
            lst.sort(key=lambda x: x["opportunity"], reverse=True)
            top = lst[:top_n]
            n = len(top) or 1
            best = top[0] if top else None
            out.append({
                "city": city,
                "flips_count": len(lst),
                "score": sum(x["opportunity"] for x in top),
                "ev_sum": sum(x["ev_unit"] for x in top),
                "profit_sum": sum(x["profit"] for x in top),
                "avg_profit_pct": round(sum(x["profit_pct"] for x in top) / n, 1),
                "avg_ev_pct": round(sum(x["ev_pct"] for x in top) / n, 1),
                "avg_reliability": round(sum(x["reliability"] for x in top) / n),
                "gank_rate": round(city_gank_rate(city, gank) * 100.0, 1),
                "trip_hours": config.CITY_TRIP_HOURS.get(city, 0.55),
                "best_item": best["name"] if best else "",
                "best_tier_ench": best["tier_ench"] if best else "",
                "best_profit": best["profit"] if best else 0,
                "best_profit_pct": best["profit_pct"] if best else 0.0,
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return {
            "window": window,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "gank_rate": gank,
            "top_n": top_n,
            "total": len(out),
            "rows": out,
        }

    # -- average price table ------------------------------------------------

    _STAT_KEYS = {
        "bm_volume": lambda r: r["bm_volume"],
        "bm_daily": lambda r: r["bm_daily"],
        "bm_avg": lambda r: r["bm_avg"],
        "bm_now": lambda r: r["bm_now"],
        "spread_pct": lambda r: r["spread_pct"],
        "cheapest": lambda r: r["cheapest_avg"],
        "name": lambda r: r["name"].lower(),
        "tier": lambda r: (r["tier"], r["enchant"]),
        "quality": lambda r: r["quality"],
    }

    def stats_table(
        self,
        window: str,
        category: str | None = None,
        tier: int | None = None,
        enchant: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        sort: str = "bm_volume",
        direction: str = "desc",
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        rows = self.storage.stats_rows(window, category, tier, enchant, quality, search)
        grouped: dict[tuple, dict] = {}
        for r in rows:
            key = (r["item_id"], r["quality"])
            g = grouped.get(key)
            if g is None:
                g = {
                    "item_id": r["item_id"], "name": r["name_disp"], "tier": r["tier"],
                    "enchant": r["enchant"], "tier_ench": tier_label(r["tier"], r["enchant"]),
                    "quality": r["quality"],
                    "quality_label": config.QUALITY_NAMES.get(r["quality"], str(r["quality"])),
                    "category": r["category"],
                    "category_label": config.CATEGORY_NAMES.get(r["category"], r["category"]),
                    "cities": {}, "bm_avg": 0, "bm_volume": 0, "bm_daily": 0.0, "bm_now": 0,
                }
                grouped[key] = g
            entry = {
                "avg": r["vwap"], "volume": r["volume"], "daily": round(r["daily"], 1),
                "now": r["sell_now"],
            }
            if r["city"] == config.BLACK_MARKET:
                g["bm_avg"] = r["vwap"]
                g["bm_volume"] = r["volume"]
                g["bm_daily"] = round(r["daily"], 1)
                g["bm_now"] = r["buy_now"]
            else:
                g["cities"][r["city"]] = entry

        net = config.net_factor()
        out: list[dict] = []
        for g in grouped.values():
            cheapest_city, cheapest_avg = None, None
            for c, e in g["cities"].items():
                if c in config.EXCLUDED_BUY_CITIES:
                    continue
                if e["avg"] > 0 and (cheapest_avg is None or e["avg"] < cheapest_avg):
                    cheapest_avg, cheapest_city = e["avg"], c
            g["cheapest_city"] = cheapest_city
            g["cheapest_avg"] = cheapest_avg or 0
            # Historical edge: BM average vs the cheapest city average, net of tax.
            g["spread_pct"] = (
                round((g["bm_avg"] * net - cheapest_avg) / cheapest_avg * 100.0, 1)
                if cheapest_avg else 0.0
            )
            out.append(g)

        if sort.startswith("city:"):
            c = sort[5:]
            keyfn = lambda r: r["cities"].get(c, {}).get("avg", 0)
            out.sort(key=keyfn, reverse=(direction != "asc"))
        else:
            _sort_and_slice(out, sort, direction, self._STAT_KEYS, 0)

        total = len(out)
        return {
            "window": window,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "window_range": self.storage.get_meta(f"agg_range_{window}"),
            "total": total, "offset": offset, "limit": limit,
            "sort": sort, "direction": direction,
            "cities": config.ROYAL_CITIES,
            "net": net,
            "rows": out[offset: offset + limit],
        }

    # -- budget planner -----------------------------------------------------

    def plan(
        self,
        budget: int,
        city: str | None = None,
        window: str = "week",
        buy_mode: str | None = None,
        sell_mode: str | None = None,
        min_profit: int | None = None,
        min_profit_pct: float | None = None,
        min_bm_volume: float | None = None,
        gank_rate: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        enchant: int | None = None,
        quality: int | None = None,
        search: str | None = None,
    ) -> dict:
        """Shopping plan for a budget: which city, which items, how many.

        Quantities come from the live order book wherever we have it — real sell
        offers matched against real Black Market buy orders, bounded by the
        actual amounts on both sides — so the plan cannot suggest buying more
        units than physically exist. Where there is no live depth the quantity
        is capped hard (NODEPTH_CAP and a share of one day's BM absorption)
        and flagged as an estimate.
        """
        buy_mode = buy_mode or config.DEFAULT_BUY_MODE
        sell_mode = sell_mode or config.DEFAULT_SELL_MODE
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_profit_pct = config.DEFAULT_MIN_PROFIT_PCT if min_profit_pct is None else min_profit_pct
        min_bm_volume = config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        gank = config.DEFAULT_GANK_RATE if gank_rate is None else gank_rate
        budget = max(0, int(budget or 0))

        net = config.net_factor(sell_mode)
        cost_mult = config.cost_factor(buy_mode)

        cands = self._candidates(
            window, buy_mode, sell_mode, min_profit, min_profit_pct, min_bm_volume, gank,
            category, tier, enchant, quality, search,
            buy_cities=[city] if city else None,
        )
        bm_req = self._bm_requests()
        by_city: dict[str, list[dict]] = defaultdict(list)
        for c in cands:
            by_city[c["buy_city"]].append(c)

        # Same guard as flips(): an excluded or unknown city must not become a
        # buy base just because it was passed as a parameter.
        cities = ([city] if city in config.BUY_CITIES else []) if city else list(config.BUY_CITIES)
        plans = []
        for cy in cities:
            offers = self._city_offers(cy)
            plans.append(self._plan_city(cy, budget, by_city.get(cy, []), offers, bm_req,
                                         net, cost_mult, gank))
        plans.sort(key=lambda p: p["ev_profit"], reverse=True)
        best = plans[0] if plans else None

        min_seen, _ = self._book_window(config.ORDER_MAX_AGE_MINUTES)
        book = self.storage.order_book_stats(min_seen)
        return {
            "window": window, "budget": budget,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "window_range": self.storage.get_meta(f"agg_range_{window}"),
            "buy_mode": buy_mode, "sell_mode": sell_mode,
            "net": net, "cost_mult": cost_mult,
            "sales_tax": config.SALES_TAX, "setup_fee": config.SETUP_FEE,
            "gank_rate": gank,
            "requested_city": city,
            "orderbook": book,
            "has_depth": bool(bm_req),
            "city": best["city"] if best else None,
            "best": best,
            "cities": [
                {k: p.get(k) for k in ("city", "ev_profit", "profit", "spent", "leftover",
                                       "roi_pct", "items_count", "source", "gank_rate")}
                for p in plans
            ],
        }

    @staticmethod
    def _volume_cap(meta: dict) -> int:
        """Units the Black Market plausibly absorbs, when live depth is unknown.

        Driven by measured daily throughput; the constant is only a backstop
        against absurd numbers, never the thing that decides the quantity.
        """
        cap = config.RECOMMEND_NODEPTH_CAP
        daily = meta["bm_daily_volume"]
        if daily > 0:
            cap = min(cap, max(1, int(daily * config.RECOMMEND_VOLUME_CAPTURE)))
        return cap

    @staticmethod
    def _synth_requests(group: list[dict]) -> list[list]:
        """Synthetic Black Market demand built from the REST snapshot.

        `buy_price_max` is the single HIGHEST buy order, and on live data that
        top order carries only a small slice of an item's real demand — measured
        on a 265k-unit snapshot of the Black Market book, as little as 1-3% for
        liquid items (T5_BAG: 48 units of 1,702 at the best price; T4_CAPE: 12 of
        1,324). Pricing a whole bulk quantity at that one bid overstates revenue,
        so only the first RECOMMEND_TRUST_MIN_PRICE_QTY units get it and the rest
        fall back to the Black Market's own volume-weighted average, which is
        what the deeper orders actually pay.

        Keyed by the buy order's own quality so that several held qualities
        cannot each discover the same order and double-count the demand.
        """
        by_order: dict[int, dict] = {}
        for m in group:
            q = m["bm_quality"]
            cur = by_order.get(q)
            if cur is None or m["bm_price"] > cur["bm_price"]:
                by_order[q] = m
        out: list[list] = []
        for q, m in by_order.items():
            cap = Analytics._volume_cap(m)
            trust = min(cap, config.RECOMMEND_TRUST_MIN_PRICE_QTY)
            top = float(m["bm_price"])
            out.append([top, trust, q])
            rest = cap - trust
            if rest > 0:
                # never above the observed top bid, and only if we have history
                deep = min(float(m["bm_vwap"] or 0), top)
                if deep > 0:
                    out.append([deep, rest, q])
        return out

    @staticmethod
    def _synth_offers(group: list[dict]) -> list[list]:
        """Synthetic city supply built from the REST snapshot.

        Same reasoning mirrored: we know the cheapest listing's price but not how
        many units sit behind it, so beyond the first few units the expected fill
        price rises to the city's own volume-weighted average.
        """
        out: list[list] = []
        for m in group:
            cap = Analytics._volume_cap(m)
            trust = min(cap, config.RECOMMEND_TRUST_MIN_PRICE_QTY)
            cheapest = float(m["buy_price"])
            out.append([cheapest, trust, m["quality"]])
            rest = cap - trust
            if rest > 0:
                deep = max(cheapest, float(m["city_vwap"] or 0))
                out.append([deep, rest, m["quality"]])
        return out

    def _plan_city(self, city, budget, cands, offers, bm_req, net, cost_mult, gank) -> dict:  # noqa: C901
        """Build one city's plan. Returns aggregated per-item buy instructions."""
        p = city_gank_rate(city, gank)
        by_item: dict[str, list[dict]] = defaultdict(list)
        for c in cands:
            by_item[c["item_id"]].append(c)

        # ---- 1. enumerate fillable lots -----------------------------------
        # Both sides are modelled as an order book: the real live one where the
        # feed has it, a small synthetic one derived from the REST snapshot where
        # it does not. A single matching routine then covers every combination
        # (both live / only the Black Market live / only the city live / neither).
        #
        # The previous all-or-nothing split required BOTH sides to be live before
        # using any real depth, which on live data threw away genuine Black
        # Market demand for 1,134 items whose city side simply had not been
        # scanned recently.
        lots: list[dict] = []
        for item_id, group in by_item.items():
            live_reqs = [list(r) for r in bm_req.get(item_id, [])]
            live_offs = [list(o) for o in offers.get((city, item_id), [])]
            bm_live, city_live = bool(live_reqs), bool(live_offs)

            reqs = live_reqs if bm_live else self._synth_requests(group)
            offs = live_offs if city_live else self._synth_offers(group)
            if not reqs or not offs:
                continue
            src = ("live" if city_live else "bm") if bm_live else ("city" if city_live else "est")
            meta_by_q = {g["quality"]: g for g in group}

            # One shared pool of buy orders per item, so the same order can never
            # be sold into twice even though several held qualities are eligible
            # for it (the quality ladder).
            for offer in sorted(offs, key=lambda x: x[0]):
                price, amount, held_q = offer[0], offer[1], offer[2]
                meta = meta_by_q.get(held_q)
                if meta is None:
                    continue
                left = amount
                while left > 0:
                    pick = next(
                        (r for r in reqs if r[2] <= held_q and r[1] > 0
                         and r[0] * net - price * cost_mult > 0),
                        None,
                    )
                    if pick is None:
                        break
                    take = min(left, pick[1])
                    lots.append({
                        "item_id": item_id, "meta": meta, "qty": take,
                        "unit_cost": price * cost_mult, "unit_rev": pick[0] * net,
                        "buy_price": round(price), "bm_price": round(pick[0]),
                        "bm_quality": pick[2], "source": src,
                    })
                    pick[1] -= take
                    left -= take

        # ---- 2. greedy budget fill ----------------------------------------
        # Rank by risk-adjusted return per silver spent, weighted by how much we
        # trust the quote: capital is the scarce resource, not item count.
        def efficiency(lot):
            c = lot["unit_cost"]
            if c <= 0:
                return 0.0
            ev = (1.0 - p) * lot["unit_rev"] - c
            return ev / c * (lot["meta"]["reliability"] / 100.0)

        lots = [l for l in lots if l["unit_cost"] > 0 and efficiency(l) > 0]
        lots.sort(key=efficiency, reverse=True)
        remaining = float(budget)
        spent_on: dict[str, float] = defaultdict(float)
        picked: dict[tuple, dict] = {}
        lot_left = [l["qty"] for l in lots]

        def take(i: int, lot: dict, qty: int) -> None:
            nonlocal remaining
            uc = lot["unit_cost"]
            key = (lot["item_id"], lot["meta"]["quality"])
            agg = picked.get(key)
            if agg is None:
                agg = {
                    "meta": lot["meta"], "qty": 0, "cost": 0.0, "revenue": 0.0,
                    "min_price": lot["buy_price"], "max_price": lot["buy_price"],
                    "bm_top": lot["bm_price"], "bm_quality": lot["bm_quality"],
                    "source": lot["source"],
                }
                picked[key] = agg
            agg["qty"] += qty
            agg["cost"] += qty * uc
            agg["revenue"] += qty * lot["unit_rev"]
            agg["min_price"] = min(agg["min_price"], lot["buy_price"])
            agg["max_price"] = max(agg["max_price"], lot["buy_price"])
            agg["bm_top"] = max(agg["bm_top"], lot["bm_price"])
            remaining -= qty * uc
            spent_on[lot["item_id"]] += qty * uc
            lot_left[i] -= qty

        # Pass 1 — diversified: no single item may eat more than a share of the
        # budget, so one hot item cannot become the whole plan.
        # Pass 2 — mop-up: spend whatever is left on the best remaining lots
        # without the share cap. Without this second pass a budget smaller than
        # (unit price / share) buys literally nothing.
        for enforce_cap in (True, False):
            item_cap = budget * config.RECOMMEND_MAX_ITEM_SHARE if (budget and enforce_cap) else 0.0
            for i, lot in enumerate(lots):
                if lot_left[i] < 1:
                    continue
                uc = lot["unit_cost"]
                if remaining < uc:
                    continue
                key = (lot["item_id"], lot["meta"]["quality"])
                if len(picked) >= config.RECOMMEND_MAX_ITEMS and key not in picked:
                    continue
                room = remaining
                if item_cap:
                    room = min(room, max(0.0, item_cap - spent_on[lot["item_id"]]))
                qty = min(lot_left[i], int(room // uc))
                if qty >= 1:
                    take(i, lot, qty)

        # ---- 3. shape the output ------------------------------------------
        items = []
        for (item_id, q), a in picked.items():
            m = a["meta"]
            qty, cost, revenue = a["qty"], a["cost"], a["revenue"]
            profit = revenue - cost
            ev = (1.0 - p) * revenue - cost
            daily = m["bm_daily_volume"]
            items.append({
                "item_id": item_id, "name": m["name"], "tier_ench": m["tier_ench"],
                "quality": q, "quality_label": m["quality_label"],
                "category_label": m["category_label"],
                "qty": qty,
                "unit_price": a["min_price"],
                "max_price": a["max_price"],
                # avg_price = average market price of the lots you clear;
                # avg_cost  = what you actually pay per unit (incl. order fee).
                "avg_price": round(cost / qty / cost_mult) if qty else 0,
                "avg_cost": round(cost / qty) if qty else 0,
                "total_cost": round(cost),
                "bm_buy_now": a["bm_top"],
                # The price you actually average across the orders being filled.
                # `bm_buy_now` is only the BEST bid; showing it alone next to a
                # profit computed from the whole matched book looks inconsistent,
                # because the deeper orders pay less.
                "avg_sell": round(revenue / qty / net) if qty else 0,
                "bm_quality": a["bm_quality"],
                "bm_quality_label": config.QUALITY_NAMES.get(a["bm_quality"], str(a["bm_quality"])),
                "quality_upsell": a["bm_quality"] < q,
                "unit_profit": round(profit / qty) if qty else 0,
                "total_profit": round(profit),
                "ev_profit": round(ev),
                "profit_pct": round(profit / cost * 100.0, 1) if cost else 0.0,
                "bm_daily_volume": daily,
                "absorb_h": round(24.0 * qty / daily, 1) if daily > 0 else None,
                "reliability": m["reliability"],
                "available": m.get("available"),
                "bm_trend_pct": m["bm_trend_pct"],
                "spike": m["spike"],
                "source": a["source"],
            })
        items.sort(key=lambda x: x["ev_profit"], reverse=True)

        spent = sum(x["total_cost"] for x in items)
        profit = sum(x["total_profit"] for x in items)
        ev = sum(x["ev_profit"] for x in items)

        # Why the budget was not fully placed. Without this the user just sees a
        # large leftover and cannot tell a deliberate limit from a bug.
        leftover = budget - spent
        depth_left = sum(lot_left)
        if not lots:
            reason = "filters"
        elif leftover <= 0 or (budget and leftover / budget < 0.02):
            reason = "budget"
        elif depth_left <= 0:
            reason = "depth"
        elif len(picked) >= config.RECOMMEND_MAX_ITEMS:
            reason = "positions"
        elif not any(l["unit_cost"] <= leftover for i, l in enumerate(lots) if lot_left[i] > 0):
            reason = "budget"
        else:
            reason = "depth"
        absorb = max((x["absorb_h"] or 0) for x in items) if items else 0
        # Wall clock for one run = shopping + ride. Absorption is reported
        # separately because you are not standing still while the BM eats the
        # load; folding it in here would make long-tail items look worthless.
        trip = config.CITY_TRIP_HOURS.get(city, 0.55) + config.LOAD_OVERHEAD_HOURS
        live_share = (
            round(sum(1 for x in items if x["source"] == "live") / len(items) * 100)
            if items else 0
        )
        return {
            "city": city,
            "items": items,
            "items_count": len(items),
            "spent": round(spent),
            "leftover": round(leftover),
            "limit_reason": reason,
            "candidates": len(cands),
            "max_items": config.RECOMMEND_MAX_ITEMS,
            "profit": round(profit),
            "ev_profit": round(ev),
            "roi_pct": round(profit / spent * 100.0, 1) if spent else 0.0,
            "ev_roi_pct": round(ev / spent * 100.0, 1) if spent else 0.0,
            "gank_rate": round(p * 100.0, 1),
            "risk_cost": round(profit - ev),
            "trip_hours": round(trip, 2),
            "profit_per_hour": round(ev / trip) if trip else 0,
            "absorb_h": round(absorb, 1),
            "live_share_pct": live_share,
            "source": "live" if live_share >= 50 else "est",
        }
