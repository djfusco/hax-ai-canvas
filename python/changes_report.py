"""
changes_report.py — AI Enhancement Review Report

Generates a standalone HTML file showing every AI-enhanced course item
side-by-side with its original Canvas content.

Features
────────
• Word-level inline diff  (green = added, red = removed)
• Side-by-side view toggle (sentence-aligned rows)
• Filter by content type (pages, assignments, quizzes, discussions)
• Filter by changed / unchanged
• Per-item evaluation scores (readiness, vulnerability, Bloom's)
• Summary stats (total, enhanced, unchanged)

Usage (standalone)
──────────────────
  python changes_report.py --course_id 2446743

Usage (as module)
─────────────────
  from changes_report import generate_changes_report
  generate_changes_report(db_client, course_id, output_path="course_2446743_ai_changes_report.html")
"""

from __future__ import annotations

import difflib
import html as _h
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from sqlite_client import SQLiteClient


# ── text helpers ──────────────────────────────────────────────────────────────

def _plain(html: str) -> str:
    """Strip HTML tags, collapse whitespace to plain text for diffing."""
    if not html:
        return ""
    try:
        return " ".join(
            BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split()
        )
    except Exception:
        return html


def _original_text(item: Dict) -> str:
    """Extract the original Canvas content from a DB item's raw_content."""
    item_type   = item.get("item_type", "")
    raw_content = item.get("raw_content", "") or ""

    try:
        d = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return _plain(raw_content)[:8000]

    if item_type == "pages":
        return _plain(d.get("body", ""))[:8000]
    if item_type == "assignments":
        return _plain(d.get("description", ""))[:8000]
    if item_type == "quizzes":
        desc = _plain(d.get("description", ""))
        qs   = d.get("questions", [])
        q_text = ""
        for i, q in enumerate(qs[:30], 1):
            q_text += f"\nQ{i}: {_plain(q.get('question_text', q.get('text', '')))}"
        return (desc + q_text)[:8000]
    if item_type == "discussions":
        return _plain(d.get("message", ""))[:8000]

    return _plain(raw_content)[:8000]


# ── diff renderers ────────────────────────────────────────────────────────────

def _inline_diff(orig: str, enh: str) -> str:
    """Word-level inline diff: deleted words in red, inserted in green."""
    orig_words = orig.split()
    enh_words  = enh.split()
    matcher    = difflib.SequenceMatcher(None, orig_words, enh_words, autojunk=False)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        ow = _h.escape(" ".join(orig_words[i1:i2]))
        ew = _h.escape(" ".join(enh_words[j1:j2]))
        if tag == "equal":
            out.append(ow)
        elif tag == "replace":
            out.append(f'<del class="diff-del">{ow}</del> <ins class="diff-ins">{ew}</ins>')
        elif tag == "delete":
            out.append(f'<del class="diff-del">{ow}</del>')
        elif tag == "insert":
            out.append(f'<ins class="diff-ins">{ew}</ins>')
    return f'<div class="inline-diff">{" ".join(out)}</div>'


def _side_by_side(orig: str, enh: str) -> str:
    """Sentence-aligned side-by-side table: original left, enhanced right."""
    def sentences(text: str) -> List[str]:
        parts = re.split(r'(?<=[.!?])\s+', text)
        return [p for p in parts if p.strip()] or [""]

    orig_lines = sentences(orig)
    enh_lines  = sentences(enh)
    opcodes    = difflib.SequenceMatcher(
        None, orig_lines, enh_lines, autojunk=False
    ).get_opcodes()

    rows = ""
    for tag, i1, i2, j1, j2 in opcodes:
        o_block = " ".join(_h.escape(s) for s in orig_lines[i1:i2]) if orig_lines[i1:i2] else ""
        e_block = " ".join(_h.escape(s) for s in enh_lines[j1:j2])  if enh_lines[j1:j2]  else ""
        if tag == "equal":
            rows += f'<tr class="eq"><td>{o_block}</td><td>{e_block}</td></tr>'
        elif tag == "replace":
            rows += f'<tr class="chg"><td class="orig-cell">{o_block}</td><td class="enh-cell">{e_block}</td></tr>'
        elif tag == "delete":
            rows += f'<tr class="del"><td class="orig-cell">{o_block}</td><td></td></tr>'
        elif tag == "insert":
            rows += f'<tr class="ins"><td></td><td class="enh-cell">{e_block}</td></tr>'

    return (
        '<div class="side-wrap">'
        '<table class="side-table">'
        '<thead><tr><th>Original (Canvas)</th><th>AI Enhanced (HAX)</th></tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table></div>'
    )


