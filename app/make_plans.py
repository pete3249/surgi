"""Make, read and approve plans for the supported surgeries (the ones in procedures.json).

    python -m app.make_plans list                          # which surgeries have a draft or an approved plan
    python -m app.make_plans draft                         # write a draft for every supported surgery
    python -m app.make_plans draft hip-arthroscopy --runs 3   # three different drafts to compare
    python -m app.make_plans approve hip-arthroscopy       # copy a draft you have read into plans/
    python -m app.make_plans approve hip-arthroscopy --run 2  # approve the second of three drafts

The steps:
  1. `draft` asks the AI model (same prompt and guardrails as the web app) and writes
     drafts/<slug>.json plus drafts/<slug>.md. Read the .md. Edit the .json to fix or delete items.
  2. `approve` checks the .json again with the guardrails and writes plans/<slug>.json.
  3. Commit plans/ and deploy. Only plans/ is shipped. drafts/ never is.
"""
import argparse
import asyncio
import logging
import sys

import httpx

from .config import Settings, get_settings
from .errors import HttpError
from .guardrails import clean, sanitize
from .models import Plan
from .planner import MIN_ITEMS, generate_plan
from .procedures import PROCEDURES, Procedure, approved_path, get_procedure, load_approved

PHASES = (("pre", "Before surgery"), ("post", "After surgery"))


def stem_for(slug: str, run: int | None) -> str:
    return slug if run is None else f"{slug}.run{run}"


def dump_json(plan: Plan) -> str:
    return plan.model_dump_json(by_alias=True, exclude_none=True, indent=1) + "\n"


def review_markdown(plan: Plan) -> str:
    """A page to read. Edit the .json next to it, not this file."""
    lines = [
        f"# Review: {plan.surgery}",
        "",
        f"Written by `{plan.model}` on {plan.generated_at:%Y-%m-%d}. "
        f"{len(plan.items)} items kept (dropped {plan.stats.dropped}, copies of the general checklist {plan.stats.duplicates}).",
        "",
        "## What to check",
        "- [ ] Is every item right for THIS surgery, not a different one?",
        "- [ ] Are claims about weight bearing, braces, positions, driving and timing hedged (\"often\", \"ask your surgeon\")?",
        "- [ ] Anything too specific, risky or out of date? Delete or reword it in the .json.",
        "- [ ] Is it free of doses, fasting hours and warning signs (the app owns those)?",
        "- [ ] Do the days (the number before the category) make sense?",
        "- [ ] Plain words, upbeat tone, no filler that repeats the general checklist?",
        "",
        "## What to expect",
        plan.expect,
        "",
    ]
    for phase, title in PHASES:
        lines += [f"## {title}", ""]
        for i in sorted((i for i in plan.items if i.phase == phase), key=lambda i: i.d):
            tier = f" [{i.tier}]" if i.tier else ""
            lines += [f"- [ ] **{i.text}**{tier}  ", f"  `day {i.d:+d} · {i.cat}`: {i.detail}", ""]
    return "\n".join(lines)


def find_procedures(slugs: list[str]) -> list[Procedure]:
    if not slugs:
        return list(PROCEDURES)
    chosen = []
    for slug in slugs:
        p = get_procedure(slug)
        if p is None:
            sys.exit(f"Unknown surgery '{slug}'. Choose from: {', '.join(p.slug for p in PROCEDURES)}")
        chosen.append(p)
    return chosen


# ---------------- list ----------------


def show_list(settings: Settings) -> int:
    print(f"{'surgery':<22} {'approved':<9} drafts")
    for p in PROCEDURES:
        drafts = sorted(f.name for f in settings.drafts_dir.glob(f"{p.slug}*.json")) if settings.drafts_dir.exists() else []
        approved = "yes" if load_approved(p, settings) else "no"
        print(f"{p.slug:<22} {approved:<9} {', '.join(drafts) or '-'}")
    return 0


# ---------------- draft ----------------


