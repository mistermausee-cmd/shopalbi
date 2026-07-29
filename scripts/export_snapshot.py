#!/usr/bin/env python3
"""Export a compact, shareable snapshot of the analyzer's database.

Why not just copy shopalbi.db: it is ~540 MB with 90 days of history, which is
over GitHub's 100 MB per-file limit, and almost all of that bulk is raw daily
history that anyone can re-download from the AODP API.

What this keeps is the part that CANNOT be reproduced later:

  order_book      the accumulated live order book (real prices AND amounts)
  current_prices  the exact quote snapshot the UI was rendering
  agg             the rolled-up history the formulas actually read
  bm_offer        the resolved Black Market quality ladder
  items, meta     catalog and refresh timestamps

Raw `history` is trimmed to a few recent days so spot-checks against `agg` are
still possible without carrying hundreds of megabytes.

The copy is made with SQLite's online backup API, so it is consistent even while
the analyzer is running and writing in WAL mode. Never `cp` a live SQLite file.

Usage (inside the container):
    python scripts/export_snapshot.py --out /logs
    python scripts/export_snapshot.py --out /logs --history-days 0   # smallest
    python scripts/export_snapshot.py --out /logs --full             # everything
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = os.environ.get("SHOPALBI_DB_PATH") or "/data/shopalbi.db"
KEEP_FULL = ["items", "current_prices", "agg", "bm_offer", "order_book", "meta"]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB, help=f"source database (default {DEFAULT_DB})")
    ap.add_argument("--out", default="/logs", help="output directory (default /logs)")
    ap.add_argument("--history-days", type=int, default=5,
                    help="days of raw history to keep, 0 = none (default 5)")
    ap.add_argument("--full", action="store_true", help="keep all raw history")
    ap.add_argument("--no-gzip", action="store_true", help="skip compression")
    args = ap.parse_args()

    src_path = Path(args.db)
    if not src_path.exists():
        print(f"ERROR: database not found at {src_path}", file=sys.stderr)
        print("       Inside Docker it lives at /data/shopalbi.db; pass --db otherwise.",
              file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"ERROR: cannot write to {out_dir}: {e}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    work = out_dir / f"shopalbi-snapshot-{stamp}.db"
    if work.exists():
        work.unlink()

    print(f"source : {src_path}  ({human(src_path.stat().st_size)})")
    print(f"target : {work}")

    # Consistent copy of a live, WAL-mode database.
    print("copying via SQLite online backup ...", flush=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(work)
    try:
        src.backup(dst)
    finally:
        src.close()
    print(f"  copied {human(work.stat().st_size)}")

    tables = {r[0] for r in dst.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if not args.full and "history" in tables:
        if args.history_days <= 0:
            dst.execute("DELETE FROM history")
            print("  raw history: dropped entirely")
        else:
            cutoff = (datetime.now(timezone.utc).date()
                      - timedelta(days=args.history_days)).isoformat()
            n = dst.execute("DELETE FROM history WHERE day < ?", (cutoff,)).rowcount
            print(f"  raw history: trimmed to >= {cutoff} (removed {n:,} rows)")
        dst.commit()

    print("\nkept rows:")
    for t in KEEP_FULL + (["history"] if "history" in tables else []):
        if t in tables:
            n = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:<16} {n:>10,}")

    # Refresh timestamps make the snapshot interpretable months later.
    print("\nrefresh state:")
    for k in ("data_epoch", "catalog_refreshed_at", "current_refreshed_at",
              "history_refreshed_at", "current_rows", "history_rows",
              "orderbook_updated_at", "orderbook_received"):
        row = dst.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        print(f"  {k:<22} {row[0] if row else '-'}")

    print("\nreclaiming space (VACUUM) ...", flush=True)
    dst.execute("VACUUM")
    dst.close()
    size = work.stat().st_size
    print(f"  {human(size)}")

    if args.no_gzip:
        final = work
    else:
        gz = work.with_suffix(".db.gz")
        print("compressing ...", flush=True)
        with open(work, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, length=4 << 20)
        work.unlink()
        final = gz
        print(f"  {human(final.stat().st_size)}")

    print(f"\nDONE -> {final}")
    limit = 100 * 1024 * 1024
    if final.stat().st_size > limit:
        print("WARNING: larger than GitHub's 100 MB per-file limit.")
        print("         Re-run with --history-days 0 to shrink it further.")
    else:
        print("Small enough to commit to a GitHub branch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
