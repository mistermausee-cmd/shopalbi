"""The analytical core.

Two jobs:
  1. Refresh: pull the catalog, current prices and daily history into SQLite.
  2. Serve: turn stored data into (a) per-city average tables over a time
     window and (b) a ranked list of city -> Black Market flips with honest
     profit figures, sell-through velocity and a reliability score.

Profit model for an instant flip (buy from a city sell order, carry to
Caerleon, sell into the Black Market buy order):

    cost    = city_sell_price_min                       # what you pay
    revenue = bm_buy_price_max * (1 - SALES_TAX)         # what you keep, 4% premium
    profit  = revenue - cost
    profit% = profit / cost * 100

Historical figures use a volume-weighted average price so a handful of tiny
trades cannot skew the number.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config
from .aodp_client import AodpClient
from .catalog import build_items
from .storage import Storage

log = logging.getLogger("shopalbi.engine")

# Reference points for the reliability score.
_LIQUIDITY_REF = 20.0   # BM units/day that counts as "very liquid"


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        txt = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _age_hours(s: str | None, now: datetime) -> float | None:
    dt = _parse_iso(s)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# refresh (write path)
# --------------------------------------------------------------------------

def ensure_catalog(storage: Storage, force: bool = False) -> int:
    if not force and storage.item_count() > 0:
        return storage.item_count()
    items = build_items(force=force)
    n = storage.replace_items(items)
    storage.set_meta("catalog_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("catalog_count", str(n))
    log.info("catalog stored: %d items", n)
    return n


def refresh_current(storage: Storage, client: AodpClient) -> int:
    item_ids = storage.all_item_ids()
    if not item_ids:
        ensure_catalog(storage)
        item_ids = storage.all_item_ids()
    log.info("refreshing current prices for %d items", len(item_ids))
    rows = client.fetch_prices(item_ids, config.ALL_LOCATIONS, config.QUALITIES)
    n = storage.upsert_current_prices(rows)
    storage.set_meta("current_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("current_rows", str(n))
    log.info("current prices stored: %d rows", n)
    return n


def refresh_history(storage: Storage, client: AodpClient) -> int:
    item_ids = storage.all_item_ids()
    if not item_ids:
        ensure_catalog(storage)
        item_ids = storage.all_item_ids()
    log.info("refreshing %d-day history for %d items", config.HISTORY_DAYS, len(item_ids))
    series = client.fetch_history(
        item_ids, config.ALL_LOCATIONS, config.QUALITIES, config.HISTORY_DAYS, time_scale=24
    )
    n = storage.upsert_history(series)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=config.HISTORY_DAYS + 3)).isoformat()
    storage.prune_history(cutoff)
    storage.set_meta("history_refreshed_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("history_rows", str(n))
    log.info("history stored: %d buckets", n)
    return n


# --------------------------------------------------------------------------
# read path with a tiny TTL cache so rapid UI requests don't recompute
# --------------------------------------------------------------------------

class Analytics:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._lock = threading.Lock()
        self._agg_cache: dict[int, tuple[float, dict]] = {}
        self._cache_ttl = 45.0

    # window aggregation: (item_id, city, quality) -> {vwap, volume, daily, days}
    def _aggregate(self, window_days: int) -> dict:
        with self._lock:
            hit = self._agg_cache.get(window_days)
            if hit and (time.monotonic() - hit[0]) < self._cache_ttl:
                return hit[1]
        since = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()
        rows = self.storage.history_rows(since)
        acc: dict[tuple, dict] = {}
        for r in rows:
            key = (r["item_id"], r["city"], r["quality"])
            a = acc.get(key)
            if a is None:
                a = {"pv": 0.0, "vol": 0, "days": 0}
                acc[key] = a
            cnt = r["item_count"]
            a["pv"] += r["avg_price"] * cnt
            a["vol"] += cnt
            a["days"] += 1
        result: dict[tuple, dict] = {}
        for key, a in acc.items():
            vol = a["vol"]
            vwap = (a["pv"] / vol) if vol > 0 else 0.0
            result[key] = {
                "vwap": round(vwap),
                "volume": vol,
                "daily": round(vol / window_days, 2),
                "days": a["days"],
            }
        with self._lock:
            self._agg_cache[window_days] = (time.monotonic(), result)
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._agg_cache.clear()

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        s = self.storage
        return {
            "items": s.item_count(),
            "catalog_refreshed_at": s.get_meta("catalog_refreshed_at"),
            "current_refreshed_at": s.get_meta("current_refreshed_at"),
            "current_rows": int(s.get_meta("current_rows", "0")),
            "history_refreshed_at": s.get_meta("history_refreshed_at"),
            "history_rows": int(s.get_meta("history_rows", "0")),
            "server": config.API_HOST,
            "sales_tax": config.SALES_TAX,
            "windows": config.STAT_WINDOWS,
        }

    # -- per-city average table --------------------------------------------

    def stats_table(
        self,
        window: str,
        category: str | None = None,
        tier: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        sort: str = "bm_volume",
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        window_days = config.STAT_WINDOWS.get(window, 7)
        agg = self._aggregate(window_days)
        meta = self.storage.item_meta_map()

        # group by (item_id, quality) -> per-city figures
        grouped: dict[tuple, dict] = {}
        for (item_id, city, q), v in agg.items():
            if item_id not in meta:
                continue
            key = (item_id, q)
            g = grouped.get(key)
            if g is None:
                g = {"cities": {}, "bm": None}
                grouped[key] = g
            entry = {"avg": v["vwap"], "volume": v["volume"], "daily": v["daily"]}
            if city == config.BLACK_MARKET:
                g["bm"] = entry
            else:
                g["cities"][city] = entry

        search_l = (search or "").strip().lower()
        rows: list[dict] = []
        for (item_id, q), g in grouped.items():
            m = meta[item_id]
            if category and m["category"] != category:
                continue
            if tier and m["tier"] != tier:
                continue
            if quality and q != quality:
                continue
            display = (m["name_ru"] or m["name_en"])
            if m["enchant"]:
                display = f"{display} .{m['enchant']}"
            if search_l and search_l not in display.lower() and search_l not in item_id.lower():
                continue
            bm_vol = g["bm"]["volume"] if g["bm"] else 0
            bm_avg = g["bm"]["avg"] if g["bm"] else 0
            best_city = None
            best_city_avg = None
            for c, e in g["cities"].items():
                if e["avg"] > 0 and (best_city_avg is None or e["avg"] < best_city_avg):
                    best_city_avg = e["avg"]
                    best_city = c
            rows.append(
                {
                    "item_id": item_id,
                    "name": display,
                    "tier": m["tier"],
                    "tier_label": f"T{m['tier']}",
                    "enchant": m["enchant"],
                    "quality": q,
                    "quality_label": config.QUALITY_NAMES.get(q, str(q)),
                    "category": m["category"],
                    "category_label": config.CATEGORY_NAMES.get(m["category"], m["category"]),
                    "cities": g["cities"],
                    "bm_avg": bm_avg,
                    "bm_volume": bm_vol,
                    "bm_daily": g["bm"]["daily"] if g["bm"] else 0,
                    "cheapest_city": best_city,
                    "cheapest_city_avg": best_city_avg or 0,
                }
            )

        sort_keys = {
            "bm_volume": lambda r: r["bm_volume"],
            "bm_avg": lambda r: r["bm_avg"],
            "name": lambda r: r["name"].lower(),
            "tier": lambda r: (r["tier"], r["enchant"]),
        }
        keyfn = sort_keys.get(sort, sort_keys["bm_volume"])
        reverse = sort not in ("name", "tier")
        rows.sort(key=keyfn, reverse=reverse)

        total = len(rows)
        page = rows[offset: offset + limit]
        return {
            "window": window,
            "window_days": window_days,
            "total": total,
            "offset": offset,
            "limit": limit,
            "cities": config.ROYAL_CITIES,
            "rows": page,
        }

    # -- flip finder --------------------------------------------------------

    def flips(
        self,
        window: str = "week",
        min_profit: int | None = None,
        min_bm_volume: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        sort: str = "profit_pct",
        limit: int = 200,
    ) -> dict:
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_bm_volume = (
            config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        )
        now = datetime.now(timezone.utc)
        window_days = config.STAT_WINDOWS.get(window, 7)
        agg = self._aggregate(window_days)
        meta = self.storage.item_meta_map()
        current = self.storage.current_prices()

        # index current prices
        bm_now: dict[tuple, dict] = {}
        city_sell: dict[tuple, list[dict]] = {}
        for r in current:
            key = (r["item_id"], r["quality"])
            if r["city"] == config.BLACK_MARKET:
                if r["buy_price_max"] > 0:
                    bm_now[key] = r
            else:
                if r["sell_price_min"] > 0:
                    city_sell.setdefault(key, []).append(r)

        tax = config.SALES_TAX
        fee = config.SETUP_FEE
        search_l = (search or "").strip().lower()
        out: list[dict] = []

        for key, bm in bm_now.items():
            item_id, q = key
            m = meta.get(item_id)
            if not m:
                continue
            if category and m["category"] != category:
                continue
            if tier and m["tier"] != tier:
                continue
            if quality and q != quality:
                continue

            bm_age = _age_hours(bm["buy_price_max_date"], now)
            if bm_age is None or bm_age > config.FLIP_MAX_AGE_HOURS:
                continue

            # cheapest fresh city sell order
            best = None
            for r in city_sell.get(key, []):
                age = _age_hours(r["sell_price_min_date"], now)
                if age is None or age > config.FLIP_MAX_AGE_HOURS:
                    continue
                if best is None or r["sell_price_min"] < best["sell_price_min"]:
                    best = r
                    best_age = age
            if best is None:
                continue

            cost = best["sell_price_min"] * (1.0 + fee)
            if cost <= 0:
                continue
            bm_price_now = bm["buy_price_max"]
            revenue_now = bm_price_now * (1.0 - tax)
            profit_now = revenue_now - cost
            profit_pct_now = profit_now / cost * 100.0

            # weekly/window reference from history (volume-weighted)
            bm_hist = agg.get((item_id, config.BLACK_MARKET, q))
            bm_ref_price = bm_hist["vwap"] if bm_hist else 0
            bm_daily = bm_hist["daily"] if bm_hist else 0.0
            revenue_ref = bm_ref_price * (1.0 - tax)
            profit_ref = revenue_ref - cost
            profit_pct_ref = (profit_ref / cost * 100.0) if cost else 0.0

            if profit_now < min_profit:
                continue
            if bm_daily < min_bm_volume:
                continue

            display = (m["name_ru"] or m["name_en"])
            if m["enchant"]:
                display = f"{display} .{m['enchant']}"
            if search_l and search_l not in display.lower() and search_l not in item_id.lower():
                continue

            # estimated time for the BM to absorb one unit, from its daily throughput
            est_hours = round(24.0 / bm_daily, 2) if bm_daily > 0 else None

            # reliability score 0..100
            fresh = _clamp(1.0 - (max(bm_age, best_age) / config.FLIP_MAX_AGE_HOURS))
            liquidity = _clamp(bm_daily / _LIQUIDITY_REF)
            stability = _clamp(profit_ref / profit_now) if profit_now > 0 else 0.0
            score = round(100.0 * (0.40 * fresh + 0.30 * liquidity + 0.30 * stability))

            out.append(
                {
                    "item_id": item_id,
                    "name": display,
                    "tier": m["tier"],
                    "tier_label": f"T{m['tier']}",
                    "enchant": m["enchant"],
                    "quality": q,
                    "quality_label": config.QUALITY_NAMES.get(q, str(q)),
                    "category": m["category"],
                    "category_label": config.CATEGORY_NAMES.get(m["category"], m["category"]),
                    "buy_city": best["city"],
                    "buy_price": best["sell_price_min"],
                    "buy_age_h": round(best_age, 1),
                    "bm_buy_now": bm_price_now,
                    "bm_age_h": round(bm_age, 1),
                    "bm_avg_window": bm_ref_price,
                    "bm_daily_volume": round(bm_daily, 1),
                    "est_sell_hours": est_hours,
                    "profit": round(profit_now),
                    "profit_pct": round(profit_pct_now, 1),
                    "profit_window": round(profit_ref),
                    "profit_pct_window": round(profit_pct_ref, 1),
                    "reliability": score,
                }
            )

        sort_keys = {
            "profit_pct": lambda r: r["profit_pct"],
            "profit": lambda r: r["profit"],
            "reliability": lambda r: r["reliability"],
            "bm_volume": lambda r: r["bm_daily_volume"],
        }
        keyfn = sort_keys.get(sort, sort_keys["profit_pct"])
        out.sort(key=keyfn, reverse=True)

        return {
            "window": window,
            "window_days": window_days,
            "sales_tax": tax,
            "min_profit": min_profit,
            "min_bm_volume": min_bm_volume,
            "total": len(out),
            "rows": out[:limit],
        }
