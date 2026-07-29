"""Live order-book consumer for the Albion Online Data Project NATS feed.

Subscribes to the public `marketorders.deduped` stream (no game client needed —
it's a plain TCP stream anyone can consume) and stores the orders relevant to us
(our equipment items, our cities + the Black Market) into the order_book table.
Each message is a single market order carrying price + Amount + side, which is
exactly the order-book depth the REST API does not expose.

Runs on its own thread with its own asyncio event loop so it never interferes
with the web server; nats-py handles reconnects automatically.
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


class NatsConsumer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._item_ids: set[str] = set()
        self._buffer: list[tuple] = []
        self._buf_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._received = 0
        self._stored = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._item_ids = set(self.storage.all_item_ids())
        if not self._item_ids:
            log.warning("no items in catalog yet; NATS consumer will store nothing until refresh")
        self._thread = threading.Thread(target=self._run_loop, name="nats-consumer", daemon=True)
        self._thread.start()
        log.info("NATS consumer thread started (topic=%s)", config.NATS_TOPIC)

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("NATS consumer loop crashed")

    # -- message handling ---------------------------------------------------

    def _on_message(self, data: bytes) -> None:
        try:
            o = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        item_id = o.get("ItemTypeId")
        if item_id not in self._item_ids:
            return
        city = config.NATS_LOCATION_IDS.get(o.get("LocationId"))
        if city is None:
            return
        side = o.get("AuctionType")
        if side not in ("offer", "request"):
            return
        amount = int(o.get("Amount") or 0)
        price = int(o.get("UnitPriceSilver") or 0)
        if amount <= 0 or price <= 0:
            return
        order_id = o.get("Id")
        if order_id is None:
            return
        seen = datetime.now(timezone.utc).isoformat()
        row = (
            int(order_id), item_id, city, int(o.get("QualityLevel") or 0),
            side, price, amount, o.get("Expires") or "", seen,
        )
        with self._buf_lock:
            self._buffer.append(row)
            self._received += 1

    def _drain_buffer(self) -> list[tuple]:
        with self._buf_lock:
            if not self._buffer:
                return []
            batch = self._buffer
            self._buffer = []
            return batch

    async def _main(self) -> None:
        nc = await nats.connect(
            config.NATS_URL,
            connect_timeout=15,
            max_reconnect_attempts=-1,      # retry forever
            reconnect_time_wait=5,
            allow_reconnect=True,
            name="shopalbi",
        )
        log.info("connected to NATS feed")

        async def cb(msg):
            self._on_message(msg.data)

        await nc.subscribe(config.NATS_TOPIC, cb=cb)

        last_prune = datetime.now(timezone.utc)
        while not self._stop.is_set():
            await asyncio.sleep(config.ORDER_FLUSH_SECONDS)
            batch = self._drain_buffer()
            if batch:
                try:
                    n = self.storage.upsert_orders(batch)
                    self._stored += n
                    self.storage.set_meta("orderbook_updated_at", datetime.now(timezone.utc).isoformat())
                    self.storage.set_meta("orderbook_received", str(self._received))
                except Exception:
                    log.exception("failed to store order batch")
            # prune stale/expired every ~2 minutes
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

        # flush remaining before exit
        batch = self._drain_buffer()
        if batch:
            try:
                self.storage.upsert_orders(batch)
            except Exception:
                pass
        try:
            await nc.drain()
        except Exception:
            pass
