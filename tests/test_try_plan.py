"""app.try_plan: the command-line runner, with a fake model (no network)."""
import pytest

from app.errors import HttpError
from app.planner import generate_plan
from app.try_plan import main, render
from tests.helpers import Calls, MODEL_OUT, SHORT_OUT, json_reply, make_client


async def test_generate_plan_runs_without_the_cache_and_renders(settings):
    calls = Calls()
    client = make_client(calls=calls)
    plan = await generate_plan("Hip arthroscopy", settings, client)
    assert len(plan.items) >= 8 and len(calls.llm) == 1
    assert not settings.cache_dir.exists(), "generate_plan never writes the cache"
    text = render(plan, 1.5)
    assert "Hip arthroscopy" in text and "PRE" in text and "POST" in text and "kept of" in text


async def test_retries_once_then_gives_up(settings):
    calls = Calls()
    client = make_client(lambda b: json_reply(SHORT_OUT), calls=calls)
    with pytest.raises(HttpError) as e:
        await generate_plan("Hip arthroscopy", settings, client)
    assert "too few" in e.value.message and len(calls.llm) == 2


def test_prompt_flag_prints_the_prompt_and_calls_nothing(capsys):
    assert main(["hip arthroscopy", "--prompt"]) == 0
    out = capsys.readouterr().out
    assert "ALREADY COVERED" in out and "<surgery>hip arthroscopy</surgery>" in out
