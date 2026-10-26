# Surgi

A mobile-first web app that helps patients prepare for and recover from routine orthopedic surgery. Even with AI at your fingertips, the details of a surgery can feel overwhelming. Surgi turns them into clear, manageable steps.

The patient enters a surgery and a date. Surgi builds a before-and-after checklist with a countdown, a timeline and progress tracking. Every screen reminds them that their surgeon's advice comes first.

## Features

- **Checklist for any surgery.** Type "hip arthroscopy" or "carpal tunnel release"; there are no per-surgery templates to maintain.
- **Vetted general checklist.** The same for every surgery and shown instantly: insurance, FMLA and disability forms, medicine-pause reminders, fasting, packing, mood check-ins, warning signs and home basics.
- **Surgery-specific items.** AI adds items for physical therapy, equipment, home setup, what to pack and what to ask the surgeon. They are marked with a ✨.
- **Reviewed plans.** Hip arthroscopy, shoulder arthroscopy and knee replacement have plans drafted ahead of time, read and edited by a person, and labeled "✓ Reviewed". Other surgeries are labeled "AI-written, not reviewed".
- **Timeline.** One line from a month or more before surgery to month 2 of recovery, with a "You are here" marker.
- **Key details card.** Patients enter the arrival time, fasting cut-off and medicine instructions their own care team gave them.
- **Private by design.** Only the surgery name is sent to the server. The patient's name, date and progress stay on their device.

## How it works

```
Browser ── POST /api/plan {surgery} ──▶ Python server (holds the API key)
   approved plan in plans/?  ─▶ return it (no AI call)
   otherwise: AI writes surgery-specific items ─▶ guardrails check them ─▶ cached for 14 days
```

Surgi uses free AI models, so they’re sometimes slow or down. If so, the patient keeps the general checklist or tries again.

## Safeguards

- Safety-critical items (warning signs / call 911, confirming fasting times, which medicines to pause) come only from the vetted checklist. The AI never writes them.
- Items with medicine doses or fasting hours are dropped, since those come from the patient's own team.
- Items that repeat the general checklist, are too long, or don't fit the expected format are dropped. If too few good items remain, the request is retried once.
- No research is attached. The AI writes from widely accepted practice, nothing is cited, and the app says so.
- A per-IP rate limit and input sanitizing protect the API key.

## Built with

Python and FastAPI on the back end, a single-page `index.html` on the front end, and any OpenAI-compatible AI API (OpenRouter by default). Tests run offline with a mocked AI.

## Run it locally

Needs Python 3.10+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
echo "LLM_API_KEY=your-key" > .env
python -m app        # http://localhost:3000
pytest
```

Opening `index.html` directly won't work; the app needs the server. `/docs` shows the API, and `/api/health` shows the model, whether the key is set, and how many core items, procedures and reviewed plans loaded.

## Reviewed plans: draft, read, approve

```bash
python -m app.make_plans list                              # which surgeries have a draft and an approved plan
python -m app.make_plans draft                             # a draft for each supported surgery (needs LLM_API_KEY)
python -m app.make_plans draft knee-replacement --runs 3   # three drafts of one surgery, to compare
python -m app.make_plans approve knee-replacement          # after reading it; --run 2 approves the second draft
```

1. `draft` writes `drafts/<surgery>.md` (read this, it has a review checklist) and `drafts/<surgery>.json` (edit this). `drafts/` is git-ignored, and `draft` never overwrites an existing draft unless you add `--force`.
2. Fix or delete items in the `.json`. Pay most attention to anything about weight bearing, braces, positions, driving or timing.
3. `approve` re-runs the guardrails on your edited file and writes `plans/<surgery>.json`. It refuses (and lists the items) if the guardrails would remove something, and won't replace an approved plan without `--force`.
4. Commit `plans/`. `/api/health` shows `reviewedPlans`, which should equal `procedures`.

To add a surgery, add it to `procedures.json` (slug, name, and the names patients type for it, like "TKA") and run `draft` for it. To change an approved plan later, edit `plans/<surgery>.json`, or approve a new draft with `--force`. Users keep their old items until they press the refresh button.

## Trying the prompt on any surgery

Useful for checking what patients get for a surgery with no reviewed plan.

```bash
python -m app.try_plan "carpal tunnel release" --prompt   # print the prompt, no AI call
python -m app.try_plan "carpal tunnel release"            # run it: model, time, kept vs dropped items, readable plan
python -m app.try_plan "carpal tunnel release" --json     # the cleaned plan as JSON
```

It runs the same steps as the web app. It skips the cache and the approved plans, so each run is a fresh answer, and for a supported surgery it shows the raw AI output rather than the reviewed plan. Read what it prints, especially the physical therapy, equipment and restriction items, before relying on it.

## Configuration

Set these as environment variables or in `.env`. The full list is in `app/config.py`.

| Variable | Default | Notes |
|---|---|---|
| `LLM_API_KEY` | none | Required. Any OpenAI-compatible API |
| `LLM_MODEL` | `oolside/laguna-s-2.1:free` | First model to try. Free models fail and get retired, so set a fallback |
| `LLM_FALLBACK_MODEL` | `cohere/north-mini-code:free,openrouter/free` | Comma-separated models tried in order |
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

- The AI writes from its training data and can be wrong or out of date. No code can check medical accuracy, and surgeon protocols vary, which is why the app keeps saying "ask your surgeon."
- "Reviewed" means a person read and edited the plan, not that a clinician signed off.
- Cached plans are shared between all users, so the first plan for a surgery sets what others see for 14 days.
- This is a personal project.

See `LEARNING.md` for a tour of the code.
