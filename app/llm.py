"""Talking to the LLM: streaming, timeouts, retries and fallback models.

Any OpenAI-compatible API works (OpenRouter by default). Free models fail, get rate
limited and get retired without warning, so we keep a chain of models and give up on a
slow one early instead of making the patient wait.
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from .config import Settings
from .errors import HttpError
from .guardrails import clean

logger = logging.getLogger("surgery.llm")
Log = Callable[..., None]


# Small exception classes let the code below say *what* went wrong with `except ThinkingTimeout`
# instead of checking error names the way the JavaScript version did.
class IdleTimeout(Exception):
    """The connection went silent for too long."""


class ThinkingTimeout(Exception):
    """The model kept "thinking" (reasoning tokens) without ever starting its answer."""


class ProviderError(Exception):
    """The API sent an error inside the stream."""


@dataclass
class LlmOutput:
    content: str
    finish: str  # finish_reason from the API: "stop", "length", ...
    actual: str  # the model that really answered (routers like openrouter/free pick one)


def _info(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# ---------------- reading the answer ----------------


async def _next_line(lines: AsyncIterator[str]) -> str | None:
    """Next line of the stream, or None when the stream has ended."""
    try:
        return await anext(lines)
    except StopAsyncIteration:
        return None


async def read_content(resp: httpx.Response, cfg: Settings, log: Log, t0: float) -> LlmOutput:
    """Read the model's answer, either as a stream (preferred, so we can log progress) or plain JSON."""
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        data = json.loads(await resp.aread())
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason") or ""
        log(f"LLM output: {len(content)} chars, finish_reason={finish or '?'}")
        return LlmOutput(content, finish, data.get("model") or "")

    lines = resp.aiter_lines()
    parts: list[str] = []
    reasoning_chars = 0
    finish = actual = ""
    first_at: float | None = None  # when the first answer token arrived
    last_log = time.monotonic()
    idle, first_limit = cfg.llm_idle_seconds, cfg.llm_first_token_seconds

    def thinking() -> ThinkingTimeout:
        secs = round(time.monotonic() - t0)
        return ThinkingTimeout(f"still thinking after {secs}s ({reasoning_chars} chars of reasoning, no answer yet)")

    while True:
        elapsed = time.monotonic() - t0
        if first_at is None:
            if first_limit > 0 and elapsed > first_limit:
                raise thinking()
            if cfg.llm_max_reasoning_chars > 0 and reasoning_chars > cfg.llm_max_reasoning_chars:
                raise thinking()
        # Don't wait for the next line longer than the idle limit, or (before the answer starts)
        # longer than what's left of the first-token limit.
        if first_at is not None or first_limit <= 0:
            wait = idle
        else:
            wait = min(idle, max(0.25, first_limit - elapsed))
        try:
            line = await asyncio.wait_for(_next_line(lines), wait)
        except asyncio.TimeoutError:
            if first_at is None and first_limit > 0 and time.monotonic() - t0 >= first_limit - 0.05:
                raise thinking() from None
            raise IdleTimeout(f"no data for {idle:g}s") from None
        if line is None:
            break

        line = line.strip()
        if not line.startswith("data:"):  # ": OPENROUTER PROCESSING" keepalive comments
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else None
            raise ProviderError("provider error: " + (msg or json.dumps(err)))
        if chunk.get("model"):
            actual = chunk["model"]
        choice = (chunk.get("choices") or [{}])[0] or {}
        delta = choice.get("delta") or {}
        if delta.get("content"):
            if first_at is None:
                first_at = time.monotonic()
                log(f"first answer token after {round((first_at - t0) * 1000)}ms (reasoning so far: {reasoning_chars} chars)")
            parts.append(delta["content"])
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if reasoning:
            reasoning_chars += len(reasoning)
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        if time.monotonic() - last_log > 10:
            last_log = time.monotonic()
            log(f"still waiting... {round(time.monotonic() - t0)}s: answer {sum(map(len, parts))} chars, reasoning {reasoning_chars} chars")

    content = "".join(parts)
    log(f"LLM output: {len(content)} chars, reasoning {reasoning_chars} chars, finish_reason={finish or '?'}, total {round((time.monotonic() - t0) * 1000)}ms")
    return LlmOutput(content, finish, actual)


