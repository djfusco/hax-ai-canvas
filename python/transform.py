"""
transform.py — Phase 3: AI Content Enhancement

Transforms PENDING course items into rich, pedagogically-enhanced HTML using
evaluation-informed, per-type prompts derived from v2.

Key improvements over the previous version
───────────────────────────────────────────
• Per-type prompts  — separate prompts for page, assignment, quiz, discussion,
                      and syllabus, each with type-specific structure guidance
• Evaluation context — every prompt includes the item's evaluation scores and
                       targeted ACTION directives (e.g. "HIGH vulnerability —
                       redesign for non-delegatable work")
• Full content       — 4 000–5 000 character content window (was 1 000)
• Concurrent workers — ThreadPoolExecutor for parallel LLM calls
• HTML output        — produces rich semantic HTML (h2/h3/p/table/details),
                       not generic markdown
• No globals         — all state is local; safe for concurrent execution

Usage (standalone)
──────────────────
  python transform.py --course_id 2446743 --model_provider nebula
  python transform.py --course_id 2446743 --workers 5 --no_ai

Usage (as module)
─────────────────
  from transform import transform_course_data
  transform_course_data(db_client, course_id, model_provider="nebula", workers=3)
"""

from __future__ import annotations

import concurrent.futures
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ai_client import AIClient, build_ai_client
from sqlite_client import SQLiteClient


# ── content helpers ───────────────────────────────────────────────────────────

def _clean(html: str) -> str:
    """Strip HTML tags, return readable plain text."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences so HTML output is clean."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# ── evaluation context builder ────────────────────────────────────────────────

def _eval_ctx(ev: Optional[Dict], item_type: str = "") -> str:
    """
    Build an evaluation context block injected into the enhancement prompt.
    When cheating vulnerability or other scores are extreme, adds explicit
    ACTION directives so the LLM knows what to prioritise.
    """
    if not ev:
        return ""

    vuln  = ev.get("cheating_vulnerability", 5.0)
    auth  = ev.get("authenticity_score",     5.0)
    trans = ev.get("transparency_score",     5.0)
    align = ev.get("alignment_score",        5.0)
    bl    = ev.get("blooms_level",           3)
    bl_lb = ev.get("blooms_label",           "Apply")

    vuln_label = (
        "HIGH RISK — aggressive redesign required"
        if vuln >= 7 else
        "moderate" if vuln >= 5 else "low"
    )

    lines = [
        "",
        "── EVALUATION CONTEXT (use this to guide your enhancement) ──",
        f"• Bloom's Level   : {bl_lb} ({bl}/6)",
        f"• AIAS Level      : {ev.get('aias_label', '')} ({ev.get('aias_level', 2)}/5)",
        f"• AI Leverage     : {ev.get('ai_leverage', 5.0):.1f}/10",
        f"• Cheat Vuln.     : {vuln:.1f}/10  ({vuln_label})",
        f"• Authenticity    : {auth:.1f}/10",
        f"• Ped. Quality    : {ev.get('pedagogical_quality', 5.0):.1f}/10",
        f"• Transparency    : {trans:.1f}/10",
        f"• Alignment       : {align:.1f}/10",
    ]

    concerns = ev.get("key_concerns", [])
    recs     = ev.get("priority_recommendations", [])
    if concerns:
        lines.append(f"• Key Concerns    : {'; '.join(concerns[:2])}")
    if recs:
        lines.append(f"• Priority Fixes  : {'; '.join(recs[:3])}")

    # ACTION directives — only when a score is genuinely extreme
    if bl <= 2:
        lines.append(f"ACTION: Content is at {bl_lb} level — PUSH to Analyze/Evaluate/Create.")
    if vuln >= 7:
        lines.append(
            "ACTION: HIGH vulnerability — redesign for authentic, "
            "non-delegatable, personal work."
        )
    if auth <= 4:
        lines.append(
            "ACTION: LOW authenticity — add personal context, unique data, "
            "or documented process."
        )
    if trans <= 4:
        lines.append(
            "ACTION: LOW transparency — add explicit rubric, success criteria, "
            "and worked examples."
        )
    if align <= 4:
        lines.append(
            "ACTION: WEAK alignment — ensure every task directly assesses "
            "the stated objectives."
        )

    lines.append("── END EVALUATION CONTEXT ──")
    return "\n".join(lines)


# ── shared system prompt ──────────────────────────────────────────────────────

_SYS = (
    "You are an expert instructional designer. Transform Canvas LMS content into "
    "rich, pedagogically-enhanced HTML for a HAX CMS website.\n"
    "Output ONLY the HTML content — NO <!DOCTYPE>, NO <html>, NO <head>, NO <body> tags.\n"
    "Use semantic HTML5: h2, h3, p, ul, ol, blockquote, table, figure, details/summary.\n"
    "Be thorough and substantive. Never truncate content. Include everything."
)


# ── per-type enhancement prompts ──────────────────────────────────────────────

def _enhance_page(content_dict: Dict, course_name: str, ev: Optional[Dict]) -> str:
    ctx   = _eval_ctx(ev, "page")
    title = content_dict.get("title", "Untitled")
    body  = _clean(content_dict.get("body", ""))[:5000]
    prompt = f"""Transform this Canvas course page into a rich, engaging, pedagogically-enhanced HTML page.{ctx}

