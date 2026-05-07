"""
dashboard_report.py — AI Readiness Dashboard Report

Generates a visually rich standalone HTML dashboard inspired by
learn-ai-insight.lovable.app.  All styles are inline so the file
can be opened directly in any browser.

Usage (as module)
─────────────────
  from dashboard_report import generate_dashboard_report
  generate_dashboard_report(db_client, course_id)
"""

from __future__ import annotations

import html as _h
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlite_client import SQLiteClient


# ── colour helpers ────────────────────────────────────────────────────────────

_TYPE_COLORS: Dict[str, str] = {
    "assignments":  "#1565c0",
    "quizzes":      "#7b1fa2",
    "discussions":  "#2e7d32",
    "pages":        "#e65100",
}

def _score_color(v: float) -> str:
    if v >= 7.0: return "#2e7d32"
    if v >= 4.0: return "#e65100"
    return "#c62828"

def _risk_color(v: float) -> str:
    if v >= 7.0: return "#c62828"
    if v >= 4.0: return "#e65100"
    return "#2e7d32"

def _readiness_label(score: float) -> Tuple[str, str]:
    if score >= 8.0: return "Excellent",  "#1b5e20"
    if score >= 6.5: return "Good",       "#2e7d32"
    if score >= 5.0: return "Moderate",   "#e65100"
    if score >= 3.5: return "Fair",       "#bf360c"
    return "Needs Work", "#b71c1c"


# ── data extraction ───────────────────────────────────────────────────────────

