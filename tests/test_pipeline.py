"""End-to-end tests of build_plan() with the LLM mocked (no network, no key).

`async def test_...` works because pyproject.toml sets asyncio_mode = "auto".
"""
import time
from datetime import datetime, timezone

import pytest

from app.cache import cache_file
from app.core import CORE_IDS
from app.errors import HttpError
from app.models import Plan
from app.planner import build_plan
from tests.helpers import (
    MODEL_OUT,
    SHORT_OUT,
    Calls,
    EndlessReasoningStream,
    endless_reasoning,
    error_reply,
    json_reply,
    make_client,
    sse_raw,
    sse_reply,
)

import httpx


# ---------- a normal plan ----------


async def test_plan_applies_every_guardrail(settings, client, calls):
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)

    texts = [i.text for i in plan.items]
    for bad in ["Take 800 mg ibuprofen daily", "Stop eating 8 hours before surgery", "No food after midnight",
                "Bag in the wrong phase", "Call 911 for chest pain", "Bad category"]:
        assert bad not in texts, f"guardrail should drop: {bad}"
    assert not any(t.startswith("Ask about something very specific") for t in texts), "over-long items are dropped, not cut"
    assert texts.count("Rent an ice machine") == 1, "dedupes"

    pt = next(i for i in plan.items if i.text.startswith("Begin physical therapy"))
    assert pt.d == 7, "non-integer day falls back to default"
    assert "<" not in pt.detail, "html stripped"
    assert next(i for i in plan.items if i.text == "Borrow a cane").d == -3, "a 'pre' item is always before surgery"
    assert next(i for i in plan.items if i.text == "Rent an ice machine").tier == "need"
    assert "<" not in plan.expect
    assert not any(i.id in CORE_IDS for i in plan.items), "plan holds only surgery-specific items"
    assert "Pack comfy slip-on shoes" not in texts, "near-duplicate of a general item is dropped"
    assert plan.stats.dropped >= 7 and plan.stats.duplicates >= 1
    assert len({i.id for i in plan.items}) == len(plan.items), "unique ids"
    assert len(plan.items) >= 8

    system, user = (m["content"] for m in calls.llm[-1]["messages"])
    assert "ALREADY COVERED" in system and "<surgery>Hip arthroscopy</surgery>" in user
    assert len(calls.llm) == 1


# ---------- cache ----------


async def test_cache_hit_and_refresh(settings, client, calls):
    await build_plan("Hip arthroscopy", settings=settings, client=client)
    again = await build_plan("hip arthroscopy", settings=settings, client=client)
    assert again.from_cache is True
    assert len(calls.llm) == 1

    await build_plan("Hip arthroscopy", refresh=True, settings=settings, client=client)
    assert len(calls.llm) == 2, "refresh bypasses cache"


async def test_different_surgeries_are_cached_separately(settings, client, calls):
    await build_plan("Hip arthroscopy", settings=settings, client=client)
    await build_plan("Knee arthroscopy", settings=settings, client=client)
    assert len(calls.llm) == 2


async def test_stale_cache_is_ignored(settings, client, calls):
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    old = plan.model_copy(update={"generated_at": datetime(2020, 1, 1, tzinfo=timezone.utc)})
    cache_file("Hip arthroscopy", settings).write_text(old.model_dump_json(by_alias=True), encoding="utf-8")
    await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert len(calls.llm) == 2


async def test_corrupt_cache_file_is_a_miss(settings, client, calls):
    path = cache_file("Hip arthroscopy", settings)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert plan.from_cache is False


async def test_reads_cache_files_in_the_camelcase_json_format(settings, client, calls):
    """Cache files use camelCase keys; files from older versions (with extra fields) still load."""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cache_json = (
        '{"source":"ai","part":"all","surgery":"Hip arthroscopy","expect":"ok",'
        '"items":[{"id":"pt-x","phase":"pre","cat":"pt","d":-7,"text":"Do a thing","detail":"d","src":[1]}],'
        f'"sources":[],"grounded":false,"model":"m","generatedAt":"{now}","stats":{{"dropped":0,"duplicates":0}},"fromCache":false}}'
    )
    path = cache_file("Hip arthroscopy", settings)
    path.parent.mkdir(parents=True)
    path.write_text(cache_json, encoding="utf-8")
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert plan.from_cache is True and plan.items[0].text == "Do a thing"
    assert calls.llm == []


