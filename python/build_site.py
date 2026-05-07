"""
Build a complete HAX site directly from the SQLite database.

Bypasses hax_prep/ and create_course_mirror.py entirely. Reads COMPLETED
items from the pipeline DB, converts markdown to HTML, and writes a
self-contained HAX site to ~/.hax-ai/sites/<site-name>/.

Usage:
    python build_site.py --course_id 2446743
    python build_site.py --course_id 2446743 --output_dir ./my_site
    python build_site.py --course_id 2446743 --theme clean-two
"""

import argparse
import json
import shutil
import uuid
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime

import markdown

from sqlite_client import SQLiteClient
from extract import get_export_dir, get_course_title
from load import copy_media_files
from utils import build_file_id_map, rewrite_canvas_links


def _safe_slug(text, fallback="item", max_len=200):
    """Convert arbitrary text into a filesystem- and URL-safe slug."""
    safe = ''.join(c for c in text if c.isalnum() or c in (' ', '_', '-'))
    safe = safe.strip().replace(' ', '_')[:max_len]
    safe = safe.rstrip('_-')  # Avoid dangling separators from truncation
    return safe if safe else fallback


def _create_site_name(course_title, course_id):
    """
    Extract a clean site name from the course title.

    Looks for common course-code patterns (e.g. 'MATH 140', 'CS101') and
    returns a lowercase slug like 'math140'. Falls back to 'course<id>'.
    """
    # Ordered by specificity — most structured patterns first
    patterns = [
        r'([A-Z]{2,}\s+\d{3})',      # "MATH 140", "IST 210"
        r'([A-Z]+\d+)',               # "CS101", "MATH240" (no space)
        r'([A-Z]{2,}\s+[A-Z]{1,})',   # Less common two-word codes
    ]
    for pattern in patterns:
        match = re.search(pattern, course_title)
        if match:
            return match.group(1).lower().replace(' ', '')

    return f"course{course_id}"


def _parse_timestamp(iso_string):
    """Parse an ISO-8601 timestamp (with optional trailing Z) into a Unix epoch int."""
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now().timestamp())


