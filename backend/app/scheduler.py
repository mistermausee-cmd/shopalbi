"""Background refresh scheduling.

Current prices are cheap to pull (~20 s for 6.8k items), so they refresh often.
History is heavier and changes in daily buckets, so it runs on a longer cadence.
Both jobs also rebuild the derived tables they feed (`bm_offer` after prices,
`agg` after history), which is what keeps the read path a plain indexed query.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, engine
from .aodp_client import AodpClient
from .engine import Analytics
from .storage import Storage

log = logging.getLogger("shopalbi.scheduler")


class RefreshManager:
    def __init__(self, storage: Storage, client: AodpClient, analytics: Analytics):
        self.storage = storage
        self.client = client
        self.analytics = analytics
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._current_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._on_catalog_change = None

    # -- jobs ---------------------------------------------------------------

    def _mark(self, running: bool) -> None:
        try:
            self.storage.set_meta("refresh_running", "1" if running else "0")
            if not running:
                self.storage.set_meta("refresh_finished_at", datetime.now(timezone.utc).isoformat())
        except Exception:
            log.debug("could not update refresh flag", exc_info=True)

    def job_current(self) -> None:
        if not self._current_lock.acquire(blocking=False):
            log.info("current refresh already running, skipping")
            return
        try:
            engine.refresh_current(self.storage, self.client)
            self.analytics.invalidate()
        except Exception:
            log.exception("current refresh failed")
        finally:
            self._current_lock.release()

    def job_history(self) -> None:
        if not self._history_lock.acquire(blocking=False):
            log.info("history refresh already running, skipping")
            return
        try:
            engine.refresh_history(self.storage, self.client)
            self.analytics.invalidate()
        except Exception:
            log.exception("history refresh failed")
        finally:
            self._history_lock.release()

    def refresh_all_async(self) -> None:
        """Kick a full refresh (catalog -> current -> history) off-thread.

        Current prices come first: they are what every tab needs to show
        anything at all, and they finish in seconds, whereas history takes
        closer to a minute.
        """
        def _run():
            self._mark(True)
            try:
                try:
                    before = self.storage.item_count()
                    engine.ensure_catalog(self.storage)
                    if self.storage.item_count() != before and self._on_catalog_change:
                        self._on_catalog_change()
                except Exception:
                    log.exception("catalog refresh failed")
                self.job_current()
                self.job_history()
            finally:
                self._mark(False)

        threading.Thread(target=_run, name="refresh-all", daemon=True).start()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.scheduler.add_job(
            self.job_current, "interval", minutes=config.CURRENT_REFRESH_MINUTES,
            id="current", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.job_history, "interval", hours=config.HISTORY_REFRESH_HOURS,
            id="history", max_instances=1, coalesce=True,
        )
        self.scheduler.start()
        log.info(
            "scheduler started: current every %d min, history every %d h",
            config.CURRENT_REFRESH_MINUTES, config.HISTORY_REFRESH_HOURS,
        )

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
