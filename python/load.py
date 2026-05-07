import json
import uuid
import shutil
from pathlib import Path
from datetime import datetime

from sqlite_client import SQLiteClient
from extract import get_export_dir, get_course_title

# Explicit singular-type mapping for HAX IDs (avoids fragile rstrip('s'))
_TYPE_SINGULAR: dict[str, str] = {
    "pages":       "page",
    "assignments": "assignment",
    "quizzes":     "quiz",
    "discussions": "discussion",
    "syllabus":    "syllabus",
}


def _safe_slug(text: str, fallback: str = "item", max_len: int = 50) -> str:
    """Convert arbitrary text into a filesystem- and URL-safe slug."""
    safe = ''.join(c for c in text if c.isalnum() or c in (' ', '_', '-'))
    safe = safe.strip().replace(' ', '_')[:max_len]
    return safe if safe else fallback


def _parse_timestamp(iso_string):
    """Parse an ISO-8601 timestamp (with optional trailing Z) into a Unix epoch int."""
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now().timestamp())  # Fallback to current time

# Media Copier
def copy_media_files(export_dir, output_media_dir):
    """
    Recursively copy supported media files from the Canvas export into the
    HAX output media/ folder, de-duplicating filenames as needed.
    """
    SUPPORTED_EXTENSIONS = {
        ".mp4", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".docx", ".pptx"
    }

    output_media_dir.mkdir(parents=True, exist_ok=True)  # Ensure media dir exists
    media_count = 0

    try:
        for file_path in export_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            # Sanitize filename for safe filesystem use
            safe_name = ''.join(
                c for c in file_path.name if c.isalnum() or c in ('.', '_', '-')
            )
            output_file = output_media_dir / safe_name

            # De-duplicate: append counter if a file with this name already exists
            counter = 1
            original_output = output_file
            while output_file.exists():
                output_file = output_media_dir / (
                    f"{original_output.stem}_{counter}{original_output.suffix}"
                )
                counter += 1

            try:
                shutil.copy2(file_path, output_file)  # copy2 preserves metadata
                media_count += 1
            except Exception as e:
                print(f"    ⚠️  Error copying {file_path.name}: {e}")

        print(f"  ✅ {media_count} media files copied")

    except Exception as e:
        print(f"  ⚠️  Error scanning media files: {e}")

    return media_count