COURSE: {course_name}
TITLE:  {title}

ORIGINAL CONTENT:
{body}

Produce a complete, well-structured page:

1. Opening <p class="intro"> (2-3 sentences — WHY this topic matters)
2. <h2>Learning Objectives</h2> — 3-4 specific, Bloom's-aligned objectives
   (higher-order verbs: analyze, evaluate, design, compare, synthesize)
3. Restructured main content with clear h2/h3, tight paragraphs, lists
4. <blockquote> for key definitions, formulas, or critical concepts
5. <table> wherever data benefits from tabular format
6. <details><summary>Deeper Dive</summary>…</details> for advanced content
7. Closing sections:
   <h2>Key Takeaways</h2>   — 4-5 bullet summary
   <h2>Reflect & Apply</h2> — 2 questions requiring synthesis or personal application
     (NOT simple recall — require genuine thinking)

Preserve every factual claim, technical detail, and example from the original.
Output ONLY the HTML content (no DOCTYPE/html/head/body)."""
    return prompt


def _enhance_assignment(content_dict: Dict, course_name: str, ev: Optional[Dict]) -> str:
    ctx        = _eval_ctx(ev, "assignment")
    name       = content_dict.get("name", "Untitled Assignment")
    desc       = _clean(content_dict.get("description", ""))[:5000]
    points     = content_dict.get("points_possible", 0) or 0
    due_at     = content_dict.get("due_at") or "See course schedule"
    sub_types  = ", ".join(content_dict.get("submission_types", []) or ["standard"])

    vuln = (ev or {}).get("cheating_vulnerability", 5.0)

    vuln_note = ""
    if vuln >= 7:
        vuln_note = (
            "\n\nCRITICAL: This assignment scored HIGH on cheating vulnerability. "
            "The primary redesign goal is to require genuine personal engagement "
            "that an AI cannot fabricate: self-documenting process artifacts, "
            "interviews with real people in the student's network, original local "
            "data collection, or arguments grounded in personal professional context."
        )

    ai_policy = ""
    if vuln >= 5.5 or points >= 10:
        ai_policy = """
<h2>AI & Academic Integrity Policy</h2>
Write a thoughtful, assignment-specific (NOT generic) policy covering:
• Whether AI tools are permitted, restricted, or prohibited for THIS assignment — and specifically why
• If permitted: exactly how (brainstorming only? drafting? code generation? which tools?)
• Required disclosure/citation format if AI was used
• What the student must be able to demonstrate or explain independently"""

    metacog = ""
    if vuln >= 6.0 or points >= 15:
        metacog = """
