"""Ready for Surgery: the Python backend.

Layout (each file is small and does one job):

    config.py      settings read from environment variables / .env
    models.py      pydantic models: the shapes of requests and responses
    errors.py      HttpError, the one exception that carries an HTTP status
    core.py        loads core-items.json (the vetted general checklist)
    procedures.py  the supported surgeries (procedures.json) and their approved plans (plans/*.json)
    make_plans.py  command line: make drafts, read them, approve them (python -m app.make_plans)
    prompts.py     builds the messages sent to the LLM
    llm.py         streaming LLM calls with timeouts, retries and fallbacks
    guardrails.py  validates and cleans what the model wrote
    cache.py       file cache of finished plans
    ratelimit.py   per-IP rate limit
    planner.py     ties it all together: build_plan()
    try_plan.py    command line: try the prompt on one surgery (python -m app.try_plan)
    main.py        the FastAPI web app (routes, static files, error handling)
"""
