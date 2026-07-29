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
import re
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


# Localized item names end with the tier word in parentheses, e.g.
# "Куртка убийцы (магистр)". That duplicates the tier badge and confuses the
# reader, so we strip it and show a clean "Т7.3" tier.enchant label instead.
_PAREN_TAIL_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _clean_name(name: str) -> str:
    return _PAREN_TAIL_RE.sub("", name or "").strip()


# Sortable columns for the flip table -> value accessor. Any of these can be
# sorted ascending or descending from the UI.
_FLIP_SORT_KEYS = {
    "opportunity": lambda r: r["opportunity"],
    "profit": lambda r: r["profit"],
    "profit_pct": lambda r: r["profit_pct"],
    "profit_window": lambda r: r["profit_window"],
    "profit_pct_window": lambda r: r["profit_pct_window"],
    "reliability": lambda r: r["reliability"],
    "bm_volume": lambda r: r["bm_daily_volume"],
    "buy_price": lambda r: r["buy_price"],
    "bm_buy_now": lambda r: r["bm_buy_now"],
    "est": lambda r: r["est_sell_hours"] if r["est_sell_hours"] is not None else float("inf"),
    "buy_city": lambda r: r["buy_city"],
    "name": lambda r: r["name"].lower(),
    "tier": lambda r: (r["tier"], r["enchant"]),
}