def extract_json(text: str) -> dict:
    """Models sometimes wrap the JSON in chatter ("Here you go: {...}"), so take first { to last }."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no json")
    return json.loads(text[a : b + 1])


# ---------------- one model ----------------

_RETRY = object()  # "the API rejected an optional setting; change it and try again"


def _provider_message(raw: str) -> str:
    """The provider's own explanation of an error (bad model id, token limit...), if it sent one."""
    try:
        j = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(j, dict):
        err = j.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
        if j.get("message"):
            return str(j["message"])
    return raw


async def _request(
    client: httpx.AsyncClient, cfg: Settings, body: dict, messages: list[dict], log: Log, attempt: int, t0: float
) -> LlmOutput | object:
    log(
        f"LLM request #{attempt} -> {cfg.llm_base_url} model={cfg.llm_model} "
        f"promptChars={sum(len(m['content']) for m in messages)} jsonMode={'response_format' in body} "
        f"reasoning={json.dumps(body['reasoning']) if 'reasoning' in body else 'default'} "
        f"maxTokens={body['max_tokens']} timeout={cfg.llm_timeout_seconds:g}s"
    )
    model = cfg.llm_model
    try:
        async with client.stream(
            "POST",
            f"{cfg.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}", "X-Title": "Ready for Surgery"},
            json=body,
            timeout=httpx.Timeout(cfg.llm_timeout_seconds),
        ) as resp:
            status = resp.status_code
            log(f"LLM responded HTTP {status} after {round((time.monotonic() - t0) * 1000)}ms")

            if status == 400 and ("reasoning" in body or "response_format" in body):
                return _RETRY  # some models reject these optional settings
            if status in (401, 403):
                raise HttpError(502, "The server's AI key was rejected. Check LLM_API_KEY.", fatal=True)
            if status == 402:
                raise HttpError(502, "The AI account is out of credit.", fatal=True)
            if status == 429:
                raise HttpError(429, "The AI service is busy (free models have limits). Please try again in a minute.")
            if not resp.is_success:
                raw = (await resp.aread()).decode("utf-8", "replace")
                msg = clean(_provider_message(raw), 240)
                logger.error("LLM error %s (model %s): %s", status, model, raw[:600])
                raise HttpError(502, f"The AI service returned an error ({status})" + (f": {msg}" if msg else "."))

            try:
                return await read_content(resp, cfg, log, t0)
            except Exception as e:  # noqa: BLE001 - turn every read failure into a friendly HttpError
                log(f"LLM answer FAILED after {round((time.monotonic() - t0) * 1000)}ms:", _info(e))
                if isinstance(e, ThinkingTimeout):
                    raise HttpError(502, f"The AI model ({model}) was thinking too long without answering, so we moved on.") from e
                if isinstance(e, (IdleTimeout, httpx.TimeoutException)):  # never retry a timeout
                    raise HttpError(502, f"The AI model ({model}) took too long to answer. Please try again, or try a faster model.") from e
                if isinstance(e, ProviderError):
                    raise HttpError(502, "The AI service reported a problem: " + clean(str(e), 200)) from e
                raise HttpError(502, "The AI connection dropped before the answer finished. Please try again.") from e
    except (httpx.HTTPError, asyncio.TimeoutError) as e:  # failed before any answer arrived
        log(f"LLM request FAILED after {round((time.monotonic() - t0) * 1000)}ms:", _info(e))
        if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
            raise HttpError(502, f"The AI model ({model}) took too long to answer. Please try again, or try a faster model.") from e
        raise HttpError(502, "The AI service didn't respond. Please try again.") from e