def _extract_items(db_client, course_id: str) -> List[Dict]:
    """Pull all evaluated items from the DB and enrich with raw_content metadata."""
    all_items = db_client.get_completed_items(course_id) + \
                db_client.get_pending_items(course_id)

    results = []
    for item in all_items:
        if item.get("item_type") == "syllabus":
            continue
        ev_json = item.get("evaluation")
        if not ev_json:
            continue
        try:
            ev = json.loads(ev_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if ev.get("noninstructional"):
            continue

        # Extract due_at / points from raw_content
        due_at = None
        points = None
        try:
            raw = json.loads(item.get("raw_content", "{}"))
            due_at = raw.get("due_at")
            points = raw.get("points_possible")
        except (json.JSONDecodeError, TypeError):
            pass

        results.append({
            "title":     ev.get("title", item.get("title", "Untitled")),
            "item_type": ev.get("item_type", item.get("item_type", "unknown")),
            "due_at":    due_at,
            "points":    points,
            "ai_leverage":            ev.get("ai_leverage", 5.0),
            "cheating_vulnerability": ev.get("cheating_vulnerability", 5.0),
            "ai_readiness_score":     ev.get("ai_readiness_score", 5.0),
            "blooms_label":           ev.get("blooms_label", "Apply"),
            "authenticity_score":     ev.get("authenticity_score", 5.0),
            "pedagogical_quality":    ev.get("pedagogical_quality", 5.0),
        })
    return results


# ── SVG scatter plot ──────────────────────────────────────────────────────────

def _scatter_svg(items: List[Dict], width: int = 520, height: int = 340) -> str:
    pad = 50
    pw = width - 2 * pad
    ph = height - 2 * pad

    # Axes
    svg = f'<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;font-family:inherit">'
    # Grid lines
    for i in range(11):
        x = pad + pw * i / 10
        y = pad + ph * i / 10
        svg += f'<line x1="{x}" y1="{pad}" x2="{x}" y2="{pad+ph}" stroke="#eee" />'
        svg += f'<line x1="{pad}" y1="{y}" x2="{pad+pw}" y2="{y}" stroke="#eee" />'
        svg += f'<text x="{x}" y="{height-12}" text-anchor="middle" fill="#999" font-size="11">{i}</text>'
        svg += f'<text x="{pad-8}" y="{pad + ph - ph*i/10 + 4}" text-anchor="end" fill="#999" font-size="11">{i}</text>'
    # Axis labels
    svg += f'<text x="{width/2}" y="{height-0}" text-anchor="middle" fill="#666" font-size="12" font-weight="600">AI Leverage →</text>'
    svg += f'<text x="14" y="{height/2}" text-anchor="middle" fill="#666" font-size="12" font-weight="600" transform="rotate(-90,14,{height/2})">Cheating Risk →</text>'
    # Axes lines
    svg += f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{pad+ph}" stroke="#ccc" stroke-width="1.5" />'
    svg += f'<line x1="{pad}" y1="{pad+ph}" x2="{pad+pw}" y2="{pad+ph}" stroke="#ccc" stroke-width="1.5" />'

    # Danger zone background
    svg += f'<rect x="{pad + pw*0.7}" y="{pad}" width="{pw*0.3}" height="{ph*0.3}" fill="rgba(255,235,238,0.4)" rx="4" />'

    # Points
    for item in items:
        lev = item["ai_leverage"]
        risk = item["cheating_vulnerability"]
        cx = pad + pw * lev / 10
        cy = pad + ph - ph * risk / 10
        color = _TYPE_COLORS.get(item["item_type"], "#888")
        title_esc = _h.escape(item["title"])
        svg += (
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" '
            f'fill="{color}" fill-opacity="0.8" stroke="#fff" stroke-width="1.5">'
            f'<title>{title_esc}\nLeverage: {lev:.1f} | Risk: {risk:.1f}</title>'
            f'</circle>'
        )

    svg += '</svg>'
    return svg


# ── due dates timeline ────────────────────────────────────────────────────────

def _due_dates_html(items: List[Dict]) -> str:
    dated = []
    for it in items:
        if not it.get("due_at"):
            continue
        try:
            dt = datetime.fromisoformat(it["due_at"].replace("Z", "+00:00"))
            dated.append((dt, it))
        except (ValueError, TypeError):
            continue
    if not dated:
        return '<p style="color:#999;font-size:.9rem">No due dates found in course data.</p>'

    dated.sort(key=lambda x: x[0])
    rows = ""
    for dt, it in dated:
        date_str = dt.strftime("%b %d")
        pts = f"{it['points']:.0f}pts" if it.get("points") else ""
        color = _TYPE_COLORS.get(it["item_type"], "#888")
        rows += (
            f'<div class="due-row">'
            f'<div class="due-date" style="background:{color}">{date_str}</div>'
            f'<div class="due-info">'
            f'<div class="due-title">{_h.escape(it["title"])}</div>'
            f'<div class="due-pts">{pts}</div>'
            f'</div></div>'
        )
    return rows


# ── detail table ──────────────────────────────────────────────────────────────

def _detail_table(items: List[Dict]) -> str:
    rows = ""
    for it in items:
        due = ""
        if it.get("due_at"):
            try:
                dt = datetime.fromisoformat(it["due_at"].replace("Z", "+00:00"))
                due = dt.strftime("%b %d, %Y")
            except (ValueError, TypeError):
                pass
        pts = f"{it['points']:.0f}" if it.get("points") else "—"
        lev = it["ai_leverage"]
        risk = it["cheating_vulnerability"]
        ready = it["ai_readiness_score"]
        lev_c = _score_color(lev)
        risk_c = _risk_color(risk)
        ready_c = _score_color(ready)
        itype = it["item_type"].rstrip("s")  # "assignments" → "assignment"
        rows += (
            f'<tr data-type="{_h.escape(it["item_type"])}">'
            f'<td>{_h.escape(it["title"])}</td>'
            f'<td><span class="type-tag" style="background:{_TYPE_COLORS.get(it["item_type"],"#888")}">{_h.escape(itype)}</span></td>'
            f'<td>{due}</td>'
            f'<td style="text-align:right">{pts}</td>'
            f'<td style="text-align:center"><span class="score-pill" style="background:{lev_c}">{lev:.1f}</span></td>'
            f'<td style="text-align:center"><span class="score-pill" style="background:{risk_c}">{risk:.1f}</span></td>'
            f'<td style="text-align:center"><span class="score-pill" style="background:{ready_c}">{ready:.1f}</span></td>'
            f'<td>{_h.escape(it["blooms_label"])}</td>'
            f'</tr>'
        )
    return rows


# ── main generator ────────────────────────────────────────────────────────────

def generate_dashboard_report(
    db_client,
    course_id:   str,
    output_dir:  Optional[str] = None,
) -> Optional[Path]:
    """Generate the AI Readiness Dashboard HTML for a course."""
    items = _extract_items(db_client, course_id)
    if not items:
        print("  ⚠️  No evaluated items — dashboard skipped.")
        return None

    # Resolve course name
    try:
        from extract import get_export_dir, get_course_title
        course_name = get_course_title(get_export_dir(course_id))
    except Exception:
        course_name = f"Course {course_id}"

    # ── aggregate stats ───────────────────────────────────────────────────
    total = len(items)
    avg_leverage = sum(i["ai_leverage"] for i in items) / total
    avg_risk     = sum(i["cheating_vulnerability"] for i in items) / total
    avg_ready    = sum(i["ai_readiness_score"] for i in items) / total
    high_leverage = sum(1 for i in items if i["ai_leverage"] >= 7)
    high_risk     = sum(1 for i in items if i["cheating_vulnerability"] >= 7)
    ready_label, ready_color = _readiness_label(avg_ready)

    # Per-type stats
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for it in items:
        by_type[it["item_type"]].append(it)

    type_abbrev = {"assignments": "A", "quizzes": "Q", "discussions": "D", "pages": "P"}
    type_summary = " / ".join(
        f"{len(v)}{type_abbrev.get(k, k[0].upper())}" for k, v in sorted(by_type.items())
    )

    # Type breakdown cards
    type_cards = ""
    for itype, type_items in sorted(by_type.items()):
        t_lev  = sum(i["ai_leverage"] for i in type_items) / len(type_items)
        t_risk = sum(i["cheating_vulnerability"] for i in type_items) / len(type_items)
        color  = _TYPE_COLORS.get(itype, "#888")
        type_cards += (
            f'<div class="type-card">'
            f'<div class="type-card-header" style="border-color:{color}">'
            f'<span class="type-dot" style="background:{color}"></span> {_h.escape(itype)}'
            f'</div>'
            f'<div class="type-card-scores">'
            f'<span><strong>{t_lev:.1f}</strong> leverage</span>'
            f'<span><strong>{t_risk:.1f}</strong> risk</span>'
            f'</div></div>'
        )

    # Filter buttons for types
    filter_buttons = '<button class="filter-btn active" onclick="filterTable(\'all\',this)">All</button>'
    for itype in sorted(by_type.keys()):
        label = itype.rstrip("s").capitalize()
        filter_buttons += f'<button class="filter-btn" onclick="filterTable(\'{_h.escape(itype)}\',this)">{label}</button>'

    generated = datetime.now().strftime("%B %d, %Y %H:%M")

    # ── HTML ──────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Dashboard — {_h.escape(course_name)}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f4f6f9;color:#1a1a2e;line-height:1.6;font-size:15px}}
.header{{background:linear-gradient(135deg,#1565c0 0%,#1976d2 70%,#42a5f5 100%);color:#fff;padding:28px 32px}}
.header h1{{font-size:1.5rem;font-weight:700}}
.header-sub{{opacity:.85;font-size:.9rem;margin-top:4px}}
.page{{max-width:960px;margin:0 auto;padding:24px}}
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}}
.summary-card{{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.summary-card .label{{font-size:.78rem;text-transform:uppercase;letter-spacing:.5px;color:#888;margin-bottom:4px}}
.summary-card .value{{font-size:1.8rem;font-weight:800;line-height:1.2}}
.summary-card .sub{{font-size:.82rem;color:#666;margin-top:4px}}
.readiness-card{{text-align:center;border:2px solid {ready_color}}}
.readiness-card .value{{color:{ready_color}}}
.readiness-card .badge{{display:inline-block;background:{ready_color};color:#fff;padding:3px 14px;border-radius:12px;font-size:.8rem;font-weight:700;margin-top:6px}}
.section{{margin-bottom:28px}}
.section-title{{font-size:1.1rem;font-weight:700;color:#1a237e;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid #e8eaf6}}
.type-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.type-card{{background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.05)}}
.type-card-header{{font-weight:700;font-size:.92rem;margin-bottom:8px;padding-bottom:6px;border-bottom:3px solid #eee;display:flex;align-items:center;gap:8px}}
.type-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.type-card-scores{{display:flex;justify-content:space-between;font-size:.85rem;color:#555}}
.type-card-scores strong{{color:#1a1a2e}}
.chart-row{{display:grid;grid-template-columns:1.1fr .9fr;gap:20px;margin-bottom:24px}}
@media(max-width:700px){{.chart-row{{grid-template-columns:1fr}}}}
.chart-box,.due-box{{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;font-size:.8rem;color:#666}}
.legend-dot{{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:4px;vertical-align:middle}}
.due-row{{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #f0f0f0}}
.due-row:last-child{{border-bottom:none}}
.due-date{{padding:4px 10px;border-radius:8px;color:#fff;font-weight:700;font-size:.82rem;white-space:nowrap}}
.due-info{{flex:1}}
.due-title{{font-weight:600;font-size:.9rem}}
.due-pts{{font-size:.78rem;color:#888}}
.filter-bar{{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}}
.filter-btn{{padding:6px 14px;border:1.5px solid #e0e0e0;background:#fff;border-radius:20px;font-size:.82rem;font-weight:600;cursor:pointer;color:#666}}
.filter-btn.active{{background:#1565c0;color:#fff;border-color:#1565c0}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}}
thead th{{text-align:left;padding:10px 8px;border-bottom:2px solid #e0e0e0;font-size:.78rem;text-transform:uppercase;letter-spacing:.4px;color:#888}}
tbody td{{padding:10px 8px;border-bottom:1px solid #f0f0f0}}
tbody tr:hover{{background:#f8f9ff}}
.type-tag{{display:inline-block;padding:2px 10px;border-radius:10px;color:#fff;font-size:.75rem;font-weight:700}}
.score-pill{{display:inline-block;padding:2px 10px;border-radius:10px;color:#fff;font-size:.82rem;font-weight:700;min-width:40px;text-align:center}}
footer{{text-align:center;padding:24px;color:#aaa;font-size:.8rem}}
</style>
</head>
<body>
<div class="header">
  <h1>AI Readiness Dashboard</h1>
  <div class="header-sub">{_h.escape(course_name)} &nbsp;·&nbsp; Course {course_id} &nbsp;·&nbsp; {generated}</div>
</div>
<div class="page">

  <div class="summary-grid">
    <div class="summary-card">
      <div class="label">Total Items</div>
      <div class="value">{total}</div>
      <div class="sub">{type_summary}</div>
    </div>
    <div class="summary-card">
      <div class="label">Avg AI Leverage</div>
      <div class="value" style="color:{_score_color(avg_leverage)}">{avg_leverage:.1f}</div>
      <div class="sub">{high_leverage} high-leverage item{"s" if high_leverage != 1 else ""}</div>
    </div>
    <div class="summary-card">
      <div class="label">Avg Cheating Risk</div>
      <div class="value" style="color:{_risk_color(avg_risk)}">{avg_risk:.1f}</div>
      <div class="sub">{high_risk} high-risk item{"s" if high_risk != 1 else ""}</div>
    </div>
    <div class="summary-card readiness-card">
      <div class="label">AI Readiness Score</div>
      <div class="value">{avg_ready:.1f}</div>
      <div class="badge">{ready_label}</div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">By Content Type</h2>
    <div class="type-cards">{type_cards}</div>
  </div>

  <div class="chart-row">
    <div class="chart-box">
      <h2 class="section-title">Risk vs. Leverage</h2>
      {_scatter_svg(items)}
      <div class="legend">
        {"".join(f'<span><span class="legend-dot" style="background:{c}"></span> {t.capitalize()}</span>' for t, c in _TYPE_COLORS.items() if t in by_type)}
      </div>
    </div>
    <div class="due-box">
      <h2 class="section-title">Upcoming Due Dates</h2>
      {_due_dates_html(items)}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">All Items</h2>
    <div class="filter-bar">{filter_buttons}</div>
    <div style="overflow-x:auto">
    <table id="items-table">
      <thead><tr>
        <th>Title</th><th>Type</th><th>Due Date</th><th style="text-align:right">Points</th>
        <th style="text-align:center">AI Leverage</th><th style="text-align:center">Cheating Risk</th>
        <th style="text-align:center">Readiness</th><th>Bloom's</th>
      </tr></thead>
      <tbody>{_detail_table(items)}</tbody>
    </table>
    </div>
  </div>

</div>
<footer>Generated by Canvas Course Builder &nbsp;·&nbsp; {total} items &nbsp;·&nbsp; {generated}</footer>
<script>
function filterTable(type, btn) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#items-table tbody tr').forEach(row => {{
    row.style.display = (type === 'all' || row.dataset.type === type) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

    # Write file
    out_dir = Path(output_dir) if output_dir else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"course_{course_id}_ai_dashboard.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"\n  📊 AI Dashboard: {report_path}")
    return report_path
