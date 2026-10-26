"""The general checklist (insurance, FMLA, packing, warning signs...).

It is vetted by hand, identical for every surgery, and shown instantly. The AI only
adds surgery-specific items on top. We load it here so the AI is told what is already
covered and so near-duplicates can be dropped.
"""
import json

from .config import ROOT

CORE_PATH = ROOT / "core-items.json"

# A list of plain dicts, e.g. {"id": "ins-auth", "phase": "pre", "cat": "insurance", "d": -35, ...}
# If the file is missing this raises at startup, which is what we want: fail loudly, early.
CORE: list[dict] = json.loads(CORE_PATH.read_text(encoding="utf-8"))
CORE_IDS: set[str] = {c["id"] for c in CORE}
