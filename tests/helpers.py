"""Shared fakes for the tests: the LLM is mocked, so no network or key is needed.

httpx lets you give a client a `MockTransport(handler)`. Every request the app makes goes to
`handler(request)`, which returns a canned httpx.Response. That keeps the tests offline and fast.
"""
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

# What the fake model writes: some good items plus one of every kind of problem the guardrails catch.
MODEL_OUT = {
    "expect": "Most people do well. <b>Your surgeon</b> gives the real timeline.",
    "items": [
        {"phase": "pre", "cat": "pt", "d": -28, "text": "Start prehab with a physical therapist", "detail": "A therapist can build strength first."},
        {"phase": "pre", "cat": "pt", "d": -21, "text": "Practice walking with crutches", "detail": "A therapist can show you."},
        {"phase": "pre", "cat": "equip", "d": -14, "text": "Rent an ice machine", "detail": "Keeps swelling down.", "tier": "need"},
        {"phase": "pre", "cat": "equip", "d": -14, "text": "Rent an ice machine", "detail": "duplicate", "tier": "need"},  # same id -> dropped
        {"phase": "pre", "cat": "equip", "d": -10, "text": "Borrow a shower chair", "detail": "Makes bathing safer.", "tier": "nice"},
        {"phase": "pre", "cat": "recovery", "d": -28, "text": "Ask exactly which version of the surgery you are having", "detail": "It can change your plan."},
        {"phase": "pre", "cat": "home", "d": -7, "text": "Practice the stairs at home", "detail": "Do it with a helper nearby."},
        {"phase": "pre", "cat": "bag", "d": -1, "text": "Pack your brace and crutches", "detail": "Ask your surgeon which to bring."},
        {"phase": "pre", "cat": "bag", "d": -1, "text": "Pack comfy slip-on shoes", "detail": "Easy on and off."},  # near-copy of a general item -> duplicate
        {"phase": "pre", "cat": "equip", "d": 3, "text": "Borrow a cane", "detail": "Some people use one at first."},  # wrong sign -> flipped to -3
        {"phase": "post", "cat": "pt", "d": 2.5, "text": "Begin physical therapy as directed", "detail": "<script>x</script>Confirm timing."},  # bad day -> default
        {"phase": "post", "cat": "pt", "d": 21, "text": "Ask your therapist about milestones", "detail": "Goals vary by person."},
        {"phase": "post", "cat": "home", "d": 1, "text": "Sleep with a pillow between your knees", "detail": "Ask which positions to avoid."},
        {"phase": "post", "cat": "recovery", "d": 14, "text": "Ask which movements to avoid", "detail": "It varies by surgeon."},
        {"phase": "pre", "cat": "meds", "d": -10, "text": "Take 800 mg ibuprofen daily", "detail": "Dose example"},  # category and dose -> dropped
        {"phase": "pre", "cat": "home", "d": -1, "text": "Stop eating 8 hours before surgery", "detail": "Fasting hours"},  # fasting hours -> dropped
        {"phase": "pre", "cat": "home", "d": -1, "text": "No food after midnight", "detail": ""},  # fasting cut-off -> dropped
        {"phase": "post", "cat": "bag", "d": 1, "text": "Bag in the wrong phase", "detail": ""},  # bad post cat -> dropped
        {"phase": "post", "cat": "recovery", "d": 0, "text": "Call 911 for chest pain", "detail": "Warning signs."},  # app owns this -> dropped
        {"phase": "post", "cat": "nonsense", "d": 3, "text": "Bad category", "detail": ""},  # dropped
        {"phase": "post", "cat": "recovery", "d": 5, "text": "Ask about " + "something very specific " * 6, "detail": "Too long."},  # over the length limit -> dropped
    ],
}

# A reply with too few good items.
SHORT_OUT = {"expect": "Fine.", "items": MODEL_OUT["items"][:3]}


@dataclass
class Calls:
    """What the fake servers saw, so tests can assert on it."""

    llm: list[dict] = field(default_factory=list)  # request bodies sent to the LLM

    @property
    def llm_models(self) -> list[str]:
        return [b["model"] for b in self.llm]


def json_reply(obj: dict) -> httpx.Response:
    """A normal (non-streaming) chat completion."""
    return httpx.Response(200, json={"choices": [{"message": {"content": "Here you go:\n" + json.dumps(obj)}}]})


def error_reply(status: int, text: str = "{}") -> httpx.Response:
    return httpx.Response(status, text=text)


class ChunkedStream(httpx.AsyncByteStream):
    """Delivers bytes in small pieces, splitting lines mid-way, like a real network does."""

    def __init__(self, data: bytes, chunk: int = 37):
        self.pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)]

    async def __aiter__(self):
        for p in self.pieces:
            yield p


def sse_reply(obj: dict, pieces: int = 3, chunk: int = 37, model_name: str | None = None) -> httpx.Response:
    """A streaming (server-sent events) chat completion, like OpenRouter sends."""
    text = json.dumps(obj)
    n = -(-len(text) // pieces)  # ceiling division
    parts = [text[i : i + n] for i in range(0, len(text), n)]
    extra = {"model": model_name} if model_name else {}
    out = ": OPENROUTER PROCESSING\n\n"
    out += f"data: {json.dumps({'choices': [{'delta': {'reasoning': 'thinking...'}}]})}\n\n"
    for p in parts:
        out += f"data: {json.dumps({**extra, 'choices': [{'delta': {'content': p}}]})}\n\n"
    out += f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkedStream(out.encode(), chunk))


def sse_raw(text: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkedStream(text.encode(), 1000))


class EndlessReasoningStream(httpx.AsyncByteStream):
    """A model that thinks forever and never answers."""

    def __init__(self, delay: float, text: str):
        self.delay, self.text = delay, text

    async def __aiter__(self):
        while True:
            await asyncio.sleep(self.delay)
            yield f"data: {json.dumps({'choices': [{'delta': {'reasoning': self.text}}]})}\n\n".encode()


def endless_reasoning(delay: float, text: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=EndlessReasoningStream(delay, text))


def make_client(
    llm: Callable[[dict], httpx.Response] | None = None,
    *,
    llm_status: int | None = None,
    calls: Calls | None = None,
) -> httpx.AsyncClient:
    """An httpx client wired to a fake LLM. Any other URL fails the test.

    `llm(body)` decides each LLM answer; by default the model replies with MODEL_OUT.
    """
    calls = calls if calls is not None else Calls()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/chat/completions" in url:
            body = json.loads(request.content)
            calls.llm.append(body)
            if llm is not None:
                return llm(body)
            if llm_status:
                return error_reply(llm_status, json.dumps({"error": {"message": "bad-model is not a valid model ID", "code": llm_status}}))
            return json_reply(MODEL_OUT)
        raise AssertionError("unexpected url " + url)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
