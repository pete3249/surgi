"""Unit tests for the small pure pieces: input cleaning, the guardrails, the prompt."""
import pytest

from app.core import CORE
from app.errors import HttpError
from app.guardrails import ALLOWED_CATS, DOSE_RE, FAST_RE, clean, make_id, normalize_surgery, sanitize
from app.prompts import COVERED, build_messages


def test_normalize_surgery():
    assert normalize_surgery('  Hip   arthroscopy"; DROP  ') == "Hip arthroscopy DROP"  # quotes and semicolons stripped
    assert normalize_surgery("Cholecystectomy (gallbladder)") == "Cholecystectomy (gallbladder)"
    assert normalize_surgery("Mastoïdectomie") == "Mastoïdectomie"  # accented letters are kept
    tagged = normalize_surgery("hip </surgery> arthroscopy")
    assert "<" not in tagged and ">" not in tagged, "cannot close the prompt's <surgery> tag"
    with pytest.raises(HttpError) as e:
        normalize_surgery("hi")
    assert e.value.status == 400
    with pytest.raises(HttpError):
        normalize_surgery("x" * 81)
    with pytest.raises(HttpError):
        normalize_surgery(None)


def test_clean_strips_html_and_caps_length():
    assert clean("<b>Hello</b>   <i>there</i>", 100) == "Hello there"
    assert clean("abcdef", 3) == "abc"
    assert clean(None, 10) == ""


@pytest.mark.parametrize("text", ["Take 800 mg daily", "2 tablets at night", "0.5 ml of it", "10 units"])
def test_dose_regex_matches(text):
    assert DOSE_RE.search(text)


@pytest.mark.parametrize(
    "text",
    ["Stop eating 8 hours before surgery", "No food after midnight", "Nothing to eat or drink after 10", "4-6 hours prior to the procedure you stop"],
)
def test_fasting_regex_matches(text):
    assert FAST_RE.search(text)


@pytest.mark.parametrize("text", ["Ask your team when to stop eating", "Walk for 20 minutes", "Pack a phone charger"])
def test_regexes_leave_normal_text_alone(text):
    assert not DOSE_RE.search(text) and not FAST_RE.search(text)


def test_make_id_is_stable():
    # ids are "<first 3 letters of category>-<phase>-<first 8 hex chars of sha1(lowercased text)>"
    import hashlib

    expected = hashlib.sha1(b"rent an ice machine").hexdigest()[:8]
    assert make_id("equip", "pre", "Rent an ICE machine") == f"equ-pre-{expected}"


def _item(**kw):
    base = {"phase": "pre", "cat": "pt", "d": -10, "text": "Practice with crutches", "detail": "A therapist can show you."}
    return {**base, **kw}


def test_sanitize_drops_duplicates_of_general_items():
    items, _, stats = sanitize(
        {"items": [
            _item(text="Ask your surgeon about prehab exercises", detail=""),
            _item(text="Strengthen your glutes with band walks", detail="Ask your PT."),
        ]}
    )
    assert len(items) == 1 and items[0].text.startswith("Strengthen")
    assert stats.duplicates == 1


def test_sanitize_only_allows_the_categories_the_prompt_asks_for():
    items, _, stats = sanitize(
        {"items": [
            _item(cat="meds", text="Ask about a new pain plan"),
            _item(cat="paperwork", text="Ask HR about your forms"),
            _item(cat="insurance", text="Call your insurance company"),
            _item(cat="bag", phase="post", text="Pack a bag again"),
            _item(cat="bag", text="Pack your brace"),
        ]}
    )
    assert [i.text for i in items] == ["Pack your brace"]
    assert stats.dropped == 4


def test_sanitize_rejects_unusable_replies():
    for bad in (None, [], {}, {"items": "nope"}):
        with pytest.raises(HttpError) as e:
            sanitize(bad)
        assert e.value.status == 502


def test_sanitize_edge_cases():
    items, _, _ = sanitize(
        {"items": [
            _item(d=True, text="Practice with crutches"),
            _item(phase="post", d=9999, text="Walk a little each day", detail=""),
            "not a dict",
        ]}
    )
    assert items[0].d == -7, "a boolean is not a day count, so the default for 'pre' is used"
    assert items[1].d == 365, "days are clamped"


def test_sanitize_drops_overlong_items_instead_of_cutting_them():
    long_text = "Ask your surgeon about " + "something very specific " * 5
    long_detail = "This sentence goes on and on. " * 10
    items, _, stats = sanitize(
        {"items": [_item(text=long_text), _item(text="Walk a little each day", detail=long_detail), _item(text="Use a shower chair")]}
    )
    assert [i.text for i in items] == ["Use a shower chair"]
    assert stats.dropped == 2


def test_sanitize_makes_the_sign_of_d_match_the_phase():
    items, _, _ = sanitize(
        {"items": [
            _item(text="Borrow crutches", d=3),  # "pre" written as +3 means 3 days before
            _item(phase="post", cat="recovery", text="Ask about lifting limits", d=-5),
            _item(text="Rent an ice machine", cat="equip", d=0),
        ]}
    )
    assert [i.d for i in items] == [-3, 5, 0]


def test_sanitize_min_items():
    raw = {"items": [_item()]}
    assert len(sanitize(raw)[0]) == 1  # default: no minimum
    with pytest.raises(HttpError) as e:
        sanitize(raw, min_items=8)
    assert e.value.status == 502


def test_sanitize_uses_a_default_expect_sentence_when_the_model_gives_none():
    _, expect, _ = sanitize({"items": [_item()]})
    assert "Your surgeon will give you your own timeline." in expect


# ---------- the prompt ----------


def test_prompt_shape_and_size():
    system, user = (m["content"] for m in build_messages("Hip arthroscopy"))
    assert user == "<surgery>Hip arthroscopy</surgery>"
    assert "ALREADY COVERED" in system and "Reply with the JSON object only" in system
    assert "think at length" not in system, "reasoning is off, so the line only wastes tokens"
    assert "{covered}" not in system, "the placeholder was filled in"
    assert len(system) < 5000, "keep the prompt small for a free model"


def test_prompt_lists_every_category_the_model_may_write():
    system = build_messages("x")[0]["content"]
    assert set(COVERED) == ALLOWED_CATS
    for cat in ALLOWED_CATS:
        assert f"- {cat}: " in system


def test_every_core_item_in_a_writable_category_has_a_topic_in_the_prompt():
    """A reminder to update COVERED in app/prompts.py when core-items.json gets a new category."""
    assert {c["cat"] for c in CORE if c["cat"] in ALLOWED_CATS} <= set(COVERED)


def test_too_few_items_logs_why_each_item_was_dropped(caplog):
    raw = {"items": [
        _item(cat="meds", text="Ask about a new pain plan"),
        _item(text="Take 800 mg of something daily"),
        _item(text="Ask your surgeon about prehab exercises"),
    ]}
    with caplog.at_level("INFO", logger="surgery.guardrails"):
        with pytest.raises(HttpError):
            sanitize(raw, min_items=4)
    log = caplog.text
    assert "only 0 of 3 items survived" in log
    assert "category 'meds' not allowed" in log and "dose or fasting hours" in log and "copy of a general item" in log