def build_hax_site(db_client, course_id: str, export_base="./exports",
                   output_dir=None, theme="clean-one"):
    """
    Build a complete HAX site from COMPLETED items in the database.

    Args:
        db_client: Database client implementing CourseDatabaseInterface
        course_id: Canvas course ID to build from
        export_base: Base directory containing Canvas export folders
        output_dir: Override output path (default: ~/.hax-ai/sites/<site-name>/)
        theme: HAX theme element name (default: clean-one)
    """

    # 1. Resolve paths and course metadata
    export_dir = get_export_dir(course_id, export_base)
    course_title = get_course_title(export_dir)
    site_name = _create_site_name(course_title, course_id)

    # 2. Scaffold a blank HAX site via CLI
    if output_dir:
        site_dir = Path(output_dir)
    else:
        site_dir = Path.home() / ".hax-ai" / "sites" / site_name

    print(f"\n=== Phase 5: Building HAX Site for \"{course_title}\" ===")
    print(f"  Course ID:   {course_id}")
    print(f"  Site name:   {site_name}")
    print(f"  Export dir:  {export_dir}")
    print(f"  Output dir:  {site_dir}")
    print(f"  Theme:       {theme}")

    hax_cmd = "hax.cmd" if sys.platform == "win32" else "hax"

    if site_dir.exists():
        print(f"  ♻️  Site directory already exists — skipping scaffold, updating content in place.")
    else:
        print("\n  🔧 Scaffolding HAX site via CLI...")
        try:
            subprocess.run(
                [hax_cmd, "site", site_name,
                 "--y", "--no-i", "--path", str(site_dir.parent)],
                check=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            print("  ✅ HAX site scaffolded successfully")
        except FileNotFoundError:
            print(f"  ❌ '{hax_cmd}' not found. Install with: npm install -g @haxtheweb/create")
            return
        except subprocess.CalledProcessError as e:
            print(f"  ❌ HAX CLI failed (exit {e.returncode}): {e.stderr.strip()}")
            return

    # Ensure required directories exist regardless of scaffold path
    site_dir.mkdir(parents=True, exist_ok=True)

    pages_dir = site_dir / "pages"
    files_dir = site_dir / "files"

    # faculty-resources/ is expected by hax-ai-interface but not created by the CLI
    (site_dir / "faculty-resources").mkdir(exist_ok=True)

    # 3. Fetch COMPLETED items from the database
    completed_items = db_client.get_completed_items(course_id)
    if not completed_items:
        print("  ⚠️  No COMPLETED items found. Run transform.py first.")
        return

    print(f"\n  📦 Processing {len(completed_items)} completed items...\n")

    # 4. Site UUID and timestamps
    site_uuid = str(uuid.uuid4())
    now_ts = int(datetime.now().timestamp())

    # 5. Build section parent items — these top-level folders group content
    #    by type in the HAX sidebar navigation
    section_ids = {}
    section_items = []

    section_defs = [
        ("pages",       "Course Pages",  1),
        ("assignments", "Assignments",   2),
        ("quizzes",     "Quizzes",       3),
        ("discussions", "Discussions",   4),
    ]

    for folder_key, display_title, order in section_defs:
        section_id = str(uuid.uuid4())
        section_ids[folder_key] = section_id  # Map item_type → section UUID for parent lookups
        section_items.append({
            "id": section_id,
            "title": display_title,
            "slug": folder_key,
            "location": f"pages/{folder_key}/index.html",
            "order": order,
            "indent": 0,
            "parent": None,
            "metadata": {}
        })

    # 6. Welcome page — always first at indent 0, order 0
    welcome_id = str(uuid.uuid4())
    welcome_html = f"<h1>Welcome to {course_title}</h1>\n<p>AI Enhanced Course Mirror</p>"

    welcome_page_dir = pages_dir / welcome_id
    welcome_page_dir.mkdir(exist_ok=True)
    (welcome_page_dir / "index.html").write_text(welcome_html, encoding="utf-8")

    welcome_item = {
        "id": welcome_id,
        "title": f"Welcome to {course_title}",
        "slug": "welcome",
        "location": f"pages/{welcome_id}/index.html",
        "order": 0,
        "indent": 0,
        "parent": None,
        "metadata": {
            "created": now_ts,
            "updated": now_ts,
            "readTime": 1,
            "contentDetails": {}
        }
    }

    # 7. Copy media files first — must populate files_dir before link rewriting
    print("\n  📷 Copying media files to files/...")
    copy_media_files(export_dir, files_dir)

    # Build file_id → filename map from export metadata for link rewriting
    file_id_map = build_file_id_map(export_dir)
    print(f"  📎 Loaded {len(file_id_map)} file ID mappings for link rewriting")

    # 8a. First pass: pre-assign page_ids and build Canvas-slug → HAX-location map so link rewriting in the second pass can resolve page-to-page links.
    #     Keys are the exact Canvas `url` field (matches $WIKI_REFERENCE$/pages/<url>).
    slug_to_location = {}
    prepared_items = []  # list of (item, page_id, title) in dedup order
    seen_titles = set()

    for item in completed_items:
        title = item["title"] or "Untitled"
        if title in seen_titles:
            continue
        seen_titles.add(title)

        page_id = str(uuid.uuid4())
        prepared_items.append((item, page_id, title))

        if item["item_type"] == "pages":
            try:
                raw = json.loads(item.get("raw_content") or "{}")
                canvas_url = raw.get("url")
                if canvas_url:
                    slug_to_location[canvas_url] = f"pages/{page_id}/index.html"
            except (json.JSONDecodeError, TypeError):
                pass

    print(f"  🔗 Built page-link map with {len(slug_to_location)} Canvas page slugs")

    # 8b. Second pass: render HTML, rewrite both file and page links, write pages
    content_items = []
    item_order = 0

    for item, page_id, title in prepared_items:
        item_type = item["item_type"]
        md_content = item.get("ai_enhanced_markdown", "") or ""

        # Auto-detect HTML vs markdown: if content already starts with '<', use directly
        if md_content and md_content.strip().startswith("<"):
            html_content = md_content
        else:
            html_content = markdown.markdown(md_content) if md_content else f"<h1>{title}</h1>"
        # Rewrite Canvas file links + page links in one pass
        html_content = rewrite_canvas_links(
            html_content, file_id_map, files_dir, slug_to_location=slug_to_location
        )

        page_dir = pages_dir / page_id
        page_dir.mkdir(exist_ok=True)
        (page_dir / "index.html").write_text(html_content, encoding="utf-8")

        # Routing: pages/syllabus → Course Pages; others → their own section
        if item_type in ("pages", "syllabus"):
            parent_id = section_ids["pages"]
        elif item_type in section_ids:
            parent_id = section_ids[item_type]
        else:
            parent_id = section_ids["pages"]

        # Negative order pins syllabus above all other items in its section
        if item_type == "syllabus":
            item_order_val = -1
        else:
            item_order += 1
            item_order_val = item_order

        slug = _safe_slug(title, fallback=f"item-{page_id[:8]}")

        created_ts = _parse_timestamp(item["created_at"]) if item.get("created_at") else now_ts
        updated_ts = _parse_timestamp(item["updated_at"]) if item.get("updated_at") else now_ts

        content_items.append({
            "id": page_id,
            "title": title,
            "slug": slug.lower().replace('_', '-').rstrip('-'),
            "location": f"pages/{page_id}/index.html",
            "order": item_order_val,
            "indent": 1,
            "parent": parent_id,
            "metadata": {
                "created": created_ts,
                "updated": updated_ts,
                "readTime": max(1, len(md_content.split()) // 200),  # ~200 wpm estimate
                "contentDetails": {}
            }
        })

        print(f"    [{item_order_val:>3}] {title[:55]}{'...' if len(title) > 55 else ''}")

    # 9. Assemble and write site.json — overwrites the CLI-scaffolded version
    all_items = [welcome_item] + section_items + content_items

    site_json = {
        "id": site_uuid,
        "title": course_title,
        "author": "",
        "description": f"AI Enhanced Course Mirror for {course_title}",
        "license": "by-sa",
        "metadata": {
            "author": {},
            "site": {
                "name": site_name,
                "settings": {"lang": "en-US"},
                "created": now_ts,
                "updated": now_ts
            },
            "theme": {
                "element": theme,
                "path": f"@haxtheweb/{theme}/{theme}.js",
                "name": "Clean course theme"
            },
            "node": {"fields": {}},
            "build": {
                "version": "11.0.5",
                "structure": "course",
                "type": "course"
            }
        },
        "items": all_items
    }

    site_json_path = site_dir / "site.json"
    with open(site_json_path, 'w', encoding='utf-8') as f:
        json.dump(site_json, f, indent=2)

    # 10. Copy both HTML reports into the site directory
    for src_name, dst_name, label in [
        (f"course_{course_id}_ai_readiness_report.html", "ai_readiness_report.html", "AI Readiness Report"),
        (f"course_{course_id}_ai_changes_report.html",  "ai_changes_report.html",  "AI Changes Report"),
    ]:
        src = Path(src_name)
        if src.exists():
            shutil.copy2(src, site_dir / dst_name)
            print(f"\n  📊 {label} copied → {site_dir / dst_name}")
        else:
            print(f"\n  ℹ️  {label} not found (will be generated on next pipeline run)")

    # 11. Summary
    print(f"\n  💾 site.json overwritten with {len(all_items)} items")

    print(f"\n  📁 Site content:")
    for subdir_name in ['pages', 'files', 'faculty-resources', 'ai_readiness_report.html']:
        subdir_path = site_dir / subdir_name
        if subdir_path.exists():
            if subdir_path.is_dir():
                children = list(subdir_path.iterdir())
                print(f"     {subdir_name}/: {len(children)} entries")
            else:
                print(f"     {subdir_name}")

    print(f"\n=== Phase 5 Complete! ===")
    print(f"  ✅ HAX site ready at: {site_dir}")
    print(f"\n  🚀 To run the site:")
    print(f"     cd {site_dir}")
    print(f"     npm install")
    print(f"     npm start")


# CLI entry point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 5 — Build HAX site directly from SQLite database"
    )
    parser.add_argument(
        "--course_id", required=True, type=str,
        help="Canvas course ID to build from"
    )
    parser.add_argument(
        "--db_path", default="course_pipeline.db",
        help="Path to the SQLite database file (default: course_pipeline.db)"
    )
    parser.add_argument(
        "--export_base", default="./exports",
        help="Base directory containing Canvas export folders"
    )
    parser.add_argument(
        "--output_dir", default=None,
        help="Override output directory (default: ~/.hax-ai/sites/<site-name>/)"
    )
    parser.add_argument(
        "--theme", default="clean-one",
        help="HAX theme element name (default: clean-one)"
    )
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as client:
        build_hax_site(
            client,
            args.course_id,
            export_base=args.export_base,
            output_dir=args.output_dir,
            theme=args.theme,
        )
