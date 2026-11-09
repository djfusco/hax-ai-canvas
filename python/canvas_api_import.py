"""Convert a Course Consultant browser-extension bundle into the course model.

The extension reads the live course from Canvas REST API endpoints using the
instructor's existing browser session, and sends one JSON bundle:

    {
      "version": 1,
      "course":            GET /api/v1/courses/:id?include[]=syllabus_body&include[]=term
      "modules":           GET /api/v1/courses/:id/modules?include[]=items
      "pages":             GET /api/v1/courses/:id/pages/:url  (each, with body)
      "assignments":       GET /api/v1/courses/:id/assignments (rubric arrays included)
      "assignment_groups": GET /api/v1/courses/:id/assignment_groups
      "quizzes":           GET /api/v1/courses/:id/quizzes (+ "questions" per quiz)
      "new_quizzes":       [{"assignment_id", "items"}] from the New Quizzes API
      "discussions":       GET /api/v1/courses/:id/discussion_topics (prompts only)
      "outcomes":          outcome objects from outcome_group_links
      "files":             GET /api/v1/courses/:id/files (metadata only)
      "late_policy":       GET /api/v1/courses/:id/late_policy (optional)
      "warnings":          [strings]
    }

Every field is read defensively: unknown keys are ignored, wrong types are
dropped, and nothing the extension must never collect (discussion entries,
submissions, enrollments, grades, users) is read even if present.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re

import course_model as cm

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
FILE_LINK = re.compile(r"/files/(\d+)")
REQUIREMENT_TYPES = frozenset((
    "must_view", "must_submit", "must_contribute", "min_score", "must_mark_done",
))


class BundleError(ValueError):
    """The extension bundle cannot be imported; the message is safe to show."""


def _s(value, maximum=cm.MAX_TEXT):
    return value[:maximum] if isinstance(value, str) else ""


def _n(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _ref(value):
    """Canvas ids arrive as numbers or strings; return a safe string or None."""
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value)):
        return str(value)
    return None


def _list(bundle, key, maximum):
    value = bundle.get(key, [])
    if not isinstance(value, list):
        raise BundleError(f"The extension bundle's {key} must be a list.")
    return [row for row in value[:maximum] if isinstance(row, dict)]


def _title(value, fallback):
    text = _s(value, cm.MAX_TITLE).strip()
    return text or fallback


def _attempts(value):
    number = _n(value)
    return int(number) if number is not None and number > 0 else None


class _Adapter:
    def __init__(self, bundle):
        if not isinstance(bundle, dict) or bundle.get("version") != 1:
            raise BundleError("This extension bundle is from an unsupported version. Update the extension.")
        self.bundle = bundle
        course = bundle.get("course")
        if not isinstance(course, dict):
            raise BundleError("The extension bundle has no course.")
        self.course = course
        self.model = cm.empty_course("extension", _title(course.get("name"), "Canvas course"))
        self.warnings = [_s(w, 1000) for w in bundle.get("warnings", []) if isinstance(w, str)][:50]
        self.items = {}
        self.page_by_url = {}
        self.graded_discussions = {}

    def add_item(self, item_id, kind, title, html="", published=True, url=None):
        if item_id in self.items:
            return self.items[item_id]
        if len(self.items) >= cm.MAX_ITEMS:
            raise BundleError(f"This course has more than {cm.MAX_ITEMS} items.")
        item = {"id": item_id, "kind": kind, "title": title, "html": _s(html, cm.MAX_ITEM_HTML_BYTES),
                "module_id": None, "published": published is not False}
        if url is not None:
            item["url"] = _s(url, 2000) or None
        self.items[item_id] = item
        self.model["items"].append(item)
        return item

    def run(self):
        model = self.model
        course = self.course
        term = course.get("term") if isinstance(course.get("term"), dict) else {}
        model["course"].update(
            code=_s(course.get("course_code"), cm.MAX_TITLE),
            term=_s(term.get("name"), cm.MAX_TITLE),
            syllabus_html=_s(course.get("syllabus_body"), cm.MAX_ITEM_HTML_BYTES),
            weighted_groups=course.get("apply_assignment_group_weights") is True,
        )
        late = self.bundle.get("late_policy")
        if isinstance(late, dict):
            model["course"]["late_policy"] = {
                k[:100]: v for k, v in list(late.items())[:30]
                if isinstance(k, str) and k not in ("id", "course_id", "created_at", "updated_at")
                and (v is None or type(v) in (bool, int, float) or isinstance(v, str))
                and not (isinstance(v, float) and not math.isfinite(v))
            } or None
        self.groups()
        self.outcomes()
        self.pages()
        self.quizzes()
        self.assignments()
        self.discussions()
        self.modules()
        self.files()
        self.outcome_links()
        counts = {plural: sum(1 for i in model["items"] if i["kind"] == kind)
                  for kind, plural in (("page", "pages"), ("assignment", "assignments"),
                                       ("quiz", "quizzes"), ("discussion", "discussions"))}
        counts.update(modules=len(model["modules"]), rubrics=len(model["rubrics"]),
                      outcomes=len(model["outcomes"]), media=len(model["media_inventory"]), materials=0)
        model["provenance"].update(
            imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            counts=counts, warnings=self.warnings[:cm.MAX_WARNINGS])
        return model

    def groups(self):
        for position, row in enumerate(_list(self.bundle, "assignment_groups", cm.MAX_GROUPS), 1):
            ref = _ref(row.get("id"))
            if ref is None:
                continue
            weight = _n(row.get("group_weight"))
            self.model["assignment_groups"].append({
                "id": "group-" + ref, "name": _s(row.get("name"), cm.MAX_TITLE),
                "weight": weight if weight is not None and weight >= 0 else None,
                "position": _n(row.get("position")) or position,
            })
        self.group_ids = {g["id"] for g in self.model["assignment_groups"]}

    def group_for(self, value):
        ref = _ref(value)
        return "group-" + ref if ref and "group-" + ref in self.group_ids else None

    def outcomes(self):
        seen = set()
        for row in _list(self.bundle, "outcomes", cm.MAX_OUTCOMES):
            ref = _ref(row.get("id"))
            title = _title(row.get("title") or row.get("display_name"), "")
            if not ref or not title or ref in seen:
                continue
            seen.add(ref)
            self.model["outcomes"].append({
                "id": "outcome-" + ref, "title": title,
                "description": _s(row.get("description")), "mastery_points": _n(row.get("mastery_points")),
                "item_ids": [],
            })
        self.outcome_ids = {o["id"] for o in self.model["outcomes"]}

    def pages(self):
        for row in _list(self.bundle, "pages", cm.MAX_ITEMS):
            ref = _ref(row.get("page_id"))
            if not ref:
                continue
            item = self.add_item("page-" + ref, "page", _title(row.get("title"), "Untitled page"),
                                 row.get("body"), row.get("published"))
            if isinstance(row.get("url"), str):
                self.page_by_url[row["url"]] = item["id"]
            if row.get("front_page") is True:
                item["front_page"] = True

    def rubric(self, assignment):
        criteria_rows = assignment.get("rubric")
        if not isinstance(criteria_rows, list) or not criteria_rows:
            return None
        settings = assignment.get("rubric_settings") if isinstance(assignment.get("rubric_settings"), dict) else {}
        ref = _ref(settings.get("id")) or "assignment-" + str(_ref(assignment.get("id")))
        rubric_id = "rubric-" + ref
        if rubric_id in self.model["rubrics"]:
            return rubric_id
        if len(self.model["rubrics"]) >= cm.MAX_RUBRICS:
            self.warnings.append("Some rubrics were not imported because the course exceeds the rubric limit.")
            return None
        criteria = []
        for row in criteria_rows[:cm.MAX_CRITERIA]:
            if not isinstance(row, dict):
                continue
            outcome = _ref(row.get("outcome_id"))
            ratings = [{"description": _s(r.get("description")), "long_description": _s(r.get("long_description")),
                        "points": _n(r.get("points"))}
                       for r in (row.get("ratings") or [])[:cm.MAX_RATINGS] if isinstance(r, dict)]
            criteria.append({
                "description": _s(row.get("description")), "long_description": _s(row.get("long_description")),
                "points": _n(row.get("points")),
                "outcome_id": "outcome-" + outcome if outcome and "outcome-" + outcome in self.outcome_ids else None,
                "ratings": ratings,
            })
        self.model["rubrics"][rubric_id] = {
            "title": _s(settings.get("title"), cm.MAX_TITLE) or "Rubric",
            "points": _n(settings.get("points_possible")), "criteria": criteria,
        }
        return rubric_id

    def details(self, row, rubric_id=None, submission_types=None):
        types = row.get("submission_types") if submission_types is None else submission_types
        return {
            "points": _n(row.get("points_possible")), "due_at": _s(row.get("due_at"), 64) or None,
            "submission_types": [_s(t, 200) for t in (types or []) if isinstance(t, str) and t][:20],
            "grading_type": _s(row.get("grading_type"), 50) or None,
            "group_id": self.group_for(row.get("assignment_group_id")), "rubric_id": rubric_id,
            "peer_reviews": row.get("peer_reviews") is True,
            "group_work": row.get("group_category_id") not in (None, 0, ""),
            "allowed_attempts": _attempts(row.get("allowed_attempts")),
        }

    def quizzes(self):
        self.quiz_by_assignment = {}
        total = 0
        for row in _list(self.bundle, "quizzes", cm.MAX_ITEMS):
            ref = _ref(row.get("id"))
            if not ref:
                continue
            item = self.add_item("quiz-" + ref, "quiz", _title(row.get("title"), "Untitled quiz"),
                                 row.get("description"), row.get("published"))
            questions = []
            for q in (row.get("questions") or []):
                if not isinstance(q, dict) or total >= cm.MAX_QUIZ_QUESTIONS:
                    continue
                total += 1
                questions.append({"text": _s(q.get("question_text")), "type": _s(q.get("question_type"), 100),
                                  "points": _n(q.get("points_possible"))})
            self.model["quizzes"][item["id"]] = {
                "time_limit": _n(row.get("time_limit")), "allowed_attempts": _attempts(row.get("allowed_attempts")),
                "points": _n(row.get("points_possible")), "quiz_type": _s(row.get("quiz_type"), 50) or None,
                "questions": questions,
            }
            assignment_ref = _ref(row.get("assignment_id"))
            if assignment_ref:
                self.quiz_by_assignment[assignment_ref] = item["id"]
        self.new_quizzes = {}
        for row in _list(self.bundle, "new_quizzes", cm.MAX_ITEMS):
            ref = _ref(row.get("assignment_id"))
            if not ref:
                continue
            questions = []
            for entry in (row.get("items") or []):
                if not isinstance(entry, dict) or total >= cm.MAX_QUIZ_QUESTIONS:
                    continue
                body = entry.get("entry") if isinstance(entry.get("entry"), dict) else {}
                total += 1
                questions.append({"text": _s(body.get("item_body")), "type": _s(body.get("interaction_type_slug"), 100),
                                  "points": _n(entry.get("points_possible"))})
            self.new_quizzes[ref] = questions

    def assignments(self):
        for row in _list(self.bundle, "assignments", cm.MAX_ITEMS):
            ref = _ref(row.get("id"))
            if not ref:
                continue
            rubric_id = self.rubric(row)
            quiz_item = self.quiz_by_assignment.get(ref) or (
                "quiz-" + str(_ref(row.get("quiz_id"))) if _ref(row.get("quiz_id")) else None)
            if quiz_item and quiz_item in self.items:
                self.model["assignments"][quiz_item] = self.details(row, rubric_id, ["online_quiz"])
                continue
            if isinstance(row.get("discussion_topic"), dict):
                topic = _ref(row["discussion_topic"].get("id"))
                if topic:
                    self.graded_discussions["discussion-" + topic] = self.details(row, rubric_id)
                    continue
            if row.get("is_quiz_lti_assignment") is True or ref in self.new_quizzes:
                item = self.add_item("assignment-" + ref, "quiz", _title(row.get("name"), "Untitled quiz"),
                                     row.get("description"), row.get("published"))
                questions = self.new_quizzes.get(ref, [])
                if ref not in self.new_quizzes:
                    self.warnings.append(f"New Quizzes questions were not available for: {item['title'][:200]}")
                self.model["quizzes"][item["id"]] = {
                    "time_limit": None, "allowed_attempts": _attempts(row.get("allowed_attempts")),
                    "points": _n(row.get("points_possible")), "quiz_type": "new_quizzes", "questions": questions,
                }
                self.model["assignments"][item["id"]] = self.details(row, rubric_id, ["online_quiz"])
                continue
            item = self.add_item("assignment-" + ref, "assignment", _title(row.get("name"), "Untitled assignment"),
                                 row.get("description"), row.get("published"))
            self.model["assignments"][item["id"]] = self.details(row, rubric_id)

    def discussions(self):
        graded = self.graded_discussions
        for row in _list(self.bundle, "discussion_topics" if "discussion_topics" in self.bundle else "discussions",
                         cm.MAX_ITEMS):
            ref = _ref(row.get("id"))
            if not ref or row.get("is_announcement") is True:
                continue
            item = self.add_item("discussion-" + ref, "discussion", _title(row.get("title"), "Untitled discussion"),
                                 row.get("message"), row.get("published"))
            if item["id"] in graded:
                self.model["assignments"][item["id"]] = graded[item["id"]]

    def module_target(self, entry):
        kind = entry.get("type")
        content = _ref(entry.get("content_id"))
        if kind == "Page":
            return self.page_by_url.get(entry.get("page_url"))
        if kind == "Assignment" and content:
            if "assignment-" + content in self.items:
                return "assignment-" + content
            return self.quiz_by_assignment.get(content)
        if kind == "Quiz" and content:
            return "quiz-" + content if "quiz-" + content in self.items else None
        if kind == "Discussion" and content:
            return "discussion-" + content if "discussion-" + content in self.items else None
        if kind in ("ExternalUrl", "ExternalTool"):
            ref = _ref(entry.get("id"))
            if not ref or len(self.items) >= cm.MAX_ITEMS:
                return None
            item = self.add_item(("link-" if kind == "ExternalUrl" else "tool-") + ref,
                                 "external_link" if kind == "ExternalUrl" else "external_tool",
                                 _title(entry.get("title"), "External link"), "", True,
                                 entry.get("external_url") or "")
            return item["id"]
        return None

    def modules(self):
        rows = sorted(_list(self.bundle, "modules", cm.MAX_MODULES), key=lambda r: _n(r.get("position")) or 0)
        ids = {}
        for row in rows:
            ref = _ref(row.get("id"))
            if ref:
                ids[ref] = "module-" + ref
        for row in rows:
            ref = _ref(row.get("id"))
            if not ref:
                continue
            module = {"id": ids[ref], "title": _s(row.get("name"), cm.MAX_TITLE),
                      "position": _n(row.get("position")), "items": [], "requirements": [],
                      "prerequisites": [ids[p] for p in map(_ref, row.get("prerequisite_module_ids") or [])
                                        if p in ids and p != ref],
                      "sequential": row.get("require_sequential_progress") is True}
            for entry in (row.get("items") or [])[:cm.MAX_ITEMS]:
                if not isinstance(entry, dict):
                    continue
                target = self.module_target(entry)
                if not target or target in module["items"]:
                    continue
                module["items"].append(target)
                if self.items[target]["module_id"] is None:
                    self.items[target]["module_id"] = module["id"]
                requirement = entry.get("completion_requirement")
                if isinstance(requirement, dict) and requirement.get("type") in REQUIREMENT_TYPES:
                    module["requirements"].append({"item_id": target, "type": requirement["type"],
                                                   "min_score": _n(requirement.get("min_score"))})
            self.model["modules"].append(module)

    def files(self):
        by_id = {}
        for row in _list(self.bundle, "files", cm.MAX_MEDIA):
            ref = _ref(row.get("id"))
            name = _s(row.get("display_name") or row.get("filename"), 500).strip()
            if not ref or not name:
                continue
            size = _n(row.get("size"))
            media = {"path": f"files/{ref}/{name}", "mime": _s(row.get("content-type"), 200),
                     "bytes": size if size is not None and size >= 0 else 0, "referenced_by": []}
            by_id[ref] = media
            self.model["media_inventory"].append(media)
        texts = [(i["id"], i["html"]) for i in self.model["items"]]
        for item_id, html in texts:
            for ref in FILE_LINK.findall(html):
                media = by_id.get(ref)
                if media is not None and item_id not in media["referenced_by"]:
                    media["referenced_by"].append(item_id)

    def outcome_links(self):
        outcomes = {o["id"]: o for o in self.model["outcomes"]}
        for item_id, details in self.model["assignments"].items():
            rubric = self.model["rubrics"].get(details.get("rubric_id")) or {}
            for criterion in rubric.get("criteria", []):
                outcome = outcomes.get(criterion.get("outcome_id"))
                if outcome is not None and item_id not in outcome["item_ids"]:
                    outcome["item_ids"].append(item_id)


def import_api_bundle(bundle, source="extension") -> dict:
    """Return a validated course model built from a Canvas API bundle."""
    try:
        model = _Adapter(bundle).run()
        model["provenance"]["source"] = source
        return cm.validate_course(model)
    except cm.CourseModelError as exc:
        raise BundleError(f"The course could not be imported: {exc}") from None
