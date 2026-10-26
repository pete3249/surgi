"""Finished plans are cached as JSON files, one per (surgery, model).

Serverless filesystems are temporary, so on those hosts the cache may reset between
requests. For durable caching, swap read_cache/write_cache for Redis or a database.
These functions do small blocking file reads; that's fine here, but a busier app
would wrap them in `asyncio.to_thread(...)`.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .models import Plan


def cache_file(surgery: str, settings: Settings) -> Path:
    key = f"v4|{surgery.lower()}|{settings.llm_model}"
    return settings.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def read_cache(surgery: str, settings: Settings) -> Plan | None:
    try:
        plan = Plan.model_validate_json(cache_file(surgery, settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # missing file, bad JSON, or wrong shape: all just a cache miss
        return None
    age = datetime.now(timezone.utc) - plan.generated_at
    return plan if age < timedelta(days=settings.cache_days) else None


def write_cache(surgery: str, settings: Settings, plan: Plan) -> None:
    try:
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file(surgery, settings).write_text(
            plan.model_dump_json(by_alias=True, exclude_none=True), encoding="utf-8"
        )
    except OSError:
        pass  # a read-only filesystem is fine, we just don't cache