# ---------- retry when the reply has too few usable items ----------


async def test_retries_once_when_too_few_items_survive(settings, calls):
    replies = iter([SHORT_OUT, MODEL_OUT])
    client = make_client(lambda b: json_reply(next(replies)), calls=calls)
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert len(calls.llm) == 2 and len(plan.items) >= 8


async def test_a_short_but_valid_answer_is_accepted(settings, calls):
    """A simple surgery (an appendectomy) has few surgery-specific items. That is not an error."""
    five = {"expect": "Fine.", "items": MODEL_OUT["items"][:2] + MODEL_OUT["items"][4:7]}
    client = make_client(lambda b: json_reply(five), calls=calls)
    plan = await build_plan("Appendectomy", settings=settings, client=client)
    assert len(plan.items) == 5 and len(calls.llm) == 1, "no retry for a short, valid list"


async def test_gives_up_after_the_retry(settings, calls):
    client = make_client(lambda b: json_reply(SHORT_OUT), calls=calls)
    with pytest.raises(HttpError) as e:
        await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert e.value.status == 502 and "too few" in e.value.message
    assert len(calls.llm) == 2
    assert not cache_file("Hip arthroscopy", settings).exists(), "a failed plan is not cached"


async def test_not_a_surgery_reply_is_an_error_not_an_empty_plan(settings):
    client = make_client(lambda b: json_reply({"expect": "", "items": []}))
    err = await status_of(build_plan("Asdfgh qwerty", settings=settings, client=client))
    assert err.status == 502


# ---------- errors ----------


async def status_of(coro) -> HttpError:
    with pytest.raises(HttpError) as e:
        await coro
    return e.value


async def test_missing_key_is_503(settings, client):
    err = await status_of(build_plan("Spinal fusion", settings=settings.model_copy(update={"llm_api_key": ""}), client=client))
    assert err.status == 503


async def test_provider_errors_map_to_friendly_statuses(settings):
    assert (await status_of(build_plan("Spinal fusion", settings=settings, client=make_client(llm_status=429)))).status == 429
    assert (await status_of(build_plan("Spinal fusion", settings=settings, client=make_client(llm_status=401)))).status == 502
    err = await status_of(build_plan("Spinal fusion", settings=settings, client=make_client(llm_status=400)))
    assert err.status == 502 and "not a valid model ID" in err.message, "400 shows the provider's reason"


async def test_bad_input_is_400(settings, client):
    assert (await status_of(build_plan("hi", settings=settings, client=client))).status == 400


async def test_unusable_model_reply(settings, calls):
    client = make_client(lambda b: httpx.Response(200, json={"choices": [{"message": {"content": "sorry, no json here"}}]}), calls=calls)
    err = await status_of(build_plan("Spinal fusion", settings=settings, client=client))
    assert err.status == 502 and "wasn't usable" in err.message
    assert len(calls.llm) == 2, "an unusable reply is retried once"


# ---------- streaming, timeouts, fallback ----------


async def test_streamed_answer_split_across_chunks(settings, calls):
    client = make_client(lambda b: sse_reply(MODEL_OUT), calls=calls)
    plan = await build_plan("Rotator cuff repair", settings=settings, client=client)
    assert any(i.text.startswith("Start prehab") for i in plan.items)
    assert plan.model == "test/model"


async def test_fallback_model_used_after_primary_fails(settings, calls):
    client = make_client(lambda b: error_reply(429) if b["model"] == "test/model" else sse_reply(MODEL_OUT), calls=calls)
    plan = await build_plan("Carpal tunnel release", settings=settings.model_copy(update={"llm_fallback_model": "fb/model"}), client=client)
    assert plan.model == "fb/model"
    assert calls.llm_models == ["test/model", "fb/model"]


async def test_a_timeout_is_not_retried_against_the_same_model(settings, calls):
    def timeout(body):
        raise httpx.ReadTimeout("The operation timed out")

    err = await status_of(build_plan("Cataract surgery", settings=settings, client=make_client(timeout, calls=calls)))
    assert err.status == 502 and "took too long" in err.message
    assert len(calls.llm) == 1