<h2>Metacognitive Reflection</h2>
Include as a submission component (not a separate assignment):
• What was the most challenging part? How did you work through it?
• What would you do differently with more time or resources?
• What connection do you see between this work and your future career or goals?"""

    prompt = f"""Redesign this Canvas assignment into a substantially more effective,
AI-resilient, and pedagogically rich assignment page.{ctx}{vuln_note}

COURSE    : {course_name}
TITLE     : {name}
POINTS    : {points}
DUE       : {due_at}
SUBMISSION: {sub_types}

ORIGINAL DESCRIPTION:
{desc}

Produce a complete, professional assignment page with these sections:

<h1>{name}</h1>

<h2>Assignment Overview</h2>
• Vivid, motivating description of what students will do
• Real-world or professional relevance — WHY this matters beyond the grade

<h2>Learning Objectives</h2>
• 3-4 specific objectives using Bloom's higher-order verbs (apply, analyze, evaluate, create)

<h2>Instructions</h2>
Numbered steps, each concrete and actionable.
If vulnerability is high (see context): include at least ONE component requiring authentic personal engagement.

<h2>Submission Requirements</h2>
Format · Length/scope · File type
Points: {points} | Due: {due_at}

<h2>Grading Rubric</h2>
<table> with: Criterion | Exemplary (90–100%) | Proficient (75–89%) | Developing (60–74%) | Beginning (<60%)
4-5 criteria with descriptive behavioral indicators — not vague labels like "Excellent work."{ai_policy}{metacog}

Output ONLY the HTML content (no DOCTYPE/html/head/body)."""
    return prompt


def _enhance_quiz(content_dict: Dict, course_name: str, ev: Optional[Dict]) -> str:
    ctx   = _eval_ctx(ev, "quiz")
    title = content_dict.get("title", "Untitled Quiz")
    desc  = _clean(content_dict.get("description", ""))[:800]
    qs    = content_dict.get("questions", [])
    time_limit = content_dict.get("time_limit", 0) or 0
    q_count    = content_dict.get("question_count", len(qs)) or len(qs)

    q_text = ""
    for i, q in enumerate(qs[:20], 1):
        q_text += f"\nQ{i} [{q.get('type','')}]: {_clean(q.get('question_text', q.get('text', '')))}\n"
        for ans in q.get("answers", [])[:5]:
            mark = "✓" if (ans.get("weight") or 0) > 0 else " "
            q_text += f"  [{mark}] {ans.get('text', '')}\n"

    prompt = f"""Transform this quiz into a richer study guide and assessment resource.{ctx}

COURSE: {course_name}  TITLE: {title}
TIME: {time_limit or 'None'} min  QUESTIONS: {q_count}
DESCRIPTION: {desc}

QUESTIONS SAMPLE:
{q_text[:3000]}

Produce a comprehensive assessment guide:

<h1>{title}</h1>

<h2>Assessment Overview</h2>
Purpose · format ({q_count} questions, {time_limit or 'no'} min limit) · what this measures

<h2>Learning Objectives Assessed</h2>
4-5 specific objectives (infer from question topics)

<h2>How to Prepare</h2>
5-6 specific, actionable strategies (not just "review your notes")
Key concepts inferred from the questions

<h2>Question Analysis: Recall → Higher-Order Upgrades</h2>
Select 4-5 questions and show how to make them better.
<table> columns: Original Question | Improved Version | Why It's Better
Improved versions should require application, analysis, or evaluation — not recall.

<h2>Test-Day Strategy</h2>
Time management · how to approach uncertain questions · format-specific tips

<h2>After the Assessment</h2>
How to use your results · connections to upcoming content · common misconceptions to avoid

