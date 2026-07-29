"""Background refresh scheduling.

Current prices are cheap to pull, so we refresh them often. History is heavier
and changes slowly (daily buckets), so it runs on a longer cadence. All jobs
run in worker threads; the API keeps serving from SQLite meanwhile.
"""

from __future__ import annotations

import logging
import threading

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

    # -- jobs ---------------------------------------------------------------

    def job_current(self) -> None:
        if not self._current_lock.acquire(blocking=False):
            log.info("current refresh already running, skipping")
            return
        try:
            engine.refresh_current(self.storage, self.client)
            self.analytics.invalidate()
        except Exception:  # never let a scheduled job die silently
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
        """Kick a full refresh (catalog check -> history -> current) off-thread."""
        def _run():
            try:
                engine.ensure_catalog(self.storage)
            except Exception:
                log.exception("catalog refresh failed")
            self.job_history()
            self.job_current()

        threading.Thread(target=_run, name="refresh-all", daemon=True).start()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.scheduler.add_job(
            self.job_current,
            "interval",
            minutes=config.CURRENT_REFRESH_MINUTES,
            id="current",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.job_history,
            "interval",
            hours=config.HISTORY_REFRESH_HOURS,
            id="history",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        log.info(
            "scheduler started: current every %d min, history every %d h",
            config.CURRENT_REFRESH_MINUTES,
            config.HISTORY_REFRESH_HOURS,
        )

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
