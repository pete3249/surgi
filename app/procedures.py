"""The surgeries that have a reviewed, pre-generated plan.

procedures.json lists them (slug, name, aliases). Each approved plan is a file, plans/<slug>.json,
made with `python -m app.make_plans` and read by a person before it was added. When a patient types
one of these surgeries (or an alias, ignoring case and punctuation) and its file exists, the app
returns the file with no AI call. Any other surgery, or one whose file is missing or broken, is
written live by the AI model and is not marked as reviewed.
"""
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Settings
from .models import Plan

logger = logging.getLogger("surgery.procedures")

PROCEDURES_PATH = ROOT / "procedures.json"


@dataclass(frozen=True)
class Procedure:
    slug: str
    name: str  # what the AI is told the surgery is called, and what is shown
    aliases: tuple[str, ...]


def normalize(text: str) -> str:
    """Lowercase, and turn anything that isn't a letter or digit into a single space."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def load_procedures(path: Path = PROCEDURES_PATH) -> list[Procedure]:
    procedures = [Procedure(e["slug"], e["name"], tuple(e["aliases"])) for e in json.loads(path.read_text(encoding="utf-8"))]
    # Fail loudly on a typo that would make two procedures answer to the same name.
    seen: dict[str, str] = {}
    slugs: set[str] = set()
    for p in procedures:
        if p.slug in slugs or not re.fullmatch(r"[a-z0-9-]+", p.slug):
            raise ValueError(f"bad or duplicate slug in procedures.json: {p.slug!r}")
        slugs.add(p.slug)
        for alias in {normalize(p.name), *map(normalize, p.aliases)}:
            if seen.setdefault(alias, p.slug) != p.slug:
                raise ValueError(f'"{alias}" is an alias of both {seen[alias]} and {p.slug}')
    return procedures


PROCEDURES: list[Procedure] = load_procedures()
_BY_ALIAS: dict[str, Procedure] = {normalize(a): p for p in PROCEDURES for a in (p.name, *p.aliases)}
_BY_SLUG: dict[str, Procedure] = {p.slug: p for p in PROCEDURES}


def match_procedure(surgery: str) -> Procedure | None:
    """The supported procedure a typed surgery name refers to, or None."""
    return _BY_ALIAS.get(normalize(surgery))


def get_procedure(slug: str) -> Procedure | None:
    return _BY_SLUG.get(slug)


def approved_path(slug: str, settings: Settings) -> Path:
    return settings.plans_dir / f"{slug}.json"


def load_approved(procedure: Procedure, settings: Settings) -> Plan | None:
    """The approved plan for a procedure, or None if there is no usable file (the caller then goes live)."""
    path = approved_path(procedure.slug, settings)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("can't read %s: %s", path, e)
        return None
    try:
        plan = Plan.model_validate_json(text)
    except ValueError as e:
        logger.warning("%s is not a valid plan, so it is ignored: %s", path, e)
        return None
    if not plan.items:
        logger.warning("%s has no items, so it is ignored", path)
        return None
    return plan.model_copy(update={"reviewed": True})  # being in plans/ is what makes a plan approved


def count_approved(settings: Settings) -> int:
    return sum(1 for p in PROCEDURES if load_approved(p, settings) is not None)