async def make_drafts(
    procedures: list[Procedure], runs: int, force: bool, verbose: bool, settings: Settings, client: httpx.AsyncClient
) -> int:
    settings.drafts_dir.mkdir(parents=True, exist_ok=True)
    log = print if verbose else (lambda *a, **k: None)
    failures = 0
    for p in procedures:
        for run in range(1, runs + 1):
            stem = stem_for(p.slug, run if runs > 1 else None)
            json_path = settings.drafts_dir / f"{stem}.json"
            if json_path.exists() and not force:
                print(f"skip   {stem}: a draft is already there (--force replaces it and loses your edits)")
                continue
            print(f"making {stem} ...", flush=True)
            try:
                plan = await generate_plan(p.name, settings, client, log)
            except HttpError as e:
                print(f"FAILED {stem}: {e.message}")
                failures += 1
                continue
            json_path.write_text(dump_json(plan), encoding="utf-8")
            (settings.drafts_dir / f"{stem}.md").write_text(review_markdown(plan), encoding="utf-8")
            print(f"wrote  drafts/{stem}.md to read, and drafts/{stem}.json to edit ({len(plan.items)} items, model {plan.model})")
    return 1 if failures else 0


# ---------------- approve ----------------


def approve(slug: str, run: int | None, force: bool, settings: Settings) -> int:
    p = find_procedures([slug])[0]
    src = settings.drafts_dir / f"{stem_for(p.slug, run)}.json"
    if not src.exists():
        print(f"No draft at {src}. Make one with: python -m app.make_plans draft {p.slug}")
        return 1
    try:
        plan = Plan.model_validate_json(src.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"{src.name} is not valid JSON for a plan (an edit may have broken it):\n{e}")
        return 1

    # Run the guardrails again, because the file may have been edited by hand.
    raw = {"expect": plan.expect, "items": [i.model_dump(exclude_none=True) for i in plan.items]}
    try:
        items, expect, _ = sanitize(raw, min_items=MIN_ITEMS)
    except HttpError as e:
        print(f"Not approved: {e.message} (fewer than {MIN_ITEMS} usable items in {src.name}, see the log above).")
        return 1
    kept = {i.text for i in items}
    removed = [clean(i.text, 80) for i in plan.items if clean(i.text, 1000) not in kept]
    if removed and not force:
        print("Not approved. The guardrails would remove these items (fix or delete them in the .json, or use --force to approve without them):")
        for t in removed:
            print(f"  - {t}")
        return 1

    dest = approved_path(p.slug, settings)
    if dest.exists() and not force:
        print(f"{dest.name} is already approved. Use --force to replace it.")
        return 1
    settings.plans_dir.mkdir(parents=True, exist_ok=True)
    approved = plan.model_copy(update={"items": items, "expect": expect, "reviewed": True})
    dest.write_text(dump_json(approved), encoding="utf-8")
    print(f"Approved: plans/{dest.name} ({len(items)} items). Commit plans/ and deploy to ship it.")
    return 0


# ---------------- command line ----------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.make_plans", description="Make, read and approve plans for the supported surgeries.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show which surgeries have drafts and approved plans")
    d = sub.add_parser("draft", help="ask the AI model for a draft (needs LLM_API_KEY); nothing is shipped")
    d.add_argument("slugs", nargs="*", metavar="surgery", help="default: all of them. See procedures.json")
    d.add_argument("--runs", type=int, default=1, help="make this many drafts of each, to compare (default 1)")
    d.add_argument("--force", action="store_true", help="replace a draft that already exists (your edits are lost)")
    d.add_argument("-v", "--verbose", action="store_true", help="also print the AI-call details")
    a = sub.add_parser("approve", help="check a draft again and copy it into plans/")
    a.add_argument("slug", metavar="surgery")
    a.add_argument("--run", type=int, default=None, help="which numbered draft to approve (after draft --runs N)")
    a.add_argument("--force", action="store_true", help="replace an approved plan, or approve even though some items would be removed")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "list":
        return show_list(settings)
    if args.command == "approve":
        return approve(args.slug, args.run, args.force, settings)

    if not 1 <= args.runs <= 5:
        parser.error("--runs must be between 1 and 5")
    if not settings.llm_api_key:
        print("No LLM_API_KEY set. Add it to your .env file first.")
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    print(f"Models: {' -> '.join(settings.model_chain)}  (reasoning: {settings.llm_reasoning_effort})\n")
    chosen = find_procedures(args.slugs)

    async def go() -> int:
        async with httpx.AsyncClient() as client:
            return await make_drafts(chosen, args.runs, args.force, args.verbose, settings, client)

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
