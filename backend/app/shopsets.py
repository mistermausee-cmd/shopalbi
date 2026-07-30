"""Gear sets: "where do I buy my whole loadout most cheaply".

Different question from the rest of the app. The Black Market side is about
selling; this is about *shopping* — a build you run with includes a mount,
potions, food, maybe a fishing rod and gatherer clothing, none of which the
Black Market buys. So it uses its own wider catalog (`shop_items`) and its own
price fetch.

Two things matter for the answer to be useful rather than merely arithmetic:

  * One city has to cover the WHOLE set. A city that is cheapest on eight items
    but does not stock the ninth means a second trip, so completeness is ranked
    before price and missing lines are named explicitly.
  * Prices are fetched on demand for just the items in the set, so this feature
    costs one or two API calls regardless of how big the catalog is.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import config
from .aodp_client import AodpClient
from .catalog import SHOP_CATEGORY_NAMES, tier_label
from .storage import Storage

log = logging.getLogger("shopalbi.sets")

# Cities worth shopping in. Unlike the flip engine this includes Brecilien and
# Caerleon: the exclusion there is about hauling to the Black Market, which has
# nothing to do with buying a set you are going to wear.
SHOP_CITIES = list(config.ROYAL_CITIES)

_PRICE_TTL = 90.0


class SetPricer:
    def __init__(self, storage: Storage, client: AodpClient | None = None):
        self.storage = storage
        self.client = client or AodpClient()
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, dict]] = {}

    # -- catalog ------------------------------------------------------------

    def ensure_catalog(self, force: bool = False) -> int:
        if not force and self.storage.shop_item_count() > 0:
            return self.storage.shop_item_count()
        from .catalog import build_shop_items
        n = self.storage.replace_shop_items(build_shop_items(force=force))
        self.storage.set_meta("shop_catalog_count", str(n))
        log.info("shopping catalog stored: %d items", n)
        return n

    def search(self, query: str | None, category: str | None = None,
               tier: int | None = None, limit: int = 50) -> dict:
        rows = self.storage.search_shop_items(query, category, tier, limit)
        return {
            "total": len(rows),
            "categories": [{"id": k, "label": v} for k, v in SHOP_CATEGORY_NAMES.items()],
            "rows": [{
                "item_id": r["item_id"],
                "name": r["name_disp"],
                "tier": r["tier"],
                "enchant": r["enchant"],
                "tier_ench": tier_label(r["tier"], r["enchant"]) if r["tier"] else "—",
                "category": r["category"],
                "category_label": SHOP_CATEGORY_NAMES.get(r["category"], r["category"]),
            } for r in rows],
        }

    # -- pricing ------------------------------------------------------------

    def _quotes(self, item_ids: list[str], qualities: list[int]) -> dict:
        """(item_id, city, quality) -> (price, date), fetched live and cached briefly."""
        key = (tuple(sorted(set(item_ids))), tuple(sorted(set(qualities))))
        with self._lock:
            hit = self._cache.get(key)
            if hit and (time.monotonic() - hit[0]) < _PRICE_TTL:
                return hit[1]
        rows = self.client.fetch_prices(sorted(set(item_ids)), SHOP_CITIES, sorted(set(qualities)))
        out: dict[tuple, tuple[int, str]] = {}
        for r in rows:
            price = int(r.get("sell_price_min") or 0)
            if price <= 0:
                continue
            out[(r.get("item_id"), r.get("city"), int(r.get("quality") or 0))] = (
                price, r.get("sell_price_min_date") or "")
        with self._lock:
            self._cache[key] = (time.monotonic(), out)
            if len(self._cache) > 16:
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]), None)
        return out

    def price_set(self, set_id: int, allow_higher_quality: bool = True) -> dict | None:
        """Cost the set in every city and rank the cities.

        `allow_higher_quality`: a buy order is not involved here — you are picking
        an item off the shelf — but if the exact quality is absent while a better
        one is on sale, that IS a usable substitute, so it is offered (flagged)
        instead of declaring the item unavailable.
        """
        s = self.storage.get_set(set_id)
        if not s:
            return None
        lines = [ln for ln in s["lines"] if ln["item_id"]]
        if not lines:
            return {**s, "cities": [], "priced_at": None, "lines_priced": []}

        item_ids = [ln["item_id"] for ln in lines]
        # Ask for the requested quality and everything above it, so a substitute
        # can be found in one request rather than a second round trip.
        qualities = sorted({q for ln in lines
                            for q in range(int(ln["quality"] or 1),
                                           max(config.QUALITIES) + 1 if allow_higher_quality
                                           else int(ln["quality"] or 1) + 1)})
        quotes = self._quotes(item_ids, qualities or [1])

        meta = self.storage.shop_items_by_ids(item_ids)
        per_city: list[dict] = []
        for city in SHOP_CITIES:
            city_lines = []
            total = 0
            missing = 0
            for ln in lines:
                want_q = int(ln["quality"] or 1)
                found = None
                for q in range(want_q, max(config.QUALITIES) + 1):
                    hit = quotes.get((ln["item_id"], city, q))
                    if hit:
                        found = (q, hit[0], hit[1])
                        break
                    if not allow_higher_quality:
                        break
                m = meta.get(ln["item_id"])
                qty = max(1, int(ln["qty"] or 1))
                entry = {
                    "item_id": ln["item_id"],
                    # A line can outlive the catalog entry it points at (saved
                    # before validation existed, or the item was removed from the
                    # game). Say so plainly instead of printing a raw id that
                    # looks like a market problem.
                    "unknown": m is None,
                    "name": (m["name_disp"] if m else ln["item_id"]),
                    "tier_ench": tier_label(m["tier"], m["enchant"]) if m and m["tier"] else "—",
                    "category_label": (SHOP_CATEGORY_NAMES.get(m["category"], m["category"])
                                       if m else "нет в каталоге"),
                    "want_quality": want_q,
                    "want_quality_label": config.QUALITY_NAMES.get(want_q, str(want_q)),
                    "qty": qty,
                }
                if found:
                    q, price, date = found
                    entry.update({
                        "available": True,
                        "quality": q,
                        "quality_label": config.QUALITY_NAMES.get(q, str(q)),
                        "substituted": q != want_q,
                        "unit_price": price,
                        "line_total": price * qty,
                        "price_date": date,
                    })
                    total += price * qty
                else:
                    entry.update({
                        "available": False, "quality": None, "quality_label": None,
                        "substituted": False, "unit_price": 0, "line_total": 0,
                        "price_date": None,
                    })
                    missing += 1
                city_lines.append(entry)
            per_city.append({
                "city": city,
                "complete": missing == 0,
                "missing": missing,
                "missing_items": [e["name"] for e in city_lines if not e["available"]],
                "total": total,
                "items_priced": len(city_lines) - missing,
                "lines": city_lines,
            })

        # Completeness first, then price: a cheaper city that cannot finish the
        # set is not actually cheaper, it is a second trip.
        per_city.sort(key=lambda c: (not c["complete"], c["missing"], c["total"] or float("inf")))
        best = per_city[0] if per_city else None

        # Cheapest possible if you were willing to shop in several cities — the
        # honest reference point for what one-stop convenience costs you.
        split_total = 0
        split_ok = True
        for i, ln in enumerate(lines):
            options = [c["lines"][i]["line_total"] for c in per_city if c["lines"][i]["available"]]
            if options:
                split_total += min(options)
            else:
                split_ok = False

        return {
            "set_id": s["set_id"], "name": s["name"], "note": s["note"],
            "lines": s["lines"],
            "priced_at": datetime.now(timezone.utc).isoformat(),
            "cities": [{k: v for k, v in c.items() if k != "lines"} for c in per_city],
            "best": best,
            "split_total": split_total if split_ok else None,
            "one_stop_premium": (
                (best["total"] - split_total) if (best and best["complete"] and split_ok) else None
            ),
            "allow_higher_quality": allow_higher_quality,
        }