Output ONLY the HTML content (no DOCTYPE/html/head/body)."""
    return prompt


def _enhance_discussion(content_dict: Dict, course_name: str, ev: Optional[Dict]) -> str:
    ctx     = _eval_ctx(ev, "discussion")
    title   = content_dict.get("title", "Untitled Discussion")
    message = _clean(content_dict.get("message", ""))[:4000]

    prompt = f"""Transform this Canvas discussion into a rich, engaged learning activity.{ctx}

COURSE: {course_name}
TITLE:  {title}

ORIGINAL PROMPT:
{message}

Produce a complete discussion activity page:

<h1>{title}</h1>

<h2>Why This Discussion Matters</h2>
Real-world relevance · connection to course themes · what you gain from genuine engagement

<h2>Learning Objectives</h2>
2-3 objectives this discussion supports

<h2>The Prompt</h2>
Enhanced, specific version requiring genuine personal engagement — not facts easily looked up.
Include 2-3 sub-questions guiding deeper thinking.
At least one prompt asking for YOUR perspective, experience, or local context specifically.

<h2>Response Requirements</h2>
<table> with: Component | Requirement (initial post length/evidence, peer responses, timeline)

<h2>Before You Post: Scaffolding</h2>
3-4 guiding questions to prompt deeper thinking before writing.
"Consider…" prompts that move beyond surface responses.

<h2>What Excellent Looks Like</h2>
4-5 characteristics of an exemplary initial post.
Common pitfalls to avoid.

Output ONLY the HTML content (no DOCTYPE/html/head/body)."""
    return prompt


def _enhance_syllabus(raw_text: str, course_name: str, ev: Optional[Dict]) -> str:
    ctx    = _eval_ctx(ev, "syllabus")
    prompt = f"""Improve this course syllabus for "{course_name}" while PRESERVING the instructor's structure and voice.{ctx}

ORIGINAL SYLLABUS:
{raw_text[:6000]}

RULES — read carefully before writing:
1. PRESERVE the existing section headings and overall organization.
2. ENRICH each existing section: improve clarity, add concrete examples, strengthen language.
3. If a section is missing but genuinely needed (e.g., no AI policy anywhere), ADD IT at the end.
4. Do NOT invent policies, dates, or content not implied by the original.
5. Only add an AI & Academic Integrity section if one does not already exist.
   If adding it, make it discipline-specific and nuanced — NOT generic boilerplate.
6. Preserve every original factual claim: dates, points, instructor names, course codes.

Output ONLY the HTML content (no DOCTYPE/html/head/body). Keep the instructor's voice."""
    return prompt


# ── content extraction ────────────────────────────────────────────────────────

def _get_prompt_for_item(item: Dict, course_name: str) -> Optional[str]:
    """
    Build the enhancement prompt for an item, injecting evaluation context.
    Returns None for item types we don't know how to enhance.
    """
    item_type   = item.get("item_type", "")
    raw_content = item.get("raw_content", "")
    ev: Optional[Dict] = None

    if item.get("evaluation"):
        try:
            ev = json.loads(item["evaluation"])
        except (json.JSONDecodeError, TypeError):
            ev = None

    # Syllabus: raw_content is plain text
    if item_type == "syllabus":
        return _enhance_syllabus(raw_content, course_name, ev)

    # All other types: raw_content is JSON
    try:
        content_dict = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        content_dict = {"body": raw_content}

    if item_type == "pages":
        return _enhance_page(content_dict, course_name, ev)
    if item_type == "assignments":
        return _enhance_assignment(content_dict, course_name, ev)
    if item_type == "quizzes":
        return _enhance_quiz(content_dict, course_name, ev)
    if item_type == "discussions":
        return _enhance_discussion(content_dict, course_name, ev)

    return None  # Unknown type — skip


# ── worker function ───────────────────────────────────────────────────────────