async def test_bad_key_skips_the_fallback_model(settings, calls):
    client = make_client(lambda b: error_reply(401), calls=calls)
    err = await status_of(build_plan("Tonsillectomy", settings=settings.model_copy(update={"llm_fallback_model": "fb/model"}), client=client))
    assert err.status == 502 and "key was rejected" in err.message
    assert len(calls.llm) == 1


async def test_the_failure_chain_seen_in_real_logs(settings, calls):
    """Provider 500, then a retired model (404), then the router answers."""

    def handler(b):
        if b["model"] == "test/model":
            return error_reply(502, '{"error": {"message": "Provider returned error", "code": 502}}')
        if b["model"] == "old/model:free":
            return error_reply(404, '{"error": {"message": "The :free variant has been retired", "code": 404}}')
        return sse_reply(MODEL_OUT, 3, 37, "xingchen-agi/xing4.0-29b-a4b:free")

    s = settings.model_copy(update={"llm_fallback_model": "old/model:free, openrouter/free"})
    plan = await build_plan("Bunion surgery", settings=s, client=make_client(handler, calls=calls))
    assert calls.llm_models == ["test/model", "old/model:free", "openrouter/free"]
    assert plan.model == "xingchen-agi/xing4.0-29b-a4b:free", "plan records which model actually answered"


async def test_when_every_model_fails_the_message_names_them(settings):
    s = settings.model_copy(update={"llm_fallback_model": "b/model"})
    err = await status_of(build_plan("Appendectomy", settings=s, client=make_client(lambda b: error_reply(502))))
    assert err.status == 502 and "test/model" in err.message and "b/model" in err.message and "try again" in err.message
    err = await status_of(build_plan("Hysterectomy", settings=s, client=make_client(lambda b: error_reply(429))))
    assert err.status == 429


async def test_out_of_token_thinker_is_not_retried(settings, calls):
    thinker = (
        'data: {"choices": [{"delta": {"reasoning": "hmm hmm"}}]}\n\n'
        'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}\n\ndata: [DONE]\n\n'
    )
    client = make_client(lambda b: sse_raw(thinker) if b["model"] == "test/model" else sse_reply(MODEL_OUT), calls=calls)
    plan = await build_plan("Spinal fusion", settings=settings.model_copy(update={"llm_fallback_model": "fb/model"}), client=client)
    assert calls.llm_models == ["test/model", "fb/model"], "length-truncated model is not retried"
    assert plan.model == "fb/model"
    assert calls.llm[0]["reasoning"] == {"enabled": False}, "reasoning is asked to be off by default"
    assert calls.llm[0]["max_tokens"] == 8000


async def test_model_that_rejects_the_reasoning_setting(settings):
    seen = []

    def handler(b):
        seen.append("reasoning" in b)
        return error_reply(400) if "reasoning" in b else sse_reply(MODEL_OUT)

    s = settings.model_copy(update={"llm_reasoning_effort": "off"})
    await build_plan("Knee replacement revision", settings=s, client=make_client(handler))
    assert seen == [True, True, False], "with 'off', a 400 tries reasoning=low, then drops the setting"


async def test_reasoning_none_sends_no_setting(settings, client, calls):
    s = settings.model_copy(update={"llm_reasoning_effort": "none"})
    await build_plan("Knee replacement revision", settings=s, client=client)
    assert "reasoning" not in calls.llm[0]


async def test_time_budget_stops_the_fallback_chain(settings, calls):
    s = settings.model_copy(update={"llm_fallback_model": "b/model", "llm_total_seconds": 5})
    err = await status_of(build_plan("Cleft lip repair", settings=s, client=make_client(lambda b: error_reply(429), calls=calls)))
    assert err.status == 429
    assert len(calls.llm) == 1, "no fallback once the time budget is gone"


async def test_model_that_never_answers_is_abandoned_after_the_first_token_limit(settings, calls):
    client = make_client(lambda b: endless_reasoning(0.08, "pondering... ") if b["model"] == "test/model" else sse_reply(MODEL_OUT), calls=calls)
    s = settings.model_copy(update={"llm_fallback_model": "fb/model", "llm_first_token_seconds": 0.4, "llm_timeout_seconds": 30})
    t0 = time.monotonic()
    plan = await build_plan("Meniscus transplant", settings=s, client=client)
    assert plan.model == "fb/model"
    assert calls.llm_models == ["test/model", "fb/model"]
    assert time.monotonic() - t0 < 3, "gave up on the thinker long before the 30s timeout"


