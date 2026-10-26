"""Try the prompt on one surgery from the command line.

    python -m app.try_plan "hip arthroscopy"
    python -m app.try_plan "hip arthroscopy" --prompt   # print the prompt, no AI call, no cost
    python -m app.try_plan "hip arthroscopy" --json     # print the cleaned plan as JSON

It runs the same steps as the web app (prompt, model chain, guardrails, one retry) but skips the
cache, so every run is a fresh answer and nothing is stored.
"""
import argparse
import asyncio
import json
import logging
import time

import httpx

from .config import get_settings
from .errors import HttpError
from .guardrails import normalize_surgery
from .models import Plan
from .planner import generate_plan
from .prompts import build_messages


def render(plan: Plan, seconds: float) -> str:
    written = len(plan.items) + plan.stats.dropped + plan.stats.duplicates
    lines = [
        f"Surgery: {plan.surgery}",
        f"Model:   {plan.model}  ({seconds:.1f}s)",
        f"Items:   {len(plan.items)} kept of {written} written "
        f"(dropped {plan.stats.dropped}, duplicates of the general checklist {plan.stats.duplicates})",
        "",
        f"What to expect: {plan.expect}",
        "",
    ]
    for phase in ("pre", "post"):
        lines.append(phase.upper())
        for i in sorted((i for i in plan.items if i.phase == phase), key=lambda i: i.d):
            tier = f" [{i.tier}]" if i.tier else ""
            lines += [f"  {i.d:>4}  {i.cat:<8} {i.text}{tier}", f"        {i.detail}"]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.try_plan", description="Try the prompt on one surgery.")
    parser.add_argument("surgery", help='for example "hip arthroscopy"')
    parser.add_argument("--prompt", action="store_true", help="print the prompt and stop (no AI call)")
    parser.add_argument("--json", action="store_true", help="print the cleaned plan as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print the AI-call details")
    args = parser.parse_args(argv)

    surgery = normalize_surgery(args.surgery)
    if args.prompt:
        for m in build_messages(surgery):
            print(f"--- {m['role']} ---\n{m['content']}\n")
        return 0

    settings = get_settings()
    if not settings.llm_api_key:
        print("No LLM_API_KEY set. Add it to your .env file first.")
        return 1
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    print(f"Models: {' -> '.join(settings.model_chain)}  (reasoning: {settings.llm_reasoning_effort})\n")

    async def go() -> tuple[Plan, float]:
        t0 = time.monotonic()
        async with httpx.AsyncClient() as client:
            plan = await generate_plan(surgery, settings, client, print if args.verbose else (lambda *a, **k: None))
        return plan, time.monotonic() - t0

    try:
        plan, seconds = asyncio.run(go())
    except HttpError as e:
        print(f"Failed: {e.message}")
        return 1
    if args.json:
        print(json.dumps(plan.model_dump(mode="json", by_alias=True, exclude_none=True), indent=1, ensure_ascii=False))
    else:
        print(render(plan, seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
