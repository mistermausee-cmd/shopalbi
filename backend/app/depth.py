"""Order-book depth analytics.

The AODP public API only publishes the single best sell price and best buy
price per city/quality. The capture agent (tools/depth_capture) feeds us the
*full* order book instead — every price level with its quantity — so here we
can answer the questions min/max simply cannot:

  * how many units are actually available at or under a given price,
  * the volume-weighted price of buying the first N units (real slippage),
  * and, for a flip, exactly how many units you can carry city -> Black Market
    at a genuine profit, walking both books level by level instead of trusting
    one price point.

Everything in this module is pure computation over rows returned by
``storage.py``; nothing here does I/O.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config
from .storage import Storage


def fresh_cutoff_iso(max_age_hours: float | None = None) -> str:
    """ISO timestamp before which captured books are considered stale."""
    hours = config.DEPTH_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# --------------------------------------------------------------------------
# level aggregation
# --------------------------------------------------------------------------

def aggregate_levels(orders: list[dict], auction_type: str) -> list[dict]:
    """Collapse individual orders into price levels with total quantity.

    Offers (sell orders) are returned cheapest-first — that is the order you
    consume them in when buying. Requests (buy orders) are returned
    highest-first — the order you consume them in when selling. Each level
    carries a running cumulative quantity so the frontend can draw a depth
    curve without extra work.
    """
    by_price: dict[int, int] = defaultdict(int)
    latest_capture: str | None = None
    for o in orders:
        by_price[int(o["unit_price"])] += int(o["amount"])
        cap = o.get("captured_at")
        if cap and (latest_capture is None or cap > latest_capture):
            latest_capture = cap

    ascending = auction_type.lower() == "offer"
    prices = sorted(by_price.keys(), reverse=not ascending)

    levels: list[dict] = []
    cumulative = 0
    for p in prices:
        qty = by_price[p]
        cumulative += qty
        levels.append({"price": p, "amount": qty, "cumulative": cumulative})
    return levels


def book_summary(levels: list[dict]) -> dict:
    """Best price, total units and total silver value for a side of the book."""
    if not levels:
        return {"levels": 0, "total_units": 0, "best_price": None, "total_value": 0}
    total_units = levels[-1]["cumulative"]
    total_value = sum(l["price"] * l["amount"] for l in levels)
    return {
        "levels": len(levels),
        "total_units": total_units,
        "best_price": levels[0]["price"],
        "total_value": total_value,
    }


def units_within(levels: list[dict], best_price: int, pct: float) -> int:
    """Units available within `pct` of the best price on this side.

    For offers (ascending) that means priced <= best*(1+pct); for requests
    (descending) priced >= best*(1-pct). We infer the side from the ordering
    of the first two levels.
    """
    if not levels:
        return 0
    ascending = len(levels) < 2 or levels[1]["price"] >= levels[0]["price"]
    if ascending:
        ceil = best_price * (1.0 + pct)
        return sum(l["amount"] for l in levels if l["price"] <= ceil)
    floor = best_price * (1.0 - pct)
    return sum(l["amount"] for l in levels if l["price"] >= floor)


# --------------------------------------------------------------------------
# walking the book
# --------------------------------------------------------------------------

def cost_to_buy(levels: list[dict], units: int) -> dict:
    """Walk an ascending offer book buying `units`. Returns fill + VWAP."""
    remaining = units
    spent = 0
    filled = 0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl["amount"])
        spent += take * lvl["price"]
        filled += take
        remaining -= take
    vwap = (spent / filled) if filled else 0.0
    return {
        "requested": units,
        "filled": filled,
        "total_cost": spent,
        "vwap": round(vwap, 2),
        "fully_filled": filled >= units,
    }


def revenue_to_sell(levels: list[dict], units: int, tax: float) -> dict:
    """Walk a descending request book selling `units` into it, net of tax."""
    remaining = units
    gross = 0
    filled = 0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl["amount"])
        gross += take * lvl["price"]
        filled += take
        remaining -= take
    net = gross * (1.0 - tax)
    vwap = (gross / filled) if filled else 0.0
    return {
        "requested": units,
        "filled": filled,
        "gross": gross,
        "net": round(net),
        "vwap": round(vwap, 2),
        "fully_filled": filled >= units,
    }


# --------------------------------------------------------------------------
# depth-aware flip fill
# --------------------------------------------------------------------------

def flip_fill(offer_levels: list[dict], request_levels: list[dict], tax: float) -> dict:
    """Match a city sell book against a Black-Market buy book, level by level.

    You buy from the cheapest offers and sell into the highest buy orders. A
    pairing is worth doing while the after-tax buy price still clears the
    purchase price. This yields the total quantity that flips at a genuine
    profit, the blended cost/revenue over that quantity, and the marginal
    (last-unit) economics — the honest ceiling that a single min/max point can
    wildly overstate on a thin book.
    """
    offers = [dict(l) for l in offer_levels]      # cheapest first
    requests = [dict(l) for l in request_levels]  # highest buy first
    i = j = 0
    units = 0
    total_cost = 0
    total_gross = 0
    last_buy_price = None
    last_sell_price = None

    while i < len(offers) and j < len(requests):
        buy_price = offers[i]["price"]
        sell_price = requests[j]["price"]
        if sell_price * (1.0 - tax) <= buy_price:
            break  # marginal unit no longer profitable
        take = min(offers[i]["amount"], requests[j]["amount"])
        if take <= 0:
            break
        units += take
        total_cost += take * buy_price
        total_gross += take * sell_price
        last_buy_price = buy_price
        last_sell_price = sell_price
        offers[i]["amount"] -= take
        requests[j]["amount"] -= take
        if offers[i]["amount"] == 0:
            i += 1
        if requests[j]["amount"] == 0:
            j += 1

    total_revenue = total_gross * (1.0 - tax)
    profit = total_revenue - total_cost
    marginal_profit = None
    if last_buy_price is not None:
        marginal_profit = round(last_sell_price * (1.0 - tax) - last_buy_price)

    return {
        "units": units,
        "total_cost": round(total_cost),
        "total_revenue": round(total_revenue),
        "profit": round(profit),
        "profit_pct": round(profit / total_cost * 100.0, 1) if total_cost else 0.0,
        "avg_buy": round(total_cost / units, 2) if units else 0.0,
        "avg_sell": round(total_gross / units, 2) if units else 0.0,
        "marginal_profit": marginal_profit,
    }


# --------------------------------------------------------------------------
# high level views over storage
# --------------------------------------------------------------------------

def order_book_view(
    storage: Storage,
    item_id: str,
    quality: int,
    city: str,
    auction_type: str,
    max_age_hours: float | None = None,
) -> dict:
    """Full aggregated book for one item variant at one city."""
    cutoff = fresh_cutoff_iso(max_age_hours)
    rows = storage.order_book_rows(
        item_id=item_id, quality=quality, auction_type=auction_type,
        fresh_since=cutoff, city=city,
    )
    levels = aggregate_levels(rows, auction_type)
    summary = book_summary(levels)
    captured = max((r.get("captured_at") for r in rows), default=None)
    within5 = units_within(levels, summary["best_price"], 0.05) if levels else 0
    return {
        "item_id": item_id,
        "quality": quality,
        "city": city,
        "auction_type": auction_type,
        "captured_at": captured,
        "summary": {**summary, "units_within_5pct": within5},
        "levels": levels,
    }


def depth_flip_view(
    storage: Storage,
    item_id: str,
    quality: int,
    tax: float | None = None,
    max_age_hours: float | None = None,
) -> dict:
    """For one item variant: the best depth-aware flip from each city that has
    sell orders into the Black Market buy book, ranked by fillable profit.
    """
    tax = config.SALES_TAX if tax is None else tax
    cutoff = fresh_cutoff_iso(max_age_hours)
    rows = storage.order_books_for_item(item_id, quality, fresh_since=cutoff)

    # split by (city, auction_type)
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[(r["city"], r["auction_type"])].append(r)

    bm_requests = grouped.get((config.BLACK_MARKET, "request"), [])
    bm_levels = aggregate_levels(bm_requests, "request")
    bm_summary = book_summary(bm_levels)

    results: list[dict] = []
    if bm_levels:
        for (city, atype), orders in grouped.items():
            if atype != "offer" or city == config.BLACK_MARKET:
                continue
            offer_levels = aggregate_levels(orders, "offer")
            if not offer_levels:
                continue
            fill = flip_fill(offer_levels, bm_levels, tax)
            if fill["units"] <= 0:
                continue
            results.append({
                "buy_city": city,
                "buy_best_price": offer_levels[0]["price"],
                "buy_units_available": offer_levels[-1]["cumulative"],
                **fill,
            })
        results.sort(key=lambda r: r["profit"], reverse=True)

    return {
        "item_id": item_id,
        "quality": quality,
        "sales_tax": tax,
        "black_market": {
            "best_buy_price": bm_summary["best_price"],
            "total_buy_units": bm_summary["total_units"],
            "levels": len(bm_levels),
        },
        "flips": results,
    }