async def test_runaway_thinking_gives_up_early(settings):
    client = make_client(lambda b: endless_reasoning(0.005, "x" * 200))
    s = settings.model_copy(update={"llm_max_reasoning_chars": 1000})
    t0 = time.monotonic()
    err = await status_of(build_plan("Meniscus repair", settings=s, client=client))
    assert "thinking too long" in err.message
    assert time.monotonic() - t0 < 3


async def test_stream_that_stalls_hits_the_idle_timeout(settings):
    class Stall(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "{"}}]}\n\n'
            import asyncio

            await asyncio.sleep(30)
            yield b""

    client = make_client(lambda b: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stall()))
    s = settings.model_copy(update={"llm_idle_seconds": 0.3})
    err = await status_of(build_plan("Spinal fusion", settings=s, client=client))
    assert "took too long" in err.message


async def test_error_inside_the_stream(settings):
    body = 'data: {"error": {"message": "upstream exploded"}}\n\n'
    err = await status_of(build_plan("Spinal fusion", settings=settings, client=make_client(lambda b: sse_raw(body))))
    assert err.status == 502 and "upstream exploded" in err.message


async def test_recently_failed_model_is_skipped_on_the_next_request(settings, calls):
    handler = lambda b: error_reply(502) if b["model"] == "cd/a" else sse_reply(MODEL_OUT)  # noqa: E731
    s = settings.model_copy(update={"llm_model": "cd/a", "llm_fallback_model": "cd/b", "llm_cooldown_seconds": 60})
    client = make_client(handler, calls=calls)
    await build_plan("Rhinoplasty", settings=s, client=client)
    await build_plan("Sinus surgery", settings=s, client=client)
    assert calls.llm_models == ["cd/a", "cd/b", "cd/b"], "second request skips the model that just failed"


# ---------- the JSON the browser gets ----------


async def test_plan_json_matches_what_the_browser_expects(settings, client):
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    data = plan.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert {"source", "surgery", "expect", "items", "model", "generatedAt", "stats", "fromCache"} <= data.keys()
    assert not ({"sources", "grounded", "part"} & data.keys()), "no research fields any more"
    assert data["generatedAt"].endswith("Z") and len(data["generatedAt"]) == 24, "2026-10-23T14:53:00.123Z"
    assert Plan.model_validate(data).items == plan.items, "round-trips through JSON"


# ---------- reviewed (approved) plans ----------


def approved_plan(settings, slug="hip-arthroscopy", text="Reviewed item"):
    from tests.test_procedures import make_plan, save
    from app.models import PlanItem

    items = [PlanItem(id=f"pt-pre-{i}", phase="pre", cat="pt", d=-7, text=f"{text} {i}", detail="Detail.") for i in range(5)]
    save(settings, slug, plan=make_plan(items=items))


async def test_an_approved_plan_is_served_without_any_ai_call(settings, client, calls):
    approved_plan(settings)
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert plan.reviewed is True and plan.items[0].text == "Reviewed item 0"
    assert calls.llm == []


async def test_an_approved_plan_needs_no_api_key_and_ignores_refresh(settings, client, calls):
    approved_plan(settings, "knee-replacement")
    no_key = settings.model_copy(update={"llm_api_key": ""})
    for typed in ("Total knee replacement", "TKA", "knee replacement"):
        plan = await build_plan(typed, refresh=True, settings=no_key, client=client)
        assert plan.reviewed is True
    assert calls.llm == []


async def test_a_supported_surgery_without_an_approved_file_is_written_live_and_not_reviewed(settings, client, calls):
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert plan.reviewed is False and len(calls.llm) == 1


async def test_a_broken_approved_file_falls_back_to_live(settings, client, calls):
    from tests.test_procedures import save

    save(settings, "hip-arthroscopy", text="{ not json")
    plan = await build_plan("Hip arthroscopy", settings=settings, client=client)
    assert plan.reviewed is False and len(calls.llm) == 1


async def test_other_surgeries_stay_live_even_when_plans_exist(settings, client, calls):
    approved_plan(settings)
    plan = await build_plan("Appendectomy", settings=settings, client=client)
    assert plan.reviewed is False and len(calls.llm) == 1
