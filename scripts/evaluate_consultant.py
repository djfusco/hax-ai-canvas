"""Evaluate the consultant end to end on a real course with a real AI model.

Runs the Quick AI version exactly as the app does (consultation on every
topic, accept the advisor's proposals, draft the plan, write revised
documents), then writes a report for a person to review:

  report.md     transcript, proposals, plan, documents, automatic checks and a
                reviewer's scoring sheet
  report.json   the same data, for comparing runs
  documents/    each revised document as HTML

Usage (the key is read from the environment, never from the command line):

  export CC_EVAL_API_KEY=...            # your own provider key
  python3 scripts/evaluate_consultant.py --export course-export.imscc \\
      --provider openrouter --model anthropic/claude-sonnet-4.5 --out eval-results

The export can be a full Canvas export or a slim package (up to 48 MiB).
Everything runs locally against a temporary database; the only network calls
are to your AI provider, which receives course text (after student details are
removed) exactly as in the app. Expect roughly 10-25 requests.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("openai", "anthropic", "gemini", "openrouter")
RUBRIC = (
    ("Accuracy", "Statements about the course match the course (names, weights, dates, items)."),
    ("Grounding", "Advice points to specific items and explains why, not generic tips."),
    ("Pedagogy", "Recommendations follow sound principles (alignment, UDL, feedback, workload) and fit the course level."),
    ("AI integration", "AI suggestions are appropriate, honest about limits and protect learning."),
    ("Actionability", "An instructor could carry out the changes next term; the effort estimates are realistic."),
    ("Documents", "Revised documents are complete, consistent with the plan and ready to edit."),
    ("Tone", "Respectful, concise and collegial; assumptions are clearly marked."),
)


class EvaluationError(RuntimeError):
    pass


def _client(data_dir):
    os.environ["HAX_EXECUTION_MODE"] = "local"
    for name in ("NEON_AUTH_BASE_URL", "HAX_HOSTED_ENABLED", "VERCEL"):
        os.environ.pop(name, None)
    sys.path[:0] = [str(ROOT / "python/app"), str(ROOT / "python")]
    import app as web
    web.app.config.update(MENTOR_DATA_ROOT=Path(data_dir))
    client = web.app.test_client()
    csrf = client.get("/mentor/api/session").json["csrf"]

    def call(method, path, data=None, form=None, expected=200):
        if form is not None:
            response = client.open("/mentor/api" + path, method=method, data=form,
                                   content_type="multipart/form-data", headers={"X-Mentor-CSRF": csrf})
        else:
            response = client.open("/mentor/api" + path, method=method, json=data, headers={"X-Mentor-CSRF": csrf})
        if response.status_code != expected:
            body = response.get_json(silent=True) or {}
            raise EvaluationError(f"{method} {path} failed ({response.status_code}): {body.get('error', '')}")
        return response.get_json()
    return call


def run(export_path, provider, model_id, api_key, out_dir, *, max_documents=6, log=print):
    """Run the evaluation; return the report data and write the files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="cc-eval-") as data_dir:
        call = _client(data_dir)
        workspace = call("POST", "/workspaces", {"title": "Evaluation"}, expected=201)
        base = f"/workspaces/{workspace['id']}"
        log("Importing the course…")
        with open(export_path, "rb") as package:
            workspace = call("POST", base + "/intake/export",
                             form={"package": (package, Path(export_path).name), "revision": str(workspace["revision"])})
        course = workspace["course"]
        call("POST", "/credentials", {"provider": provider, "api_key": api_key, "model": model_id})
        timings, errors = {}, []

        def step(name, method, path, data=None):
            t0 = time.time()
            try:
                return call(method, path, data)
            except EvaluationError as exc:
                errors.append(f"{name}: {exc}")
                log(f"  ! {exc}")
                return None
            finally:
                timings[name] = timings.get(name, 0) + round(time.time() - t0, 1)

        log("Consultation (Quick AI version)…")
        for turn in range(8):
            view = step("consultation", "POST", base + "/consult", {"mode": "auto", "provider": provider})
            if view is None:
                break
            topics = view["state"]["topics"]
            log(f"  turn {turn + 1}: " + ", ".join(f"{t}={v['status']}" for t, v in topics.items()))
            if all(v["status"] in ("covered", "skipped") for v in topics.values()):
                break
        call("POST", base + "/consult/proposals/accept-all", {})
        log("Drafting the plan…")
        step("plan", "POST", base + "/plan/draft", {"provider": provider})
        documents = call("GET", base + "/documents")
        wanted = [t for t in documents["targets"] if t["kind"] == "syllabus" or t["proposals"]][:max_documents]
        for index, target in enumerate(wanted, 1):
            log(f"Writing {target['title']} ({index} of {len(wanted)})…")
            step("documents", "POST", base + "/documents/generate",
                 {"provider": provider, "kind": target["kind"], "item_id": target.get("item_id"), "approve": True})
        consult = call("GET", base + "/consult")
        plan = call("GET", base + "/plan")
        documents = call("GET", base + "/documents")["documents"]
        quality = call("GET", base + "/quality")
        accessibility = call("GET", base + "/accessibility")

    report = {
        "course": {"title": course["title"], "source": course.get("source"), "counts": course.get("counts")},
        "provider": provider, "model": model_id,
        "date": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - started),
        "timings": timings, "errors": errors,
        "messages": consult["messages"], "consultation": consult["state"],
        "plan": plan["plan"], "proposals": plan["proposals"],
        "documents": [{k: d.get(k) for k in ("id", "kind", "title", "status", "summary", "applied_proposals")}
                      for d in documents],
        "quality": quality,
        "accessibility": {k: accessibility[k] for k in ("items_checked", "items_with_issues", "errors", "warnings")},
    }
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(exist_ok=True)
    for document in documents:
        name = re.sub(r"[^A-Za-z0-9]+", "-", document["title"]).strip("-")[:60] or document["id"]
        (docs_dir / f"{document['id']}-{name}.html").write_text(
            f"<!doctype html><meta charset=utf-8><title>{document['title']}</title>{document['html']}", encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(markdown(report), encoding="utf-8")
    return report


def markdown(report):
    lines = [f"# Consultant evaluation: {report['course']['title']}", "",
             f"Model: `{report['provider']}` / `{report['model']}` · {report['date']} · {report['seconds']} s", ""]
    if report["errors"]:
        lines += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]
    summary = report["quality"]["summary"]
    lines += ["## Automatic checks", "",
              f"{summary['pass']} passed, {summary['warn']} to review, {summary['fail']} failed. "
              + report["quality"]["note"], ""]
    for group in report["quality"]["groups"]:
        lines.append(f"**{group['name']}**")
        lines += [f"- {c['status'].upper()}: {c['check']}: {c['detail']}" for c in group["checks"]]
        lines.append("")
    a11y = report["accessibility"]
    lines += [f"Course-site accessibility: {a11y['errors']} errors, {a11y['warnings']} warnings in "
              f"{a11y['items_with_issues']} of {a11y['items_checked']} pages.", ""]
    lines += ["## Reviewer scoring (fill in)", "",
              "Score each 1 (poor) to 4 (excellent) and note examples.", "",
              "| Criterion | What to look for | Score | Notes |", "|---|---|---|---|"]
    lines += [f"| {name} | {hint} | | |" for name, hint in RUBRIC]
    lines += ["", "## Proposals", ""]
    for p in report["proposals"]:
        lines += [f"### {p['title']} ({p['topic']}, {p.get('effort', '')}, AI: {p.get('ai_dimension', '')})",
                  f"- Change: {p['change']}", f"- Why: {p['rationale']}", f"- Principle: {p.get('principle', '')}",
                  f"- Evidence: {', '.join(p.get('evidence', []))}", ""]
    plan = report["plan"]
    lines += ["## Plan", "", "**Vision.** " + (plan.get("vision") or "(none)"), "", "**Outcomes**", ""]
    from quality_checks import outcome_text
    lines += [f"1. {outcome_text(o)}" for o in plan.get("outcomes") or []] or ["(none)"]
    lines += ["", "**Student AI policy.** " + (plan.get("ai_policy") or "(none)"), ""]
    lines += ["## Documents", ""] + [f"- {d['title']} ({d['kind']}, {d['status']}): {d.get('summary') or ''}"
                                     for d in report["documents"]] + ["", "Full text: `documents/`.", ""]
    lines += ["## Transcript", ""]
    for m in report["messages"]:
        lines += [f"**{'Instructor' if m['role'] == 'user' else 'Consultant'}:**", "", m["content"], ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", required=True, type=Path, help="Canvas export (.imscc/.zip) or slim package")
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument("--model", required=True, help="Model ID as your provider names it")
    parser.add_argument("--out", type=Path, default=Path("eval-results"))
    parser.add_argument("--max-documents", type=int, default=6)
    args = parser.parse_args(argv)
    key = os.environ.get("CC_EVAL_API_KEY", "")
    if not key:
        parser.error("Set CC_EVAL_API_KEY to your provider key (it is never printed or saved).")
    try:
        report = run(args.export, args.provider, args.model, key, args.out, max_documents=args.max_documents)
    except EvaluationError as exc:
        print(f"Evaluation stopped: {exc}", file=sys.stderr)
        return 1
    s = report["quality"]["summary"]
    print(f"Done in {report['seconds']} s: {s['pass']} checks passed, {s['warn']} to review, {s['fail']} failed.")
    print(f"Report: {args.out / 'report.md'}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
