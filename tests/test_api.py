"""Tests of the web layer (routes, error format, static files) using FastAPI's TestClient."""
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, get_settings
from app.main import app, get_client


@pytest.fixture
def make_api(settings, client):
    """Returns a function that builds a TestClient, optionally with changed settings."""
    clients = []

    def build(**changes) -> TestClient:
        s = settings.model_copy(update=changes)
        app.dependency_overrides[get_settings] = lambda: s  # swap the real dependencies for test ones
        app.dependency_overrides[get_client] = lambda: client
        c = TestClient(app)
        c.__enter__()  # runs the app's startup (lifespan)
        clients.append(c)
        return c

    yield build
    for c in clients:
        c.__exit__(None, None, None)
    app.dependency_overrides.clear()


@pytest.fixture
def api(make_api):
    return make_api()


def post(api, body=None, **kw):
    return api.post("/api/plan", json=body if body is not None else {"surgery": "Hip arthroscopy"}, **kw)


def test_health(api):
    r = api.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["llmKeyConfigured"] is True and data["model"] == "test/model"
    assert data["coreItems"] > 0, "core-items.json loaded"
    assert data["procedures"] == 3 and data["reviewedPlans"] == 0, "no approved plan files in the test folder"


def test_plan_success_and_response_shape(api):
    r = post(api, {"surgery": "Hip arthroscopy", "refresh": True})
    assert r.status_code == 200
    data = r.json()
    assert data["fromCache"] is False and "grounded" not in data and "sources" not in data
    assert data["generatedAt"].endswith("Z")
    assert "null" not in r.text, "unset optional fields are left out, not sent as null"
    again = post(api, {"surgery": "Hip arthroscopy"}).json()
    assert again["fromCache"] is True


def test_an_old_page_that_still_sends_a_part_is_fine(api):
    assert post(api, {"surgery": "Hip arthroscopy", "part": "rehab"}).status_code == 200


def test_security_headers(api):
    r = api.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_bad_json_is_400_with_an_error_key(api):
    r = api.post("/api/plan", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json() == {"error": "Bad request."}


def test_wrong_types_are_400(api):
    assert post(api, {"surgery": 123}).status_code == 400
    assert post(api, {"surgery": "Hip arthroscopy", "refresh": "yes"}).status_code == 400


def test_empty_body_means_no_surgery(api):
    r = api.post("/api/plan", content=b"")
    assert r.status_code == 400 and "name of your surgery" in r.json()["error"]


def test_oversized_body_is_413(api):
    r = post(api, {"surgery": "x" * 5000})
    assert r.status_code == 413 and r.json() == {"error": "Request too large."}


def test_short_surgery_name_is_400(api):
    r = post(api, {"surgery": "hi"})
    assert r.status_code == 400 and "3 to 80 characters" in r.json()["error"]


def test_missing_key_is_503(make_api):
    r = post(make_api(llm_api_key=""))
    assert r.status_code == 503 and "LLM_API_KEY" in r.json()["error"]


def test_rate_limit_is_429(make_api):
    api = make_api(rate_limit_per_hour=2)
    assert [post(api).status_code for _ in range(3)] == [200, 200, 429]


def test_proxy_header_is_ignored_unless_trusted(make_api):
    api = make_api(rate_limit_per_hour=1)
    assert post(api, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert post(api, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429, "spoofed header didn't buy a fresh quota"


def test_proxy_header_is_used_when_trusted(make_api):
    api = make_api(rate_limit_per_hour=1, trust_proxy_headers=True)
    assert post(api, headers={"x-forwarded-for": "1.1.1.1, 10.0.0.1"}).status_code == 200
    assert post(api, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200
    assert post(api, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 429


def test_unknown_paths_and_wrong_methods_use_the_same_error_format(api):
    assert api.get("/nope").json() == {"error": "Not found"}
    r = api.get("/api/plan")
    assert r.status_code == 405 and r.json() == {"error": "Use POST."}


def test_only_the_front_end_files_are_served(api):
    assert api.get("/core-items.json").status_code == 200
    assert isinstance(api.get("/core-items.json").json(), list)
    # The important one: secrets and source code must never be downloadable.
    for path in ["/.env", "/.env.example", "/app/main.py", "/app/config.py", "/requirements.txt", "/../.env", "/%2e%2e/.env"]:
        assert api.get(path).status_code == 404, path


@pytest.mark.skipif(not (ROOT / "index.html").exists(), reason="front end not in this folder")
def test_front_end_is_served(api):
    for path in ["/", "/index.html"]:
        r = api.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


def test_an_approved_plan_comes_back_marked_reviewed(api, settings):
    from tests.test_pipeline import approved_plan

    approved_plan(settings)
    data = post(api, {"surgery": "hip arthroscopy"}).json()
    assert data["reviewed"] is True and data["items"][0]["text"].startswith("Reviewed item")
    assert api.get("/api/health").json()["reviewedPlans"] == 1
    assert post(api, {"surgery": "Appendectomy"}).json()["reviewed"] is False
