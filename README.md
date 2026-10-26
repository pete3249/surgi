# Ready for Surgery

A mobile-first web app that helps a patient prepare for routine surgery. The patient enters a surgery and a date, and the app builds a **plain-language before/after checklist**, with a countdown and progress tracking. A vetted general checklist is the same for every surgery, and AI adds items specific to the surgery. Every screen reminds them that their surgeon's advice comes first.

## How the checklist is made (no hand-maintained lists)

```
Browser ── POST /api/plan {surgery} ──▶ small Python server (holds the API key)
   supported surgery with an approved file in plans/?  ─▶ return it (no AI call, marked "Reviewed")
   otherwise: 1. One short prompt (app/prompts.py) asks the AI model for surgery-specific items
              2. Guardrails validate the result (one retry if too few good items)
              3. Cached per surgery and model (14 days), returned to the browser (marked "not reviewed")
```

- **General first, tailored second.** The general checklist (`core-items.json`: insurance, FMLA and disability forms, medicine-pause reminders, fasting, packing, mood check-in, warning signs, home basics) is vetted and the same for every surgery. It appears **instantly**. The AI adds **surgery-specific** items in a single request: physical therapy, equipment, home setup, what to pack and what to ask the surgeon about. They show up marked with ✨ when they are ready.
- **The prompt knows what is already covered.** `COVERED` in `app/prompts.py` is a short topic list per category, so the model does not repeat the general checklist. Add a topic there when you add a core item in `pt`, `equip`, `home`, `bag` or `recovery` (a test reminds you).
- **Timeline.** The first tab lays the whole journey out on one line, from a month or more before surgery to month 2 of recovery, with a "You are here" marker. Earlier stops show how many items are still open instead of a warning on every item.
- **Any surgery works.** Type "hip arthroscopy" or "carpal tunnel release"; there are no per-surgery templates to maintain.
- **Reviewed plans for a few surgeries.** Hip arthroscopy, shoulder arthroscopy and knee replacement (see `procedures.json`, which also lists the names patients type for each, like "TKA") have plans that are drafted ahead of time, read by a person, and shipped as files in `plans/`. They load instantly and are labeled "✓ Reviewed". Every other surgery is written live by the AI and labeled "AI-written, not reviewed".
- **No research is attached.** The model writes from widely accepted practice and the plan says so. Nothing is cited. "Write new surgery-specific items" in the app asks the model again (checked-off general items stay checked).

### Guardrails (in `app/guardrails.py`)
- Items containing **medicine doses** or **fasting hours or cut-offs** are dropped. Patients enter the times their own team gives them in the *Key details* card.
- The model is **told what the general checklist already covers** and is asked not to write it. Near-duplicates of general items that slip through are dropped automatically (same phase and category, heavy word overlap).
- Safety-critical items (warning signs / call 911, confirm fasting times, ask which medicines to pause) come only from `core-items.json`. The model never writes them.
- Output is schema-checked and HTML-stripped. Only the categories the prompt asks for are kept (`pt`, `equip`, `home`, `bag`, `recovery`). The sign of `d` must match the phase (a wrong sign is flipped). Over-long items (text over 100 or detail over 240 characters) are dropped, not cut. Duplicates are removed. If fewer than 4 items survive, the request is retried once, then fails with a friendly error.
- Per-IP rate limit and input sanitizing protect your API key.
- The patient's name, date and progress never leave the device. Only the surgery name is sent.

If the AI models are down or too slow, the patient simply keeps the general checklist ("Keep the general checklist"), or retries.

## Reviewed plans: make, read, approve, ship

```bash
python -m app.make_plans list                           # which surgeries have a draft and an approved plan
python -m app.make_plans draft                          # a draft for each supported surgery (needs LLM_API_KEY)
python -m app.make_plans draft knee-replacement --runs 3   # three drafts of one surgery, to compare
python -m app.make_plans approve knee-replacement       # after reading it; use --run 2 to approve the second draft
```

1. `draft` writes `drafts/<surgery>.md` (read this, it has a review checklist) and `drafts/<surgery>.json` (edit this). `drafts/` is not committed or deployed, and `draft` never overwrites a draft you may have edited unless you add `--force`.
2. Read the `.md`. Fix or delete items in the `.json`. Pay most attention to anything about weight bearing, braces, positions, driving or timing.
3. `approve` runs the guardrails again on your edited file and writes `plans/<surgery>.json`. It refuses (and lists the items) if the guardrails would remove something, and it will not replace an approved plan without `--force`.
4. Commit `plans/` and deploy. The Dockerfile copies `plans/` and `procedures.json`, and never `drafts/`. After deploying, `/api/health` shows `"reviewedPlans": 3`. A lower number means a file didn't ship or didn't load, and that surgery is then written live and shown as "not reviewed".

To add a surgery, add it to `procedures.json` (slug, name, the names patients type) and run `draft` for it. Changing an approved plan later is an edit to `plans/<surgery>.json` (or a new draft you approve with `--force`), then commit and deploy. People who already built a plan keep the old items in their browser until they use the refresh button.

The label only says a person read the plan. It does not say a clinician reviewed it. If a surgeon or physical therapist reviews them, you can say so in the wording in `index.html` (`howBuilt()`).