def _sort_rows(rows: list[dict], sort: str, direction: str, keymap: dict) -> None:
    keyfn = keymap.get(sort) or next(iter(keymap.values()))
    rows.sort(key=keyfn, reverse=(direction != "asc"))


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
        direction: str = "desc",
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
            display = _clean_name(m["name_ru"] or m["name_en"])
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
                    "tier_ench": f"Т{m['tier']}.{m['enchant']}",
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

        if sort and sort.startswith("city:"):
            _c = sort[5:]
            keyfn = lambda r: r["cities"].get(_c, {}).get("avg", 0)
        else:
            _stat_keys = {
                "bm_volume": lambda r: r["bm_volume"],
                "bm_avg": lambda r: r["bm_avg"],
                "cheapest": lambda r: r["cheapest_city_avg"],
                "name": lambda r: r["name"].lower(),
                "tier": lambda r: (r["tier"], r["enchant"]),
            }
            keyfn = _stat_keys.get(sort, _stat_keys["bm_volume"])
        rows.sort(key=keyfn, reverse=(direction != "asc"))

        total = len(rows)
        page = rows[offset: offset + limit]
        return {
            "window": window,
            "window_days": window_days,
            "total": total,
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "direction": direction,
            "cities": config.ROYAL_CITIES,
            "rows": page,
        }

    # -- flip finder --------------------------------------------------------

    def _flip_candidates(
        self,
        window: str,
        min_profit: int,
        min_bm_volume: float,
        category: str | None,
        tier: int | None,
        quality: int | None,
        search: str | None,
    ) -> list[dict]:
        """Every profitable (item, quality, source city -> Black Market) flip,
        one row per source city. Prices are fetched once here; both the flip
        table and the city ranking are derived from this list."""
        now = datetime.now(timezone.utc)
        window_days = config.STAT_WINDOWS.get(window, 7)
        agg = self._aggregate(window_days)
        meta = self.storage.item_meta_map()
        current = self.storage.current_prices()

        bm_now: dict[tuple, dict] = {}
        city_sell: dict[tuple, list[dict]] = {}
        for r in current:
            key = (r["item_id"], r["quality"])
            if r["city"] == config.BLACK_MARKET:
                if r["buy_price_max"] > 0:
                    bm_now[key] = r
            elif r["sell_price_min"] > 0:
                city_sell.setdefault(key, []).append(r)

        tax = config.SALES_TAX
        fee = config.SETUP_FEE
        search_l = (search or "").strip().lower()
        cands: list[dict] = []

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

            bm_hist = agg.get((item_id, config.BLACK_MARKET, q))
            bm_ref_price = bm_hist["vwap"] if bm_hist else 0
            bm_daily = bm_hist["daily"] if bm_hist else 0.0
            if bm_daily < min_bm_volume:
                continue

            display = _clean_name(m["name_ru"] or m["name_en"])
            tier_ench = f"Т{m['tier']}.{m['enchant']}"
            if search_l and search_l not in display.lower() and search_l not in item_id.lower():
                continue

            net = 1.0 - tax - fee  # Black Market sale net of sales tax + setup fee
            bm_price_now = bm["buy_price_max"]
            revenue_now = bm_price_now * net
            revenue_ref = bm_ref_price * net
            est_hours = round(24.0 / bm_daily, 2) if bm_daily > 0 else None
            liquidity = _clamp(bm_daily / _LIQUIDITY_REF)

            for r in city_sell.get(key, []):
                if r["city"] not in config.BUY_CITIES:
                    continue
                age = _age_hours(r["sell_price_min_date"], now)
                if age is None or age > config.FLIP_MAX_AGE_HOURS:
                    continue
                cost = r["sell_price_min"]   # buying from an existing sell order has no fee
                if cost <= 0:
                    continue
                profit_now = revenue_now - cost
                if profit_now < min_profit:
                    continue
                profit_pct_now = profit_now / cost * 100.0
                profit_ref = revenue_ref - cost
                profit_pct_ref = profit_ref / cost * 100.0

                fresh = _clamp(1.0 - (max(bm_age, age) / config.FLIP_MAX_AGE_HOURS))
                stability = _clamp(profit_ref / profit_now) if profit_now > 0 else 0.0
                score = round(100.0 * (0.40 * fresh + 0.30 * liquidity + 0.30 * stability))

                cands.append(
                    {
                        "item_id": item_id,
                        "name": display,
                        "tier": m["tier"],
                        "tier_label": f"T{m['tier']}",
                        "tier_ench": tier_ench,
                        "enchant": m["enchant"],
                        "quality": q,
                        "quality_label": config.QUALITY_NAMES.get(q, str(q)),
                        "category": m["category"],
                        "category_label": config.CATEGORY_NAMES.get(m["category"], m["category"]),
                        "buy_city": r["city"],
                        "buy_price": r["sell_price_min"],
                        "buy_age_h": round(age, 1),
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
                        # composite "worth it" score: expected profit discounted
                        # by how trustworthy/liquid the flip is (profit x score).
                        "opportunity": round(profit_now * score / 100.0),
                    }
                )
        return cands

    def flips(
        self,
        window: str = "week",
        min_profit: int | None = None,
        min_bm_volume: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        buy_city: str | None = None,
        sort: str = "opportunity",
        direction: str = "desc",
        limit: int = 200,
    ) -> dict:
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_bm_volume = (
            config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        )
        cands = self._flip_candidates(
            window, min_profit, min_bm_volume, category, tier, quality, search
        )
        if buy_city:
            rows = [c for c in cands if c["buy_city"] == buy_city]
        else:
            # collapse to the single best source city per (item, quality)
            best: dict[tuple, dict] = {}
            for c in cands:
                k = (c["item_id"], c["quality"])
                cur = best.get(k)
                if cur is None or c["opportunity"] > cur["opportunity"]:
                    best[k] = c
            rows = list(best.values())

        _sort_rows(rows, sort, direction, _FLIP_SORT_KEYS)

        return {
            "window": window,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "sales_tax": config.SALES_TAX,
            "min_profit": min_profit,
            "min_bm_volume": min_bm_volume,
            "buy_city": buy_city,
            "sort": sort,
            "direction": direction,
            "total": len(rows),
            "rows": rows[:limit],
        }

    def city_ranking(
        self,
        window: str = "week",
        min_profit: int | None = None,
        min_bm_volume: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        top_n: int = 100,
    ) -> dict:
        """Rank royal cities as a buy base. Each city's best `top_n` flips
        (by opportunity = profit x reliability) are summed, so the city that
        offers the most realizable profit across items ranks first."""
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_bm_volume = (
            config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        )
        cands = self._flip_candidates(
            window, min_profit, min_bm_volume, category, tier, quality, search
        )
        from collections import defaultdict

        buckets: dict[str, list[dict]] = defaultdict(list)
        for c in cands:
            buckets[c["buy_city"]].append(c)

        out: list[dict] = []
        for city, lst in buckets.items():
            lst.sort(key=lambda x: x["opportunity"], reverse=True)
            top = lst[:top_n]
            n = len(top)
            best = top[0] if top else None
            out.append(
                {
                    "city": city,
                    "flips_count": len(lst),
                    "score": sum(x["opportunity"] for x in top),
                    "top_profit_sum": sum(x["profit"] for x in top),
                    "avg_profit_pct": round(sum(x["profit_pct"] for x in top) / n, 1) if n else 0.0,
                    "avg_reliability": round(sum(x["reliability"] for x in top) / n) if n else 0,
                    "best_item": best["name"] if best else "",
                    "best_profit": best["profit"] if best else 0,
                    "best_profit_pct": best["profit_pct"] if best else 0.0,
                }
            )
        out.sort(key=lambda r: r["score"], reverse=True)
        return {
            "window": window,
            "window_days": config.STAT_WINDOWS.get(window, 7),
            "top_n": top_n,
            "total": len(out),
            "rows": out,
        }

    # -- budget recommender -------------------------------------------------

    def recommend(
        self,
        budget: int,
        city: str | None = None,
        window: str = "week",
        min_profit: int | None = None,
        min_bm_volume: float | None = None,
        category: str | None = None,
        tier: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        slippage: float | None = None,
        capture: float | None = None,
    ) -> dict:
        """Given a budget, decide which city to shop in and exactly what to buy
        (item + quantity) to maximise expected profit, then re-sell on the Black
        Market. Quantities are bounded by the Black Market's daily throughput and
        an average buy price that rises with quantity (slippage), because the API
        exposes only the single lowest sell price, not full order-book depth."""
        min_profit = config.DEFAULT_MIN_PROFIT if min_profit is None else min_profit
        min_bm_volume = (
            config.DEFAULT_MIN_BM_DAILY_VOLUME if min_bm_volume is None else min_bm_volume
        )
        slippage = config.RECOMMEND_SLIPPAGE if slippage is None else slippage
        capture = config.RECOMMEND_VOLUME_CAPTURE if capture is None else capture
        budget = max(0, int(budget or 0))

        cands = self._flip_candidates(
            window, min_profit, min_bm_volume, category, tier, quality, search
        )
        from collections import defaultdict

        by_city: dict[str, list[dict]] = defaultdict(list)
        for c in cands:
            by_city[c["buy_city"]].append(c)

        cities = [city] if city else list(by_city.keys())
        plans = [
            {**self._allocate_budget(by_city.get(cy, []), budget, slippage, capture), "city": cy}
            for cy in cities
        ]
        plans.sort(key=lambda p: p["expected_profit"], reverse=True)
        best = plans[0] if plans else None
        return {
            "window": window,
            "budget": budget,
            "sales_tax": config.SALES_TAX,
            "setup_fee": config.SETUP_FEE,
            "requested_city": city,
            "city": best["city"] if best else None,
            "best": best,
            "cities": [
                {k: p[k] for k in ("city", "expected_profit", "spent", "leftover", "roi_pct", "items_count")}
                for p in plans
            ],
        }

    @staticmethod
    def _allocate_budget(candidates: list[dict], budget: int, slippage: float, capture: float) -> dict:
        # Spend on the most silver-efficient flips first (profit per silver),
        # capped per item by how much the Black Market can realistically absorb.
        ranked = sorted(
            candidates,
            key=lambda c: (c["profit"] / c["buy_price"] if c["buy_price"] else 0.0, c["reliability"]),
            reverse=True,
        )
        net = 1.0 - config.SALES_TAX - config.SETUP_FEE
        remaining = float(budget)
        items: list[dict] = []
        spent = 0.0
        profit_total = 0.0

        for c in ranked:
            price = c["buy_price"]
            if price <= 0 or remaining < price:
                continue
            vol = max(1.0, c["bm_daily_volume"])
            vol_cap = max(1, int(round(vol * capture)))
            qty = min(vol_cap, int(remaining // price))
            if qty < 1:
                continue
            # average buy price rises with the share of daily volume you take
            avg_price = price * (1.0 + slippage * (qty / vol))
            if avg_price * qty > remaining:
                qty = int(remaining // avg_price)
                if qty < 1:
                    continue
            cost = avg_price * qty
            unit_profit = c["bm_buy_now"] * net - avg_price
            if unit_profit <= 0:
                continue
            total_profit = unit_profit * qty
            items.append(
                {
                    "item_id": c["item_id"],
                    "name": c["name"],
                    "tier_ench": c["tier_ench"],
                    "quality": c["quality"],
                    "quality_label": c["quality_label"],
                    "category_label": c["category_label"],
                    "qty": qty,
                    "unit_price": price,
                    "avg_price": round(avg_price),
                    "total_cost": round(cost),
                    "bm_buy_now": c["bm_buy_now"],
                    "unit_profit": round(unit_profit),
                    "total_profit": round(total_profit),
                    "profit_pct": round(unit_profit / avg_price * 100, 1) if avg_price else 0.0,
                    "bm_daily_volume": c["bm_daily_volume"],
                    "reliability": c["reliability"],
                }
            )
            spent += cost
            remaining -= cost
            profit_total += total_profit

        items.sort(key=lambda x: x["total_profit"], reverse=True)
        return {
            "expected_profit": round(profit_total),
            "spent": round(spent),
            "leftover": round(budget - spent),
            "roi_pct": round(profit_total / spent * 100, 1) if spent else 0.0,
            "items_count": len(items),
            "items": items,
        }
