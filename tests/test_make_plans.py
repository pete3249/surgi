"""python -m app.make_plans: draft, read, approve. The AI model is faked."""
import json

import pytest

from app.make_plans import approve, main, make_drafts, review_markdown
from app.procedures import get_procedure, load_approved
from tests.helpers import Calls, MODEL_OUT, SHORT_OUT, json_reply, make_client

HIP = get_procedure("hip-arthroscopy")
KNEE = get_procedure("knee-replacement")


async def draft(settings, procedures=(HIP,), runs=1, force=False, client=None, calls=None):
    return await make_drafts(list(procedures), runs, force, False, settings, client or make_client(calls=calls))


async def test_draft_writes_json_to_edit_and_markdown_to_read(settings, capsys):
    assert await draft(settings) == 0
    assert (settings.drafts_dir / "hip-arthroscopy.json").exists()
    md = (settings.drafts_dir / "hip-arthroscopy.md").read_text(encoding="utf-8")
    assert "# Review: Hip arthroscopy" in md and "Practice walking with crutches" in md and "## What to check" in md
    assert "wrote  drafts/hip-arthroscopy.md" in capsys.readouterr().out
    assert not settings.plans_dir.exists(), "a draft is never shipped"


async def test_a_draft_is_not_overwritten_without_force(settings, capsys):
    calls = Calls()
    await draft(settings, calls=calls)
    path = settings.drafts_dir / "hip-arthroscopy.json"
    path.write_text('{"my": "edits"}', encoding="utf-8")
    await draft(settings, calls=calls)
    assert path.read_text(encoding="utf-8") == '{"my": "edits"}' and len(calls.llm) == 1
    assert "skip   hip-arthroscopy" in capsys.readouterr().out
    await draft(settings, force=True, calls=calls)
    assert path.read_text(encoding="utf-8") != '{"my": "edits"}' and len(calls.llm) == 2


async def test_several_runs_make_numbered_drafts(settings):
    await draft(settings, procedures=(HIP, KNEE), runs=2)
    names = sorted(f.name for f in settings.drafts_dir.glob("*.json"))
    assert names == ["hip-arthroscopy.run1.json", "hip-arthroscopy.run2.json", "knee-replacement.run1.json", "knee-replacement.run2.json"]


async def test_a_failed_draft_is_reported_and_others_continue(settings, capsys):
    replies = iter([SHORT_OUT, SHORT_OUT, MODEL_OUT])  # hip: both attempts too short; knee: fine
    client = make_client(lambda b: json_reply(next(replies)))
    assert await draft(settings, procedures=(HIP, KNEE), client=client) == 1
    out = capsys.readouterr().out
    assert "FAILED hip-arthroscopy" in out and "wrote  drafts/knee-replacement.md" in out


async def test_approve_copies_the_draft_into_plans_and_marks_it_reviewed(settings, capsys):
    await draft(settings)
    assert approve("hip-arthroscopy", None, False, settings) == 0
    plan = load_approved(HIP, settings)
    assert plan.reviewed is True and len(plan.items) >= 8
    assert "Commit plans/" in capsys.readouterr().out
    assert approve("hip-arthroscopy", None, False, settings) == 1, "an approved plan is not replaced by accident"
    assert "--force" in capsys.readouterr().out
    assert approve("hip-arthroscopy", None, True, settings) == 0


async def test_approve_checks_hand_edits_with_the_guardrails(settings, capsys):
    await draft(settings)
    path = settings.drafts_dir / "hip-arthroscopy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"].append({"id": "x", "phase": "pre", "cat": "pt", "d": -5, "text": "Take 800 mg of something", "detail": "A dose slipped in."})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert approve("hip-arthroscopy", None, False, settings) == 1
    assert "Take 800 mg of something" in capsys.readouterr().out
    assert load_approved(HIP, settings) is None, "nothing was approved"
    assert approve("hip-arthroscopy", None, True, settings) == 0, "--force approves it without the bad item"
    assert all("800 mg" not in i.text for i in load_approved(HIP, settings).items)


async def test_approve_a_numbered_draft_and_missing_or_broken_drafts(settings, capsys):
    assert approve("hip-arthroscopy", None, False, settings) == 1
    assert "No draft" in capsys.readouterr().out
    await draft(settings, runs=2)
    assert approve("hip-arthroscopy", 2, False, settings) == 0
    (settings.drafts_dir / "knee-replacement.json").parent.mkdir(exist_ok=True)
    (settings.drafts_dir / "knee-replacement.json").write_text("{ broken", encoding="utf-8")
    assert approve("knee-replacement", None, False, settings) == 1
    assert "not valid" in capsys.readouterr().out


def test_unknown_surgery_is_an_error(settings):
    with pytest.raises(SystemExit):
        approve("appendectomy", None, False, settings)


async def test_review_markdown_lists_items_by_phase_and_day(settings):
    await draft(settings)
    from app.models import Plan

    p = Plan.model_validate_json((settings.drafts_dir / "hip-arthroscopy.json").read_text(encoding="utf-8"))
    md = review_markdown(p)
    assert md.index("## Before surgery") < md.index("## After surgery")
    assert "`day -28 · pt`" in md and "[need]" in md


def test_list_command_shows_status(settings, capsys, monkeypatch):
    monkeypatch.setattr("app.make_plans.get_settings", lambda: settings)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "hip-arthroscopy" in out and "knee-replacement" in out and "shoulder-arthroscopy" in out
