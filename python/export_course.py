#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException
import requests
from dotenv import load_dotenv

def download_file(url, local_path, token, max_retries=3):
    """Download file if it doesn't exist using Bearer token with retry logic."""
    if local_path.exists():
        print(f"Skipping existing file: {local_path}")
        return True
    
    # Create safe filename to avoid filesystem issues
    safe_path = Path(str(local_path).replace(':', '_').replace('?', '_').replace('*', '_'))
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    
    for attempt in range(max_retries):
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(url, headers=headers, timeout=30, stream=True)
            response.raise_for_status()
            
            # Download in chunks for large files
            with open(safe_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            print(f"Downloaded: {safe_path}")
            return True
            
        except requests.RequestException as e:
            print(f"Download attempt {attempt + 1}/{max_retries} failed for {url}: {e}")
            if attempt == max_retries - 1:
                print(f"Failed to download {url} after {max_retries} attempts")
                return False
    
    return False

def export_course(canvas, course_id, output_dir, token):
    try:
        course = canvas.get_course(course_id)
        print(f"Found course: {course.name} (ID: {course_id})")
    except CanvasException as e:
        print(f"Error fetching course {course_id}: {e}")
        return False
    
    # Create course-specific output directory with safe naming
    course_name_safe = ''.join(c for c in course.name if c.isalnum() or c in (' ', '_', '-')).replace(' ', '_')
    base_dir = Path(output_dir) / f"course_{course_id}_{course_name_safe}"
    base_dir.mkdir(exist_ok=True, parents=True)
    print(f"Export directory: {base_dir}")
    
    export_summary = {
        "course_id": course_id,
        "course_name": course.name,
        "export_directory": str(base_dir),
        "components": {}
    }
    
    # 1. Assignments
    print("\n=== Exporting Assignments ===")
    assignments_dir = base_dir / "assignments"
    assignments_dir.mkdir(exist_ok=True)
    assignment_count = 0
    
    try:
        assignments = list(course.get_assignments())
        print(f"Found {len(assignments)} assignments")
        export_summary["components"]["assignments"] = {"total": len(assignments), "exported": 0, "errors": 0}
        
        for i, assignment in enumerate(assignments, 1):
            try:
                print(f"  [{i}/{len(assignments)}] Processing: {getattr(assignment, 'name', f'Assignment {assignment.id}')}")
                
                assign_data = {
                    "id": assignment.id,
                    "name": getattr(assignment, 'name', None),
                    "description": getattr(assignment, 'description', None),
                    "due_at": getattr(assignment, 'due_at', None),
                    "points_possible": getattr(assignment, 'points_possible', None),
                    "assignment_group_id": getattr(assignment, 'assignment_group_id', None),
                    "submission_types": getattr(assignment, 'submission_types', []),
                    "published": getattr(assignment, 'published', None)
                }
                
                # Try to get rubric information safely
                try:
                    if hasattr(assignment, 'get_rubric_assessments'):
                        rubric_assessments = list(assignment.get_rubric_assessments())
                        assign_data["rubric"] = [r.__dict__ for r in rubric_assessments]
                    else:
                        assign_data["rubric"] = None
                except Exception as rubric_error:
                    print(f"    Warning: Could not fetch rubric for assignment {assignment.id}: {rubric_error}")
                    assign_data["rubric"] = None
                
                # Handle attachments safely
                assign_data["attachments"] = []
                try:
                    if hasattr(assignment, 'get_submission_attachments'):
                        attachments = list(assignment.get_submission_attachments())
                        for att in attachments:
                            if hasattr(att, 'url'):
                                assign_data["attachments"].append(att.url)
                                filename = att.url.split('/')[-1].replace(':', '_')
                                download_success = download_file(att.url, assignments_dir / f"{assignment.id}_{filename}", token)
                                if not download_success:
                                    print(f"    Warning: Failed to download attachment for assignment {assignment.id}")
                except Exception as att_error:
                    print(f"    Warning: Could not fetch attachments for assignment {assignment.id}: {att_error}")
                
                # Save assignment data
                with open(assignments_dir / f"{assignment.id}.json", 'w', encoding='utf-8') as f:
                    json.dump(assign_data, f, indent=2, default=str, ensure_ascii=False)
                
                assignment_count += 1
                export_summary["components"]["assignments"]["exported"] += 1
                
            except Exception as assign_error:
                print(f"    Error processing assignment {assignment.id}: {assign_error}")
                export_summary["components"]["assignments"]["errors"] += 1
                continue
                
    except CanvasException as e:
        print(f"Error fetching assignments: {e}")
        export_summary["components"]["assignments"] = {"total": 0, "exported": 0, "errors": 1}
    
    print(f"Assignments exported: {assignment_count}")

    # 2. Pages
    print("\n=== Exporting Pages ===")
    pages_dir = base_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    page_count = 0
    try:
        pages = list(course.get_pages())
        print(f"Found {len(pages)} pages")
        for i, page in enumerate(pages, 1):
            page_url = getattr(page, 'url', None)
            print(f"  [{i}/{len(pages)}] Fetching: {getattr(page, 'title', page_url)}")
            try:
                full_page = course.get_page(page_url)
            except Exception as e:
                print(f"    Warning: Could not fetch full page body for '{page_url}': {e}")
                full_page = page  # Fall back to partial list data
            page_data = {
                "url": getattr(full_page, 'url', None),
                "title": getattr(full_page, 'title', None),
                "body": getattr(full_page, 'body', None),
                "created_at": getattr(full_page, 'created_at', None),
                "updated_at": getattr(full_page, 'updated_at', None),
                "published": getattr(full_page, 'published', None),
                "front_page": getattr(full_page, 'front_page', False)
            }
            page_filename = page_data["url"].replace('/', '_') if page_data["url"] else f"page_{id(page)}"
            with open(pages_dir / f"{page_filename}.json", 'w', encoding='utf-8') as f:
                json.dump(page_data, f, indent=2, default=str)
            page_count += 1
    except CanvasException as e:
        print(f"Error fetching pages: {e}")
    print(f"Pages exported: {page_count}")

    # 3. Quizzes
    quizzes_dir = base_dir / "quizzes"
    quizzes_dir.mkdir(exist_ok=True)
    try:
        for quiz in course.get_quizzes():
            quiz_data = {
                "id": quiz.id,
                "title": getattr(quiz, 'title', None),
                "description": getattr(quiz, 'description', None),
                "quiz_type": getattr(quiz, 'quiz_type', None),
                "questions": [q.__dict__ for q in quiz.get_questions()] if hasattr(quiz, 'get_questions') else []
            }
            for question in quiz_data["questions"]:
                if "attachments" in question:
                    for url in question.get("attachments", []):
                        filename = url.split('/')[-1].replace(':', '_')
                        download_file(url, quizzes_dir / f"question_{question['id']}_{filename}", token)
            with open(quizzes_dir / f"{quiz.id}.json", 'w') as f:
                json.dump(quiz_data, f, indent=2, default=str)
    except CanvasException as e:
        print(f"Error fetching quizzes: {e}")

    # 4. Files/Media
    files_dir = base_dir / "files"
    files_dir.mkdir(exist_ok=True)
    try:
        for file_obj in course.get_files():
            file_data = {
                "id": file_obj.id,
                "filename": getattr(file_obj, 'filename', None),
                "display_name": getattr(file_obj, 'display_name', None),
                "url": getattr(file_obj, 'url', None),
                "created_at": getattr(file_obj, 'created_at', None)
            }
            local_path = files_dir / file_data["filename"].replace(':', '_') if file_data["filename"] else f"file_{file_obj.id}"
            if not local_path.exists():
                download_file(file_obj.url, local_path, token)
            file_data["local_path"] = str(local_path)
            with open(files_dir / f"{file_obj.id}.json", 'w') as f:
                json.dump(file_data, f, indent=2)
    except CanvasException as e:
        print(f"Error fetching files: {e}")

    # 5. Modules (Fixed)
    print("\n=== Exporting Modules ===")
    modules_dir = base_dir / "modules"
    modules_dir.mkdir(exist_ok=True)
    try:
        for module in course.get_modules():
            module_data = {
                "id": module.id,
                "name": getattr(module, 'name', None),
                "position": getattr(module, 'position', None),
                "items": []
            }
            if hasattr(module, 'get_module_items'):
                for item in module.get_module_items():
                    item_data = {
                        "id": getattr(item, 'id', None),
                        "title": getattr(item, 'title', None),
                        "type": getattr(item, 'type', None),
                        "content_id": getattr(item, 'content_id', None),
                        "position": getattr(item, 'position', None),
                        "indent": getattr(item, 'indent', None),
                        "url": getattr(item, 'url', None),
                        "external_url": getattr(item, 'external_url', None)
                    }
                    module_data["items"].append(item_data)
            with open(modules_dir / f"{module.id}.json", 'w') as f:
                json.dump(module_data, f, indent=2, default=str)
    except CanvasException as e:
        print(f"Error fetching modules: {e}")

    # 6. Discussions
    discussions_dir = base_dir / "discussions"
    discussions_dir.mkdir(exist_ok=True)
    try:
        for discussion in course.get_discussion_topics():
            discussion_data = {
                "id": discussion.id,
                "title": getattr(discussion, 'title', None),
                "message": getattr(discussion, 'message', None),
                "posted_at": getattr(discussion, 'posted_at', None),
                "attachments": getattr(discussion, 'attachments', [])
            }
            for url in discussion_data["attachments"]:
                filename = url.split('/')[-1].replace(':', '_')
                download_file(url, discussions_dir / f"discussion_{discussion.id}_{filename}", token)
            with open(discussions_dir / f"{discussion.id}.json", 'w') as f:
                json.dump(discussion_data, f, indent=2, default=str)
    except CanvasException as e:
        print(f"Error fetching discussions: {e}")

    # 7. Syllabus
    try:
        syllabus = getattr(course, 'syllabus_body', None)
        if syllabus:
            with open(base_dir / "syllabus.html", 'w') as f:
                f.write(syllabus)
    except CanvasException as e:
        print(f"Error fetching syllabus: {e}")
    
    # Save export summary
    with open(base_dir / "export_summary.json", 'w', encoding='utf-8') as f:
        json.dump(export_summary, f, indent=2, default=str, ensure_ascii=False)
    
    print(f"\n=== Export Complete ===")
    print(f"Export directory: {base_dir}")
    print(f"Summary saved to: {base_dir / 'export_summary.json'}")
    
    # Print component summary
    total_exported = 0
    total_errors = 0
    for comp_name, stats in export_summary.get("components", {}).items():
        exported = stats.get("exported", 0)
        errors = stats.get("errors", 0)
        total_exported += exported
        total_errors += errors
        print(f"  {comp_name.title()}: {exported} exported, {errors} errors")
    
    print(f"Total: {total_exported} items exported, {total_errors} errors")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Canvas course to structured JSON/files")
    parser.add_argument("--course_id", required=True, type=int, help="Canvas course ID")
    parser.add_argument("--output_dir", default="./exports", help="Output directory")
    args = parser.parse_args()
    
    # Load .env
    load_dotenv()
    CANVAS_URL = os.getenv("CANVAS_URL")
    CANVAS_TOKEN = os.getenv("CANVAS_TOKEN")
    if not CANVAS_URL or not CANVAS_TOKEN:
        raise ValueError("CANVAS_URL or CANVAS_TOKEN not set in .env file")
    
    try:
        canvas = Canvas(CANVAS_URL, CANVAS_TOKEN)
        export_course(canvas, args.course_id, args.output_dir, CANVAS_TOKEN)
    except CanvasException as e:
        print(f"Error initializing Canvas API: {e}")
