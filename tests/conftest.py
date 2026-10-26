"""pytest fixtures: setup that tests ask for by naming it as a parameter."""
import pytest

from app.config import Settings
from app.llm import reset_cooldowns
from app.ratelimit import rate_limiter
from tests.helpers import Calls, make_client


@pytest.fixture
def settings(tmp_path) -> Settings:
    # _env_file=None: never read the real .env while testing. Each test gets its own empty cache folder.
    return Settings(
        _env_file=None,
        llm_api_key="test-key",
        llm_model="test/model",
        llm_base_url="https://llm.test/v1",
        cache_dir=tmp_path / "cache",
        plans_dir=tmp_path / "plans",  # never the real plans/ or drafts/
        drafts_dir=tmp_path / "drafts",
        llm_cooldown_seconds=0,
    )


@pytest.fixture(autouse=True)  # autouse = applied to every test without being requested
def clean_global_state():
    reset_cooldowns()
    rate_limiter.reset()
    yield


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def client(calls):
    """Default fake: the model replies with MODEL_OUT."""
    return make_client(calls=calls)
