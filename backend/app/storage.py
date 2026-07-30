"""SQLite storage layer.

Design note (this is the big change in v5): all heavy aggregation now happens
*inside* SQLite. The previous version pulled ~887k history rows and ~274k price
rows into Python dictionaries on every request, and the budget recommender did
that once per candidate city — enough to OOM the 1 GB VPS this runs on.

Now:
  * `history` is rolled up into an `agg` table once per history refresh
    (one INSERT..SELECT..GROUP BY, zero Python memory).
  * `bm_offer` precomputes, per (item, quality you own), the best Black Market
    bid you can actually sell into, honouring the quality-ladder rule.
  * the flip finder is a single indexed SQL query that does the profit filter in
    SQL, so only the handful of surviving rows ever reach Python.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .catalog import Item

log = logging.getLogger("shopalbi.storage")

# Tables first, then column migrations, then indexes. An index on a column
# that an older database has not got yet would abort the whole upgrade, so
# index creation deliberately runs last (see Storage.__init__).
_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS items (
    item_id   TEXT PRIMARY KEY,
    base_id   TEXT NOT NULL,
    tier      INTEGER NOT NULL,
    enchant   INTEGER NOT NULL,
    slot      TEXT NOT NULL,
    category  TEXT NOT NULL,
    name_ru   TEXT NOT NULL,
    name_en   TEXT NOT NULL,
    name_disp TEXT NOT NULL DEFAULT '',
    name_norm TEXT NOT NULL DEFAULT ''
);

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

CREATE TABLE IF NOT EXISTS history (
    item_id    TEXT NOT NULL,
    city       TEXT NOT NULL,
    quality    INTEGER NOT NULL,
    day        TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    avg_price  INTEGER NOT NULL,
    PRIMARY KEY (item_id, city, quality, day)
);

-- Rolled-up history per window. Rebuilt after every history refresh.
CREATE TABLE IF NOT EXISTS agg (
    window   TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    city     TEXT NOT NULL,
    quality  INTEGER NOT NULL,
    vwap     INTEGER NOT NULL,
    volume   INTEGER NOT NULL,
    daily    REAL NOT NULL,
    days     INTEGER NOT NULL,
    last_day TEXT,
    PRIMARY KEY (window, item_id, city, quality)
);

-- Best Black Market bid reachable for an item you hold at a given quality.
-- Encodes the game rule "a buy order accepts its own quality or higher", so
-- src_quality may be lower than `quality`.
CREATE TABLE IF NOT EXISTS bm_offer (
    item_id     TEXT NOT NULL,
    quality     INTEGER NOT NULL,   -- the quality you are holding
    price       INTEGER NOT NULL,   -- bid you can actually hit
    src_quality INTEGER NOT NULL,   -- which quality's buy order that is
    price_date  TEXT,
    PRIMARY KEY (item_id, quality)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Searchable shopping catalog for the "my sets" feature. Much wider than
-- `items` (mounts, potions, food, tools, gatherer gear) and deliberately has no
-- prices: quotes for the few items in a set are fetched on demand.
CREATE TABLE IF NOT EXISTS shop_items (
    item_id   TEXT PRIMARY KEY,
    tier      INTEGER NOT NULL,
    enchant   INTEGER NOT NULL,
    category  TEXT NOT NULL,
    name_disp TEXT NOT NULL,
    name_norm TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_norm ON shop_items(name_norm);
CREATE INDEX IF NOT EXISTS idx_shop_cat  ON shop_items(category);

CREATE TABLE IF NOT EXISTS gear_sets (
    set_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gear_set_lines (
    set_id   INTEGER NOT NULL,
    line_no  INTEGER NOT NULL,
    item_id  TEXT NOT NULL,
    quality  INTEGER NOT NULL DEFAULT 1,
    qty      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (set_id, line_no),
    FOREIGN KEY (set_id) REFERENCES gear_sets(set_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_book (
    order_id INTEGER PRIMARY KEY,
    item_id  TEXT NOT NULL,
    city     TEXT NOT NULL,
    quality  INTEGER NOT NULL,
    side     TEXT NOT NULL,          -- 'offer' (sell) | 'request' (buy)
    price    INTEGER NOT NULL,
    amount   INTEGER NOT NULL,
    npc      INTEGER NOT NULL DEFAULT 0,   -- 1 = system order (BM buy order)
    expires  TEXT,
    seen_at  TEXT NOT NULL
);
"""

