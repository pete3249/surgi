"""The web app. Run it with:  python -m app   (or:  uvicorn app.main:app --reload)

Routes
    POST /api/plan        build the surgery-specific part of the checklist
    GET  /api/health      is the server up, which model, is the key set
    GET  /, /index.html, /core-items.json   the front end
    GET  /docs            automatic interactive API docs (FastAPI gives you this for free)
"""
import logging
import platform
import time
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ROOT, Settings, get_settings
from .core import CORE
from .errors import HttpError
from .models import Plan, PlanRequest
from .planner import build_plan
from .procedures import PROCEDURES, count_approved
from .ratelimit import rate_limiter

BUILD = "2026-10-23-python-1"
MAX_BODY_BYTES = 2048

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("surgery.web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup (before `yield`) and once at shutdown (after it)."""
    settings = get_settings()
    # One shared HTTP client for the whole app reuses connections instead of opening new ones every time.
    async with httpx.AsyncClient() as client:
        app.state.client = client
        logger.info("Ready for Surgery (build %s) | python %s", BUILD, platform.python_version())
        logger.info("model chain: %s", "  ->  ".join(settings.model_chain))
        if not settings.llm_api_key:
            logger.warning("no LLM_API_KEY set, so plan building will return an error")
        yield


app = FastAPI(title="Ready for Surgery", lifespan=lifespan)


# ---------- dependencies: FastAPI passes these into routes that ask for them ----------


def get_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.client


SettingsDep = Annotated[Settings, Depends(get_settings)]
ClientDep = Annotated[httpx.AsyncClient, Depends(get_client)]


# ---------- errors and headers ----------


@app.exception_handler(HttpError)
async def handle_http_error(request: Request, exc: HttpError):
    logger.error("%s error %s: %s", request.url.path, exc.status, exc.message)
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(StarletteHTTPException)
async def handle_not_found_etc(request: Request, exc: StarletteHTTPException):
    messages = {404: "Not found", 405: "Use POST."}
    return JSONResponse({"error": messages.get(exc.status_code, str(exc.detail))}, status_code=exc.status_code)


@app.middleware("http")
async def headers_and_logging(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    if request.url.path == "/api/plan":
        logger.info("%s %s -> %s in %dms", request.method, request.url.path, response.status_code, (time.perf_counter() - t0) * 1000)
    return response


# ---------- the API ----------


async def read_limited_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read the request body, refusing anything bigger than `limit` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HttpError(413, "Request too large.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HttpError(413, "Request too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "local"


@app.post("/api/plan", response_model=Plan, response_model_exclude_none=True)
async def create_plan(request: Request, settings: SettingsDep, client: ClientDep):
    try:
        body = await read_limited_body(request)
        try:
            req = PlanRequest.model_validate_json(body or b"{}")
        except ValidationError:
            raise HttpError(400, "Bad request.") from None
        rate_limiter.check(client_ip(request, settings), settings.rate_limit_per_hour)
        return await build_plan(req.surgery, req.refresh, settings=settings, client=client)
    except HttpError:
        raise  # already friendly; the handler above turns it into JSON
    except Exception:
        logger.exception("unexpected error in /api/plan")
        raise HttpError(500, "Something went wrong. Please try again.") from None


@app.get("/api/health")
async def health(settings: SettingsDep):
    return {
        "ok": True,
        "build": BUILD,
        "python": platform.python_version(),
        "model": settings.llm_model,
        "baseUrl": settings.llm_base_url,
        "llmKeyConfigured": bool(settings.llm_api_key),
        "coreItems": len(CORE),  # 0 would mean core-items.json didn't ship with the deploy
        "procedures": len(PROCEDURES),
        # Approved plan files that load: 3 of 3 means all are shipped. Fewer on a deployed server
        # usually means plans/ didn't ship, so those surgeries are written live and marked "not reviewed".
        "reviewedPlans": count_approved(settings),
    }


# ---------- the front end ----------
# Only these exact files are served. Never mount the whole project folder as static files:
# that would hand out .env (your API key) and the source code.


def add_static_route(url_path: str, filename: str, media_type: str) -> None:
    # A helper function (instead of writing this inline in a loop) gives each route its own
    # copy of `filename`. Closures in a loop all see the loop variable's *last* value.
    async def serve():
        return FileResponse(ROOT / filename, media_type=media_type)

    app.get(url_path, include_in_schema=False)(serve)


add_static_route("/", "index.html", "text/html; charset=utf-8")
add_static_route("/index.html", "index.html", "text/html; charset=utf-8")
add_static_route("/core-items.json", "core-items.json", "application/json")
