"""Client for the Albion Online Data Project public API.

Responsibilities:
  * batch item ids so every request URL stays under the API's 4096 char limit
  * pace requests under the published rate limit (180/min) with a token bucket
  * request gzip and decompress, as the project asks heavy users to
  * retry transient failures (429 / 5xx / network) with exponential backoff

Two endpoints are used:
  * /api/v2/stats/prices/{ids}   -> current best sell/buy orders per city/quality
  * /api/v2/stats/history/{ids}  -> daily volume + average price per city/quality
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger("shopalbi.aodp")


class _RateLimiter:
    """Steady pacing: never issue requests faster than the allowed rate."""

    def __init__(self, per_minute: int):
        self._min_interval = 60.0 / max(1, per_minute)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class AodpClient:
    def __init__(self, host: str | None = None):
        self.host = (host or config.API_HOST).rstrip("/")
        self._limiter = _RateLimiter(config.RATE_PER_MIN)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "shopalbi/1.0 (+market analytics)",
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
            }
        )

    # -- low level ----------------------------------------------------------

    def _get(self, url: str) -> list[dict]:
        last_exc: Exception | None = None
        for attempt in range(config.HTTP_RETRIES):
            self._limiter.wait()
            try:
                resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
                if resp.status_code == 429:
                    backoff = min(30.0, 2.0 ** attempt)
                    log.warning("429 rate limited, backing off %.1fs", backoff)
                    time.sleep(backoff)
                    continue
                if resp.status_code >= 500:
                    backoff = min(30.0, 2.0 ** attempt)
                    log.warning("HTTP %s, retry in %.1fs", resp.status_code, backoff)
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                backoff = min(30.0, 2.0 ** attempt)
                log.warning("request failed (%s), retry in %.1fs", exc, backoff)
                time.sleep(backoff)
        if last_exc:
            raise last_exc
        return []

    # -- batching -----------------------------------------------------------

    def _batches(self, item_ids: list[str], suffix_len: int) -> list[list[str]]:
        """Split ids so the full request URL length stays under the limit.

        suffix_len accounts for the query string (locations, qualities, etc.).
        """
        base = f"{self.host}/api/v2/stats/prices/.json"
        budget = config.MAX_URL_LEN - len(base) - suffix_len - 8
        batches: list[list[str]] = []
        cur: list[str] = []
        cur_len = 0
        for iid in item_ids:
            add = len(iid) + 1  # +1 for the comma
            if cur and cur_len + add > budget:
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(iid)
            cur_len += add
        if cur:
            batches.append(cur)
        return batches

    # -- public endpoints ---------------------------------------------------

    def fetch_prices(
        self,
        item_ids: list[str],
        locations: list[str],
        qualities: list[int],
    ) -> list[dict]:
        loc = urllib.parse.quote(",".join(locations))
        qual = ",".join(str(q) for q in qualities)
        suffix = len(f"?locations={loc}&qualities={qual}")
        out: list[dict] = []
        batches = self._batches(item_ids, suffix)
        for i, batch in enumerate(batches, 1):
            ids = ",".join(batch)
            url = (
                f"{self.host}/api/v2/stats/prices/"
                f"{urllib.parse.quote(ids)}.json?locations={loc}&qualities={qual}"
            )
            rows = self._get(url)
            out.extend(rows)
            log.debug("prices batch %d/%d: %d ids -> %d rows", i, len(batches), len(batch), len(rows))
        return out

    def fetch_history(
        self,
        item_ids: list[str],
        locations: list[str],
        qualities: list[int],
        days: int,
        time_scale: int = 24,
    ) -> list[dict]:
        loc = urllib.parse.quote(",".join(locations))
        qual = ",".join(str(q) for q in qualities)
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        date_s = f"{start.month}-{start.day}-{start.year}"
        end_s = f"{end.month}-{end.day}-{end.year}"
        suffix = len(
            f"?date={date_s}&end_date={end_s}&locations={loc}&qualities={qual}&time-scale={time_scale}"
        )
        out: list[dict] = []
        batches = self._batches(item_ids, suffix)
        for i, batch in enumerate(batches, 1):
            ids = ",".join(batch)
            url = (
                f"{self.host}/api/v2/stats/history/"
                f"{urllib.parse.quote(ids)}.json?date={date_s}&end_date={end_s}"
                f"&locations={loc}&qualities={qual}&time-scale={time_scale}"
            )
            rows = self._get(url)
            out.extend(rows)
            log.debug("history batch %d/%d: %d ids -> %d series", i, len(batches), len(batch), len(rows))
        return out