def _enhance_item(ai: AIClient, item: Dict, course_name: str) -> Tuple[str, str]:
    """
    Call the LLM to enhance a single item.
    Returns (item_id, enhanced_html).  Raises on failure.
    """
    prompt = _get_prompt_for_item(item, course_name)
    if prompt is None:
        raise ValueError(f"Unknown item_type: {item.get('item_type')!r}")
    html = _strip_fences(ai.call(_SYS, prompt, max_tokens=4096))
    return item["id"], html


# ── main transform entry point ────────────────────────────────────────────────

def transform_course_data(
    db_client,
    course_id:      str,
    model_provider: str  = "nebula",
    no_ai:          bool = False,
    workers:        int  = 3,
) -> None:
    """
    Phase 3: Enhance PENDING items using the LLM (or fallback when --no_ai).

    LLM calls are parallelised across `workers` threads.
    DB writes are serialised in the main thread to avoid SQLite contention.
    """
    pending = db_client.get_pending_items(course_id)
    if not pending:
        print(f"  No PENDING items found for course {course_id}.")
        return

    # Resolve course name for prompt context
    try:
        from extract import get_export_dir, get_course_title
        course_name = get_course_title(get_export_dir(course_id))
    except Exception:
        course_name = f"Course {course_id}"

    # ── no-AI fallback ────────────────────────────────────────────────────────
    if no_ai:
        print(f"  No-AI mode — using fallback content for {len(pending)} items.")
        for item in pending:
            title = item.get("title") or "Untitled"
            try:
                raw = json.loads(item.get("raw_content", "{}"))
                body = raw.get("body") or raw.get("description") or raw.get("message", "")
            except (json.JSONDecodeError, TypeError):
                body = item.get("raw_content", "")
            body = BeautifulSoup(body, "html.parser").get_text(separator=" ", strip=True) if body else ""
            html = f"<h1>{title}</h1>\n<p>{body[:2000]}</p>" if body else f"<h1>{title}</h1>"
            db_client.update_enhanced_item(item["id"], course_id, html)
        print(f"  ✅ Fallback applied to {len(pending)} items.")
        return

    # ── AI enhancement ────────────────────────────────────────────────────────
    ai = build_ai_client(model_provider)

    total   = len(pending)
    done    = 0
    success = 0
    skipped = 0

    print(f"  Enhancing {total} items · {workers} worker(s)\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {
            pool.submit(_enhance_item, ai, item, course_name): item
            for item in pending
        }

        for fut in concurrent.futures.as_completed(fut_map):
            item = fut_map[fut]
            done += 1
            label = (item.get("title") or item["id"])[:55]
            try:
                item_id, html = fut.result()
                db_client.update_enhanced_item(item_id, course_id, html)
                print(f"  [{done:>3}/{total}] ✅ {item['item_type']:<12} {label}")
                success += 1
            except Exception as exc:
                print(f"  [{done:>3}/{total}] ⚠️  {item['item_type']:<12} {label} — left PENDING: {exc}")
                skipped += 1

    print(f"\n--- Transform Summary ---")
    print(f"✅ Enhanced:  {success} items")
    print(f"⏭️  Skipped:   {skipped} items (left PENDING for retry)")

    # Generate AI changes review report after enhancement
    if success > 0:
        try:
            from changes_report import generate_changes_report
            generate_changes_report(db_client, course_id)
        except Exception as exc:
            print(f"  ⚠️  Changes report failed: {exc}")


# ── standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 3 — Transform PENDING items via LLM")
    parser.add_argument("--course_id",      required=True, type=str)
    parser.add_argument("--db_path",        default="course_pipeline.db")
    parser.add_argument("--model_provider", default="nebula",
                        choices=["openai", "anthropic", "gemini", "nebula"])
    parser.add_argument("--no_ai",          action="store_true")
    parser.add_argument("--workers",        default=3, type=int)
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as client:
        transform_course_data(
            client, args.course_id,
            model_provider=args.model_provider,
            no_ai=args.no_ai,
            workers=args.workers,
        )