# ── evaluation badge ──────────────────────────────────────────────────────────

def _score_color(value: float, inverted: bool = False) -> str:
    v = (10.0 - value) if inverted else value
    if v >= 7.5: return "#2e7d32"
    if v >= 5.0: return "#e65100"
    return "#c62828"


def _eval_badge(evaluation: Optional[Dict]) -> str:
    if not evaluation:
        return ""
    rc = _score_color(evaluation.get("ai_readiness_score", 5.0))
    vc = _score_color(evaluation.get("cheating_vulnerability", 5.0), inverted=True)
    bl = evaluation.get("blooms_label", "Apply")
    rs = evaluation.get("ai_readiness_score", 5.0)
    cv = evaluation.get("cheating_vulnerability", 5.0)
    return (
        f'<span class="eval-pip" style="background:{rc}" title="AI Readiness">{rs:.1f}</span>'
        f'<span class="eval-pip" style="background:{vc}" title="Cheat Vuln">⚠{cv:.1f}</span>'
        f'<span class="bloom-tag" title="Bloom\'s">{bl}</span>'
    )


# ── main report function ──────────────────────────────────────────────────────

def generate_changes_report(
    db_client,
    course_id:   str,
    output_path: Optional[str] = None,
) -> Path:
    """
    Generate the AI changes review report for a course.

    Reads all COMPLETED items from the DB, compares original Canvas content
    against AI-enhanced content, and writes a standalone HTML report.

    Returns the path to the generated report.
    """
    completed = db_client.get_completed_items(course_id)
    if not completed:
        print("  ⚠️  No COMPLETED items — run transform first.")
        return None

    # Resolve course name
    try:
        from extract import get_export_dir, get_course_title
        course_name = get_course_title(get_export_dir(course_id))
    except Exception:
        course_name = f"Course {course_id}"

    generated = datetime.now().strftime("%B %d, %Y %H:%M")
    items_html = ""
    total = 0
    changed_count = 0

    for item in completed:
        if item.get("item_type") == "syllabus":
            continue  # Syllabus is context-only; skip diff

        total += 1
        item_type = item.get("item_type", "unknown")
        title     = item.get("title") or "Untitled"

        orig_text = _original_text(item)
        enh_raw   = item.get("ai_enhanced_markdown", "") or ""
        enh_text  = _plain(enh_raw)  # Strip HTML tags for diffing

        # Load evaluation JSON if available
        ev: Optional[Dict] = None
        if item.get("evaluation"):
            try:
                ev = json.loads(item["evaluation"])
            except (json.JSONDecodeError, TypeError):
                ev = None

        changed = orig_text.strip() != enh_text.strip() and bool(enh_text.strip())
        if changed:
            changed_count += 1

        ev_badge = _eval_badge(ev)

        if changed:
            diff_html = _inline_diff(orig_text, enh_text)
            side_html = _side_by_side(orig_text, enh_text)
            changed_cls = "changed"
            pill_cls    = "pill-changed"
            pill_lbl    = "&#10002; enhanced"
        else:
            diff_html = "<p class='no-change-note'>No changes — content was not enhanced.</p>"
            side_html = "<p class='no-change-note'>No changes — content was not enhanced.</p>"
            changed_cls = "unchanged"
            pill_cls    = "pill-same"
            pill_lbl    = "&mdash; unchanged"

        safe_title = _h.escape(title[:90])
        items_html += f"""
<details class="item-block {changed_cls}" data-type="{item_type}" data-changed="{str(changed).lower()}">
  <summary>
    <span class="type-badge badge-{item_type}">{item_type}</span>
    <span class="item-title">{safe_title}</span>
    {ev_badge}
    <span class="change-pill {pill_cls}">{pill_lbl}</span>
    <span class="expand-arrow">&#9658;</span>
  </summary>
  <div class="item-body">
    <div class="view-tabs">
      <button class="tab-btn active" onclick="showTab(this,'diff')">Highlighted Changes</button>
      <button class="tab-btn" onclick="showTab(this,'side')">Side by Side</button>
    </div>
    <div class="tab-panel" data-tab="diff">{diff_html}</div>
    <div class="tab-panel" data-tab="side" style="display:none">{side_html}</div>
  </div>
</details>"""

    unchanged_count = total - changed_count

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Changes Review — {_h.escape(course_name)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.6;font-size:15px}}
header{{background:linear-gradient(135deg,#1b5e20 0%,#2e7d32 60%,#66bb6a 100%);color:#fff;padding:32px 40px}}
.header-inner{{display:flex;justify-content:space-between;align-items:flex-start;max-width:1200px;margin:0 auto;gap:24px;flex-wrap:wrap}}
h1{{font-size:1.8rem;font-weight:700;margin-bottom:6px}}
.header-meta{{opacity:.85;font-size:.9rem;margin-bottom:6px}}
.header-sub{{opacity:.75;font-size:.82rem;max-width:560px;line-height:1.5}}
.stat-group{{display:flex;gap:12px;flex-shrink:0}}
.stat-box{{background:rgba(255,255,255,.18);border-radius:12px;padding:14px 20px;text-align:center;min-width:80px}}
.changed-stat{{background:rgba(255,255,255,.28)}}
.stat-num{{font-size:1.9rem;font-weight:800;line-height:1}}
.stat-lbl{{font-size:.75rem;opacity:.85;margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.page{{max-width:1200px;margin:0 auto;padding:24px}}
.filter-bar{{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}}
.filter-btn{{padding:5px 16px;border-radius:20px;border:1px solid #d0d5dd;background:#fff;cursor:pointer;font-size:.82rem;font-weight:500}}
.filter-btn.active{{background:#2e7d32;color:#fff;border-color:#2e7d32}}
details.item-block{{background:#fff;border-radius:10px;margin-bottom:10px;box-shadow:0 1px 5px rgba(0,0,0,.06);overflow:hidden}}
details.item-block.changed{{border-left:4px solid #2e7d32}}
details.item-block.unchanged{{border-left:4px solid #ccc}}
details.item-block summary{{padding:13px 18px;cursor:pointer;display:flex;align-items:center;gap:10px;list-style:none;flex-wrap:wrap}}
details.item-block summary::-webkit-details-marker{{display:none}}
details.item-block summary:hover{{background:#f8fdf8}}
.item-title{{flex:1;font-weight:500;font-size:.9rem;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.expand-arrow{{color:#bbb;font-size:.9rem;margin-left:4px}}
details[open] .expand-arrow{{transform:rotate(90deg);display:inline-block}}
.change-pill{{padding:2px 10px;border-radius:10px;font-size:.75rem;font-weight:700;white-space:nowrap}}
.pill-changed{{background:#e8f5e9;color:#1b5e20}}
.pill-same{{background:#f5f5f5;color:#999}}
.type-badge{{display:inline-block;padding:2px 10px;border-radius:10px;font-size:.74rem;font-weight:700;white-space:nowrap}}
.badge-pages,.badge-page{{background:#e3f2fd;color:#0d47a1}}
.badge-assignments,.badge-assignment{{background:#f3e5f5;color:#6a1b9a}}
.badge-quizzes,.badge-quiz{{background:#e8f5e9;color:#1b5e20}}
.badge-discussions,.badge-discussion{{background:#fff3e0;color:#e65100}}
.badge-syllabus{{background:#fce4ec;color:#880e4f}}
.eval-pip{{padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:700;color:#fff;white-space:nowrap}}
.bloom-tag{{font-size:.75rem;background:#e8eaf6;color:#3949ab;padding:2px 8px;border-radius:8px;white-space:nowrap}}
.item-body{{padding:16px 20px 20px;border-top:1px solid #f0f0f0}}
.view-tabs{{display:flex;gap:8px;margin-bottom:12px}}
.tab-btn{{padding:4px 16px;border-radius:16px;border:1px solid #d0d5dd;background:#fff;cursor:pointer;font-size:.8rem;font-weight:500}}
.tab-btn.active{{background:#1565c0;color:#fff;border-color:#1565c0}}
.inline-diff{{font-size:.88rem;line-height:1.8;background:#fafafa;border-radius:8px;padding:16px;border:1px solid #e8e8e8;white-space:pre-wrap;word-break:break-word}}
del.diff-del{{background:#ffcdd2;color:#b71c1c;text-decoration:line-through;padding:0 2px;border-radius:2px}}
ins.diff-ins{{background:#c8e6c9;color:#1b5e20;font-weight:600;text-decoration:none;padding:0 2px;border-radius:2px}}
.no-change-note{{color:#aaa;font-style:italic;font-size:.88rem;padding:8px 0}}
.side-wrap{{overflow-x:auto}}
.side-table{{width:100%;border-collapse:collapse;font-size:.83rem}}
.side-table th{{background:#f5f7fa;padding:8px 12px;text-align:left;font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0}}
.side-table td{{padding:6px 12px;vertical-align:top;border-bottom:1px solid #f0f0f0;line-height:1.6}}
.side-table tr.eq td{{background:#fff}}
.side-table tr.chg .orig-cell{{background:#ffebee}}
.side-table tr.chg .enh-cell{{background:#e8f5e9}}
.side-table tr.del .orig-cell{{background:#ffcdd2}}
.side-table tr.ins .enh-cell{{background:#c8e6c9}}
footer{{text-align:center;padding:24px;color:#aaa;font-size:.8rem}}
@media(max-width:640px){{header{{padding:20px}}h1{{font-size:1.3rem}}.stat-group{{display:none}}.item-title{{font-size:.82rem}}.eval-pip,.bloom-tag{{display:none}}}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <h1>{_h.escape(course_name)}</h1>
      <p class="header-meta">AI Enhancement Review &nbsp;&middot;&nbsp; {generated}</p>
      <p class="header-sub">Review every AI-proposed change before using the HAX site.
        <strong>Green&nbsp;=&nbsp;added&nbsp;/&nbsp;improved.</strong>
        <strong>Red&nbsp;=&nbsp;removed&nbsp;/&nbsp;replaced.</strong>
        Expand each item to inspect changes in detail.</p>
    </div>
    <div class="stat-group">
      <div class="stat-box"><div class="stat-num">{total}</div><div class="stat-lbl">Total Items</div></div>
      <div class="stat-box changed-stat"><div class="stat-num">{changed_count}</div><div class="stat-lbl">Enhanced</div></div>
      <div class="stat-box"><div class="stat-num">{unchanged_count}</div><div class="stat-lbl">Unchanged</div></div>
    </div>
  </div>
</header>
<div class="page">
  <div class="filter-bar">
    <button class="filter-btn active" onclick="filterItems('all',this)">All ({total})</button>
    <button class="filter-btn" onclick="filterItems('changed',this)">Enhanced ({changed_count})</button>
    <button class="filter-btn" onclick="filterItems('unchanged',this)">Unchanged ({unchanged_count})</button>
    <button class="filter-btn" onclick="filterItems('assignments',this)">Assignments</button>
    <button class="filter-btn" onclick="filterItems('pages',this)">Pages</button>
    <button class="filter-btn" onclick="filterItems('quizzes',this)">Quizzes</button>
    <button class="filter-btn" onclick="filterItems('discussions',this)">Discussions</button>
  </div>
  <div id="items-container">{items_html}</div>
</div>
<footer>
  <p>Generated by canvas-to-hax &nbsp;&middot;&nbsp; {total} items reviewed &nbsp;&middot;&nbsp; {generated}</p>
  <p style="margin-top:6px;opacity:.7">The HAX site uses the <strong>Enhanced</strong> versions.
    Review flagged changes with your instructional designer before publishing.</p>
</footer>
<script>
function filterItems(type,btn){{
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.item-block').forEach(row=>{{
    if(type==='all'){{row.style.display='';return;}}
    if(type==='changed'){{row.style.display=row.dataset.changed==='true'?'':'none';return;}}
    if(type==='unchanged'){{row.style.display=row.dataset.changed==='false'?'':'none';return;}}
    row.style.display=row.dataset.type===type?'':'none';
  }});
}}
function showTab(btn,tab){{
  var body=btn.closest('.item-body');
  body.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  body.querySelectorAll('.tab-panel').forEach(p=>{{p.style.display=p.dataset.tab===tab?'':'none';}});
}}
</script>
</body>
</html>"""

    # Write report
    if output_path:
        report_path = Path(output_path)
    else:
        report_path = Path(f"course_{course_id}_ai_changes_report.html")

    report_path.write_text(html, encoding="utf-8")
    print(f"  📝 AI Changes Report: {report_path}  ({changed_count}/{total} items enhanced)")
    return report_path


# ── standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate AI changes review report (original vs enhanced)"
    )
    parser.add_argument("--course_id",   required=True, type=str)
    parser.add_argument("--db_path",     default="course_pipeline.db")
    parser.add_argument("--output_path", default=None,
                        help="Output HTML path (default: course_<id>_ai_changes_report.html)")
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as db:
        generate_changes_report(db, args.course_id, output_path=args.output_path)
