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

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_book (
    order_id INTEGER PRIMARY KEY,
    item_id  TEXT NOT NULL,
    city     TEXT NOT NULL,
    quality  INTEGER NOT NULL,
    side     TEXT NOT NULL,          -- 'offer' (sell) | 'request' (buy)
    price    INTEGER NOT NULL,
    amount   INTEGER NOT NULL,
    expires  TEXT,
    seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ob_offer ON order_book(city, side, item_id, quality);
CREATE INDEX IF NOT EXISTS idx_ob_seen ON order_book(seen_at);
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

    # -- live order book (from the NATS feed) -------------------------------

    def upsert_orders(self, rows: list[tuple]) -> int:
        """rows: (order_id, item_id, city, quality, side, price, amount, expires, seen_at)."""
        if not rows:
            return 0
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO order_book
                   (order_id, item_id, city, quality, side, price, amount, expires, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                     price=excluded.price, amount=excluded.amount,
                     expires=excluded.expires, seen_at=excluded.seen_at""",
                rows,
            )
        return len(rows)

    def prune_orders(self, min_seen_iso: str, now_iso: str) -> int:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM order_book WHERE seen_at < ? OR (expires IS NOT NULL AND expires <> '' AND expires < ?) OR amount <= 0",
                (min_seen_iso, now_iso),
            )
            return cur.rowcount

    def live_book(self, side: str, min_seen_iso: str, now_iso: str, city: str | None = None) -> list[dict]:
        """Fresh, unexpired orders of a side. Optionally restricted to one city."""
        q = (
            "SELECT item_id, city, quality, price, amount FROM order_book "
            "WHERE side=? AND amount>0 AND seen_at>=? "
            "AND (expires IS NULL OR expires='' OR expires>=?)"
        )
        params: list = [side, min_seen_iso, now_iso]
        if city is not None:
            q += " AND city=?"
            params.append(city)
        with self.cursor() as cur:
            cur.execute(q, params)
            return [dict(r) for r in cur.fetchall()]

    def order_book_count(self, min_seen_iso: str) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM order_book WHERE seen_at>=?", (min_seen_iso,))
            return cur.fetchone()["n"]
