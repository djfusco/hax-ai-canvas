"""Server-side Canvas course reader for the institutional OAuth connection.

Mirrors extension/collect.js: GET only, same Canvas host only, an allowlist of
course-content endpoints, and only the fields Course Consultant uses. Produces
the same bundle that canvas_api_import.import_api_bundle accepts. Never calls
users, enrollments, submissions, grades, analytics or discussion entries.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

MAX_REQUESTS = 2000
CONCURRENCY = 4
PER_PAGE = 100
TIME_BUDGET = 240  # seconds; stays inside hosted function limits
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

ALLOWED = [re.compile(p) for p in (
    r"^/api/v1/courses$",  # the instructor's own course list (teacher enrollments)
    r"^/api/v1/courses/\d+$",
    r"^/api/v1/courses/\d+/modules$",
    r"^/api/v1/courses/\d+/pages$",
    r"^/api/v1/courses/\d+/pages/[^/?#]+$",
    r"^/api/v1/courses/\d+/assignments$",
    r"^/api/v1/courses/\d+/assignment_groups$",
    r"^/api/v1/courses/\d+/quizzes$",
    r"^/api/v1/courses/\d+/quizzes/\d+/questions$",
    r"^/api/v1/courses/\d+/discussion_topics$",
    r"^/api/v1/courses/\d+/outcome_group_links$",
    r"^/api/v1/courses/\d+/files$",
    r"^/api/v1/courses/\d+/late_policy$",
    r"^/api/quiz/v1/courses/\d+/quizzes/\d+/items$",
)]

KEEP = {
    "course": ["id", "name", "course_code", "syllabus_body", "apply_assignment_group_weights", "term"],
    "term": ["name"],
    "module": ["id", "name", "position", "require_sequential_progress", "prerequisite_module_ids", "items"],
    "moduleItem": ["id", "title", "type", "content_id", "page_url", "external_url", "position", "completion_requirement"],
    "requirement": ["type", "min_score"],
    "page": ["page_id", "url", "title", "body", "published", "front_page"],
    "assignment": ["id", "name", "description", "points_possible", "due_at", "submission_types", "grading_type",
                   "assignment_group_id", "peer_reviews", "group_category_id", "allowed_attempts", "published",
                   "rubric", "rubric_settings", "quiz_id", "discussion_topic", "is_quiz_lti_assignment"],
    "criterion": ["id", "points", "description", "long_description", "outcome_id", "ratings"],
    "rating": ["points", "description", "long_description"],
    "rubricSettings": ["id", "title", "points_possible"],
    "discussionRef": ["id"],
    "group": ["id", "name", "position", "group_weight"],
    "quiz": ["id", "title", "description", "quiz_type", "time_limit", "allowed_attempts", "points_possible",
             "assignment_id", "published"],
    "question": ["question_text", "question_type", "points_possible"],
    "newQuizItem": ["points_possible", "entry"],
    "newQuizEntry": ["item_body", "interaction_type_slug"],
    "topic": ["id", "title", "message", "published", "is_announcement", "assignment_id"],
    "outcome": ["id", "title", "display_name", "description", "mastery_points"],
    "file": ["id", "display_name", "filename", "size", "content-type"],
    "latePolicy": ["late_submission_deduction_enabled", "late_submission_deduction", "late_submission_interval",
                   "late_submission_minimum_percent_enabled", "late_submission_minimum_percent",
                   "missing_submission_deduction_enabled", "missing_submission_deduction"],
    "courseListing": ["id", "name", "course_code", "term", "workflow_state"],
}


class CollectError(ValueError):
    """Reading the course failed; the message is safe to show."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _pick(value, key):
    return {k: value[k] for k in KEEP[key] if k in value} if isinstance(value, dict) else None


def _objects(rows):
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _clean_module(module):
    out = _pick(module, "module")
    items = []
    for item in _objects(module.get("items")):
        kept = _pick(item, "moduleItem")
        if isinstance(kept.get("completion_requirement"), dict):
            kept["completion_requirement"] = _pick(kept["completion_requirement"], "requirement")
        items.append(kept)
    out["items"] = items
    return out


def _clean_assignment(assignment):
    out = _pick(assignment, "assignment")
    if isinstance(out.get("rubric"), list):
        out["rubric"] = [{**_pick(c, "criterion"), "ratings": [_pick(r, "rating") for r in _objects(c.get("ratings"))]}
                         for c in _objects(out["rubric"])]
    if isinstance(out.get("rubric_settings"), dict):
        out["rubric_settings"] = _pick(out["rubric_settings"], "rubricSettings")
    if isinstance(out.get("discussion_topic"), dict):
        out["discussion_topic"] = _pick(out["discussion_topic"], "discussionRef")
    return out