## Trying the prompt on any surgery

```bash
python -m app.try_plan "hip arthroscopy" --prompt   # print the prompt, no AI call
python -m app.try_plan "hip arthroscopy"            # run it for real: model, time, kept vs dropped items, readable plan
python -m app.try_plan "hip arthroscopy" --json     # the cleaned plan as JSON
```

It runs the same steps as the web app but skips the cache and the approved plans, so each run is a fresh answer. Read what it prints, especially the physical therapy, equipment and restriction items, before relying on it.

## Run it locally

Needs Python 3.10 or newer.

```bash
python3 -m venv .venv                  # a private folder of packages for this project
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt    # runtime packages + pytest

cp .env.example .env                   # add your LLM_API_KEY (OpenRouter by default)
python -m app                          # open http://localhost:3000
pytest                                 # offline tests with a mocked LLM
```

Opening `index.html` directly won't work, since the app needs the server.

Handy extras: `http://localhost:3000/docs` is an interactive page for the API, and `/api/health` shows the model, whether the key is set, and how many core items loaded. For development, `uvicorn app.main:app --reload --port 3000` restarts whenever you save a file.

## Deploy

The app is a normal long-running Python web server, so any host that can run a Docker container works.

### Render (runs the container as is)

1. Put the project in a GitHub repo (`.gitignore` keeps `.env` out; double-check before pushing).
2. In Render: **New + > Blueprint**, pick the repo. It reads `render.yaml`.
3. When asked, paste your `LLM_API_KEY`. Set `LLM_FALLBACK_MODEL` there too if you use one.
4. Open `https://<your-service>.onrender.com/api/health`. You want `"llmKeyConfigured": true`, `"coreItems"` above 0 (0 means `core-items.json` didn't ship with the deploy), and `"reviewedPlans"` equal to `"procedures"` (fewer means `plans/` didn't ship).

Free Render instances sleep when idle, so the first request after a pause is slow. Plans are cached on disk, which Render clears on each deploy and restart.

### Vercel

Vercel runs FastAPI directly from `app/main.py`, and `vercel.json` already allows 60 seconds. Import the folder and add `LLM_API_KEY` and `TRUST_PROXY_HEADERS=true` under Environment Variables. Then check `/api/health`: `"coreItems"` above 0 proves `core-items.json` was bundled, and `"reviewedPlans"` shows how many approved plans came with it. Serverless filesystems are temporary, so the cache may reset between requests; for durable caching, swap `read_cache` and `write_cache` in `app/cache.py` for a database or Redis.

### Anywhere else

```bash
docker build -t ready-for-surgery .
docker run --rm -p 8000:8000 --env-file .env ready-for-surgery
```

### Rate limiting behind a proxy

Hosts like Render and Vercel put a proxy in front of your app, so every request appears to come from the proxy's IP. Set `TRUST_PROXY_HEADERS=true` there so the limiter uses the real visitor IP from `X-Forwarded-For`. Leave it off when the app is directly exposed, because then anyone can fake that header.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LLM_API_KEY` | none | Required. Any OpenAI-compatible API |
| `LLM_MODEL` | `openrouter/free` | First model to try. Free `:free` models fail, get rate-limited, and get retired without warning, so use a fallback chain |
| `LLM_FIRST_TOKEN_SECONDS` | 45 | Give up on a model that is still "thinking" with no answer after this long, then try the next one |
| `LLM_COOLDOWN_SECONDS` | 600 | After a model fails, skip it for this long so later requests do not repeat the wait |
| `LLM_TIMEOUT_SECONDS` | 60 | Max wait for one model |
| `LLM_TOTAL_SECONDS` | 120 | Time budget across the whole model chain; once spent, no more fallbacks are tried |
| `LLM_FALLBACK_MODEL` | none | Comma-separated models tried in order if the first fails or times out |
| `LLM_BASE_URL` | OpenRouter | Point at any OpenAI-compatible endpoint |
| `CACHE_DAYS` | 14 | How long a plan is reused |
| `RATE_LIMIT_PER_HOUR` | 36 | Per IP. One plan is one request |
| `TRUST_PROXY_HEADERS` | false | Use `X-Forwarded-For` for the visitor IP. Only behind a proxy you trust |
| `HOST` / `PORT` | `127.0.0.1` / 3000 | Where the server listens |

## Limits to know about

- The model writes from what it learned in training. It can get details wrong or be out of date, and no code can check medical accuracy. Surgeon protocols vary a lot, which is why the app keeps saying "ask your surgeon."
- Reviewed plans are only as good as the review. "Reviewed" here means a person read and edited them, not that a clinician signed off.
- Cached plans are shared between users of your deployment, so the first plan for a surgery sets what others see for 14 days (use "Write new surgery-specific items" to rebuild).
- This is a personal project. Before sharing it widely, have a surgeon or PT review a few generated plans, add more test cases for the guardrails, and review the liability and privacy side.
- The tests mock the LLM. Read a few real generated plans over before trusting them (`python -m app.try_plan`).

See `LEARNING.md` for a tour of the code and Python concepts.
