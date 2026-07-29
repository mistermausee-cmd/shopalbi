"""FastAPI application: JSON API + static frontend.

Endpoints
  GET  /api/status                 refresh timestamps, counts, config
  GET  /api/stats?window=week&...  per-city average price / volume table
  GET  /api/flips?window=week&...  ranked city -> Black Market flips
  POST /api/refresh                trigger a background data refresh
  GET  /                           the web UI (static SPA)
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import config, engine
from .aodp_client import AodpClient
from .engine import Analytics
from .scheduler import RefreshManager
from .storage import Storage

# Log to stdout (docker logs) AND to a rotating file on the data volume, so
# errors can be inspected even without `docker compose logs`.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_path = config.LOG_DIR / "shopalbi.log"
    _handlers.append(
        RotatingFileHandler(_log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    )
except OSError:
    pass  # if the volume isn't writable, keep stdout logging only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
# Route uvicorn's own loggers through the same handlers so access/error logs
# also land in the file.
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _ul = logging.getLogger(_name)
    _ul.handlers = _handlers
    _ul.propagate = False

log = logging.getLogger("shopalbi")

# shared singletons, wired up in the lifespan handler
_storage: Storage | None = None
_analytics: Analytics | None = None
_manager: RefreshManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _storage, _analytics, _manager
    _storage = Storage()
    client = AodpClient()
    _analytics = Analytics(_storage)
    engine.ensure_catalog(_storage)
    _manager = RefreshManager(_storage, client, _analytics)
    _manager.start()
    if config.REFRESH_ON_START:
        log.info("kicking off initial data refresh")
        _manager.refresh_all_async()
    try:
        yield
    finally:
        if _manager:
            _manager.shutdown()


app = FastAPI(title="shopalbi", version="1.0.0", lifespan=lifespan)

# -- optional HTTP Basic auth ----------------------------------------------

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)):
    if not config.BASIC_AUTH_USER:
        return  # auth disabled
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, config.BASIC_AUTH_USER)
    ok_pass = secrets.compare_digest(credentials.password, config.BASIC_AUTH_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _analytics_or_503() -> Analytics:
    if _analytics is None:
        raise HTTPException(status_code=503, detail="Service starting up")
    return _analytics


# -- API --------------------------------------------------------------------

@app.get("/api/status")
def api_status(_=Depends(require_auth)):
    return _analytics_or_503().status()


@app.get("/api/meta")
def api_meta(_=Depends(require_auth)):
    """Static reference data for the frontend (cities, qualities, categories)."""
    return {
        "cities": config.ROYAL_CITIES,
        "black_market": config.BLACK_MARKET,
        "qualities": [{"id": q, "label": config.QUALITY_NAMES[q]} for q in config.QUALITIES],
        "tiers": config.TIERS,
        "categories": [
            {"id": k, "label": v} for k, v in config.CATEGORY_NAMES.items()
        ],
        "windows": list(config.STAT_WINDOWS.keys()),
        "sales_tax": config.SALES_TAX,
    }


@app.get("/api/stats")
def api_stats(
    window: str = Query("week"),
    category: str | None = None,
    tier: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    sort: str = Query("bm_volume"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _=Depends(require_auth),
):
    if window not in config.STAT_WINDOWS:
        raise HTTPException(status_code=400, detail=f"unknown window '{window}'")
    return _analytics_or_503().stats_table(
        window=window, category=category, tier=tier, quality=quality,
        search=search, sort=sort, limit=limit, offset=offset,
    )


@app.get("/api/flips")
def api_flips(
    window: str = Query("week"),
    min_profit: int | None = None,
    min_bm_volume: float | None = None,
    category: str | None = None,
    tier: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    sort: str = Query("profit_pct"),
    limit: int = Query(200, ge=1, le=1000),
    _=Depends(require_auth),
):
    if window not in config.STAT_WINDOWS:
        raise HTTPException(status_code=400, detail=f"unknown window '{window}'")
    return _analytics_or_503().flips(
        window=window, min_profit=min_profit, min_bm_volume=min_bm_volume,
        category=category, tier=tier, quality=quality, search=search,
        sort=sort, limit=limit,
    )


@app.post("/api/refresh")
def api_refresh(_=Depends(require_auth)):
    if _manager is None:
        raise HTTPException(status_code=503, detail="Service starting up")
    _manager.refresh_all_async()
    return JSONResponse({"status": "refresh started"})


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


# -- static frontend --------------------------------------------------------

if config.FRONTEND_DIR.exists():
    @app.get("/")
    def index():
        return FileResponse(config.FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/")
    def index_missing():
        return JSONResponse(
            {"error": f"frontend directory not found at {config.FRONTEND_DIR}"},
            status_code=500,
        )
