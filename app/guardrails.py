"""Cleans user input and validates everything the model wrote.

The rules (also in the README):
  * items with medicine doses or fasting hours/cut-offs are dropped
  * safety-critical items (911 / warning signs) come only from core-items.json
  * near-duplicates of the general checklist are dropped
  * output is schema-checked, HTML-stripped, de-duplicated; items over the length limits are dropped
"""
import hashlib
import logging
import re
import unicodedata

from .core import CORE
from .errors import HttpError
from .models import PlanItem, PlanStats

logger = logging.getLogger("surgery.guardrails")

# ---------------- input ----------------

_ALLOWED_PUNCTUATION = set("-(),'/")


def normalize_surgery(value: object) -> str:
    """Keep letters, numbers, spaces and a few punctuation marks; collapse whitespace."""
    s = unicodedata.normalize("NFKC", "" if value is None else str(value))
    # unicodedata.category gives "Lu", "Ll", "Nd"...: the first letter says Letter / Number / etc.
    s = "".join(ch for ch in s if ch.isspace() or unicodedata.category(ch)[0] in "LN" or ch in _ALLOWED_PUNCTUATION)
    s = " ".join(s.split())
    if not 3 <= len(s) <= 80:
        raise HttpError(400, "Please enter the name of your surgery (3 to 80 characters).")
    return s


def clean(value: object, max_len: int) -> str:
    """Strip HTML tags, collapse whitespace, trim, and cap the length."""
    s = "" if value is None else str(value)
    s = re.sub(r"<[^>]*>", "", s)
    return re.sub(r"\s+", " ", s).strip()[:max_len]


# ---------------- model output ----------------

# Doses, schedules, and specific fasting cut-offs are the surgical team's job, so the item is dropped.
DOSE_RE = re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|µg|ml|g|iu|units?|tablets?|pills?|capsules?)\b", re.I)
FAST_RE = re.compile(
    "|".join(
        [
            r"\b(\d+|one|two|three|four|five|six|eight|twelve)(\s?(-|to)\s?\d+)?\s?(hours?|hrs?|h)\b[^.]{0,60}\b(before|prior to)\b[^.]{0,40}\b(surgery|anesthesia|procedure)\b",
            r"\b(no|stop|avoid)\s+(eating|food|drinking|fluids?|liquids?|water|solids?)\b[^.]{0,40}\b(after|at|past|by)\s+(midnight|\d)",
            r"\bnothing (to eat or drink|by mouth)\s+after\s+(midnight|\d)",
        ]
    ),
    re.I,
)


# The categories the model may write (the prompt says the same). Everything else, such as insurance,
# paperwork, medicines and fasting, comes only from core-items.json. Before surgery any of these is
# fine; after surgery "bag" makes no sense.
ALLOWED_CATS = {"pt", "equip", "home", "bag", "recovery"}
POST_CATS = {"recovery", "pt", "equip", "home"}

# Length limits. The prompt asks for text < 80 and detail < 200 characters, but models count
# characters badly, so the hard limits leave some room. An item over a limit is DROPPED rather than
# cut short, because a truncated sentence ("...ask your surgeon about whet") looks broken.
MAX_TEXT = 100
MAX_DETAIL = 240


def make_id(cat: str, phase: str, text: str) -> str:
    # Ids must stay stable: the browser stores the patient's check marks by item id.
    digest = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:8]
    return f"{cat[:3]}-{phase}-{digest}"


_STOP = {"your", "with", "that", "this", "from", "have", "will", "before", "after", "about", "when", "what", "into", "them", "they", "their", "make", "sure", "also"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) >= 4 and w not in _STOP}


def duplicates_core(phase: str, cat: str, text: str) -> bool:
    """True when a model item is basically a general item the app already shows
    (same phase and category, heavy word overlap)."""
    t = _tokens(text)
    if len(t) < 2:
        return False
    for c in CORE:
        if c["phase"] != phase or c["cat"] != cat:
            continue
        u = _tokens(c["text"])
        if len(u) < 2:
            continue
        if len(t & u) / min(len(t), len(u)) >= 0.7:  # `&` on sets = intersection
            return True
    return False