_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_cat  ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_tier ON items(tier);
CREATE INDEX IF NOT EXISTS idx_items_norm ON items(name_norm);
CREATE INDEX IF NOT EXISTS idx_cp_city_item ON current_prices(city, item_id, quality);
CREATE INDEX IF NOT EXISTS idx_hist_day ON history(day);
CREATE INDEX IF NOT EXISTS idx_agg_lookup ON agg(window, city, item_id, quality);
CREATE INDEX IF NOT EXISTS idx_ob_lookup ON order_book(city, side, item_id, quality);
CREATE INDEX IF NOT EXISTS idx_ob_seen   ON order_book(seen_at);
"""

# Migrations for databases created by earlier versions.
_MIGRATIONS = [
    ("items", "name_disp", "ALTER TABLE items ADD COLUMN name_disp TEXT NOT NULL DEFAULT ''"),
    ("items", "name_norm", "ALTER TABLE items ADD COLUMN name_norm TEXT NOT NULL DEFAULT ''"),
    ("order_book", "npc", "ALTER TABLE order_book ADD COLUMN npc INTEGER NOT NULL DEFAULT 0"),
]

_QUALITY_UNION = " UNION ALL ".join(f"SELECT {q} AS quality" for q in config.QUALITIES)

# Bump when previously stored rows become *semantically wrong* (not merely
# stale), so an upgrade discards them instead of serving bad data.
#
#   2 -> v5.0: releases before this wrote the order book with Caerleon and the
#              Black Market swapped (location ids 3003/3005 were inverted).
#              Those rows would keep poisoning live depth for up to
#              ORDER_MAX_AGE_MINUTES after the upgrade, so the book is purged.
#              `agg`/`bm_offer` are rebuilt on every refresh anyway, but they are
#              cleared too so nothing derived from the old model can be read
#              before the first refresh completes.
DATA_EPOCH = 2
_EPOCH_PURGE = ["order_book", "agg", "bm_offer"]

# Allow-list for the only place a table name reaches SQL as a string.
_KNOWN_TABLES = frozenset(
    {"items", "current_prices", "history", "agg", "bm_offer", "meta", "order_book"}
)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class Storage:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA_TABLES)
            self._migrate(conn)
            conn.executescript(_SCHEMA_INDEXES)
            self._apply_epoch(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, ddl in _MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                log.info("migrating: %s", ddl)
                conn.execute(ddl)
        conn.commit()

    def _apply_epoch(self, conn: sqlite3.Connection) -> None:
        """Discard rows written under an older, incorrect data model."""
        row = conn.execute("SELECT value FROM meta WHERE key='data_epoch'").fetchone()
        try:
            found = int(row[0]) if row else 0
        except (TypeError, ValueError):
            found = 0
        if found >= DATA_EPOCH:
            return
        for table in _EPOCH_PURGE:
            try:
                n = conn.execute(f"DELETE FROM {table}").rowcount
                if n:
                    log.warning(
                        "data epoch %d -> %d: purged %d rows from %s "
                        "(written under the old swapped-location model)",
                        found, DATA_EPOCH, n, table,
                    )
            except sqlite3.Error:
                log.debug("could not purge %s", table, exc_info=True)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('data_epoch', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(DATA_EPOCH),),
        )
        conn.commit()
        log.info("data epoch set to %d", DATA_EPOCH)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute(f"PRAGMA cache_size=-{config.SQLITE_CACHE_MB * 1000}")
            conn.execute(f"PRAGMA mmap_size={config.SQLITE_MMAP_MB * 1024 * 1024}")
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

    # -- shopping catalog ---------------------------------------------------

    def replace_shop_items(self, items: list[Item]) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM shop_items")
            cur.executemany(
                "INSERT INTO shop_items (item_id, tier, enchant, category, name_disp, name_norm) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(it.item_id, it.tier, it.enchant, it.category, it.display_name(),
                  (it.display_name() + " " + it.item_id).lower()) for it in items],
            )
        return len(items)

    def shop_item_count(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM shop_items")
            return cur.fetchone()["n"]

    def search_shop_items(self, query: str | None, category: str | None = None,
                          tier: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM shop_items WHERE 1=1"
        params: dict = {}
        if query and query.strip():
            sql += " AND name_norm LIKE :q"
            params["q"] = f"%{query.strip().lower()}%"
        if category:
            sql += " AND category = :cat"
            params["cat"] = category
        if tier is not None:
            sql += " AND tier = :tier"
            params["tier"] = tier
        # Shortest name first: searching "суп" should surface "Морковный суп"
        # ahead of every soup variant that merely contains the word.
        sql += " ORDER BY LENGTH(name_disp), tier, enchant, item_id LIMIT :lim"
        params["lim"] = max(1, min(limit, 200))
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def shop_items_by_ids(self, item_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not item_ids:
            return {}
        with self.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shop_items WHERE item_id IN ({_placeholders(len(item_ids))})",
                item_ids)
            return {r["item_id"]: r for r in cur.fetchall()}

    # -- gear sets ----------------------------------------------------------

    def create_set(self, name: str, note: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO gear_sets (name, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name.strip() or "Без названия", note.strip(), now, now))
            return cur.lastrowid

    def list_sets(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM gear_set_lines l WHERE l.set_id=s.set_id) lines "
                "FROM gear_sets s ORDER BY s.updated_at DESC")
            return [dict(r) for r in cur.fetchall()]

    def get_set(self, set_id: int) -> dict | None:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM gear_sets WHERE set_id=?", (set_id,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT l.*, i.name_disp, i.category, i.tier, i.enchant FROM gear_set_lines l "
                "LEFT JOIN shop_items i ON i.item_id = l.item_id "
                "WHERE l.set_id=? ORDER BY l.line_no", (set_id,))
            return {**dict(row), "lines": [dict(r) for r in cur.fetchall()]}

    def replace_set_lines(self, set_id: int, lines: list[dict]) -> int:
        """Rewrite a set's contents. Lines are renumbered so ordering is stable."""
        with self.cursor() as cur:
            cur.execute("DELETE FROM gear_set_lines WHERE set_id=?", (set_id,))
            cur.executemany(
                "INSERT INTO gear_set_lines (set_id, line_no, item_id, quality, qty) "
                "VALUES (?, ?, ?, ?, ?)",
                [(set_id, i, ln["item_id"], int(ln.get("quality") or 1),
                  max(1, int(ln.get("qty") or 1))) for i, ln in enumerate(lines)],
            )
            cur.execute("UPDATE gear_sets SET updated_at=? WHERE set_id=?",
                        (datetime.now(timezone.utc).isoformat(), set_id))
        return len(lines)

    def rename_set(self, set_id: int, name: str, note: str | None = None) -> None:
        with self.cursor() as cur:
            if note is None:
                cur.execute("UPDATE gear_sets SET name=?, updated_at=? WHERE set_id=?",
                            (name.strip() or "Без названия",
                             datetime.now(timezone.utc).isoformat(), set_id))
            else:
                cur.execute("UPDATE gear_sets SET name=?, note=?, updated_at=? WHERE set_id=?",
                            (name.strip() or "Без названия", note.strip(),
                             datetime.now(timezone.utc).isoformat(), set_id))

    def delete_set(self, set_id: int) -> bool:
        with self.cursor() as cur:
            cur.execute("DELETE FROM gear_set_lines WHERE set_id=?", (set_id,))
            cur.execute("DELETE FROM gear_sets WHERE set_id=?", (set_id,))
            return cur.rowcount > 0

    def purge_orphans(self) -> dict[str, int]:
        """Drop rows for item ids that are no longer in the catalog.

        When the catalog shrinks (v5 removed gathering gear: 6,852 -> 6,402
        items) the price/history rows for the dropped ids stay behind forever.
        Queries JOIN `items`, so they never reach the UI, but they waste space
        and make row counts lie — a live database showed 274,080 rows in
        `current_prices` while a refresh only ever writes 256,080.
        """
        out: dict[str, int] = {}
        with self.cursor() as cur:
            for table in ("current_prices", "history", "bm_offer", "agg", "order_book"):
                cur.execute(
                    f"DELETE FROM {table} WHERE item_id NOT IN (SELECT item_id FROM items)")
                if cur.rowcount:
                    out[table] = cur.rowcount
        if out:
            log.info("purged rows for items no longer in the catalog: %s", out)
        return out

    def replace_items(self, items: list[Item]) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM items")
            cur.executemany(
                """INSERT INTO items
                   (item_id, base_id, tier, enchant, slot, category,
                    name_ru, name_en, name_disp, name_norm)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (it.item_id, it.base_id, it.tier, it.enchant, it.slot, it.category,
                     it.name_ru, it.name_en, it.display_name(),
                     (it.display_name() + " " + it.item_id).lower())
                    for it in items
                ],
            )
        return len(items)

    def all_item_ids(self) -> list[str]:
        with self.cursor() as cur:
            cur.execute("SELECT item_id FROM items ORDER BY item_id")
            return [r["item_id"] for r in cur.fetchall()]

    def item_count(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM items")
            return cur.fetchone()["n"]

    def table_empty(self, table: str) -> bool:
        if table not in _KNOWN_TABLES:
            raise ValueError(f"unknown table {table!r}")
        with self.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
            return cur.fetchone() is None

    def catalog_needs_rebuild(self) -> bool:
        """True when the catalog is missing or predates a column we now rely on.

        `name_disp`/`name_norm` are added empty by the column migration, and
        search matches on `name_norm` — so an upgraded database would silently
        return nothing for every search until the catalog was rebuilt. Detect it
        and rebuild instead of waiting for someone to notice.
        """
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM items")
            if cur.fetchone()["n"] == 0:
                return True
            cur.execute("SELECT 1 FROM items WHERE name_norm = '' LIMIT 1")
            return cur.fetchone() is not None

    # -- current prices -----------------------------------------------------

    def upsert_current_prices(self, rows: list[dict]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        payload = [
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
            for r in rows
            if r.get("item_id") and r.get("city")
        ]
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

    # -- history ------------------------------------------------------------

    def upsert_history(self, series: list[dict]) -> int:
        payload = []
        for s in series:
            item_id = s.get("item_id")
            city = s.get("location")
            quality = int(s.get("quality") or 0)
            if not item_id or not city:
                continue
            for pt in s.get("data") or []:
                day = (pt.get("timestamp") or "")[:10]
                if not day:
                    continue
                payload.append(
                    (item_id, city, quality, day,
                     int(pt.get("item_count") or 0), int(pt.get("avg_price") or 0))
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

    def prune_history(self, before_day: str) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM history WHERE day < ?", (before_day,))
            return cur.rowcount

    # -- aggregation --------------------------------------------------------

    def rebuild_aggregates(self) -> dict[str, int]:
        """Roll `history` up into `agg`, one row per (window, item, city, quality).

        Windows cover the last N *complete* UTC days and deliberately exclude
        today, whose bucket is partial: including it while dividing by N is what
        made the old daily-volume figures too high (the previous code also
        matched `day >= today - N`, picking up N+1 buckets).
        """
        out: dict[str, int] = {}
        today = datetime.now(timezone.utc).date()
        with self.cursor() as cur:
            for window, days in config.STAT_WINDOWS.items():
                first = (today - timedelta(days=days)).isoformat()
                last = (today - timedelta(days=1)).isoformat()
                cur.execute("DELETE FROM agg WHERE window=?", (window,))
                cur.execute(
                    """INSERT INTO agg (window, item_id, city, quality, vwap, volume, daily, days, last_day)
                       SELECT ?,
                              item_id, city, quality,
                              CAST(ROUND(
                                CASE WHEN SUM(item_count) > 0
                                     THEN SUM(avg_price * 1.0 * item_count) / SUM(item_count)
                                     ELSE AVG(avg_price) END) AS INTEGER),
                              SUM(item_count),
                              SUM(item_count) * 1.0 / ?,
                              COUNT(*),
                              MAX(day)
                       FROM history
                       WHERE day >= ? AND day <= ?
                       GROUP BY item_id, city, quality""",
                    (window, days, first, last),
                )
                out[window] = cur.rowcount
                # Record the exact range each window covers. `agg` is a stored
                # snapshot rebuilt every HISTORY_REFRESH_HOURS, so shortly after
                # UTC midnight it still describes yesterday's range. Saying which
                # days are actually averaged beats claiming "the last N days".
                cur.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"agg_range_{window}", f"{first}..{last}"),
                )
        log.info("aggregates rebuilt: %s", out)
        return out

    def rebuild_bm_offers(self, max_age_hours: float | None = None) -> int:
        """Precompute the best reachable Black Market bid per (item, held quality).

        Game rule (wiki, Marketplace + Black Market pages): a buy order accepts
        items of its own quality *or higher*, and the Black Market treats each
        quality as a separate item — so it frequently pays more for a lower
        quality than a higher one. Selling a quality-3 item therefore means
        taking the best of the q1/q2/q3 buy orders, not just the q3 one.

        The `MAX(price)` + bare `quality`/`price_date` columns rely on SQLite's
        documented guarantee that bare columns in a MAX()/MIN() aggregate come
        from the row that produced the extreme value.
        """
        max_age = config.BM_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
        with self.cursor() as cur:
            cur.execute("DELETE FROM bm_offer")
            cur.execute(
                f"""INSERT INTO bm_offer (item_id, quality, price, src_quality, price_date)
                    SELECT bm.item_id, q.quality,
                           MAX(bm.buy_price_max), bm.quality, bm.buy_price_max_date
                    FROM ({_QUALITY_UNION}) q
                    JOIN current_prices bm
                      ON bm.city = ?
                     AND bm.buy_price_max > 0
                     AND bm.quality <= q.quality
                     AND (julianday('now') - julianday(bm.buy_price_max_date)) * 24.0 <= ?
                    GROUP BY bm.item_id, q.quality""",
                (config.BLACK_MARKET, max_age),
            )
            n = cur.rowcount
        log.info("bm_offer rebuilt: %d rows (max age %.1f h)", n, max_age)
        return n

    # -- flip query ---------------------------------------------------------

    def flip_rows(
        self,
        window: str,
        net: float,
        cost_mult: float,
        min_profit: int,
        min_profit_pct: float,
        min_bm_daily: float,
        buy_cities: list[str],
        category: str | None = None,
        tier: int | None = None,
        enchant: int | None = None,
        quality: int | None = None,
        search: str | None = None,
        max_buy_age_h: float | None = None,
        max_bm_age_h: float | None = None,
        hard_limit: int = 20000,
    ) -> list[sqlite3.Row]:
        """Every (item, quality, source city -> Black Market) flip that clears the
        profit filters. Profit arithmetic and freshness live in SQL so only the
        surviving rows are materialised in Python."""
        if not buy_cities:
            return []
        max_buy_age_h = config.FLIP_MAX_AGE_HOURS if max_buy_age_h is None else max_buy_age_h
        max_bm_age_h = config.BM_MAX_AGE_HOURS if max_bm_age_h is None else max_bm_age_h

        buy_age = "(julianday('now') - julianday(cp.sell_price_min_date)) * 24.0"
        bm_age = "(julianday('now') - julianday(bo.price_date)) * 24.0"
        revenue = "(bo.price * :net)"
        cost = "(cp.sell_price_min * :cost_mult)"
        profit = f"({revenue} - {cost})"

        city_in = ",".join(f":city{i}" for i in range(len(buy_cities)))

        sql = f"""
        SELECT
            i.item_id, i.tier, i.enchant, i.category, i.name_disp,
            cp.city                AS buy_city,
            cp.quality             AS quality,
            cp.sell_price_min      AS buy_price_raw,
            {cost}                 AS buy_cost,
            {buy_age}              AS buy_age_h,
            bo.price               AS bm_price,
            bo.src_quality         AS bm_quality,
            {bm_age}               AS bm_age_h,
            {profit}               AS profit,
            COALESCE(ab.vwap, 0)   AS bm_vwap,
            COALESCE(ab.volume, 0) AS bm_volume,
            COALESCE(ab.daily, 0)  AS bm_daily,
            COALESCE(ab.days, 0)   AS bm_days,
            COALESCE(ad.vwap, 0)   AS bm_vwap_day,
            COALESCE(ac.vwap, 0)   AS city_vwap,
            COALESCE(bmc.sell_price_min, 0) AS bm_ask
        FROM current_prices cp
        JOIN items i     ON i.item_id = cp.item_id
        JOIN bm_offer bo ON bo.item_id = cp.item_id AND bo.quality = cp.quality
        LEFT JOIN agg ab ON ab.window = :window AND ab.city = :bm
                        AND ab.item_id = cp.item_id AND ab.quality = bo.src_quality
        LEFT JOIN agg ad ON ad.window = 'day'   AND ad.city = :bm
                        AND ad.item_id = cp.item_id AND ad.quality = bo.src_quality
        LEFT JOIN agg ac ON ac.window = :window AND ac.city = cp.city
                        AND ac.item_id = cp.item_id AND ac.quality = cp.quality
        LEFT JOIN current_prices bmc ON bmc.item_id = cp.item_id AND bmc.city = :bm
                        AND bmc.quality = bo.src_quality
        WHERE cp.city IN ({city_in})
          AND cp.sell_price_min > 0
          AND {buy_age} <= :max_buy_age
          AND {bm_age}  <= :max_bm_age
          AND {profit}  >= :min_profit
          AND {profit}  >= :min_profit_pct * {cost}
          AND COALESCE(ab.daily, 0) >= :min_bm_daily
          -- data sanity: ignore degenerate lowball bids and troll-priced offers
          AND (ab.vwap IS NULL OR ab.vwap <= 0 OR bo.price >= :bm_min_vs_vwap * ab.vwap)
          AND (ac.vwap IS NULL OR ac.vwap <= 0
               OR cp.sell_price_min <= :city_max_vs_vwap * ac.vwap)
        """
        params: dict = {
            "net": net,
            "cost_mult": cost_mult,
            "window": window,
            "bm": config.BLACK_MARKET,
            "max_buy_age": max_buy_age_h,
            "max_bm_age": max_bm_age_h,
            "min_profit": min_profit,
            "min_profit_pct": min_profit_pct / 100.0,
            "min_bm_daily": min_bm_daily,
            "bm_min_vs_vwap": config.BM_MIN_VS_VWAP,
            "city_max_vs_vwap": config.CITY_MAX_VS_VWAP,
        }
        params.update({f"city{i}": c for i, c in enumerate(buy_cities)})
        if config.DROP_SPIKED_BIDS:
            sql += " AND (ab.vwap IS NULL OR ab.vwap <= 0 OR bo.price <= :bm_max_vs_vwap * ab.vwap)"
            params["bm_max_vs_vwap"] = config.BM_MAX_VS_VWAP
        if category:
            sql += " AND i.category = :category"
            params["category"] = category
        if tier:
            sql += " AND i.tier = :tier"
            params["tier"] = tier
        if enchant is not None:
            sql += " AND i.enchant = :enchant"
            params["enchant"] = enchant
        if quality:
            sql += " AND cp.quality = :quality"
            params["quality"] = quality
        if search and search.strip():
            sql += " AND i.name_norm LIKE :search"
            params["search"] = f"%{search.strip().lower()}%"
        sql += " ORDER BY profit DESC LIMIT :hard_limit"
        params["hard_limit"] = hard_limit

        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # -- stats table --------------------------------------------------------

    def stats_rows(
        self,
        window: str,
        category: str | None = None,
        tier: int | None = None,
        enchant: int | None = None,
        quality: int | None = None,
        search: str | None = None,
    ) -> list[sqlite3.Row]:
        """One row per (item, quality) with the Black Market figures, plus the
        per-city VWAP/volume rows needed to fill the wide price table."""
        sql = """
        SELECT i.item_id, i.tier, i.enchant, i.category, i.name_disp,
               a.city, a.quality, a.vwap, a.volume, a.daily,
               COALESCE(cp.sell_price_min, 0) AS sell_now,
               COALESCE(cp.buy_price_max, 0)  AS buy_now
        FROM agg a
        JOIN items i ON i.item_id = a.item_id
        LEFT JOIN current_prices cp
               ON cp.item_id = a.item_id AND cp.city = a.city AND cp.quality = a.quality
        WHERE a.window = :window
        """
        params: dict = {"window": window}
        if category:
            sql += " AND i.category = :category"
            params["category"] = category
        if tier:
            sql += " AND i.tier = :tier"
            params["tier"] = tier
        if enchant is not None:
            sql += " AND i.enchant = :enchant"
            params["enchant"] = enchant
        if quality:
            sql += " AND a.quality = :quality"
            params["quality"] = quality
        if search and search.strip():
            sql += " AND i.name_norm LIKE :search"
            params["search"] = f"%{search.strip().lower()}%"
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

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
        """rows: (order_id, item_id, city, quality, side, price, amount, npc, expires, seen_at)."""
        if not rows:
            return 0
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO order_book
                   (order_id, item_id, city, quality, side, price, amount, npc, expires, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                     price=excluded.price, amount=excluded.amount,
                     npc=excluded.npc, expires=excluded.expires, seen_at=excluded.seen_at""",
                rows,
            )
        return len(rows)

    def prune_orders(self, min_seen_iso: str, now_iso: str) -> int:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM order_book WHERE seen_at < ? OR amount <= 0 "
                "OR (expires IS NOT NULL AND expires <> '' AND expires < ?)",
                (min_seen_iso, now_iso),
            )
            return cur.rowcount

    def live_book(
        self,
        side: str,
        min_seen_iso: str,
        now_iso: str,
        city: str | None = None,
        item_ids: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        q = (
            "SELECT item_id, city, quality, price, amount, npc, seen_at FROM order_book "
            "WHERE side=? AND amount>0 AND seen_at>=? "
            "AND (expires IS NULL OR expires='' OR expires>=?)"
        )
        params: list = [side, min_seen_iso, now_iso]
        if city is not None:
            q += " AND city=?"
            params.append(city)
        if item_ids:
            q += f" AND item_id IN ({_placeholders(len(item_ids))})"
            params.extend(item_ids)
        with self.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def order_book_stats(self, min_seen_iso: str) -> dict:
        with self.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN npc=1 THEN 1 ELSE 0 END) AS npc, "
                "MAX(seen_at) AS newest FROM order_book WHERE seen_at>=?",
                (min_seen_iso,),
            )
            r = cur.fetchone()
            cur.execute(
                "SELECT city, COUNT(*) AS n FROM order_book WHERE seen_at>=? GROUP BY city",
                (min_seen_iso,),
            )
            by_city = {row["city"]: row["n"] for row in cur.fetchall()}
        return {
            "orders": r["n"] or 0,
            "npc_orders": r["npc"] or 0,
            "newest": r["newest"],
            "by_city": by_city,
        }

    def vacuum_analyze(self) -> None:
        try:
            with self.cursor() as cur:
                cur.execute("ANALYZE")
        except sqlite3.Error:
            log.debug("ANALYZE failed", exc_info=True)
