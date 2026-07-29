"""SQLite storage layer.

Small, dependency-free persistence for the item catalog, the latest price
snapshot, and daily history buckets. Reads are cheap enough that the API
computes stats and flips directly from these tables on each request.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .catalog import Item

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id   TEXT PRIMARY KEY,
    base_id   TEXT NOT NULL,
    tier      INTEGER NOT NULL,
    enchant   INTEGER NOT NULL,
    slot      TEXT NOT NULL,
    category  TEXT NOT NULL,
    name_ru   TEXT NOT NULL,
    name_en   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_cat ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_tier ON items(tier);

CREATE TABLE IF NOT EXISTS current_prices (
    item_id             TEXT NOT NULL,
    city                TEXT NOT NULL,
    quality             INTEGER NOT NULL,
    sell_price_min      INTEGER NOT NULL,
    sell_price_min_date TEXT,
    buy_price_max       INTEGER NOT NULL,
    buy_price_max_date  TEXT,
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (item_id, city, quality)
);
CREATE INDEX IF NOT EXISTS idx_cp_item ON current_prices(item_id, quality);
CREATE INDEX IF NOT EXISTS idx_cp_city ON current_prices(city);

CREATE TABLE IF NOT EXISTS history (
    item_id    TEXT NOT NULL,
    city       TEXT NOT NULL,
    quality    INTEGER NOT NULL,
    day        TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    avg_price  INTEGER NOT NULL,
    PRIMARY KEY (item_id, city, quality, day)
);
CREATE INDEX IF NOT EXISTS idx_hist_item ON history(item_id, quality);
CREATE INDEX IF NOT EXISTS idx_hist_city ON history(city);
CREATE INDEX IF NOT EXISTS idx_hist_day ON history(day);

CREATE TABLE IF NOT EXISTS market_orders (
    order_id     INTEGER PRIMARY KEY,   -- Albion's globally-unique auction id
    item_id      TEXT NOT NULL,         -- normalized, incl. @enchant (e.g. T5_BAG@2)
    city         TEXT NOT NULL,
    quality      INTEGER NOT NULL,
    auction_type TEXT NOT NULL,         -- 'offer' (sell order) or 'request' (buy order)
    unit_price   INTEGER NOT NULL,      -- silver per unit (already divided by 10000)
    amount       INTEGER NOT NULL,
    expires      TEXT,
    captured_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mo_book
    ON market_orders(item_id, quality, auction_type);
CREATE INDEX IF NOT EXISTS idx_mo_city ON market_orders(city);
CREATE INDEX IF NOT EXISTS idx_mo_captured ON market_orders(captured_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def cursor(self):
        conn = self._connect()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # -- catalog ------------------------------------------------------------

    def replace_items(self, items: list[Item]) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM items")
            cur.executemany(
                """INSERT INTO items
                   (item_id, base_id, tier, enchant, slot, category, name_ru, name_en)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (it.item_id, it.base_id, it.tier, it.enchant, it.slot,
                     it.category, it.name_ru, it.name_en)
                    for it in items
                ],
            )
        return len(items)

    def all_item_ids(self) -> list[str]:
        with self.cursor() as cur:
            cur.execute("SELECT item_id FROM items ORDER BY item_id")
            return [r["item_id"] for r in cur.fetchall()]

    def item_meta_map(self) -> dict[str, dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM items")
            return {r["item_id"]: dict(r) for r in cur.fetchall()}

    def item_count(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM items")
            return cur.fetchone()["n"]

    # -- current prices -----------------------------------------------------

    def upsert_current_prices(self, rows: list[dict]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        payload = []
        for r in rows:
            payload.append(
                (
                    r.get("item_id"),
                    r.get("city"),
                    int(r.get("quality") or 0),
                    int(r.get("sell_price_min") or 0),
                    r.get("sell_price_min_date"),
                    int(r.get("buy_price_max") or 0),
                    r.get("buy_price_max_date"),
                    now,
                )
            )
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO current_prices
                   (item_id, city, quality, sell_price_min, sell_price_min_date,
                    buy_price_max, buy_price_max_date, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id, city, quality) DO UPDATE SET
                     sell_price_min=excluded.sell_price_min,
                     sell_price_min_date=excluded.sell_price_min_date,
                     buy_price_max=excluded.buy_price_max,
                     buy_price_max_date=excluded.buy_price_max_date,
                     fetched_at=excluded.fetched_at""",
                payload,
            )
        return len(payload)

    def current_prices(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM current_prices")
            return [dict(r) for r in cur.fetchall()]

    # -- history ------------------------------------------------------------

    def upsert_history(self, series: list[dict]) -> int:
        payload = []
        for s in series:
            item_id = s.get("item_id")
            city = s.get("location")
            quality = int(s.get("quality") or 0)
            for pt in s.get("data") or []:
                ts = pt.get("timestamp") or ""
                day = ts[:10]
                if not day:
                    continue
                payload.append(
                    (
                        item_id,
                        city,
                        quality,
                        day,
                        int(pt.get("item_count") or 0),
                        int(pt.get("avg_price") or 0),
                    )
                )
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO history
                   (item_id, city, quality, day, item_count, avg_price)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id, city, quality, day) DO UPDATE SET
                     item_count=excluded.item_count,
                     avg_price=excluded.avg_price""",
                payload,
            )
        return len(payload)

    def history_rows(self, since_day: str) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM history WHERE day >= ?", (since_day,))
            return [dict(r) for r in cur.fetchall()]

    def prune_history(self, before_day: str) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM history WHERE day < ?", (before_day,))
            return cur.rowcount

    # -- market depth (full order books) ------------------------------------

    def ingest_order_books(self, books: list[dict]) -> dict:
        """Replace captured order books with fresh ones, one book at a time.

        A "book" is the complete set of orders of a single auction type for one
        item variant at one city/quality, as observed in a single market window
        response. Replacing per book (delete-then-insert) keeps the stored depth
        an exact mirror of what was last seen, and never mixes a stale offer
        book with a fresh request book.

        Each book:
            {item_id, city, quality, auction_type, captured_at,
             orders: [{order_id, unit_price, amount, expires}, ...]}

        Returns counts for observability.
        """
        books_written = 0
        orders_written = 0
        with self.cursor() as cur:
            for b in books:
                item_id = b.get("item_id")
                city = b.get("city")
                auction_type = (b.get("auction_type") or "").lower()
                try:
                    quality = int(b.get("quality") or 0)
                except (TypeError, ValueError):
                    continue
                if not item_id or not city or auction_type not in ("offer", "request"):
                    continue
                captured_at = b.get("captured_at") or datetime.now(timezone.utc).isoformat()

                cur.execute(
                    "DELETE FROM market_orders "
                    "WHERE item_id=? AND city=? AND quality=? AND auction_type=?",
                    (item_id, city, quality, auction_type),
                )
                payload = []
                for o in b.get("orders") or []:
                    try:
                        order_id = int(o["order_id"])
                        unit_price = int(o["unit_price"])
                        amount = int(o["amount"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if order_id <= 0 or unit_price <= 0 or amount <= 0:
                        continue
                    payload.append(
                        (order_id, item_id, city, quality, auction_type,
                         unit_price, amount, o.get("expires"), captured_at)
                    )
                if payload:
                    # A given order_id may still exist under a different book
                    # (e.g. re-listed after an item edit); OR-REPLACE keeps the
                    # newest write authoritative on the primary key.
                    cur.executemany(
                        """INSERT OR REPLACE INTO market_orders
                           (order_id, item_id, city, quality, auction_type,
                            unit_price, amount, expires, captured_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        payload,
                    )
                    orders_written += len(payload)
                books_written += 1
        return {"books": books_written, "orders": orders_written}

    def order_book_rows(
        self,
        item_id: str,
        quality: int,
        auction_type: str,
        fresh_since: str | None = None,
        city: str | None = None,
    ) -> list[dict]:
        """Raw orders for one book, optionally limited to one city and to
        captures newer than `fresh_since` (ISO timestamp)."""
        sql = (
            "SELECT * FROM market_orders "
            "WHERE item_id=? AND quality=? AND auction_type=?"
        )
        args: list = [item_id, int(quality), auction_type.lower()]
        if city:
            sql += " AND city=?"
            args.append(city)
        if fresh_since:
            sql += " AND captured_at >= ?"
            args.append(fresh_since)
        with self.cursor() as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def order_books_for_item(
        self, item_id: str, quality: int, fresh_since: str | None = None
    ) -> list[dict]:
        """Every fresh order (both types, all cities) for one item variant."""
        sql = "SELECT * FROM market_orders WHERE item_id=? AND quality=?"
        args: list = [item_id, int(quality)]
        if fresh_since:
            sql += " AND captured_at >= ?"
            args.append(fresh_since)
        with self.cursor() as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def depth_coverage(self, fresh_since: str | None = None) -> dict:
        """Summary of what depth data we currently hold, for /api/status."""
        with self.cursor() as cur:
            if fresh_since:
                cur.execute(
                    "SELECT COUNT(*) AS orders, "
                    "COUNT(DISTINCT item_id||'/'||quality||'/'||city||'/'||auction_type) AS books, "
                    "MAX(captured_at) AS last "
                    "FROM market_orders WHERE captured_at >= ?",
                    (fresh_since,),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) AS orders, "
                    "COUNT(DISTINCT item_id||'/'||quality||'/'||city||'/'||auction_type) AS books, "
                    "MAX(captured_at) AS last FROM market_orders"
                )
            r = cur.fetchone()
            return {
                "orders": r["orders"] or 0,
                "books": r["books"] or 0,
                "last_capture_at": r["last"],
            }

    def prune_orders(self, before: str) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM market_orders WHERE captured_at < ?", (before,))
            return cur.rowcount

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default
