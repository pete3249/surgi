"""build_plan(): approved plan, else cache, else LLM -> guardrails -> cache. The heart of the backend."""
import json
import logging
import secrets
import time
from datetime import datetime, timezone

import httpx

from .cache import read_cache, write_cache
from .config import Settings
from .errors import HttpError
from .guardrails import normalize_surgery, sanitize
from .llm import call_llm
from .models import Plan
from .procedures import load_approved, match_procedure
from .prompts import build_messages

logger = logging.getLogger("surgery.plan")

MIN_ITEMS = 4  # simple surgeries (an appendectomy) really do have only a handful of surgery-specific items
ATTEMPTS = 2  # one retry when the reply is unusable or has too few good items


def _logger(run_id: str):
    def L(msg: str, extra: object = None) -> None:
        suffix = "" if extra is None else " " + (extra if isinstance(extra, str) else json.dumps(extra))
        logger.info("[plan %s] %s%s", run_id, msg, suffix)

    return L


async def generate_plan(surgery: str, settings: Settings, client: httpx.AsyncClient, log=lambda *a, **k: None) -> Plan:
    """Ask the model and run the guardrails. No cache. Raises HttpError with a friendly message."""
    surgery = normalize_surgery(surgery)
    messages = build_messages(surgery)
    for attempt in range(1, ATTEMPTS + 1):
        raw, used_model = await call_llm(messages, settings, client, log)
        try:
            items, expect, stats = sanitize(raw, min_items=MIN_ITEMS)
        except HttpError as e:
            if attempt < ATTEMPTS:
                log(f"attempt {attempt}: {e.message} Trying once more.")
                continue
            raise
        return Plan(
            surgery=surgery,
            expect=expect,
            items=items,
            model=used_model,
            generated_at=datetime.now(timezone.utc),
            stats=stats,
        )
    raise AssertionError("unreachable")  # the loop always returns or raises


async def build_plan(
    surgery: str,
    refresh: bool = False,
    *,  # everything after this star must be passed by name: build_plan("x", settings=..., client=...)
    settings: Settings,
    client: httpx.AsyncClient,
) -> Plan:
    surgery = normalize_surgery(surgery)
    run_id = secrets.token_hex(2)  # short id so one request's log lines can be told apart
    t0 = time.monotonic()
    L = _logger(run_id)
    L(f'start surgery="{surgery}" refresh={refresh} model={settings.llm_model} key={"set" if settings.llm_api_key else "MISSING"}')

    # A supported surgery with an approved plan file is served as is: no AI call, no key needed, and
    # "refresh" changes nothing, because new drafts are made and approved with `python -m app.make_plans`.
    procedure = match_procedure(surgery)
    if procedure:
        approved = load_approved(procedure, settings)
        if approved:
            L(f"served the approved plan ({procedure.slug})")
            return approved

    if not refresh:
        cached = read_cache(surgery, settings)
        if cached:
            L("served from cache")
            return cached.model_copy(update={"from_cache": True})
    if not settings.llm_api_key:
        raise HttpError(503, "Plan building isn't set up on this server yet (missing LLM_API_KEY).")

    plan = await generate_plan(surgery, settings, client, L)
    L(
        f"plan ready: {len(plan.items)} surgery-specific items (dropped {plan.stats.dropped}, "
        f"duplicates of general items {plan.stats.duplicates}), total {round((time.monotonic() - t0) * 1000)}ms"
    )
    write_cache(surgery, settings, plan)
    return plan
