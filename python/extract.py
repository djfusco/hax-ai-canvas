import json
from pathlib import Path

import pypdf
from bs4 import BeautifulSoup

from sqlite_client import SQLiteClient


def get_export_dir(course_id: str, export_base: str = "./exports") -> Path:
    """Return the export directory for a given course ID."""
    export_base_path = Path(export_base)
    matches = list(export_base_path.glob(f"course_{course_id}_*"))
    if not matches:
        raise FileNotFoundError(
            f"No export folder found for course ID {course_id} in {export_base}"
        )
    return matches[0]


def get_course_title(course_export_dir: Path) -> str:
    """Extract the human-readable course name from export_summary.json."""
    summary_path = course_export_dir / "export_summary.json"
    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            return data.get("course_name", f"Course {course_export_dir.name}")
        except (json.JSONDecodeError, KeyError, IOError):
            pass
    return f"Course {course_export_dir.name}"


def find_latest_syllabus(export_dir: Path):
    """Return the most relevant syllabus file from the export, or None."""
    if not export_dir.exists():
        return None
    syllabus_files = [
        f for f in export_dir.rglob("*")
        if "syllabus" in f.name.lower() and f.is_file()
    ]
    if not syllabus_files:
        return None

    # Higher score = more likely to be the definitive version.
    # Weights: update/final/revised (+10), v2/v3 (+5), PDF format (+2).
    def score_file(filepath: Path) -> int:
        name = filepath.name.lower()
        score = 0
        if "update"  in name: score += 10
        if "final"   in name: score += 10
        if "revised" in name: score += 10
        if "v2" in name or "v3" in name: score += 5
        if name.endswith(".pdf"): score += 2
        return score

    syllabus_files.sort(key=lambda x: (score_file(x), len(x.name)), reverse=True)
    return syllabus_files[0]


def extract_syllabus_text(filepath: Path) -> str:
    """Extract raw text from a syllabus PDF."""
    if filepath.suffix.lower() != ".pdf":
        print(f"  Unsupported syllabus format: {filepath.suffix}")
        return ""
    try:
        reader = pypdf.PdfReader(filepath)
        return "\n".join(
            page.extract_text() or "" for page in reader.pages
        ).strip()
    except Exception as exc:
        print(f"  Error reading PDF {filepath.name}: {exc}")
        return ""


def _strip_html(text: str) -> str:
    """Strip HTML tags using BeautifulSoup, return plain text."""
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)


def _has_meaningful_content(content_dict: dict, item_type: str) -> bool:
    """
    Return True when an item has enough real content to warrant AI enhancement.
    Filters out empty grade-column placeholders, stubs, etc.
    """
    if item_type == "pages":
        body  = _strip_html(content_dict.get("body", ""))
        title = content_dict.get("title", "")
        return bool(title and len(body) > 20)

    if item_type == "assignments":
        desc = _strip_html(content_dict.get("description", ""))
        name = content_dict.get("name", "")
        return bool(name and len(desc) > 20)

    if item_type == "quizzes":
        title     = content_dict.get("title", "")
        questions = content_dict.get("questions", [])
        return bool(title and len(questions) > 0)

    if item_type == "discussions":
        message = _strip_html(content_dict.get("message", ""))
        title   = content_dict.get("title", "")
        return bool(title and len(message) > 20)

    return True  # Unknown types pass through by default


def extract_course_data(db_client, course_id: str, export_dir: Path) -> None:
    """
    Phase 2: Parse the Canvas export and load raw items into the database.

    Covers: assignments, pages, quizzes, discussions, and syllabus PDF.
    Items that are unpublished or lack meaningful content are skipped.
    """
    folders_to_scan = ["assignments", "pages", "quizzes", "discussions"]
    items_processed = 0
    items_skipped   = 0

    print(f"Starting extraction from: {export_dir}")

    # ── 1. JSON content items ─────────────────────────────────────────────────
    for folder_name in folders_to_scan:
        folder_path = export_dir / folder_name

        if not folder_path.exists():
            print(f"  ℹ️  No {folder_name}/ folder found — skipping")
            continue

        for file in folder_path.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as fh:
                    content = json.load(fh)

                label = content.get("title") or content.get("name", file.stem)

                # Skip explicitly unpublished items
                if content.get("published") is False:
                    items_skipped += 1
                    print(f"  ⏭️  Skipped (unpublished): {label}")
                    continue

                # Skip items with no meaningful body
                if not _has_meaningful_content(content, folder_name):
                    items_skipped += 1
                    print(f"  ⏭️  Skipped (no content): {label}")
                    continue

                item_id  = str(content.get("id") or content.get("url"))
                title    = content.get("title") or content.get("name", "Untitled")
                db_client.insert_raw_item(
                    item_id, course_id, folder_name, title, json.dumps(content)
                )
                items_processed += 1

            except (json.JSONDecodeError, KeyError, IOError) as exc:
                print(f"  Error processing {file.name}: {exc}")
                items_skipped += 1

    # ── 2. Syllabus PDF ───────────────────────────────────────────────────────
    print("🔍 Searching for Syllabus document...")
    # Check both 'Files/' and 'files/' (export_course.py uses lowercase)
    syllabus_file = find_latest_syllabus(export_dir / "Files") or \
                    find_latest_syllabus(export_dir / "files")

    if syllabus_file:
        print(f"📄 Found Syllabus: {syllabus_file.name}")
        syllabus_text = extract_syllabus_text(syllabus_file)
        if syllabus_text:
            db_client.insert_raw_item(
                item_id     = f"syllabus-{course_id}",
                course_id   = course_id,
                item_type   = "syllabus",
                title       = f"Course Syllabus ({syllabus_file.name})",
                raw_content = syllabus_text,
                status      = "COMPLETED",  # Syllabus is used for context, not enhanced
            )
            items_processed += 1
        else:
            print("  ⚠️  Syllabus found but text could not be extracted.")
    else:
        print("  ⚠️  No Syllabus PDF found in export directory.")

    print(
        f"Extraction complete! "
        f"Processed: {items_processed} | Skipped/Errors: {items_skipped}"
    )


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 2 — Extract Canvas export into SQLite")
    parser.add_argument("--course_id",   required=True, type=str)
    parser.add_argument("--db_path",     default="course_pipeline.db")
    parser.add_argument("--export_base", default="./exports")
    args = parser.parse_args()

    with SQLiteClient(db_path=args.db_path) as client:
        extract_course_data(client, args.course_id, get_export_dir(args.course_id, args.export_base))
