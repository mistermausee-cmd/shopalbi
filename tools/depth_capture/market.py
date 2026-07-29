"""Turn decoded Photon messages into shopalbi order books.

The market responses (AuctionGetOffers / AuctionGetRequests and friends) carry
the order list as an array of JSON strings inside the message parameters. Each
JSON object is one live order:

    {"Id": 123, "ItemTypeId": "T5_BAG@1", "ItemGroupTypeId": "T5_BAG",
     "LocationId": 3005, "QualityLevel": 2, "EnchantmentLevel": 1,
     "UnitPriceSilver": 1234500, "Amount": 7, "AuctionType": "offer",
     "Expires": "2026-08-01T12:00:00"}

We do **not** key off operation codes — those numbers change between patches.
Instead we detect market data structurally: any parameter value that is (or
contains) JSON strings which parse into objects carrying the tell-tale order
fields. That makes the agent survive client updates untouched.

Prices in the raw stream are silver x 10000; we normalize to whole silver to
match what the backend and the AODP API already store.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from .locations import LocationMap

log = logging.getLogger("depth_capture.market")

# An object is an order if it has at least these keys.
_ORDER_KEYS = ("UnitPriceSilver", "Amount", "AuctionType")
_PRICE_DIVISOR = 10000


def _iter_json_strings(value):
    """Yield candidate JSON strings from an arbitrarily-nested parameter."""
    if value is None:
        return
    if isinstance(value, (bytes, bytearray)):
        try:
            yield value.decode("utf-8")
        except UnicodeDecodeError:
            return
        return
    if isinstance(value, str):
        s = value.lstrip()
        if s[:1] in ("{", "["):
            yield value
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_json_strings(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_json_strings(item)


def _coerce_orders(parsed):
    """A parsed JSON blob may be a single order or a list of them."""
    if isinstance(parsed, dict):
        if all(k in parsed for k in _ORDER_KEYS):
            return [parsed]
        return []
    if isinstance(parsed, list):
        return [o for o in parsed if isinstance(o, dict) and all(k in o for k in _ORDER_KEYS)]
    return []


def extract_orders(params: dict) -> list[dict]:
    """Pull every market order object out of a message's parameter table."""
    orders: list[dict] = []
    seen_ids: set = set()
    for value in params.values():
        for candidate in _iter_json_strings(value):
            try:
                parsed = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            for order in _coerce_orders(parsed):
                oid = order.get("Id")
                if oid is not None and oid in seen_ids:
                    continue
                if oid is not None:
                    seen_ids.add(oid)
                orders.append(order)
    return orders


def _normalize_item_id(order: dict) -> str | None:
    item_type = order.get("ItemTypeId") or order.get("ItemGroupTypeId")
    if not item_type or not isinstance(item_type, str):
        return None
    base = item_type.split("@", 1)[0]
    enchant = 0
    if "@" in item_type:
        try:
            enchant = int(item_type.split("@", 1)[1])
        except ValueError:
            enchant = 0
    # EnchantmentLevel field wins if the id itself carried none.
    try:
        field_ench = int(order.get("EnchantmentLevel") or 0)
    except (TypeError, ValueError):
        field_ench = 0
    enchant = enchant or field_ench
    return f"{base}@{enchant}" if enchant else base


def _auction_type(order: dict) -> str | None:
    raw = order.get("AuctionType")
    if isinstance(raw, str):
        v = raw.strip().lower()
        if "request" in v:
            return "request"
        if "offer" in v:
            return "offer"
    if isinstance(raw, int):
        # Fallback for enum-encoded types seen on some builds: 0=offer,1=request.
        return "request" if raw == 1 else "offer"
    return None


class MarketExtractor:
    """Stateful: converts message params into normalized, grouped order books."""

    def __init__(self, locations: LocationMap | None = None):
        self.locations = locations or LocationMap()
        self.stats = {"responses": 0, "orders": 0, "books": 0, "unmapped": 0}

    def books_from_params(self, params: dict) -> list[dict]:
        raw_orders = extract_orders(params)
        if not raw_orders:
            return []
        self.stats["responses"] += 1

        captured_at = datetime.now(timezone.utc).isoformat()
        # group into books keyed by (item_id, city, quality, auction_type)
        grouped: dict[tuple, dict] = {}
        for order in raw_orders:
            item_id = _normalize_item_id(order)
            atype = _auction_type(order)
            if not item_id or atype is None:
                continue
            try:
                order_id = int(order.get("Id") or 0)
                unit_price = int(order.get("UnitPriceSilver") or 0) // _PRICE_DIVISOR
                amount = int(order.get("Amount") or 0)
                quality = int(order.get("QualityLevel") or 1)
            except (TypeError, ValueError):
                continue
            if order_id <= 0 or unit_price <= 0 or amount <= 0:
                continue

            city, known = self.locations.resolve(order.get("LocationId"))
            if not known:
                self.stats["unmapped"] += 1

            key = (item_id, city, quality, atype)
            book = grouped.get(key)
            if book is None:
                book = {
                    "item_id": item_id,
                    "city": city,
                    "quality": quality,
                    "auction_type": atype,
                    "captured_at": captured_at,
                    "orders": [],
                    "_ids": set(),
                }
                grouped[key] = book
            if order_id in book["_ids"]:
                continue
            book["_ids"].add(order_id)
            book["orders"].append({
                "order_id": order_id,
                "unit_price": unit_price,
                "amount": amount,
                "expires": order.get("Expires"),
            })
            self.stats["orders"] += 1

        books = []
        for book in grouped.values():
            book.pop("_ids", None)
            if book["orders"]:
                books.append(book)
        self.stats["books"] += len(books)

        if books and log.isEnabledFor(logging.DEBUG):
            for b in books:
                log.debug("captured book: %s %s q%d %s -> %d orders (loc resolved)",
                          b["item_id"], b["city"], b["quality"],
                          b["auction_type"], len(b["orders"]))
        return books
