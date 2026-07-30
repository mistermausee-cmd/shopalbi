"""FastAPI application: JSON API + static frontend.

Endpoints
  GET  /api/status                  refresh timestamps, counts, live-book stats
  GET  /api/meta                    reference data for the UI
  GET  /api/flips?...               ranked city -> Black Market flips
  GET  /api/cities?...              which city is the best buy base
  GET  /api/plan?budget=...         budget shopping plan on real order depth
  GET  /api/stats?...               per-city average price / volume table
  POST /api/refresh                 trigger a background data refresh
  GET  /api/health                  liveness probe
  GET  /                            the web UI

Static assets are served with `Cache-Control: no-cache` and the UI requests
them with a `?v=<version>` query string, so a deploy can never leave a browser
running last release's JavaScript against this release's API.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, engine
from .aodp_client import AodpClient
from .engine import Analytics
from .nats_consumer import NatsConsumer
from .scheduler import RefreshManager
from .storage import Storage

# Log to stdout (docker logs) AND to a rotating file, so errors can be
# inspected on the host at $SHOPALBI_LOG_DIR/shopalbi.log (/root/logs in Docker).
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.append(
        RotatingFileHandler(
            config.LOG_DIR / "shopalbi.log",
            maxBytes=5_000_000, backupCount=3, encoding="utf-8",
        )
    )
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _ul = logging.getLogger(_name)
    _ul.handlers = _handlers
    _ul.propagate = False

log = logging.getLogger("shopalbi")

_storage: Storage | None = None
_analytics: Analytics | None = None
_manager: RefreshManager | None = None
_nats: NatsConsumer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _storage, _analytics, _manager, _nats
    log.info("shopalbi %s starting (server=%s)", config.VERSION, config.API_HOST)
    _storage = Storage()
    client = AodpClient()
    _analytics = Analytics(_storage)
    engine.ensure_catalog(_storage)
    # Existing databases carry price/history rows for items the catalog has since
    # dropped; clear them once at startup so row counts mean what they say.
    _storage.purge_orphans()
    # After an upgrade the derived tables are empty (they are rebuilt from
    # scratch whenever the data model changes), while `history` and
    # `current_prices` survive. Rebuilding them here costs a second or two and
    # means the site has real numbers immediately instead of looking empty until
    # the first full refresh finishes a couple of minutes later.
    engine.ensure_derived(_storage)
    _manager = RefreshManager(_storage, client, _analytics)
    _manager.start()
    if config.NATS_ENABLE:
        _nats = NatsConsumer(_storage)
        _nats.start()
        _manager._on_catalog_change = _nats.refresh_items
    if config.REFRESH_ON_START:
        log.info("kicking off initial data refresh")
        _manager.refresh_all_async()
    try:
        yield
    finally:
        if _nats:
            _nats.stop()
        if _manager:
            _manager.shutdown()


app = FastAPI(title="shopalbi", version=config.VERSION, lifespan=lifespan)

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)):
    if not config.BASIC_AUTH_USER:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok = (secrets.compare_digest(credentials.username, config.BASIC_AUTH_USER)
          and secrets.compare_digest(credentials.password, config.BASIC_AUTH_PASS))
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _an() -> Analytics:
    if _analytics is None:
        raise HTTPException(status_code=503, detail="Service starting up")
    return _analytics


def _check_window(window: str) -> None:
    if window not in config.STAT_WINDOWS:
        raise HTTPException(status_code=400, detail=f"unknown window '{window}'")


def _check_mode(name: str, value: str) -> None:
    if value not in ("instant", "order"):
        raise HTTPException(status_code=400, detail=f"{name} must be 'instant' or 'order'")


# -- API --------------------------------------------------------------------

@app.get("/api/status")
def api_status(_=Depends(require_auth)):
    return _an().status()


@app.get("/api/meta")
def api_meta(_=Depends(require_auth)):
    return {
        "version": config.VERSION,
        "cities": config.ROYAL_CITIES,
        "buy_cities": config.BUY_CITIES,
        "excluded_buy_cities": sorted(config.EXCLUDED_BUY_CITIES),
        "black_market": config.BLACK_MARKET,
        "qualities": [{"id": q, "label": config.QUALITY_NAMES[q]} for q in config.QUALITIES],
        "tiers": config.TIERS,
        "enchants": [0, 1, 2, 3, 4],
        "categories": [{"id": k, "label": v} for k, v in config.CATEGORY_NAMES.items()],
        "windows": [{"id": k, "label": config.WINDOW_LABELS.get(k, k), "days": d}
                    for k, d in config.STAT_WINDOWS.items()],
        "sales_tax": config.SALES_TAX,
        "setup_fee": config.SETUP_FEE,
        "gank_rate": config.DEFAULT_GANK_RATE,
        "city_risk": config.CITY_RISK_MULTIPLIER,
        "buy_mode": config.DEFAULT_BUY_MODE,
        "sell_mode": config.DEFAULT_SELL_MODE,
    }


@app.get("/api/flips")
def api_flips(
    window: str = Query("day"),
    buy_mode: str = Query("instant"),
    sell_mode: str = Query("instant"),
    min_profit: int | None = None,
    min_profit_pct: float | None = None,
    min_bm_volume: float | None = None,
    gank_rate: float | None = Query(None, ge=0.0, le=0.95),
    category: str | None = None,
    tier: int | None = None,
    enchant: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    buy_city: str | None = None,
    sort: str = Query("opportunity"),
    direction: str = Query("desc"),
    limit: int = Query(300, ge=1, le=2000),
    _=Depends(require_auth),
):
    _check_window(window)
    _check_mode("buy_mode", buy_mode)
    _check_mode("sell_mode", sell_mode)
    return _an().flips(
        window=window, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, min_profit_pct=min_profit_pct,
        min_bm_volume=min_bm_volume, gank_rate=gank_rate,
        category=category, tier=tier, enchant=enchant, quality=quality,
        search=search, buy_city=buy_city, sort=sort, direction=direction, limit=limit,
    )


@app.get("/api/cities")
def api_cities(
    window: str = Query("day"),
    buy_mode: str = Query("instant"),
    sell_mode: str = Query("instant"),
    min_profit: int | None = None,
    min_profit_pct: float | None = None,
    min_bm_volume: float | None = None,
    gank_rate: float | None = Query(None, ge=0.0, le=0.95),
    category: str | None = None,
    tier: int | None = None,
    enchant: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    top_n: int = Query(100, ge=1, le=1000),
    _=Depends(require_auth),
):
    _check_window(window)
    _check_mode("buy_mode", buy_mode)
    _check_mode("sell_mode", sell_mode)
    return _an().city_ranking(
        window=window, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, min_profit_pct=min_profit_pct,
        min_bm_volume=min_bm_volume, gank_rate=gank_rate,
        category=category, tier=tier, enchant=enchant, quality=quality,
        search=search, top_n=top_n,
    )


@app.get("/api/plan")
def api_plan(
    budget: int = Query(..., ge=0),
    city: str | None = None,
    window: str = Query("day"),
    buy_mode: str = Query("instant"),
    sell_mode: str = Query("instant"),
    min_profit: int | None = None,
    min_profit_pct: float | None = None,
    min_bm_volume: float | None = None,
    gank_rate: float | None = Query(None, ge=0.0, le=0.95),
    category: str | None = None,
    tier: int | None = None,
    enchant: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    _=Depends(require_auth),
):
    _check_window(window)
    _check_mode("buy_mode", buy_mode)
    _check_mode("sell_mode", sell_mode)
    return _an().plan(
        budget=budget, city=city, window=window, buy_mode=buy_mode, sell_mode=sell_mode,
        min_profit=min_profit, min_profit_pct=min_profit_pct,
        min_bm_volume=min_bm_volume, gank_rate=gank_rate,
        category=category, tier=tier, enchant=enchant, quality=quality, search=search,
    )


# kept for backwards compatibility with the previous release's URL
@app.get("/api/recommend", include_in_schema=False)
def api_recommend(budget: int = Query(..., ge=0), city: str | None = None,
                  window: str = Query("day"), _=Depends(require_auth)):
    _check_window(window)
    return _an().plan(budget=budget, city=city, window=window)


@app.get("/api/stats")
def api_stats(
    window: str = Query("week"),
    category: str | None = None,
    tier: int | None = None,
    enchant: int | None = None,
    quality: int | None = None,
    search: str | None = None,
    sort: str = Query("bm_volume"),
    direction: str = Query("desc"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _=Depends(require_auth),
):
    _check_window(window)
    return _an().stats_table(
        window=window, category=category, tier=tier, enchant=enchant, quality=quality,
        search=search, sort=sort, direction=direction, limit=limit, offset=offset,
    )


@app.post("/api/refresh")
def api_refresh(_=Depends(require_auth)):
    if _manager is None:
        raise HTTPException(status_code=503, detail="Service starting up")
    _manager.refresh_all_async()
    return JSONResponse({"status": "refresh started"})


@app.get("/api/health")
def api_health():
    return {"status": "ok", "version": config.VERSION}


# -- static frontend --------------------------------------------------------

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
_ASSET_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}


@app.get("/", response_class=HTMLResponse)
def index():
    path = config.FRONTEND_DIR / "index.html"
    if not path.exists():
        return JSONResponse(
            {"error": f"frontend not found at {config.FRONTEND_DIR}"}, status_code=500
        )
    # Stamp the build version into the asset URLs so browsers cannot serve a
    # stale app.js after a deploy. This is what made the old release need a
    # manual Ctrl+F5 (and look like an infinite loading spinner).
    html = path.read_text(encoding="utf-8").replace("__VERSION__", config.VERSION)
    return HTMLResponse(html, headers=_NO_CACHE)


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#12141c"/>'
        '<path d="M32 12l16 9v22l-16 9-16-9V21z" fill="none" stroke="#e8c265" '
        'stroke-width="4" stroke-linejoin="round"/>'
        '<circle cx="32" cy="32" r="6" fill="#e8c265"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return Response(status_code=204)


@app.get("/{asset:path}", include_in_schema=False)
def static_asset(asset: str, request: Request):
    """Serve frontend files, refusing anything outside the frontend directory."""
    if not asset or asset.startswith("api/"):
        raise HTTPException(status_code=404, detail="not found")
    root = config.FRONTEND_DIR.resolve()
    try:
        target = (root / asset).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = _ASSET_TYPES.get(target.suffix.lower())
    return FileResponse(target, media_type=media, headers=_NO_CACHE)
