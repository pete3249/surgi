# Learning Python with this project

## Tour of `app/`

| File | What it does |
|---|---|
| `main.py` | The FastAPI web app: routes, static files, error format |
| `config.py` | Settings from environment variables and `.env` (pydantic-settings) |
| `models.py` | The shapes of requests and responses, validated by pydantic |
| `planner.py` | `build_plan()`: cache, then LLM, then guardrails (with one retry), then cache |
| `llm.py` | Streaming LLM calls, timeouts, retries, fallback models |
| `prompts.py` | The prompt sent to the LLM, with the "already covered" topic list |
| `guardrails.py` | Input cleaning and validation of what the model wrote |
| `cache.py`, `ratelimit.py` | File cache of plans; per-IP rate limit |
| `core.py`, `errors.py` | Loads `core-items.json`; the `HttpError` exception |
| `procedures.py` | The surgeries with approved plans (`procedures.json`), name matching, loading `plans/*.json` |
| `make_plans.py` | Command line: draft, read and approve plans (`python -m app.make_plans`) |
| `try_plan.py` | Command line (`python -m app.try_plan "hip arthroscopy"`): try the prompt on one surgery |

A good reading order: `models.py`, `guardrails.py`, `planner.py`, then `llm.py` last (it's the trickiest).

## Python ideas, and where to see them

- **Virtual environments** (`.venv`): each project gets its own packages. `requirements.txt` lists them, and `pip install -r` installs the list.
- **Type hints** (`def build_plan(surgery: str, part: str | None = None) -> Plan`): Python doesn't enforce them, but editors use them, and pydantic and FastAPI turn them into validation. See `models.py`.
- **async / await** (`llm.py`, `planner.py`): a function declared `async def` must be `await`ed. Calling it without `await` gives you a coroutine object, not the result.
- **Context managers** (`async with client.stream(...) as resp:`): guarantee cleanup (closing the connection) even if an error happens. Compare with `try/finally`.
- **Dependency injection** (`Depends(get_settings)` in `main.py`): FastAPI passes things into your route. The tests swap them for fakes with `app.dependency_overrides`.
- **Keyword-only arguments** (the bare `*` in `build_plan`'s signature): forces `settings=...` to be named at the call site.
- **Comprehensions** (`[c for c in core if c["cat"] in WRITE_CATS]`): the Python way to map and filter. Set operations like `t & u` are in `guardrails.py`.
- **Command-line programs** (`argparse` in `try_plan.py`): `python -m app.try_plan "hip arthroscopy" --json` is parsed into `args.surgery` and `args.json`. Run `python -m app.try_plan --help` to see what it generates for free.
- **Pydantic models**: `Plan` in `models.py` validates data and converts it to and from JSON (camelCase for the browser, snake_case in Python).
- **pytest fixtures** (`tests/conftest.py`): setup that a test receives by naming it as a parameter. `tests/helpers.py` shows how httpx's `MockTransport` fakes the network.

## Exercises

1. Add a `GET /api/version` route that returns `BUILD`.
2. Add an `ENABLE_DOCS` setting that switches off `/docs` when it is `false`.
3. Change `cache.py` to store plans in SQLite (`sqlite3` is built in).
4. Add a guardrail that drops items mentioning a specific brand name, and a test for it.
5. Add a `--runs 3` option to `try_plan.py` that runs the prompt three times and prints which items appear every time.