def _as_int(v: object, *, allow_numeric_strings: bool = False) -> int | None:
    """int(v) if v is a whole number, else None. (bool is a subclass of int in Python, so exclude it.)"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and allow_numeric_strings:
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def sanitize(raw: object, *, min_items: int = 0) -> tuple[list[PlanItem], str, PlanStats]:
    """Validate the model's JSON. Returns (items, expect sentence, stats).

    `min_items`: when fewer items survive the checks, raise a 502 so the caller can retry or tell the
    patient to try again. The default (0) means no minimum. When that happens the reason each item
    was dropped is logged, so a thin answer can be understood.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise HttpError(502, "The AI reply wasn't usable. Please try again.")
    stats = PlanStats()
    seen: set[str] = set()
    items: list[PlanItem] = []
    why: list[str] = []  # one line per dropped item: "reason: text"

    def drop(reason: str, text: object = "") -> None:
        stats.dropped += 1
        why.append(f"{reason}: {str(text)[:60]}")

    for x in raw["items"]:
        if not isinstance(x, dict):
            drop("not an object", x)
            continue
        phase, cat = x.get("phase"), x.get("cat")
        if phase not in ("pre", "post"):
            drop(f"bad phase {phase!r}", x.get("text"))
            continue
        if cat not in ALLOWED_CATS:
            drop(f"category {cat!r} not allowed", x.get("text"))
            continue
        if phase == "post" and cat not in POST_CATS:
            drop(f"{cat!r} not allowed after surgery", x.get("text"))
            continue
        # clean() with a large cap only strips HTML and whitespace; length is judged below, not cut
        text, detail = clean(x.get("text"), 1000), clean(x.get("detail"), 1000)
        if len(text) < 4:
            drop("text too short", text)
            continue
        if len(text) > MAX_TEXT or len(detail) > MAX_DETAIL:
            drop(f"too long ({len(text)}/{len(detail)} chars)", text)
            continue
        both = f"{text} {detail}"
        if DOSE_RE.search(both) or FAST_RE.search(both):
            drop("dose or fasting hours", text)
            continue
        # the app owns the warning-signs item
        if re.search(r"\b911\b", both) or re.search(r"warning signs?|when to call", text, re.I):
            drop("warning signs", text)
            continue
        if duplicates_core(phase, cat, text):
            stats.duplicates += 1
            why.append(f"copy of a general item: {text[:60]}")
            continue
        item_id = make_id(cat, phase, text)
        if item_id in seen:
            drop("repeat", text)
            continue
        seen.add(item_id)

        d = _as_int(x.get("d"))
        if d is None:
            d = -7 if phase == "pre" else 7
        else:
            # The sign must match the phase. Models often write "3" for "3 days before", so a wrong
            # sign is flipped instead of dropping the item ("pre" is never after surgery, "post" never before).
            d = -abs(d) if phase == "pre" else abs(d)
        d = max(-120, min(365, d))

        item = PlanItem(id=item_id, phase=phase, cat=cat, d=d, text=text, detail=detail)
        if cat == "equip" and x.get("tier") in ("need", "nice"):
            item.tier = x["tier"]
        items.append(item)
        if len(items) >= 30:
            break

    if len(items) < min_items:
        logger.info("only %d of %d items survived (minimum %d). Dropped: %s", len(items), len(raw["items"]), min_items, "; ".join(why) or "none")
        raise HttpError(502, "The AI reply had too few usable items. Please try again.")

    expect = clean(raw.get("expect"), 420) or (
        "Most people recover well from routine surgery with good preparation and support. "
        "Your surgeon will give you your own timeline."
    )
    return items, expect, stats
