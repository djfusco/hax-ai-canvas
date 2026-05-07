"""
evaluate.py — Phase 2.5: LLM-Based AI Readiness Evaluation

Scores every course item across 8 pedagogical dimensions using a real LLM
(not keyword matching), writes per-item JSON to the database, and generates
a standalone HTML readiness report.

8 scored dimensions
───────────────────
  Bloom's Taxonomy level     (1–6)
  AIAS level                 (1–5)
  AI Leverage Potential      (1–10)
  Cheating Vulnerability     (1–10)
  Authenticity Score         (1–10)
  Pedagogical Quality        (1–10)
  Transparency Score         (1–10)
  Alignment Score            (1–10)

Usage (standalone)
──────────────────
  python evaluate.py --course_id 2446743 --model_provider nebula
  python evaluate.py --course_id 2446743 --workers 5

Usage (as module)
─────────────────
  from evaluate import evaluate_course_data
  evaluate_course_data(db_client, course_id, ai_client, workers=3)
"""

from __future__ import annotations

import concurrent.futures
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ai_client import AIClient, build_ai_client
from sqlite_client import SQLiteClient


# ── taxonomy / scale label maps ───────────────────────────────────────────────

BLOOMS = {
    1: "Remember", 2: "Understand", 3: "Apply",
    4: "Analyze",  5: "Evaluate",  6: "Create",
}
AIAS = {
    1: "No AI",  2: "AI as Info Source",  3: "AI as Tutor",
    4: "AI as Collaborator",  5: "AI as Co-creator",
}
READINESS_LABELS: List[Tuple[float, str, str]] = [
    (8.0, "Excellent",   "#1b5e20"),
    (6.5, "Good",        "#2e7d32"),
    (5.0, "Moderate",    "#e65100"),
    (3.5, "Fair",        "#bf360c"),
    (0.0, "Needs Work",  "#b71c1c"),
]

