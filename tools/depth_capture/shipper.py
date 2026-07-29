"""Ship captured order books to the shopalbi backend.

Books are queued from the capture thread and flushed by a background worker on
a size/time trigger. Within a pending batch, only the newest book per
(item_id, city, quality, auction_type) is kept — re-opening a market before a
flush should overwrite, not duplicate. If the backend is unreachable the queue
is retained (bounded, newest-wins) and retried on the next tick, so a restart
of the backend never loses a session's captures beyond the cap.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .config import AgentConfig

log = logging.getLogger("depth_capture.shipper")


def _book_key(book: dict) -> tuple:
    return (book["item_id"], book["city"], book["quality"], book["auction_type"])


class Shipper:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._pending: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_flush = time.monotonic()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "shopalbi-depth-capture/1.0",
            "Content-Type": "application/json",
            "X-Ingest-Token": cfg.ingest_token,
        })
        self.stats = {"shipped_books": 0, "shipped_orders": 0, "flushes": 0, "failures": 0, "dropped": 0}

    # -- producer side ------------------------------------------------------

    def enqueue(self, books: list[dict]) -> None:
        if not books:
            return
        with self._lock:
            for b in books:
                self._pending[_book_key(b)] = b
            if len(self._pending) > self.cfg.max_queue_books:
                # newest-wins: drop the oldest keys to stay bounded
                overflow = len(self._pending) - self.cfg.max_queue_books
                for k in list(self._pending.keys())[:overflow]:
                    self._pending.pop(k, None)
                self.stats["dropped"] += overflow
                log.warning("queue over cap, dropped %d oldest books", overflow)

    # -- worker -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="shipper", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(0.5)
            with self._lock:
                due = (
                    len(self._pending) >= self.cfg.flush_max_books
                    or (self._pending and
                        time.monotonic() - self._last_flush >= self.cfg.flush_interval_s)
                )
            if due:
                self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                self._last_flush = time.monotonic()
                return
            batch = list(self._pending.values())

        if self._post(batch):
            with self._lock:
                for b in batch:
                    # only clear keys we actually sent; newer captures that
                    # arrived mid-flush are preserved
                    k = _book_key(b)
                    if self._pending.get(k) is b:
                        self._pending.pop(k, None)
                self._last_flush = time.monotonic()
            self.stats["flushes"] += 1
            self.stats["shipped_books"] += len(batch)
            self.stats["shipped_orders"] += sum(len(b["orders"]) for b in batch)
            log.info("shipped %d books (%d orders)",
                     len(batch), sum(len(b["orders"]) for b in batch))
        else:
            self.stats["failures"] += 1
            log.warning("flush failed, %d books retained for retry", len(batch))

    def _post(self, batch: list[dict]) -> bool:
        payload = {"books": [
            {k: v for k, v in b.items() if k != "_ids"} for b in batch
        ]}
        for attempt in range(self.cfg.http_retries):
            try:
                resp = self._session.post(
                    self.cfg.ingest_endpoint(),
                    json=payload,
                    timeout=self.cfg.http_timeout_s,
                )
                if resp.status_code == 200:
                    return True
                if resp.status_code in (401, 403):
                    log.error("ingest rejected (%s): check SHOPALBI_INGEST_TOKEN", resp.status_code)
                    return False
                if resp.status_code == 503:
                    log.error("ingest disabled on backend (no token configured server-side)")
                    return False
                backoff = min(30.0, 2.0 ** attempt)
                log.warning("ingest HTTP %s, retry in %.1fs", resp.status_code, backoff)
                time.sleep(backoff)
            except requests.RequestException as exc:
                backoff = min(30.0, 2.0 ** attempt)
                log.warning("ingest request failed (%s), retry in %.1fs", exc, backoff)
                time.sleep(backoff)
        return False

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.flush()  # best-effort final drain
