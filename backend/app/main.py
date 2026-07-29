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

import secrets as _secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, depth, engine
from .aodp_client import AodpClient
from .engine import Analytics
from .scheduler import RefreshManager
from .storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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


# -- full order-book depth --------------------------------------------------

class IngestOrder(BaseModel):
    order_id: int
    unit_price: int          # silver per unit, already divided by 10000
    amount: int
    expires: str | None = None


class IngestBook(BaseModel):
    item_id: str
    city: str
    quality: int
    auction_type: str                     # 'offer' or 'request'
    captured_at: str | None = None
    orders: list[IngestOrder] = Field(default_factory=list)


class IngestPayload(BaseModel):
    books: list[IngestBook] = Field(default_factory=list)


def _storage_or_503() -> Storage:
    if _storage is None:
        raise HTTPException(status_code=503, detail="Service starting up")
    return _storage


def require_ingest_token(x_ingest_token: str | None = Header(default=None)):
    """Fail closed: ingest is only possible when a token is configured and the
    caller presents it. An unset token disables writes entirely."""
    if not config.INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="Ingest disabled (no token configured)")
    if not x_ingest_token or not _secrets.compare_digest(x_ingest_token, config.INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


@app.post("/api/ingest/orders")
def api_ingest_orders(payload: IngestPayload, _=Depends(require_ingest_token)):
    """Receive full order books captured client-side by the depth agent."""
    storage = _storage_or_503()
    if len(payload.books) > config.INGEST_MAX_BOOKS:
        raise HTTPException(
            status_code=413,
            detail=f"too many books ({len(payload.books)} > {config.INGEST_MAX_BOOKS})",
        )
    books = [b.model_dump() for b in payload.books]
    result = storage.ingest_order_books(books)

    # opportunistic retention: drop books no agent has refreshed in a long time
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.DEPTH_PRUNE_AGE_HOURS)).isoformat()
    pruned = storage.prune_orders(cutoff)
    storage.set_meta("depth_ingested_at", datetime.now(timezone.utc).isoformat())

    if _analytics is not None:
        _analytics.invalidate()
    log.info("ingest: %d books / %d orders written, %d stale pruned",
             result["books"], result["orders"], pruned)
    return {"status": "ok", **result, "pruned": pruned}


@app.get("/api/depth")
def api_depth(
    item_id: str = Query(..., description="normalized item id, e.g. T5_BAG@2"),
    city: str = Query(...),
    quality: int = Query(1, ge=1, le=5),
    type: str = Query("offer", pattern="^(offer|request)$"),
    _=Depends(require_auth),
):
    """Full aggregated order book (price levels + cumulative depth)."""
    return depth.order_book_view(
        _storage_or_503(), item_id=item_id, quality=quality, city=city, auction_type=type,
    )


@app.get("/api/depth/flip")
def api_depth_flip(
    item_id: str = Query(..., description="normalized item id, e.g. T5_BAG@2"),
    quality: int = Query(1, ge=1, le=5),
    _=Depends(require_auth),
):
    """Depth-aware, fully-fillable flips from each city into the Black Market."""
    return depth.depth_flip_view(_storage_or_503(), item_id=item_id, quality=quality)


@app.get("/api/depth/status")
def api_depth_status(_=Depends(require_auth)):
    """What captured depth we currently hold and how fresh it is."""
    storage = _storage_or_503()
    return {
        "fresh": storage.depth_coverage(depth.fresh_cutoff_iso()),
        "total": storage.depth_coverage(),
        "last_ingest_at": storage.get_meta("depth_ingested_at"),
        "max_age_hours": config.DEPTH_MAX_AGE_HOURS,
        "ingest_enabled": bool(config.INGEST_TOKEN),
    }


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
