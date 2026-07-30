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
import re
import threading
import time
from datetime import datetime, timezone

from . import config
from .aodp_client import AodpClient
from .catalog import SHOP_CATEGORY_NAMES, tier_label
from .storage import Storage

log = logging.getLogger("shopalbi.sets")

# --------------------------------------------------------------------------
# Item power equivalence
# --------------------------------------------------------------------------
#
# Verified against https://wiki.albiononline.com/wiki/Enchantment ("Each
# enchantment level adds 100 IP") and the per-tier tables on the gear pages
# (Cape: T2 = 500, T3 = 600, T4 = 700 -> +100 per tier):
#
#     item power = 300 + (tier + enchant) * 100
#
# So anything with the same `tier + enchant` has the same item power and is
# interchangeable in a build:
#
#     T8.0  =  T7.1  =  T6.2  =  T5.3  =  T4.4   -> 1100 IP
#
# That matters for shopping because the prices of equivalent variants differ
# wildly — an enchanted lower tier is often far cheaper than the plain high tier,
# and it is the same item power on your character.
#
# Consumables and mounts are deliberately excluded: enchanted food is *better*
# food rather than an equal-power alternative, and mounts have no enchant ladder.
_EQUIV_CATEGORIES = {"weapon", "armor", "offhand", "cape", "bag", "gatherer", "tool"}

_TIER_PREFIX_RE = re.compile(r"^T(\d)_")
_ENCH_SUFFIX_RE = re.compile(r"@(\d)$")


def item_power(tier: int, enchant: int) -> int:
    return 300 + (tier + enchant) * 100


def item_family(item_id: str) -> str | None:
    """`T8_ARMOR_PLATE_SET1` and `T5_ARMOR_PLATE_SET1@3` -> `ARMOR_PLATE_SET1`.

    The family is what makes two variants the *same item* rather than merely
    equal in power; without it a plate chest would be swapped for a cloth robe.
    """
    m = _TIER_PREFIX_RE.match(item_id)
    if not m:
        return None
    return _ENCH_SUFFIX_RE.sub("", item_id[m.end():])


def equivalent_ids(item_id: str, tier: int, enchant: int) -> list[str]:
    """Every tier/enchant combination of the same item with equal item power."""
    family = item_family(item_id)
    if family is None:
        return [item_id]
    target = tier + enchant
    out = []
    for t in config.TIERS:                       # T4..T8 by default
        e = target - t
        if 0 <= e <= 4:
            out.append(f"T{t}_{family}" + (f"@{e}" if e else ""))
    return out or [item_id]

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

    def _line_variants(self, ln: dict, meta: dict, use_equivalents: bool) -> list[dict]:
        """Candidate items that satisfy one set line, cheapest-first at price time.

        With equivalence on, an equipment line accepts any tier/enchant with the
        same item power, so the city that has T6.2 cheap can still complete a set
        built around T8.0.
        """
        m = meta.get(ln["item_id"])
        cat = m["category"] if m else ""
        if not use_equivalents or cat not in _EQUIV_CATEGORIES or not m:
            return [{"item_id": ln["item_id"], "tier": m["tier"] if m else 0,
                     "enchant": m["enchant"] if m else 0}]
        ids = equivalent_ids(ln["item_id"], m["tier"], m["enchant"])
        known = self.storage.shop_items_by_ids(ids)
        # The id family is NOT sufficient on its own: the game reuses an id
        # pattern for named artifacts at the top tier. `T8_2H_AXE` is "The Hand of
        # Khor", not a T8 Greataxe, so treating it as equivalent to `T7_2H_AXE@1`
        # would swap a unique weapon for an ordinary one with different abilities.
        # Requiring an identical display name (tier word already stripped) is a
        # data-driven guard that catches every such case.
        want_name = m["name_disp"]
        out = [{"item_id": i, "tier": known[i]["tier"], "enchant": known[i]["enchant"]}
               for i in ids if i in known and known[i]["name_disp"] == want_name]
        return out or [{"item_id": ln["item_id"], "tier": m["tier"], "enchant": m["enchant"]}]

    def price_set(self, set_id: int, allow_higher_quality: bool = True,
                  use_equivalents: bool = True) -> dict | None:
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

        meta = self.storage.shop_items_by_ids([ln["item_id"] for ln in lines])

        # Expand each line into its acceptable variants, then price them all in a
        # single request. Up to 5 equivalents per line still fits one API call.
        variants = [self._line_variants(ln, meta, use_equivalents) for ln in lines]
        item_ids = sorted({v["item_id"] for vs in variants for v in vs})
        # Ask for the requested quality and everything above it, so a substitute
        # can be found in one request rather than a second round trip.
        qualities = sorted({q for ln in lines
                            for q in range(int(ln["quality"] or 1),
                                           max(config.QUALITIES) + 1 if allow_higher_quality
                                           else int(ln["quality"] or 1) + 1)})
        quotes = self._quotes(item_ids, qualities or [1])
        var_meta = self.storage.shop_items_by_ids(item_ids)
        per_city: list[dict] = []
        for city in SHOP_CITIES:
            city_lines = []
            total = 0
            missing = 0
            for li, ln in enumerate(lines):
                want_q = int(ln["quality"] or 1)
                # Cheapest equal-power variant available IN THIS CITY. Choosing per
                # city (not globally) is the whole point: the answer has to be
                # buyable in one place.
                found = None
                for v in variants[li]:
                    for q in range(want_q, max(config.QUALITIES) + 1):
                        hit = quotes.get((v["item_id"], city, q))
                        if hit and (found is None or hit[0] < found[1]):
                            found = (q, hit[0], hit[1], v)
                        if hit or not allow_higher_quality:
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
                    q, price, date, v = found
                    vm = var_meta.get(v["item_id"])
                    swapped = v["item_id"] != ln["item_id"]
                    entry.update({
                        "available": True,
                        "quality": q,
                        "quality_label": config.QUALITY_NAMES.get(q, str(q)),
                        "substituted": q != want_q,
                        "unit_price": price,
                        "line_total": price * qty,
                        "price_date": date,
                        # What to actually put in the basket, which may be a
                        # different tier/enchant of the same item at equal power.
                        "buy_item_id": v["item_id"],
                        "buy_name": vm["name_disp"] if vm else v["item_id"],
                        "buy_tier_ench": tier_label(v["tier"], v["enchant"]),
                        "equivalent": swapped,
                        "item_power": item_power(v["tier"], v["enchant"]),
                    })
                    total += price * qty
                else:
                    entry.update({
                        "available": False, "quality": None, "quality_label": None,
                        "substituted": False, "unit_price": 0, "line_total": 0,
                        "price_date": None, "buy_item_id": None, "buy_name": None,
                        "buy_tier_ench": None, "equivalent": False,
                        "item_power": item_power(m["tier"], m["enchant"]) if m and m["tier"] else None,
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
            "use_equivalents": use_equivalents,
            "variants_considered": sum(len(v) for v in variants),
        }
