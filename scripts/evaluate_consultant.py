"""Run a real course through Course Consultant end to end with a real AI model.

Does what an instructor does with the Quick AI version, then the course design and the build,
and writes a report for a person to read:

  1. slim the export with the browser's own code (pictures and documents included)
  2. import it; the app strips likely student information
  3. consultation on every topic; accept the proposals (the evaluator stands in for the instructor)
  4. draft the plan and the course design; write and approve revised documents
  5. propose the week-by-week structure and build the course (Essential by default)
  6. run every automatic check and save the HAX course site

Output folder:
  report.md      what happened, what the AI wrote, every check, and a scoring sheet
  report.json    the same data, for comparing runs
  documents/     each revised document as HTML
  site.zip       the HAX course site exactly as "Download course site" gives it
  site/          the same, unpacked; open site/preview/index.html for the plain pages

Usage (the key is read from the environment, never from the command line):

  export CC_EVAL_API_KEY=...            # your own provider key
  python3 scripts/evaluate_consultant.py --export course-export.imscc \\
      --provider openrouter --model anthropic/claude-sonnet-5.5 --out eval-results

Everything runs locally against a temporary database; the only network calls are to your AI
provider, which receives course text (after student details are removed) exactly as in the app.
Expect 40-70 AI requests for a 15-week course with the Essential build.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("openai", "anthropic", "gemini", "openrouter")
SLIM_LIMIT = 40 * 1024 * 1024
RUBRIC = (
    ("Accuracy", "Statements about the course match the course (names, weights, dates, items)."),
    ("Grounding", "Advice points to specific items and explains why, not generic tips."),
    ("Pedagogy", "Recommendations follow sound principles (alignment, UDL, feedback, workload) and fit the course level."),
    ("AI integration", "AI suggestions are appropriate, honest about limits and protect learning."),
    ("Actionability", "An instructor could carry out the changes next term; the effort estimates are realistic."),
    ("Course design", "Understandings are real propositions, questions are open, the evidence map is right."),
    ("Practice questions", "Kept questions are correct, worth asking and answerable from the page; none good was dropped."),
    ("Built course", "Weeks are sensibly ordered and titled; overviews are accurate and useful to a student."),
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

    def call(method, path, data=None, form=None, expected=200, raw=False):
        if form is not None:
            response = client.open("/mentor/api" + path, method=method, data=form,
                                   content_type="multipart/form-data", headers={"X-Mentor-CSRF": csrf})
        else:
            response = client.open("/mentor/api" + path, method=method, json=data, headers={"X-Mentor-CSRF": csrf})
        if response.status_code != expected:
            body = response.get_json(silent=True) or {}
            raise EvaluationError(f"{method} {path} failed ({response.status_code}): {body.get('error', '')}")
        return response.get_data() if raw else response.get_json()
    return call


def slim(export_path, work, log):
    """Slim a large export with the browser's own code; small packages are used as they are."""
    export_path = Path(export_path)
    if export_path.stat().st_size <= SLIM_LIMIT and export_path.suffix.lower() == ".zip":
        return export_path, None
    target = Path(work) / "slim.zip"
    log(f"Slimming {export_path.name} ({export_path.stat().st_size / 1048576:.0f} MB)…")
    try:
        done = subprocess.run(["node", str(ROOT / "scripts/slim_export.cjs"), str(export_path), str(target),
                               "--images", "--documents"], capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvaluationError(f"Could not slim the export: {exc}") from None
    if done.returncode != 0:
        raise EvaluationError("Could not slim the export: " + (done.stderr.strip() or "unknown error")[:500])
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    log(f"  {summary['slimBytes'] / 1048576:.1f} MB sent: {summary['textEntries']} text files, "
        f"{summary['imagesIncluded']} pictures, {summary['documentsIncluded']} documents; "
        f"{summary['mediaBytesSkipped'] / 1048576:.0f} MB of media stayed behind")
    return target, summary


class Usage:
    """Counts AI requests and their size by wrapping the app's AI client (no change to the app)."""

    def __init__(self):
        self.requests, self.sent, self.received, self.failed, self.by_step = 0, 0, 0, 0, {}
        self.step = "setup"
        import ai_client
        original = ai_client.AIClient.call
        usage = self

        def counted(client, system, prompt, *args, **kwargs):
            row = usage.by_step.setdefault(usage.step, {"requests": 0, "sent": 0, "received": 0, "seconds": 0.0})
            started = time.time()
            usage.requests += 1
            row["requests"] += 1
            usage.sent += len(system or "") + len(prompt or "")
            row["sent"] += len(system or "") + len(prompt or "")
            try:
                answer = original(client, system, prompt, *args, **kwargs)
            except Exception:
                usage.failed += 1
                raise
            finally:
                row["seconds"] = round(row["seconds"] + time.time() - started, 1)
            size = len(answer if isinstance(answer, str) else json.dumps(answer))
            usage.received += size
            row["received"] += size
            return answer
        ai_client.AIClient.call = counted

    def summary(self, prices):
        tokens_in, tokens_out = self.sent // 4, self.received // 4  # a rough rule: four characters a token
        cost = None
        if prices:
            cost = round(tokens_in * prices[0] + tokens_out * prices[1], 2)
        return {"requests": self.requests, "failed": self.failed, "characters_sent": self.sent,
                "characters_received": self.received, "tokens_in_estimate": tokens_in,
                "tokens_out_estimate": tokens_out, "cost_estimate_usd": cost, "by_step": self.by_step}


def prices_for(provider, model_id):
    """(dollars per input token, per output token) from OpenRouter's public list, or None."""
    if provider != "openrouter":
        return None
    try:
        import httpx
        rows = httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
        row = next(r for r in rows if r["id"] == model_id)
        return float(row["pricing"]["prompt"]), float(row["pricing"]["completion"])
    except Exception:
        return None


def run(export_path, provider, model_id, api_key, out_dir, *, max_documents=6, mode="essential", max_weeks=None,
        build=True, log=print):
    """Run the evaluation; return the report data and write the files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="cc-eval-") as data_dir:
        package_path, slimmed = slim(export_path, data_dir, log)
        call = _client(data_dir)
        usage = Usage()
        import course_build
        import hax_elements
        practice_log = []
        original_practice = course_build.practice_questions

        def recorded(raw, week, texts, titles):
            blocks, dropped = original_practice(raw, week, texts, titles)
            rows = []
            for row in raw if isinstance(raw, list) else []:
                if not isinstance(row, dict):
                    continue
                single, _ = original_practice([row], week, texts, titles)
                reason = ""
                if not single:
                    source, evidence = row.get("source_item"), row.get("evidence")
                    if source not in texts:
                        reason = "names a source item that is not a readable page of this week"
                    elif not isinstance(evidence, str) or course_build._normal(evidence) not in texts[source]:
                        reason = "the quoted passage is not in that item's text"
                    else:
                        reason = "malformed (options, answer or length)"
                rows.append({"kind": row.get("kind"), "question": row.get("question"), "options": row.get("options"),
                             "explanation": row.get("explanation"), "evidence": row.get("evidence"),
                             "source": titles.get(row.get("source_item"), row.get("source_item")),
                             "kept": bool(single), "why_dropped": reason})
            practice_log.append({"week": week["number"], "title": week["title"], "questions": rows,
                                 "kept": len(blocks), "dropped": dropped})
            return blocks, dropped
        course_build.practice_questions = recorded

        workspace = call("POST", "/workspaces", {"title": "Evaluation"}, expected=201)
        base = f"/workspaces/{workspace['id']}"
        log("Importing the course…")
        with open(package_path, "rb") as package:
            workspace = call("POST", base + "/intake/export",
                             form={"package": (package, Path(package_path).name), "revision": str(workspace["revision"])})
        course = workspace["course"]
        log(f"  {course['title']}: " + ", ".join(f"{v} {k}" for k, v in (course.get('counts') or {}).items() if v))
        # Choose the model the way the screen does: save the key, refresh the provider's list, pick
        # from it. That is what gives the app the model's real limits; without it the app assumes
        # small defaults and long answers are cut off.
        call("POST", "/credentials", {"provider": provider, "api_key": api_key})
        catalog = call("POST", f"/providers/{provider}/models/refresh", {})
        if model_id not in {row["id"] for row in catalog["models"]}:
            raise EvaluationError(f"{model_id} is not in this key's {provider} model list.")
        chosen = call("PUT", f"/providers/{provider}/model", {"model": model_id, "version": catalog["version"]})
        limits = chosen["selected_capabilities"]
        advice = next((row.get("advice") for row in catalog["models"] if row["id"] == model_id), None) or {}
        log(f"  model {model_id}: reads {limits.get('input_limit'):,} tokens, writes {limits.get('output_limit'):,}; "
            f"the app rates it “{advice.get('tier', 'unknown')}”")
        model_info = {"input_limit": limits.get("input_limit"), "output_limit": limits.get("output_limit"),
                      "structured_outputs": limits.get("structured_outputs"), "reasoning": limits.get("reasoning"),
                      "advice": advice, "suggested": catalog.get("suggested")}
        timings, errors = {}, []

        def step(name, method, path, data=None):
            usage.step = name
            t0 = time.time()
            try:
                return call(method, path, data)
            except EvaluationError as exc:
                errors.append(f"{name}: {exc}")
                log(f"  ! {exc}")
                return None
            finally:
                timings[name] = round(timings.get(name, 0) + time.time() - t0, 1)

        course_map = call("GET", base + "/course-map")
        log("Consultation (the AI works through each topic)…")
        for turn in range(8):
            view = step("consultation", "POST", base + "/consult", {"mode": "auto", "provider": provider})
            if view is None:
                break
            topics = view["state"]["topics"]
            log(f"  turn {turn + 1}: " + ", ".join(f"{t}={v['status']}" for t, v in topics.items()))
            if all(v["status"] in ("covered", "skipped") for v in topics.values()):
                break
        call("POST", base + "/consult/proposals/accept-all", {})  # the evaluator stands in for the instructor
        log("Drafting the plan…")
        step("plan", "POST", base + "/plan/draft", {"provider": provider})
        log("Drafting the course design…")
        step("design", "POST", base + "/design/draft", {"provider": provider})
        documents = call("GET", base + "/documents")
        wanted = [t for t in documents["targets"] if t["kind"] == "syllabus" or t["proposals"]][:max_documents]
        for index, target in enumerate(wanted, 1):
            log(f"Writing {target['title']} ({index} of {len(wanted)})…")
            step("documents", "POST", base + "/documents/generate",
                 {"provider": provider, "kind": target["kind"], "item_id": target.get("item_id")})
        call("POST", base + "/documents/approve-all", {})

        built = None
        if build:
            log(f"Proposing the week-by-week structure ({mode} build)…")
            built = step("structure", "POST", base + "/build/blueprint", {"provider": provider, "mode": mode})
            if built and built["build"]:
                weeks = built["build"]["blueprint"]["weeks"]
                log(f"  {len(weeks)} weeks: " + "; ".join(w["title"] for w in weeks)[:400])
                for _ in range(400):
                    pending = [s for s in built["steps"]
                               if max_weeks is None or not re.match(r"week-(\d+)", s["id"])
                               or int(re.match(r"week-(\d+)", s["id"]).group(1)) <= max_weeks]
                    if not pending:
                        break
                    log(f"  building: {pending[0]['label']} ({len(pending)} left)")
                    payload = {"provider": provider, "step": pending[0]["id"]}
                    result = step("build", "POST", base + "/build/step", payload)
                    if result is None:  # one retry, then stop: finished steps are kept
                        result = step("build", "POST", base + "/build/step", payload)
                        if result is None:
                            errors.append(f"build stopped at {pending[0]['id']}")
                            break
                    built = result
        usage.step = "checks"
        consult = call("GET", base + "/consult")
        plan = call("GET", base + "/plan")
        design = call("GET", base + "/design")
        documents = call("GET", base + "/documents")["documents"]
        quality = call("GET", base + "/quality")
        accessibility = call("GET", base + "/accessibility")
        page_check = call("GET", base + "/page-check")
        built = call("GET", base + "/build")
        site = call("GET", base + "/documents/export?format=hax", raw=True)

    (out_dir / "site.zip").write_bytes(site)
    site_dir = out_dir / "site"
    if site_dir.exists():
        import shutil
        shutil.rmtree(site_dir)
    with zipfile.ZipFile(io.BytesIO(site)) as archive:
        archive.extractall(site_dir)
        site_names = archive.namelist()
    manifest = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
    pages_text = "".join((site_dir / i["location"]).read_text(encoding="utf-8") for i in manifest["items"])
    site_summary = {
        "bytes": len(site), "pages": len(manifest["items"]),
        "pictures": sum(1 for n in site_names if n.startswith("files/")),
        "slugs": [i["slug"] for i in manifest["items"]][:60],
        "elements": {tag: pages_text.count("<" + tag) for tag in hax_elements.ELEMENTS},
        "not_included_notes": pages_text.count("[Image not included"),
        "canvas_only_notes": pages_text.count("(in the Canvas course)"),
        "unresolved_references": len(re.findall(r"IMS-CC-FILEBASE|\$WIKI_REFERENCE\$|\$CANVAS_", pages_text)),
        "instructor_pages": sum(1 for i in manifest["items"] if (i.get("metadata") or {}).get("audience") == "instructor"),
    }
    build_state = built.get("build") or {}
    report = {
        "course": {"title": course["title"], "source": course.get("source"), "counts": course.get("counts"),
                   "privacy": (course.get("privacy") or {}).get("counts"),
                   "import_warnings": course.get("warning_count")},
        "export": {"file": Path(export_path).name, "bytes": Path(export_path).stat().st_size, "slimmed": slimmed},
        "provider": provider, "model": model_id, "model_info": model_info,
        "date": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - started),
        "timings": timings, "errors": errors, "usage": usage.summary(prices_for(provider, model_id)),
        "course_map": {"grading": course_map["grading"], "assessment_summary": course_map["assessment_summary"],
                       "modules": len(course_map["structure"]["modules"])},
        "messages": consult["messages"], "consultation": consult["state"],
        "plan": plan["plan"], "proposals": plan["proposals"], "manual": plan.get("manual", []),
        "design": design["design"], "design_audit": design.get("audit"),
        "documents": [{k: d.get(k) for k in ("id", "kind", "title", "status", "summary", "applied_proposals")}
                      for d in documents],
        "structure": build_state.get("blueprint"), "build_mode": build_state.get("mode"),
        "build_pages": [{k: p.get(k) for k in ("id", "kind", "title", "audience", "week", "practice", "practice_dropped")}
                        for p in build_state.get("pages", [])],
        "steps_left": [s["id"] for s in built.get("steps", [])],
        "practice": practice_log,
        "quality": quality, "page_check": page_check,
        "accessibility": {k: accessibility[k] for k in ("items_checked", "items_with_issues", "errors", "warnings", "totals")},
        "site": site_summary,
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
    from quality_checks import outcome_text
    usage = report["usage"]
    lines = [f"# Evaluation: {report['course']['title']}", "",
             f"Model: `{report['provider']}` / `{report['model']}` · {report['date']} · "
             f"{report['seconds'] // 60} min {report['seconds'] % 60} s", ""]
    info = report.get("model_info") or {}
    if info:
        lines += [f"The app's view of this model: reads {info['input_limit']:,} tokens, writes {info['output_limit']:,}, "
                  f"rated “{(info.get('advice') or {}).get('tier', 'unknown')}”. "
                  f"Models the app would suggest for this key: {', '.join(info.get('suggested') or []) or 'none'}.", ""]
    cost = f", about ${usage['cost_estimate_usd']:.2f}" if usage["cost_estimate_usd"] is not None else ""
    lines += [f"**AI use:** {usage['requests']} requests ({usage['failed']} failed), roughly "
              f"{usage['tokens_in_estimate']:,} tokens in and {usage['tokens_out_estimate']:,} out{cost} "
              "(an estimate from text length, not the provider's count).", "",
              "| Step | Requests | Seconds |", "|---|---|---|"]
    lines += [f"| {name} | {row['requests']} | {row['seconds']:.0f} |" for name, row in usage["by_step"].items()]
    lines.append("")
    if report["errors"]:
        lines += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]

    export, course = report["export"], report["course"]
    lines += ["## Import", "", f"- Export: `{export['file']}`, {export['bytes'] / 1048576:.0f} MB."]
    if export["slimmed"]:
        s = export["slimmed"]
        lines.append(f"- Sent after slimming: {s['slimBytes'] / 1048576:.1f} MB ({s['textEntries']} text files, "
                     f"{s['imagesIncluded']} of {s['imagesOffered']} pictures"
                     + (f", {s['imagesTooLarge']} too large" if s["imagesTooLarge"] else "")
                     + f", {s['documentsIncluded']} documents).")
    lines.append("- Found: " + ", ".join(f"{v} {k}" for k, v in (course["counts"] or {}).items() if v) + ".")
    lines.append(f"- Removed as likely student information: {course['privacy']}. Import warnings: {course['import_warnings']}.")
    grading = report["course_map"]["grading"]
    lines.append("- Grading: " + ("weighted groups: " + "; ".join(
        f"{g['name']} {g['weight']:g}%" for g in grading["groups"] if g.get("weight")) if grading["weighted_groups"]
        else "total points") + ".")
    lines.append("")

    site = report["site"]
    lines += ["## The HAX course site", "",
              f"- {site['pages']} pages, {site['pictures']} pictures, {site['bytes'] / 1048576:.1f} MB "
              "(HAX Lite accepts 10 MB by default).",
              "- HAX elements used: " + (", ".join(f"{n} {tag}" for tag, n in site["elements"].items() if n) or "none") + ".",
              f"- Notes in place of things only Canvas could show: {site['not_included_notes']} pictures, "
              f"{site['canvas_only_notes']} links or files.",
              f"- Canvas-only references left in pages (should be 0): {site['unresolved_references']}.",
              f"- Instructor-only pages in the site (should be 0): {site['instructor_pages']}.",
              "- First page addresses: " + ", ".join(f"`{s}`" for s in site["slugs"][:14]) + ".", ""]

    lines += ["## Automatic checks", ""]
    summary = report["quality"]["summary"]
    lines.append(f"**Quality:** {summary['pass']} passed, {summary['warn']} to review, {summary['fail']} failed.")
    for group in report["quality"]["groups"]:
        lines += [f"- {c['status'].upper()}: {c['check']}: {c['detail']}" for c in group["checks"] if c["status"] != "pass"]
    audit = report.get("design_audit")
    if audit:
        lines += ["", f"**Course design: {audit['rating']}.** {audit['summary']}"]
        lines += [f"- {'MET' if c['status'] == 'pass' else 'LATER' if c['status'] == 'todo' else 'GAP'}: "
                  f"{c['name']}. {c['detail']}" for c in audit["checks"]]
    check = report["page_check"]
    lines += ["", f"**Page check:** {check['overall']}"]
    lines += [f"- Week {w['number']} ({w['rating']}): missing {'; '.join(w['missing']) or 'nothing'}" for w in check["weeks"]]
    lines += [f"- {p['title']}: " + " ".join(f["detail"] for f in p["findings"]) for p in check["pages"][:25]]
    a11y = report["accessibility"]
    lines += ["", f"**Accessibility:** {a11y['errors']} errors, {a11y['warnings']} warnings in "
              f"{a11y['items_with_issues']} of {a11y['items_checked']} pages: {a11y['totals']}.", ""]

    lines += ["## Reviewer scoring (fill in)", "", "Score each 1 (poor) to 4 (excellent) and note examples.", "",
              "| Criterion | What to look for | Score | Notes |", "|---|---|---|---|"]
    lines += [f"| {name} | {hint} | | |" for name, hint in RUBRIC]

    design = report["design"]
    lines += ["", "## Course design", "", "**Big ideas**", ""] + [f"- {i}" for i in design["big_ideas"]]
    lines += ["", "**Enduring understandings**", ""] + [f"{n}. {u}" for n, u in enumerate(design["understandings"], 1)]
    lines += ["", "**Essential questions (whole course)**", ""] + [f"- {q}" for q in design["overarching_questions"]]
    lines += ["", "**Essential questions (parts)**", ""] + [f"- {q}" for q in design["topical_questions"]]
    lines += ["", "**Predictable misunderstandings**", ""] + [
        f"- {m['misconception']} — catch: {m['pre_assess']} — confront: {m['confront']}" for m in design["misunderstandings"]]
    kinds = {}
    for row in design["evidence"]:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    lines += ["", f"**Evidence map:** {len(design['evidence'])} graded items classified: {kinds}; "
              f"{sum(1 for e in design['evidence'] if e['targets'])} show an understanding; "
              f"{sum(1 for e in design['evidence'] if e['problem'])} marked a real problem.", ""]

    structure = report.get("structure")
    if structure:
        lines += ["## Week-by-week structure", "", structure["summary"], ""]
        for week in structure["weeks"]:
            lines += [f"**Week {week['number']}: {week['title']}** ({len(week['items'])} items, about {week['hours']} h) — {week['theme']}",
                      *[f"  - objective: {o}" for o in week["objectives"]],
                      *[f"  - serves: {u}" for u in week.get("understandings", [])]]
        lines += ["", f"Start here: {len(structure['start_items'])} items · instructor only: "
                  f"{len(structure['instructor_items'])} · not placed in a week: {len(structure['unplaced'])}.", ""]
        if report["steps_left"]:
            lines += [f"Not built: {', '.join(report['steps_left'])}.", ""]

    if report["practice"]:
        kept = sum(w["kept"] for w in report["practice"])
        dropped = sum(w["dropped"] for w in report["practice"])
        lines += ["## Practice questions", "", f"{kept} kept, {dropped} discarded across {len(report['practice'])} weeks.", ""]
        for week in report["practice"]:
            lines.append(f"### Week {week['week']}: {week['title']} ({week['kept']} kept, {week['dropped']} discarded)")
            for q in week["questions"]:
                options = "; ".join(("✔ " if o.get("correct") else "") + str(o.get("text")) for o in q.get("options") or []
                                    if isinstance(o, dict))
                lines += [f"- **{'KEPT' if q['kept'] else 'DISCARDED: ' + q['why_dropped']}** ({q['kind']}) {q['question']}",
                          *([f"  - options: {options}"] if options else []),
                          f"  - answer: {q['explanation']}", f"  - evidence from “{q['source']}”: “{q['evidence']}”"]
            lines.append("")

    lines += ["## Proposals", ""]
    for p in report["proposals"]:
        manual = f", hand-work: {p['manual_work']}" if p.get("manual_work") not in (None, "none") else ""
        lines += [f"### {p['title']} ({p['topic']}, {p.get('effort', '')}, AI: {p.get('ai_dimension', '')}{manual})",
                  f"- Change: {p['change']}", f"- Why: {p['rationale']}", f"- Principle: {p.get('principle', '')}",
                  f"- Evidence: {', '.join(p.get('evidence', []))}", ""]
    plan = report["plan"]
    lines += ["## Plan", "", "**Vision.** " + (plan.get("vision") or "(none)"), "", "**Outcomes**", ""]
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
    parser.add_argument("--export", required=True, type=Path, help="Canvas export (.imscc) or slim package (.zip)")
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument("--model", required=True, help="Model ID as your provider names it")
    parser.add_argument("--out", type=Path, default=Path("eval-results"))
    parser.add_argument("--max-documents", type=int, default=6)
    parser.add_argument("--mode", choices=("essential", "full"), default="essential")
    parser.add_argument("--max-weeks", type=int, default=None, help="build only the first N weeks")
    parser.add_argument("--no-build", action="store_true", help="stop after the documents")
    args = parser.parse_args(argv)
    key = os.environ.get("CC_EVAL_API_KEY", "")
    if not key:
        parser.error("Set CC_EVAL_API_KEY to your provider key (it is never printed or saved).")

    def log(text):
        print(text, flush=True)
    try:
        report = run(args.export, args.provider, args.model, key, args.out, max_documents=args.max_documents,
                     mode=args.mode, max_weeks=args.max_weeks, build=not args.no_build, log=log)
    except EvaluationError as exc:
        print(f"Evaluation stopped: {exc}", file=sys.stderr)
        return 1
    s, usage = report["quality"]["summary"], report["usage"]
    cost = f", about ${usage['cost_estimate_usd']:.2f}" if usage["cost_estimate_usd"] is not None else ""
    print(f"Done in {report['seconds']} s with {usage['requests']} AI requests{cost}: "
          f"{s['pass']} checks passed, {s['warn']} to review, {s['fail']} failed.")
    print(f"Report: {args.out / 'report.md'}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