# Core Load Function
def load_course_site(db_client, course_id: str, export_base="./exports", prep_base="./hax_prep"):
    """
    Reads all COMPLETED items for a course from the database, creates the
    HAX-ready directory structure, writes markdown files, copies media,
    and generates the hax_structure.json manifest (JOS).
    """

    #1. Resolve paths
    export_dir = get_export_dir(course_id, export_base)  # Original Canvas export
    course_title = get_course_title(export_dir)           # Human-readable course name
    prep_dir = Path(prep_base) / export_dir.name          # Output root
    prep_dir.mkdir(parents=True, exist_ok=True)           # Create if missing

    print(f"\n=== Phase 4: Loading HAX Site for \"{course_title}\" ===")
    print(f"  Source DB course_id: {course_id}")
    print(f"  Export directory:    {export_dir}")
    print(f"  Output directory:    {prep_dir}")

    # 2. Create subdirectories
    subdirs = {}
    for folder in ["pages", "assignments", "quizzes", "discussions"]:
        folder_path = prep_dir / folder
        folder_path.mkdir(exist_ok=True)
        subdirs[folder] = folder_path

    # 3. Initialize the JOS manifest
    hax_structure = {
        "id": course_id,
        "title": course_title,
        "description": f"AI Enhanced Course Mirror for {course_title}",
        "items": []
    }

    # Welcome page — write the actual HTML file so the manifest link works
    welcome_id  = str(uuid.uuid4())
    welcome_dir = prep_dir / "pages" / welcome_id
    welcome_dir.mkdir(parents=True, exist_ok=True)
    (welcome_dir / "index.html").write_text(
        f"<h1>Welcome to {course_title}</h1>\n<p>AI-Enhanced Course Mirror</p>",
        encoding="utf-8",
    )
    hax_structure["items"].append({
        "id": welcome_id,
        "title": f"Welcome to {course_title}",
        "indent": 0,
        "location": f"pages/{welcome_id}/index.html",
        "slug": "welcome",
        "order": 0,
        "parent": None,
        "description": "Course Homepage",
        "metadata": {
            "created": int(datetime.now().timestamp()),
            "updated": int(datetime.now().timestamp()),
            "readTime": 1,
            "contentDetails": {}
        }
    })

    # Top-level section parents — write index.html stubs so links work
    section_ids = {}
    section_defs = [
        ("pages",       "Course Pages",  1),
        ("assignments", "Assignments",   2),
        ("quizzes",     "Quizzes",       3),
        ("discussions", "Discussions",   4),
    ]
    for folder_key, display_title, order in section_defs:
        section_id = str(uuid.uuid4())
        section_ids[folder_key] = section_id
        # Write the section index file
        sec_dir = prep_dir / "pages" / folder_key
        sec_dir.mkdir(parents=True, exist_ok=True)
        (sec_dir / "index.html").write_text(
            f"<h1>{display_title}</h1>", encoding="utf-8"
        )
        hax_structure["items"].append({
            "id": section_id,
            "title": display_title,
            "indent": 0,
            "location": f"pages/{folder_key}/index.html",
            "slug": folder_key,
            "order": order,
            "parent": None,
        })

    # 4. Fetch COMPLETED items from the database
    completed_items = db_client.get_completed_items(course_id)
    if not completed_items:
        print("  ⚠️  No COMPLETED items found. Run transform.py first.")
        return

    print(f"\n  📦 Writing {len(completed_items)} items to disk...\n")

    item_order = 0              # Running counter for ordering within sections
    seen_titles = set()         # De-duplicate items with identical titles

    for item in completed_items:
        item_id = str(item["id"])
        item_type = item["item_type"]          # e.g. "pages", "assignments", "syllabus"
        title = item["title"] or "Untitled"
        markdown = item.get("ai_enhanced_markdown", "")

        # Skip syllabus rows (already COMPLETED by extract, not a page)
        if item_type == "syllabus":
            # Write the syllabus as a standalone file inside pages/
            slug = "syllabus"
            output_file = subdirs["pages"] / "syllabus.md"
            output_file.write_text(markdown or f"# {title}", encoding="utf-8")

            # Register it at the top of Course Pages with negative order
            hax_structure["items"].append({
                "id": item_id,
                "title": title,
                "indent": 1,
                "parent": section_ids["pages"],  # Child of "Course Pages"
                "location": f"pages/{item_id}/index.html",
                "slug": slug,
                "order": -1,  # Negative order pins it to the top
                "content_source": str(output_file.relative_to(prep_dir)),
            })
            print(f"    📜 Syllabus → pages/syllabus.md")
            continue

        # Determine output folder based on item_type
        if item_type in subdirs:
            target_dir = subdirs[item_type]
        else:
            target_dir = subdirs["pages"]  # Fallback: unknown types go to pages/

        parent_key = item_type if item_type in section_ids else "pages"

        # De-duplicate by title — warn instead of silently dropping
        if title in seen_titles:
            print(f"    ⚠️  Duplicate title skipped: {title[:60]}")
            continue
        seen_titles.add(title)

        # Write the markdown file
        slug = _safe_slug(title, fallback=f"item_{item_id}")
        output_file = target_dir / f"{slug}.md"

        # Handle filename collisions within the same directory
        counter = 1
        while output_file.exists():
            output_file = target_dir / f"{slug}_{counter}.md"
            counter += 1

        content_to_write = markdown if markdown else f"# {title}\n\nContent pending."
        output_file.write_text(content_to_write, encoding="utf-8")

        # Extract timestamps from the original raw_content JSON 
        # Note: get_completed_items doesn't return raw_content,
        # so we use created_at/updated_at from the DB row itself.
        created_ts = int(datetime.now().timestamp())
        updated_ts = int(datetime.now().timestamp())
        if item.get("created_at"):
            created_ts = _parse_timestamp(item["created_at"])
        if item.get("updated_at"):
            updated_ts = _parse_timestamp(item["updated_at"])

        # Build the JOS item entry
        item_order += 1
        singular = _TYPE_SINGULAR.get(item_type, item_type)
        hax_id = f"{singular}-{item_id}"  # e.g. "page-12345"

        hax_structure["items"].append({
            "id": hax_id,
            "title": title,
            "indent": 1,                              # Child of its section parent
            "parent": section_ids[parent_key],         # Link to correct nav folder
            "location": f"pages/{hax_id}/index.html",
            "slug": slug.lower().replace('_', '-'),
            "order": item_order,
            "content_source": str(output_file.relative_to(prep_dir)),
            "metadata": {
                "created": created_ts,
                "updated": updated_ts,
                "readTime": 0,
                "contentDetails": {}
            }
        })

        print(f"    [{item_order}] {title[:55]}{'...' if len(title) > 55 else ''}")

    # 5. Copy media files from the Canvas export 
    print("\n  📷 Copying media files...")
    media_dir = prep_dir / "media"
    copy_media_files(export_dir, media_dir)

    # 6. Write the JOS manifest
    manifest_path = prep_dir / "hax_structure.json"
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(hax_structure, f, indent=2)

    # 7. Summary 
    total_items = len(hax_structure["items"])
    print(f"\n  💾 JOS manifest saved: {manifest_path}")
    print(f"  ✅ Indexed {total_items} items for the website builder.")

    print(f"\n=== Phase 4 Complete! ===\n")
    print(f"  ✅ Output directory: {prep_dir}")

    # Show what was created
    print(f"\n  📁 HAX-ready content structure:")
    for subdir_name in ['pages', 'assignments', 'quizzes', 'media']:
        subdir_path = prep_dir / subdir_name
        if subdir_path.exists():
            files = list(subdir_path.glob('*'))
            print(f"     {subdir_name}/: {len(files)} files")

    print(f"\n  🚀 Next: python build_site.py --course_id {course_id}")


# CLI entry point (standalone usage)
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 4 — Generate hax_prep from COMPLETED items")
    parser.add_argument("--course_id",   required=True, type=str)
    parser.add_argument("--db_path",     default="course_pipeline.db")
    parser.add_argument("--export_base", default="./exports")
    parser.add_argument("--prep_base",   default="./hax_prep")
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as client:
        load_course_site(client, args.course_id,
                         export_base=args.export_base, prep_base=args.prep_base)