async def call_model(messages: list[dict], cfg: Settings, client: httpx.AsyncClient, log: Log) -> tuple[dict, str]:
    """Ask ONE model (cfg.llm_model). Returns (parsed JSON, the model that actually answered)."""
    body: dict = {
        "model": cfg.llm_model,
        "temperature": 0.2,
        "max_tokens": cfg.llm_max_tokens,
        "stream": True,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    # Reasoning settings to try, in order. Some models refuse "off" (thinking is built in),
    # so an HTTP 400 moves to the next one, and finally drops the setting.
    eff = cfg.llm_reasoning_effort
    if eff in ("none", ""):
        ladder: list[dict] = []
    elif eff == "off":
        ladder = [{"enabled": False}, {"effort": "low"}]
    else:
        ladder = [{"effort": eff}]
    if ladder:
        body["reasoning"] = ladder.pop(0)

    attempt = 0
    while attempt < 2:
        attempt += 1
        t0 = time.monotonic()
        try:
            # wait_for puts a hard cap on the whole request, answer included.
            result = await asyncio.wait_for(
                _request(client, cfg, body, messages, log, attempt, t0), cfg.llm_timeout_seconds
            )
        except asyncio.TimeoutError:
            log(f"LLM request FAILED after {round((time.monotonic() - t0) * 1000)}ms:", "TimeoutError")
            raise HttpError(502, f"The AI model ({cfg.llm_model}) took too long to answer. Please try again, or try a faster model.") from None

        if result is _RETRY:
            if "reasoning" in body and ladder:
                body["reasoning"] = ladder.pop(0)
                log(f"400 received, retrying with reasoning={json.dumps(body['reasoning'])}")
            elif "reasoning" in body:
                del body["reasoning"]
                log("400 received, retrying without the reasoning setting")
            else:
                del body["response_format"]
                log("400 received, retrying without JSON mode")
            attempt -= 1  # changing a setting doesn't use up one of our two tries
            continue

        out: LlmOutput = result  # type: ignore[assignment]
        if out.finish == "length":
            # Ran out of tokens (often spent on "thinking"). Repeating the same request would fail the same way.
            log(f"LLM hit the {body['max_tokens']}-token limit with {len(out.content)} answer chars, so not retrying this model")
            raise HttpError(502, f"The AI model ({cfg.llm_model}) ran out of room before finishing its answer (it spent its budget thinking).")
        if not out.content.strip():
            # Reasoning only, no answer: asking the same model again just burns time.
            log("LLM finished with thinking only and no answer, moving on to the next model")
            raise HttpError(502, f"The AI model ({cfg.llm_model}) spent its time thinking and gave no answer.")
        try:
            return extract_json(out.content), out.actual
        except ValueError as e:
            log(f"LLM output unusable ({e}), length={len(out.content)}. First 300 chars:", json.dumps(out.content[:300]))
    raise HttpError(502, "The AI reply wasn't usable. Please try again.")


# ---------------- the chain of models ----------------

_cooling: dict[str, float] = {}  # model -> time.monotonic() until which it is skipped after failing


def reset_cooldowns() -> None:
    _cooling.clear()


async def call_llm(
    messages: list[dict], cfg: Settings, client: httpx.AsyncClient, log: Log = lambda *a, **k: None
) -> tuple[dict, str]:
    """Try the main model, then each fallback. A model that just failed is skipped for a while.

    Returns (parsed JSON, name of the model that answered).
    """
    chain = cfg.model_chain
    now = time.monotonic()
    ready = [m for m in chain if _cooling.get(m, 0) <= now]
    models = ready or chain  # if everything is cooling down, try them anyway
    if len(models) < len(chain):
        log(f"skipping recently failing model(s): {', '.join(m for m in chain if m not in models)}")

    failures: list[tuple[str, HttpError]] = []
    started = time.monotonic()
    for i, model in enumerate(models):
        remaining = cfg.llm_total_seconds - (time.monotonic() - started)
        if i > 0 and remaining < 8:
            log(f"out of time budget ({cfg.llm_total_seconds:g}s), not trying more models")
            break
        # model_copy(update=...) = the same settings, with a few fields changed for this attempt
        per_model = cfg.model_copy(update={"llm_model": model, "llm_timeout_seconds": max(5, min(cfg.llm_timeout_seconds, remaining))})
        try:
            out, actual = await call_model(messages, per_model, client, log)
        except HttpError as e:
            failures.append((model, e))
            log(f"model {model} failed: {e.message}")
            if e.fatal:
                raise  # bad key or no credit: other models won't help
            if cfg.llm_cooldown_seconds > 0:
                secs = min(cfg.llm_cooldown_seconds, 120) if e.status == 429 or "thinking" in e.message else cfg.llm_cooldown_seconds
                _cooling[model] = time.monotonic() + secs
                log(f"will skip {model} for the next {secs:g}s")
            if i + 1 < len(models):
                log(f"trying next model: {models[i + 1]}")
            continue
        _cooling.pop(model, None)
        used = actual or model
        if used != model:
            log(f"answered by {used} (routed from {model})")
        return out, used

    if len(failures) == 1:
        raise failures[0][1]
    all_busy = all(e.status == 429 for _, e in failures)
    detail = "; ".join(f"{m}: {clean(e.message, 90)}" for m, e in failures)
    raise HttpError(
        429 if all_busy else 502,
        f"None of the AI models could answer right now ({detail}). Free models are often busy or retired, so please try again in a minute.",
    )
