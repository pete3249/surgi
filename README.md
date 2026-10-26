# Surgi

A mobile-first web app that helps a patient prepare for and recover from routine surgery. The patient enters a surgery and a date, and Surgi builds a plain-language before/after checklist with a countdown, a timeline and progress tracking. Every screen reminds them that their surgeon's advice comes first.

## How the checklist is made

```
Browser ── POST /api/plan {surgery} ──▶ Python server (holds the API key)
   approved file in plans/?  ─▶ return it (no AI call, marked "Reviewed")
   otherwise: 1. one short prompt (app/prompts.py) asks the AI model for surgery-specific items
              2. guardrails validate the result (one retry if fewer than 4 good items)
              3. cached per surgery and model (14 days), marked "AI-written, not reviewed"
```

- **General first, tailored second.** `core-items.json` holds the vetted checklist that is the same for every surgery (insurance, FMLA forms, medicine-pause reminders, fasting, packing, warning signs, home basics). It appears instantly. AI-written items for the specific surgery are added afterward and marked ✨.
- **No repeats.** `COVERED` in `app/prompts.py` lists the topics the general checklist already handles. Add a topic there when you add a core item in `pt`, `equip`, `home`, `bag` or `recovery` (a test reminds you).
- **Any surgery works.** There are no per-surgery templates. Hip arthroscopy, shoulder arthroscopy and knee replacement (listed in `procedures.json` with the names patients type, like "TKA") also have person-read plans in `plans/`.
- **No research is attached.** The model writes from widely accepted practice, nothing is cited, and the app says so.
- **Privacy.** Only the surgery name is sent to the server. The patient's name, date and progress stay on the device.

### Guardrails (`app/guardrails.py`)
- Items with medicine doses or fasting hours are dropped. Patients enter the times their own team gives them.
- Safety-critical items (warning signs / call 911, confirm fasting, which medicines to pause) come only from `core-items.json`.
- Near-duplicates of general items are dropped. Output is schema-checked, HTML-stripped and limited to the categories `pt`, `equip`, `home`, `bag` and `recovery`.
- Text over 100 characters or detail over 240 is dropped, not cut. If fewer than 4 items survive, the request is retried once, then fails with a friendly error.
- A per-IP rate limit and input sanitizing protect your API key.

If the AI models are down, the patient keeps the general checklist or retries.

## Reviewed plans: draft, read, approve, ship

```bash
python -m app.make_plans list                              # drafts and approved plans per surgery
python -m app.make_plans draft [surgery] [--runs 3]        # needs LLM_API_KEY
python -m app.make_plans approve knee-replacement [--run 2]
```

1. `draft` writes `drafts/<surgery>.md` (read this) and `drafts/<surgery>.json` (edit this). `drafts/` is git-ignored and never deployed. Existing drafts are kept unless you pass `--force`.
2. Fix or delete items in the `.json`, especially anything about weight bearing, braces, positions, driving or timing.
3. `approve` re-runs the guardrails and writes `plans/<surgery>.json`. It refuses if guardrails would remove items, and won't replace an approved plan without `--force`.
4. Commit `plans/` and deploy. `/api/health` should then show `reviewedPlans` equal to `procedures`.

To add a surgery, add it to `procedures.json` and run `draft` for it. "Reviewed" means a person read and edited the plan, not that a clinician signed off. If one did, say so in `howBuilt()` in `index.html`.

To try the prompt on any surgery without the cache or approved plans:

```bash
python -m app.try_plan "hip arthroscopy" --prompt   # print the prompt, no AI call
python -m app.try_plan "hip arthroscopy"            # run it: model, time, kept vs dropped items
python -m app.try_plan "hip arthroscopy" --json     # the cleaned plan as JSON
```

## Run it locally

Needs Python 3.10+.

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
echo "LLM_API_KEY=your-key" > .env                   # OpenRouter by default
python -m app                                        # http://localhost:3000
pytest                                               # offline tests with a mocked LLM
```

Open the app through the server, not by double-clicking `index.html`. `/docs` shows the API, and `/api/health` shows the model, whether the key is set, and how many core items, procedures and reviewed plans loaded.

## Deploy

**Render (Docker).** Push to GitHub (`.env` is git-ignored; double-check). Then **New + > Blueprint**, pick the repo, and paste `LLM_API_KEY` when asked. Check `/api/health` for `llmKeyConfigured: true`, `coreItems` above 0 and `reviewedPlans` equal to `procedures`. Free instances sleep when idle, and the plan cache is cleared on each deploy.

**Vercel.** Import the folder and set `LLM_API_KEY` and `TRUST_PROXY_HEADERS=true`. `vercel.json` allows 60 seconds. The filesystem is temporary, so for durable caching swap `read_cache` and `write_cache` in `app/cache.py` for a database or Redis.

**Anywhere else.**

```bash
docker build -t surgi . && docker run --rm -p 8000:8000 --env-file .env surgi
```

Behind a proxy (Render, Vercel), set `TRUST_PROXY_HEADERS=true` so the rate limiter sees the real visitor IP. Leave it off when the app is directly exposed, because the header can be faked.

## Configuration

Set these as environment variables or in `.env`. The rest (token limits, reasoning effort, cache directory) are in `app/config.py`.

| Variable | Default | Notes |
|---|---|---|
| `LLM_API_KEY` | none | Required. Any OpenAI-compatible API |
| `LLM_MODEL` | `openrouter/free` | First model to try. Free models fail and get retired, so set a fallback |
| `LLM_FALLBACK_MODEL` | none | Comma-separated models tried in order |
| `LLM_BASE_URL` | OpenRouter | Any OpenAI-compatible endpoint |
| `LLM_FIRST_TOKEN_SECONDS` | 45 | Give up on a model that is silent this long |
| `LLM_TIMEOUT_SECONDS` | 60 | Max wait for one model |
| `LLM_TOTAL_SECONDS` | 120 | Time budget across the whole model chain |
| `LLM_COOLDOWN_SECONDS` | 600 | Skip a model that just failed for this long |
| `CACHE_DAYS` | 14 | How long a plan is reused |
| `RATE_LIMIT_PER_HOUR` | 36 | Per IP; one plan is one request |
| `TRUST_PROXY_HEADERS` | false | Only behind a proxy you trust |
| `HOST` / `PORT` | `127.0.0.1` / 3000 | Where the server listens |

## Limits to know about

- The model writes from training data and can be wrong or out of date. No code can check medical accuracy, and surgeon protocols vary, which is why the app keeps saying "ask your surgeon."
- Cached plans are shared between users of a deployment, so the first plan for a surgery sets what others see for 14 days.
- This is a personal project. Before sharing widely, have a surgeon or PT review sample plans, and review the liability and privacy side.
- Read a few real generated plans (`python -m app.try_plan`) before trusting them. The tests mock the LLM.

See `LEARNING.md` for a tour of the code.