class CanvasClient:
    """GET-only, allowlisted, same-host Canvas API client with a request budget."""

    def __init__(self, base_url, token, *, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise CollectError("The Canvas address must use HTTPS.")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        import httpx
        self.http = httpx.Client(trust_env=False, follow_redirects=False, timeout=30, transport=transport,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        self.requests = 0
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + TIME_BUDGET

    def close(self):
        self.http.close()

    def _check(self, url):
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            raise CollectError("Refusing a request to another site.")
        if not any(p.match(parsed.path) for p in ALLOWED):
            raise CollectError("Refusing a request outside course content.")

    def _get(self, url):
        self._check(url)
        with self.lock:
            self.requests += 1
            if self.requests > MAX_REQUESTS:
                raise CollectError("This course needs too many requests to read.")
        if time.monotonic() > self.deadline:
            raise CollectError("Reading this course took too long. Try the export upload instead.")
        response = self.http.get(url)
        if response.status_code != 200:
            raise CollectError(f"Canvas returned {response.status_code}.", response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise CollectError("A Canvas response was too large.")
        text = response.text
        if text.startswith("while(1);"):
            text = text[len("while(1);"):]
        try:
            return json.loads(text), response.headers.get("Link", "")
        except ValueError:
            raise CollectError("Canvas returned an unreadable response.") from None

    def url(self, path, params=None):
        query = urlencode(params or {}, doseq=True)
        return self.origin + path + ("?" + query if query else "")

    def get(self, path, params=None):
        return self._get(self.url(path, params))[0]

    def get_all(self, path, params=None):
        params = dict(params or {}, per_page=PER_PAGE)
        rows, url = [], self.url(path, params)
        while url:
            data, link = self._get(url)
            if not isinstance(data, list):
                raise CollectError("Canvas returned an unexpected response.")
            rows.extend(_objects(data))
            match = next((part for part in link.split(",") if 'rel="next"' in part), None)
            url = match[match.index("<") + 1:match.index(">")] if match else None
        return rows


def list_courses(client):
    """Courses the user teaches (active and unpublished), for choosing one."""
    rows = client.get_all("/api/v1/courses", {"enrollment_type": "teacher", "include[]": ["term"],
                                              "state[]": ["available", "unpublished", "completed"]})
    courses = []
    for row in rows:
        kept = _pick(row, "courseListing")
        if kept.get("id") and kept.get("name"):
            term = kept.get("term") if isinstance(kept.get("term"), dict) else {}
            courses.append({"id": kept["id"], "name": str(kept["name"])[:300],
                            "code": str(kept.get("course_code") or "")[:100], "term": str(term.get("name") or "")[:100]})
    return courses


def collect(client, course_id):
    """Read one course; returns a bundle for canvas_api_import.import_api_bundle."""
    if not re.fullmatch(r"\d{1,20}", str(course_id)):
        raise CollectError("Choose a course.")
    base = f"/api/v1/courses/{course_id}"
    warnings = []

    def optional(label, task, fallback):
        try:
            return task()
        except CollectError as exc:
            if exc.status is None:
                raise
            warnings.append(f"{label} could not be read ({exc.status}).")
            return fallback

    try:
        course = _pick(client.get(base, {"include[]": ["syllabus_body", "term"]}), "course")
    except CollectError as exc:
        if exc.status in (401, 403):
            raise CollectError("Canvas did not allow reading this course. Check that you teach it.") from None
        raise
    if isinstance(course.get("term"), dict):
        course["term"] = _pick(course["term"], "term")
    modules = [_clean_module(m) for m in optional("Modules", lambda: client.get_all(base + "/modules", {"include[]": "items"}), [])]
    page_list = optional("Pages", lambda: client.get_all(base + "/pages"), [])[:500]
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        pages = [p for p in pool.map(lambda page: optional(
            f'Page "{str(page.get("title", ""))[:80]}"',
            lambda: _pick(client.get(f"{base}/pages/{quote(str(page.get('url', '')), safe='')}"), "page"), None),
            page_list) if p]
        assignments = [_clean_assignment(a) for a in optional("Assignments", lambda: client.get_all(base + "/assignments"), [])]
        groups = [_pick(g, "group") for g in optional("Assignment groups", lambda: client.get_all(base + "/assignment_groups"), [])]
        quiz_list = optional("Quizzes", lambda: client.get_all(base + "/quizzes"), [])[:500]

        def quiz(row):
            kept = _pick(row, "quiz")
            kept["questions"] = [_pick(q, "question") for q in optional(
                f'Questions for "{str(row.get("title", ""))[:80]}"',
                lambda: client.get_all(f"{base}/quizzes/{int(row.get('id', 0))}/questions"), [])]
            return kept
        quizzes = list(pool.map(quiz, quiz_list))

        def new_quiz(assignment):
            items = optional(f'New Quiz "{str(assignment.get("name", ""))[:80]}"', lambda: client.get_all(
                f"/api/quiz/v1/courses/{course_id}/quizzes/{int(assignment['id'])}/items"), None)
            if items is None:
                return None
            cleaned = []
            for item in items:
                kept = _pick(item, "newQuizItem")
                if isinstance(kept.get("entry"), dict):
                    kept["entry"] = _pick(kept["entry"], "newQuizEntry")
                cleaned.append(kept)
            return {"assignment_id": assignment["id"], "items": cleaned}
        new_quizzes = [q for q in pool.map(new_quiz, [a for a in assignments if a.get("is_quiz_lti_assignment") is True][:500]) if q]
    topics = [t for t in (_pick(t, "topic") for t in optional("Discussions", lambda: client.get_all(base + "/discussion_topics"), []))
              if t.get("is_announcement") is not True]
    outcomes = [o for o in (_pick(link.get("outcome"), "outcome") for link in optional(
        "Outcomes", lambda: client.get_all(base + "/outcome_group_links", {"outcome_style": "full"}), [])) if o]
    files = [_pick(f, "file") for f in optional("File list", lambda: client.get_all(base + "/files"), [])]
    late = optional("Late policy", lambda: client.get(base + "/late_policy"), None)
    return {
        "version": 1, "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canvas_origin": client.origin, "course": course, "modules": modules, "pages": pages,
        "assignments": assignments, "assignment_groups": groups, "quizzes": quizzes, "new_quizzes": new_quizzes,
        "discussion_topics": topics, "outcomes": outcomes, "files": files,
        "late_policy": _pick(late.get("late_policy"), "latePolicy") if isinstance(late, dict) else None,
        "warnings": warnings, "request_count": client.requests,
    }
