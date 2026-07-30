"""Live order-book consumer for the Albion Online Data Project NATS feed.

Why this exists: the REST API only ever returns ONE price per (item, city,
quality) — the best offer and the best bid. It never tells you how many units
sit behind that price. The public NATS feed carries the raw market orders, each
with `UnitPriceSilver` AND `Amount`, which is the depth the planner needs to
avoid recommending quantities that do not physically exist.

No game client is required; this is a plain authenticated TCP stream that a VPS
can consume directly. Verified live: ~6 orders/second worldwide on EU.

Topics (verified by subscribing to `>` and inspecting the traffic):
  marketorders.deduped        one order per message, prices in plain silver
  marketorders.deduped.bulk   the same orders batched into JSON arrays
  marketorders.ingest         DO NOT USE — carries duplicates and prices scaled
                              by 10000 (7160000 where deduped says 716)

Runs on its own thread with its own asyncio loop; nats-py handles reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import nats

from . import config
from .storage import Storage

log = logging.getLogger("shopalbi.nats")

# Sanity ceiling: a single unit price above this is a scaling artifact, not a
# real order. Keeps a malformed producer from poisoning the book.
_MAX_SANE_UNIT_PRICE = 2_000_000_000


class NatsConsumer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._item_ids: set[str] = set()
        self._buffer: dict[int, tuple] = {}     # order_id -> row (last write wins)
        self._buf_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Session counters. `accepted` counts order objects that pass our filters
        # and `written` counts rows actually upserted. They should track each
        # other closely; a lasting gap means orders are being dropped. (They used
        # to differ by exactly 2x, which was not data loss at all but the same
        # order arriving on two duplicate topics — see config.NATS_TOPICS.)
        self._messages = 0
        self._seen = 0
        self._accepted = 0
        self._written = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.refresh_items()
        self._thread = threading.Thread(target=self._run_loop, name="nats-consumer", daemon=True)
        self._thread.start()
        log.info("NATS consumer started (topics=%s)", ",".join(config.NATS_TOPICS))

    def backlog(self) -> int:
        """Orders accepted but not yet written. Non-zero for long means the
        flush cap is too small for the current feed rate."""
        with self._buf_lock:
            return len(self._buffer)

    def refresh_items(self) -> None:
        """Re-read the catalog so a fresh install starts filtering correctly."""
        self._item_ids = set(self.storage.all_item_ids())
        if not self._item_ids:
            log.warning("catalog empty; the order book stays empty until a refresh completes")

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("NATS consumer loop crashed")

    # -- message handling ---------------------------------------------------

    @staticmethod
    def _is_npc(expires: str) -> bool:
        """System orders (the Black Market's own bids) never really expire.

        Verified on the live feed: BM `request` orders come back with
        Expires around year 3025, while player orders expire within 30 days.
        """
        try:
            return int(expires[:4]) >= config.NPC_EXPIRY_YEAR
        except (ValueError, TypeError, IndexError):
            return False

    def _handle_order(self, o: dict) -> None:
        self._seen += 1
        item_id = o.get("ItemTypeId")
        if item_id not in self._item_ids:
            return
        city = config.NATS_LOCATION_IDS.get(o.get("LocationId"))
        if city is None:
            return
        side = o.get("AuctionType")
        if side not in ("offer", "request"):
            return
        try:
            amount = int(o.get("Amount") or 0)
            price = int(o.get("UnitPriceSilver") or 0)
            order_id = int(o["Id"])
        except (TypeError, ValueError, KeyError):
            return
        if amount <= 0 or price <= 0 or price > _MAX_SANE_UNIT_PRICE:
            return
        expires = o.get("Expires") or ""
        row = (
            order_id, item_id, city, int(o.get("QualityLevel") or 0), side,
            price, amount, 1 if self._is_npc(expires) else 0, expires,
            datetime.now(timezone.utc).isoformat(),
        )
        with self._buf_lock:
            self._buffer[order_id] = row
            self._accepted += 1

    def _on_message(self, data: bytes) -> None:
        self._messages += 1
        try:
            payload = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(payload, list):
            for o in payload:
                if isinstance(o, dict):
                    self._handle_order(o)
        elif isinstance(payload, dict):
            orders = payload.get("Orders")
            if isinstance(orders, list):
                for o in orders:
                    if isinstance(o, dict):
                        self._handle_order(o)
            else:
                self._handle_order(payload)

    def _drain_buffer(self) -> list[tuple]:
        """Take up to ORDER_FLUSH_MAX orders, LEAVING the remainder buffered.

        This used to clear the whole buffer and return only the first slice,
        silently discarding the overflow. The bulk topic delivers arrays, so
        bursts routinely exceed the cap: on a live run the log read
        "413144 seen, 317734 relevant, 159415 stored" — half the orders we had
        already decided to keep never reached the database.
        """
        with self._buf_lock:
            if not self._buffer:
                return []
            if len(self._buffer) <= config.ORDER_FLUSH_MAX:
                batch = list(self._buffer.values())
                self._buffer = {}
                return batch
            keys = list(self._buffer.keys())[: config.ORDER_FLUSH_MAX]
            batch = [self._buffer.pop(k) for k in keys]
        return batch

    async def _main(self) -> None:
        nc = await nats.connect(
            config.NATS_URL,
            connect_timeout=20,
            max_reconnect_attempts=-1,      # retry forever
            reconnect_time_wait=5,
            allow_reconnect=True,
            name=f"shopalbi/{config.VERSION}",
        )
        log.info("connected to NATS feed %s", config.NATS_URL.rsplit("@", 1)[-1])

        async def cb(msg):
            self._on_message(msg.data)

        for topic in config.NATS_TOPICS:
            await nc.subscribe(topic, cb=cb)
            log.info("subscribed to %s", topic)

        last_prune = datetime.now(timezone.utc)
        last_log = last_prune
        while not self._stop.is_set():
            await asyncio.sleep(config.ORDER_FLUSH_SECONDS)
            batch = self._drain_buffer()
            if batch:
                try:
                    self._written += self.storage.upsert_orders(batch)
                    now = datetime.now(timezone.utc)
                    self.storage.set_meta("orderbook_updated_at", now.isoformat())
                    self.storage.set_meta("orderbook_accepted", str(self._accepted))
                    self.storage.set_meta("orderbook_written", str(self._written))
                except Exception:
                    log.exception("failed to store order batch")

            now = datetime.now(timezone.utc)
            if (now - last_prune).total_seconds() > 120:
                last_prune = now
                min_seen = (now - timedelta(minutes=config.ORDER_MAX_AGE_MINUTES)).isoformat()
                try:
                    removed = self.storage.prune_orders(min_seen, now.isoformat())
                    if removed:
                        log.debug("pruned %d stale orders", removed)
                except Exception:
                    log.exception("failed to prune orders")
            if (now - last_log).total_seconds() > 600:
                last_log = now
                # `stored` must track `relevant` closely; a persistent gap means
                # orders are being dropped rather than merely queued.
                log.info(
                    "order feed (session): %d msgs, %d orders seen, %d ours, %d written, "
                    "%d queued",
                    self._messages, self._seen, self._accepted, self._written, self.backlog(),
                )

        batch = self._drain_buffer()
        if batch:
            try:
                self.storage.upsert_orders(batch)
            except Exception:
                log.debug("final flush failed", exc_info=True)
        try:
            await nc.drain()
        except Exception:
            pass