# Administrative/hidden page patterns to skip
_NONINSTRUCTIONAL_RE = re.compile(
    r"KEEP\s+HIDDEN|HIDDEN\s*:|INSTRUCTORS?\s+ONLY|DO\s+NOT\s+PUBLISH"
    r"|CHANGE\s*LOG|CHANGELOG|LETTER\s+TO\s+INSTRUCTOR"
    r"|\[INTERNAL\]|\[ADMIN\]|\[DRAFT\]|^\[HIDDEN\]",
    re.IGNORECASE,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _readiness_label(score: float) -> Tuple[str, str]:
    for threshold, label, color in READINESS_LABELS:
        if score >= threshold:
            return label, color
    return "Needs Work", "#b71c1c"


def _clean(html: str) -> str:
    """Strip HTML tags and collapse whitespace to readable plain text."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def _is_noninstructional(title: str, content: str = "") -> bool:
    """Return True for admin/hidden pages that should not be evaluated."""
    if _NONINSTRUCTIONAL_RE.search(title):
        return True
    stripped = _clean(content)
    # Very thin content with no recognisable instructional signal
    if len(stripped) < 40 and not any(
        kw in title.lower()
        for kw in ("overview", "intro", "welcome", "about", "syllabus")
    ):
        return True
    return False


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_json(raw: str) -> Dict:
    """Robustly extract a JSON object from an LLM response."""
    clean = _strip_fences(raw)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"Cannot parse JSON from:\n{raw[:300]}")


# ── evaluation prompt / system ────────────────────────────────────────────────

_EVAL_SYSTEM = (
    "You are an expert educational researcher specializing in AI readiness assessment. "
    "Evaluate university course content across multiple pedagogical frameworks. "
    "You must return ONLY a valid JSON object — no prose, no markdown fences, no explanation."
)

_EVAL_PROMPT = """Evaluate this {item_type} from a university course for AI readiness and pedagogical quality.

COURSE: {course_name}
TITLE: {title}
CONTENT PREVIEW:
{content}

Score on each dimension and return EXACTLY this JSON (no other text):
{{
  "blooms_level": <integer 1-6>,
  "blooms_label": "<Remember|Understand|Apply|Analyze|Evaluate|Create>",
  "aias_level":   <integer 1-5>,
  "aias_label":   "<No AI|AI as Info Source|AI as Tutor|AI as Collaborator|AI as Co-creator>",
  "ai_leverage":            <float 1-10>,
  "cheating_vulnerability": <float 1-10>,
  "authenticity_score":     <float 1-10>,
  "pedagogical_quality":    <float 1-10>,
  "transparency_score":     <float 1-10>,
  "alignment_score":        <float 1-10>,
  "key_strengths":            ["<strength1>", "<strength2>"],
  "key_concerns":             ["<concern1>", "<concern2>"],
  "priority_recommendations": ["<rec 1>", "<rec 2>", "<rec 3>"]
}}

Scoring rubric:
• blooms_level           — cognitive level primarily targeted (1=Remember/recall, 6=Create/synthesize new work)
• aias_level             — AI Integration Assessment Scale (1=no AI role envisioned; 5=AI is genuine co-creator)
• ai_leverage            — how transformatively AI could HELP students learn this (10=outstanding AI partner potential)
• cheating_vulnerability — how easily a student could fully delegate this to AI and receive full credit (10=trivially outsourceable)
• authenticity_score     — how much the work requires unique personal knowledge or experience (10=fully personal)
• pedagogical_quality    — overall instructional design quality of the ORIGINAL content (10=excellent)
• transparency_score     — how clearly expectations, success criteria, and rubric are communicated (10=crystal clear)
• alignment_score        — how well the assessment measures its stated learning objectives (10=tightly aligned)

Be specific and actionable in recommendations. Assess based on actual content semantics, not keywords."""


# ── core evaluation function ──────────────────────────────────────────────────

def _evaluate_item(
    ai: AIClient,
    item_type: str,
    item_id:   str,
    content:   str,
    title:     str,
    course_name: str,
) -> Dict:
    """
    Call the LLM to score one item.  Returns the structured evaluation dict.
    Raises on failure (caller handles it and stores a default).
    """
    prompt = _EVAL_PROMPT.format(
        item_type=item_type,
        course_name=course_name,
        title=title,
        content=content,
    )
    raw = ai.call(_EVAL_SYSTEM, prompt, max_tokens=1024)
    d = _extract_json(raw)

    vuln         = float(d.get("cheating_vulnerability", 5))
    leverage     = float(d.get("ai_leverage", 5))
    auth         = float(d.get("authenticity_score", 5))
    quality      = float(d.get("pedagogical_quality", 5))
    transparency = float(d.get("transparency_score", 5))
    alignment    = float(d.get("alignment_score", 5))

    # Weighted composite: vulnerability is highest-weight (it is the key risk metric)
    readiness = round(
        leverage * 0.25 + (10.0 - vuln) * 0.35 + auth * 0.25 + quality * 0.15,
        2,
    )

    return {
        "item_id":   item_id,
        "item_type": item_type,
        "title":     title,
        "blooms_level":           int(d.get("blooms_level", 3)),
        "blooms_label":           d.get("blooms_label", "Apply"),
        "aias_level":             int(d.get("aias_level", 2)),
        "aias_label":             d.get("aias_label", "AI as Info Source"),
        "ai_leverage":            leverage,
        "cheating_vulnerability": vuln,
        "authenticity_score":     auth,
        "pedagogical_quality":    quality,
        "transparency_score":     transparency,
        "alignment_score":        alignment,
        "ai_readiness_score":     readiness,
        "key_strengths":            d.get("key_strengths", []),
        "key_concerns":             d.get("key_concerns", []),
        "priority_recommendations": d.get("priority_recommendations", []),
        "noninstructional": False,
    }


def _default_eval(item_id: str, item_type: str, title: str) -> Dict:
    """Fallback evaluation for items that fail LLM scoring."""
    return {
        "item_id": item_id, "item_type": item_type, "title": title,
        "blooms_level": 3, "blooms_label": "Apply",
        "aias_level": 2, "aias_label": "AI as Info Source",
        "ai_leverage": 5.0, "cheating_vulnerability": 5.0,
        "authenticity_score": 5.0, "pedagogical_quality": 5.0,
        "transparency_score": 5.0, "alignment_score": 5.0,
        "ai_readiness_score": 5.0,
        "key_strengths": [], "key_concerns": [],
        "priority_recommendations": [],
        "noninstructional": False,
    }


# ── content extraction helpers ────────────────────────────────────────────────

def _item_content_and_title(raw: dict) -> Tuple[str, str]:
    """
    Extract a (clean_text_content, title) pair from a raw DB item dict.
    raw_content is stored as JSON for pages/assignments/quizzes/discussions,
    or as plain text for syllabus.
    """
    item_type = raw.get("item_type", "")
    title     = raw.get("title", "Untitled")
    raw_content = raw.get("raw_content", "")

    # Try to parse as JSON first
    try:
        content_dict = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        # Plain text (e.g. syllabus)
        return raw_content[:6000], title

    if item_type == "pages":
        text = _clean(content_dict.get("body", ""))
        return text[:4000], content_dict.get("title", title)

    if item_type == "assignments":
        text = _clean(content_dict.get("description", ""))
        return text[:5000], content_dict.get("name", title)

    if item_type == "quizzes":
        desc = _clean(content_dict.get("description", ""))
        qs   = content_dict.get("questions", [])
        q_text = ""
        for i, q in enumerate(qs[:20], 1):
            q_text += f"\nQ{i}: {_clean(q.get('question_text', q.get('text', '')))}\n"
        return (desc[:800] + q_text)[:4000], content_dict.get("title", title)

    if item_type == "discussions":
        text = _clean(content_dict.get("message", ""))
        return text[:4000], content_dict.get("title", title)

    # Fallback for unknown types
    return str(raw_content)[:4000], title


# ── main evaluation entry point ───────────────────────────────────────────────

def evaluate_course_data(
    db_client,
    course_id:   str,
    ai_client:   AIClient,
    workers:     int = 3,
    output_dir:  Optional[str] = None,
) -> List[Dict]:
    """
    Phase 2.5: Evaluate all unevaluated items for a course.

    LLM calls are made in parallel (up to `workers` threads).
    Results are written to the DB sequentially in the main thread.
    Returns the list of evaluation dicts for all items.
    """
    # Get course name for prompt context
    items_to_eval = db_client.get_items_for_evaluation(course_id)
    if not items_to_eval:
        print("  No items require evaluation for this course.")
        # Still load existing evals for the report
        all_items = db_client.get_completed_items(course_id) + \
                    db_client.get_pending_items(course_id)
        return [
            json.loads(item["evaluation"])
            for item in all_items
            if item.get("evaluation")
        ]

    # Derive course name from the first item's course context
    try:
        from extract import get_export_dir, get_course_title
        export_dir = get_export_dir(course_id)
        course_name = get_course_title(export_dir)
    except Exception:
        course_name = f"Course {course_id}"

    # Detect and pre-mark non-instructional items
    tasks: List[Tuple[str, str, str, str]] = []  # (type, id, content, title)
    noninstructional_count = 0

    for item in items_to_eval:
        content, title = _item_content_and_title(item)
        item_type = item["item_type"]

        if _is_noninstructional(title, content):
            ev = _default_eval(item["id"], item_type, title)
            ev["noninstructional"] = True
            db_client.update_item_evaluation(item["id"], course_id, json.dumps(ev))
            noninstructional_count += 1
        else:
            tasks.append((item_type, item["id"], content, title))

    if noninstructional_count:
        print(f"  ⚠  {noninstructional_count} non-instructional item(s) flagged — skipped")

    total = len(tasks)
    done  = 0
    print(f"  Evaluating {total} items · {workers} worker(s)\n")

    all_evals: List[Dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {
            pool.submit(
                _evaluate_item, ai_client, itype, iid, content, title, course_name
            ): (itype, iid, title)
            for itype, iid, content, title in tasks
        }

        for fut in concurrent.futures.as_completed(fut_map):
            itype, iid, title = fut_map[fut]
            done += 1
            try:
                ev = fut.result()
                db_client.update_item_evaluation(iid, course_id, json.dumps(ev))
                vuln_badge = f"  vuln={ev['cheating_vulnerability']:.1f}"
                print(f"  [{done:>3}/{total}] ✓ {itype:<12} {title[:48]}{vuln_badge}")
                all_evals.append(ev)
            except Exception as exc:
                ev = _default_eval(iid, itype, title)
                db_client.update_item_evaluation(iid, course_id, json.dumps(ev))
                print(f"  [{done:>3}/{total}] ✗ {itype:<12} {title[:48]} — {exc}")
                all_evals.append(ev)

    # Summary stats
    instructional = [e for e in all_evals if not e.get("noninstructional")]
    if instructional:
        avg_vuln  = sum(e["cheating_vulnerability"] for e in instructional) / len(instructional)
        avg_ready = sum(e["ai_readiness_score"]     for e in instructional) / len(instructional)
        label, _  = _readiness_label(avg_ready)
        high_risk = sum(1 for e in instructional if e["cheating_vulnerability"] >= 7)
        print(f"\n  Overall AI Readiness: {avg_ready:.1f}/10 — {label}")
        print(f"  High-risk items (vulnerability ≥ 7): {high_risk}")

    # Generate HTML report
    _generate_report(db_client, course_id, course_name, all_evals, output_dir)

    return all_evals


# ── HTML report generation ────────────────────────────────────────────────────

def _generate_report(
    db_client,
    course_id:   str,
    course_name: str,
    evals:       List[Dict],
    output_dir:  Optional[str],
) -> None:
    """Write a standalone HTML AI-readiness dashboard."""
    # Merge with any already-stored evals (in case some existed before this run)
    all_items = db_client.get_completed_items(course_id) + \
                db_client.get_pending_items(course_id)
    stored: Dict[str, Dict] = {}
    for item in all_items:
        if item.get("evaluation"):
            try:
                stored[item["id"]] = json.loads(item["evaluation"])
            except (json.JSONDecodeError, TypeError):
                pass
    # Overlay fresh evals over stored ones
    for ev in evals:
        stored[ev["item_id"]] = ev
    all_evals = list(stored.values())

    if not all_evals:
        return

    # Determine output path
    if output_dir:
        report_dir = Path(output_dir)
    else:
        report_dir = Path(".")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"course_{course_id}_ai_readiness_report.html"

    html = _build_report_html(course_name, course_id, all_evals)
    report_path.write_text(html, encoding="utf-8")
    print(f"\n  📊 AI Readiness Report: {report_path}")


def _score_color(value: float, inverted: bool = False) -> str:
    v = (10.0 - value) if inverted else value
    if v >= 7.5: return "#2e7d32"
    if v >= 5.0: return "#e65100"
    return "#c62828"


def _build_report_html(course_name: str, course_id: str, evals: List[Dict]) -> str:
    instructional = [e for e in evals if not e.get("noninstructional")]
    if not instructional:
        instructional = evals

    # Aggregate stats
    avg_vuln    = sum(e["cheating_vulnerability"] for e in instructional) / len(instructional)
    avg_ready   = sum(e["ai_readiness_score"]     for e in instructional) / len(instructional)
    avg_leverage= sum(e["ai_leverage"]            for e in instructional) / len(instructional)
    avg_auth    = sum(e["authenticity_score"]      for e in instructional) / len(instructional)
    avg_qual    = sum(e["pedagogical_quality"]     for e in instructional) / len(instructional)
    ov_label, ov_color = _readiness_label(avg_ready)
    high_risk   = [e for e in instructional if e["cheating_vulnerability"] >= 7]
    generated   = datetime.now().strftime("%B %d, %Y %H:%M")

    # Top recommendations (deduplicated, sorted by vulnerability desc)
    all_recs: List[str] = []
    for ev in sorted(instructional, key=lambda x: x["cheating_vulnerability"], reverse=True):
        for rec in ev.get("priority_recommendations", []):
            if rec and rec not in all_recs:
                all_recs.append(rec)
    top_recs = all_recs[:12]

    def card(label: str, value: float, inverted: bool = False) -> str:
        c   = _score_color(value, inverted)
        pct = int(value * 10)
        return (
            f'<div class="metric-card">'
            f'<div class="metric-value" style="color:{c}">{value:.1f}</div>'
            f'<div class="metric-label">{label}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{c}"></div></div>'
            f'</div>'
        )

    # Per-item rows
    item_rows_html = ""
    for r in sorted(instructional, key=lambda x: x["cheating_vulnerability"], reverse=True):
        vc = _score_color(r["cheating_vulnerability"], inverted=True)
        rc = _score_color(r["ai_readiness_score"])
        strengths = "".join(f"<li>{s}</li>" for s in r.get("key_strengths", [])[:3])
        concerns  = "".join(f"<li>{c}</li>" for c in r.get("key_concerns",  [])[:3])
        recs      = "".join(f"<li>{x}</li>" for x in r.get("priority_recommendations", [])[:3])
        high_cls  = " high-risk" if r["cheating_vulnerability"] >= 7 else ""
        item_rows_html += f"""<details class="item-row{high_cls}" data-type="{r['item_type']}" data-vuln="{r['cheating_vulnerability']}">
  <summary>
    <span class="type-badge badge-{r['item_type']}">{r['item_type']}</span>
    <span class="item-title">{r['title'][:80]}</span>
    <span class="score-pip" style="background:{vc}" title="Vulnerability">{r['cheating_vulnerability']:.1f}</span>
    <span class="score-pip" style="background:{rc}" title="AI Readiness">{r['ai_readiness_score']:.1f}</span>
    <span class="bloom-tag">{r.get('blooms_label','Apply')}</span>
    <span class="expand-arrow">&#9658;</span>
  </summary>
  <div class="item-detail">
    <div class="detail-scores">
      <div class="mini-score"><div class="mini-label">AI Leverage</div><div class="mini-val" style="color:{_score_color(r['ai_leverage'])}">{r['ai_leverage']:.1f}</div></div>
      <div class="mini-score"><div class="mini-label">Vulnerability</div><div class="mini-val" style="color:{vc}">{r['cheating_vulnerability']:.1f}</div></div>
      <div class="mini-score"><div class="mini-label">Authenticity</div><div class="mini-val" style="color:{_score_color(r['authenticity_score'])}">{r['authenticity_score']:.1f}</div></div>
      <div class="mini-score"><div class="mini-label">Ped. Quality</div><div class="mini-val" style="color:{_score_color(r['pedagogical_quality'])}">{r['pedagogical_quality']:.1f}</div></div>
      <div class="mini-score"><div class="mini-label">AIAS</div><div class="mini-val">{r.get('aias_label','')}</div></div>
    </div>
    <div class="detail-grid">
      <div><h4>Strengths</h4><ul>{strengths}</ul></div>
      <div><h4>Concerns</h4><ul>{concerns}</ul></div>
      <div><h4>Priority Recommendations</h4><ul>{recs}</ul></div>
    </div>
  </div>
</details>"""

    # High-risk flags
    if high_risk:
        flags_html = "".join(
            f'<div class="flag-row">'
            f'<span class="flag-type badge-{r["item_type"]}">{r["item_type"]}</span>'
            f'<strong>{r["title"][:70]}</strong>'
            f'<span class="vuln-num">{r["cheating_vulnerability"]:.1f}/10</span>'
            f'<div class="flag-recs"><em>{"; ".join(r.get("priority_recommendations", [])[:2])}</em></div>'
            f'</div>'
            for r in high_risk[:10]
        )
    else:
        flags_html = '<p class="ok-note">✓ No items scored above 7.0 on cheating vulnerability.</p>'

    recs_html = "".join(
        f'<div class="rec-row"><span class="rec-num">{i}</span><span>{rec}</span></div>'
        for i, rec in enumerate(top_recs, 1)
    ) or "<p>No recommendations generated.</p>"

    # By-type summary table
    type_rows = ""
    for t in ["page", "assignment", "quiz", "discussion", "pages", "assignments", "quizzes", "discussions"]:
        items_t = [e for e in instructional if e.get("item_type") == t]
        if not items_t:
            continue
        # Normalise display label
        display_t = t.rstrip("s") if t.endswith("s") else t
        ar = sum(e["ai_readiness_score"] for e in items_t) / len(items_t)
        vr = sum(e["cheating_vulnerability"] for e in items_t) / len(items_t)
        bl = sum(e["blooms_level"] for e in items_t) / len(items_t)
        type_rows += (
            f"<tr><td><span class='badge-{display_t} type-badge'>{t}</span></td>"
            f"<td>{len(items_t)}</td>"
            f"<td><span style='color:{_score_color(ar)};font-weight:700'>{ar:.1f}</span></td>"
            f"<td><span style='color:{_score_color(vr, inverted=True)};font-weight:700'>{vr:.1f}</span></td>"
            f"<td>{BLOOMS.get(round(bl), 'Apply')} ({bl:.1f})</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Readiness Report — {course_name}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.6;font-size:15px}}
header{{background:linear-gradient(135deg,#1565c0 0%,#1976d2 60%,#42a5f5 100%);color:#fff;padding:32px 40px}}
.header-inner{{display:flex;justify-content:space-between;align-items:center;max-width:1100px;margin:0 auto;gap:24px;flex-wrap:wrap}}
h1{{font-size:1.8rem;font-weight:700;margin-bottom:6px}}
.header-meta{{opacity:.82;font-size:.9rem}}
.overall-badge{{border:4px solid;border-radius:16px;padding:16px 24px;text-align:center;background:rgba(255,255,255,.15);min-width:120px}}
.overall-num{{font-size:2.4rem;font-weight:800;line-height:1}}
.overall-label{{font-size:.85rem;font-weight:600;margin-top:4px}}
.page{{max-width:1100px;margin:0 auto;padding:24px}}
.section{{background:#fff;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.section-title{{font-size:1.1rem;font-weight:700;margin-bottom:16px;padding-bottom:10px;border-bottom:2px solid #e8eaf6}}
.metrics-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}}
.metric-card{{background:#f8f9ff;border-radius:10px;padding:18px}}
.metric-value{{font-size:2rem;font-weight:800;line-height:1}}
.metric-label{{color:#666;font-size:.82rem;margin:4px 0 10px;text-transform:uppercase;letter-spacing:.5px}}
.bar-track{{background:#e0e0e0;border-radius:4px;height:7px;overflow:hidden}}
.bar-fill{{height:7px;border-radius:4px}}
.summary-table{{width:100%;border-collapse:collapse;font-size:.9rem}}
.summary-table th{{text-align:left;padding:10px 14px;background:#f5f7fa;font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;color:#555}}
.summary-table td{{padding:10px 14px;border-bottom:1px solid #f0f0f0}}
.type-badge{{display:inline-block;padding:2px 10px;border-radius:10px;font-size:.74rem;font-weight:700;white-space:nowrap}}
.badge-page,.badge-pages{{background:#e3f2fd;color:#0d47a1}}
.badge-assignment,.badge-assignments{{background:#f3e5f5;color:#6a1b9a}}
.badge-quiz,.badge-quizzes{{background:#e8f5e9;color:#1b5e20}}
.badge-discussion,.badge-discussions{{background:#fff3e0;color:#e65100}}
.badge-syllabus{{background:#fce4ec;color:#880e4f}}
.filter-bar{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.filter-btn{{padding:5px 16px;border-radius:20px;border:1px solid #d0d5dd;background:#fff;cursor:pointer;font-size:.82rem;font-weight:500}}
.filter-btn.active{{background:#1565c0;color:#fff;border-color:#1565c0}}
details.item-row{{background:#fff;border-radius:8px;margin-bottom:8px;box-shadow:0 1px 4px rgba(0,0,0,.05);overflow:hidden}}
details.item-row.high-risk{{border-left:3px solid #c62828}}
details.item-row summary{{padding:13px 18px;cursor:pointer;display:flex;align-items:center;gap:10px;list-style:none;flex-wrap:wrap}}
details.item-row summary::-webkit-details-marker{{display:none}}
.item-title{{flex:1;font-weight:500;font-size:.9rem;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.score-pip{{padding:2px 9px;border-radius:10px;font-size:.78rem;font-weight:700;color:#fff;white-space:nowrap}}
.bloom-tag{{font-size:.76rem;background:#e8eaf6;color:#3949ab;padding:2px 8px;border-radius:8px}}
.expand-arrow{{margin-left:auto;color:#bbb;font-size:.9rem}}
.item-detail{{padding:0 18px 18px;border-top:1px solid #f0f0f0}}
.detail-scores{{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0;background:#f8f9ff;border-radius:8px;padding:14px}}
.mini-score{{min-width:100px}}
.mini-label{{font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;color:#888;margin-bottom:2px}}
.mini-val{{font-size:.95rem;font-weight:700}}
.detail-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
.detail-grid h4{{font-size:.78rem;text-transform:uppercase;letter-spacing:.5px;color:#888;margin-bottom:8px}}
.detail-grid ul{{list-style:none}}
.detail-grid li{{font-size:.85rem;padding:3px 0 3px 14px;position:relative}}
.detail-grid li::before{{content:'•';position:absolute;left:0;color:#1565c0}}
.flags-container{{display:flex;flex-direction:column;gap:10px}}
.flag-row{{border-left:4px solid #c62828;background:#fff5f5;border-radius:0 8px 8px 0;padding:12px 16px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 12px;align-items:center}}
.flag-recs{{grid-column:2/4;font-size:.83rem;color:#666}}
.vuln-num{{font-weight:700;color:#c62828;white-space:nowrap}}
.ok-note{{color:#2e7d32;font-weight:600}}
.recs-container{{display:flex;flex-direction:column;gap:8px}}
.rec-row{{display:flex;gap:12px;align-items:flex-start;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:.9rem}}
.rec-num{{width:26px;height:26px;border-radius:50%;background:#1565c0;color:#fff;display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:700;flex-shrink:0}}
footer{{text-align:center;padding:24px;color:#aaa;font-size:.8rem}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <h1>{course_name}</h1>
      <p class="header-meta">Course {course_id} &nbsp;·&nbsp; AI Readiness Report &nbsp;·&nbsp; {generated}</p>
    </div>
    <div class="overall-badge" style="border-color:{ov_color}">
      <div class="overall-num" style="color:{ov_color}">{avg_ready:.1f}</div>
      <div class="overall-label">{ov_label}</div>
      <div style="font-size:.75rem;opacity:.7">/ 10</div>
    </div>
  </div>
</header>
<div class="page">
  <section class="section">
    <h2 class="section-title">Course-Level Scores</h2>
    <div class="metrics-grid">
      {card("AI Leverage Potential", avg_leverage)}
      {card("Cheating Vulnerability", avg_vuln, inverted=True)}
      {card("Authenticity Score",     avg_auth)}
      {card("Pedagogical Quality",    avg_qual)}
    </div>
  </section>
  <section class="section">
    <h2 class="section-title">Breakdown by Content Type</h2>
    <table class="summary-table">
      <thead><tr><th>Type</th><th>Count</th><th>Avg AI Readiness</th><th>Avg Vulnerability</th><th>Avg Bloom's</th></tr></thead>
      <tbody>{type_rows}</tbody>
    </table>
  </section>
  <section class="section">
    <h2 class="section-title">⚠ High-Risk Items (Vulnerability ≥ 7.0)</h2>
    <div class="flags-container">{flags_html}</div>
  </section>
  <section class="section">
    <h2 class="section-title">Top Recommendations (Prioritized by Risk)</h2>
    <div class="recs-container">{recs_html}</div>
  </section>
  <section class="section">
    <h2 class="section-title">Full Item Analysis</h2>
    <div class="filter-bar">
      <button class="filter-btn active" onclick="filterItems('all',this)">All</button>
      <button class="filter-btn" onclick="filterItems('assignment',this)">Assignments</button>
      <button class="filter-btn" onclick="filterItems('page',this)">Pages</button>
      <button class="filter-btn" onclick="filterItems('quiz',this)">Quizzes</button>
      <button class="filter-btn" onclick="filterItems('discussion',this)">Discussions</button>
      <button class="filter-btn" onclick="filterItems('high-risk',this)">⚠ High Risk</button>
    </div>
    <div id="items-list">{item_rows_html}</div>
  </section>
</div>
<footer><p>Generated by canvas-to-hax &nbsp;·&nbsp; {len(instructional)} items evaluated &nbsp;·&nbsp; {generated}</p></footer>
<script>
function filterItems(type,btn){{
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.item-row').forEach(row=>{{
    if(type==='all'){{row.style.display='';return;}}
    if(type==='high-risk'){{row.style.display=row.dataset.vuln>=7?'':'none';return;}}
    row.style.display=row.dataset.type===type||row.dataset.type===type+'s'?'':'none';
  }});
}}
</script>
</body>
</html>"""


# ── standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Phase 2.5 — LLM-based AI readiness evaluation"
    )
    parser.add_argument("--course_id",      required=True, type=str)
    parser.add_argument("--db_path",        default="course_pipeline.db")
    parser.add_argument("--model_provider", default="nebula",
                        choices=["openai", "anthropic", "gemini", "nebula"])
    parser.add_argument("--workers",        default=3, type=int,
                        help="Parallel LLM worker threads (default: 3)")
    parser.add_argument("--output_dir",     default=None,
                        help="Directory for the HTML report (default: current dir)")
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as db:
        ai = build_ai_client(args.model_provider)
        evaluate_course_data(db, args.course_id, ai,
                             workers=args.workers, output_dir=args.output_dir)
