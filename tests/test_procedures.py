"""The supported surgeries, matching typed names, and loading approved plans."""
import json
from datetime import datetime, timezone

import pytest

from app.config import ROOT
from app.guardrails import sanitize
from app.models import Plan, PlanItem, PlanStats
from app.procedures import PROCEDURES, count_approved, get_procedure, load_approved, load_procedures, match_procedure


def make_plan(**changes) -> Plan:
    items = [PlanItem(id=f"pt-pre-{i}", phase="pre", cat="pt", d=-7, text=f"Do surgery specific thing {i}", detail="Detail.") for i in range(5)]
    base = dict(surgery="Hip arthroscopy", expect="Fine. Your surgeon will give you your own timeline.", items=items, model="m",
                generated_at=datetime.now(timezone.utc), stats=PlanStats())
    return Plan(**{**base, **changes})


def save(settings, slug, plan: Plan | None = None, text: str | None = None):
    settings.plans_dir.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else (plan or make_plan()).model_dump_json(by_alias=True, exclude_none=True)
    (settings.plans_dir / f"{slug}.json").write_text(body, encoding="utf-8")


def test_the_three_supported_surgeries():
    assert [p.slug for p in PROCEDURES] == ["hip-arthroscopy", "shoulder-arthroscopy", "knee-replacement"]


@pytest.mark.parametrize(
    "typed,slug",
    [
        ("Hip Arthroscopy", "hip-arthroscopy"),
        ("  hip   arthroscopy!! ", "hip-arthroscopy"),
        ("hip scope", "hip-arthroscopy"),
        ("shoulder arthroscopy", "shoulder-arthroscopy"),
        ("Arthroscopic shoulder surgery", "shoulder-arthroscopy"),
        ("knee replacement", "knee-replacement"),
        ("Total Knee Replacement", "knee-replacement"),
        ("TKA", "knee-replacement"),
        ("total knee arthroplasty", "knee-replacement"),
    ],
)
def test_names_patients_type_find_the_right_surgery(typed, slug):
    assert match_procedure(typed).slug == slug


@pytest.mark.parametrize("typed", ["appendectomy", "partial knee replacement", "rotator cuff repair", "hip replacement", "knee arthroscopy", "hip"])
def test_other_surgeries_are_not_matched(typed):
    assert match_procedure(typed) is None


def test_get_procedure_by_slug():
    assert get_procedure("hip-arthroscopy").name == "Hip arthroscopy"
    assert get_procedure("nope") is None


def test_two_surgeries_cannot_share_an_alias(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([
        {"slug": "a", "name": "Thing A", "aliases": ["shared name"]},
        {"slug": "b", "name": "Thing B", "aliases": ["Shared  Name"]},
    ]))
    with pytest.raises(ValueError, match="alias of both"):
        load_procedures(path)


def test_slugs_must_be_safe_file_names(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([{"slug": "../evil", "name": "X", "aliases": []}]))
    with pytest.raises(ValueError):
        load_procedures(path)


def test_load_approved(settings):
    p = get_procedure("hip-arthroscopy")
    assert load_approved(p, settings) is None, "no file yet"
    save(settings, "hip-arthroscopy")
    plan = load_approved(p, settings)
    assert plan.reviewed is True and len(plan.items) == 5, "being in plans/ makes it reviewed"
    assert count_approved(settings) == 1


def test_a_broken_or_empty_file_is_ignored_not_a_crash(settings):
    p = get_procedure("hip-arthroscopy")
    save(settings, "hip-arthroscopy", text="{ not json")
    assert load_approved(p, settings) is None
    save(settings, "hip-arthroscopy", text='{"surgery": "x"}')
    assert load_approved(p, settings) is None
    save(settings, "hip-arthroscopy", plan=make_plan(items=[]))
    assert load_approved(p, settings) is None
    assert count_approved(settings) == 0


def test_every_shipped_plan_is_valid_and_passes_the_guardrails():
    """Runs on the real plans/ folder, so a bad hand edit is caught before a deploy."""
    plans_dir = ROOT / "plans"
    known = {p.slug for p in PROCEDURES}
    for path in sorted(plans_dir.glob("*.json")):
        assert path.stem in known, f"{path.name} is not listed in procedures.json"
        plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
        assert plan.reviewed is True, f"{path.name} should be marked reviewed"
        raw = {"expect": plan.expect, "items": [i.model_dump(exclude_none=True) for i in plan.items]}
        items, _, stats = sanitize(raw, min_items=4)
        assert len(items) == len(plan.items) and stats.duplicates == 0, f"{path.name}: the guardrails would remove items"
