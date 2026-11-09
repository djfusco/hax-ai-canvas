"""Normalized course model shared by every intake path.

Both the slim export importer and the browser-extension adapter produce this
structure. Privacy stripping, the course map and the consultation read only
this model, never raw archives or Canvas API payloads.

The model is plain JSON-compatible dicts. ``validate_course`` enforces types,
references and size limits and raises ``CourseModelError`` with a message
that names the failing field. It never echoes course content.
"""

from __future__ import annotations

import math

MODEL_VERSION = 1

MAX_ITEMS = 500
MAX_ITEM_HTML_BYTES = 2 * 1024 * 1024
MAX_TOTAL_HTML_BYTES = 16 * 1024 * 1024
MAX_MODULES = 300
MAX_RUBRICS = 200
MAX_CRITERIA = 60
MAX_RATINGS = 20
MAX_QUIZ_QUESTIONS = 2000
MAX_OUTCOMES = 500
MAX_GROUPS = 100
MAX_MEDIA = 5000
MAX_MATERIALS = 30
MAX_MATERIAL_CHARS = 60000
MAX_WARNINGS = 200
MAX_TITLE = 500
MAX_TEXT = 20000
MAX_ID = 200

ITEM_KINDS = frozenset((
    "page", "assignment", "quiz", "discussion", "file", "external_link", "external_tool",
))
SOURCES = frozenset(("export", "extension", "documents", "canvas"))
MATERIAL_ROLES = frozenset(("reference", "policy", "student"))


class CourseModelError(ValueError):
    """The course model is structurally invalid or exceeds a limit."""


def empty_course(source: str, title: str = "Untitled course") -> dict:
    """Return a minimal valid model to be filled by an adapter."""
    return {
        "version": MODEL_VERSION,
        "course": {"title": title, "code": "", "term": "", "syllabus_html": "",
                   "grading_scheme": None, "late_policy": None, "weighted_groups": False},
        "modules": [],
        "items": [],
        "assignments": {},
        "assignment_groups": [],
        "rubrics": {},
        "quizzes": {},
        "outcomes": [],
        "question_banks": [],
        "media_inventory": [],
        "materials": [],
        "provenance": {"source": source, "imported_at": None, "counts": {}, "warnings": []},
    }


# ── field helpers ────────────────────────────────────────────────────────────

def _fail(field, reason):
    raise CourseModelError(f"Course model field '{field}' {reason}.")


def _dict(value, field):
    if not isinstance(value, dict):
        _fail(field, "must be an object")
    return value


def _list(value, field, maximum):
    if not isinstance(value, list):
        _fail(field, "must be a list")
    if len(value) > maximum:
        _fail(field, f"exceeds the limit of {maximum} entries")
    return value


def _str(value, field, maximum, *, empty=True):
    if not isinstance(value, str):
        _fail(field, "must be text")
    if len(value) > maximum:
        _fail(field, f"exceeds {maximum} characters")
    if not empty and not value.strip():
        _fail(field, "must not be empty")
    return value


def _opt_str(value, field, maximum):
    return None if value is None else _str(value, field, maximum)


def _id(value, field):
    _str(value, field, MAX_ID, empty=False)
    if any(ord(c) < 32 for c in value):
        _fail(field, "contains control characters")
    return value


def _num(value, field, *, nullable=True, minimum=None):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(field, "must be a finite number")
    if minimum is not None and value < minimum:
        _fail(field, f"must be at least {minimum}")
    return value


def _bool(value, field):
    if type(value) is not bool:
        _fail(field, "must be true or false")
    return value


def _ids(value, field, known, maximum=1000):
    _list(value, field, maximum)
    for index, ref in enumerate(value):
        _id(ref, f"{field}[{index}]")
        if known is not None and ref not in known:
            _fail(f"{field}[{index}]", "references an unknown id")
    return value


# ── validator ────────────────────────────────────────────────────────────────

def validate_course(model: dict) -> dict:
    """Validate ``model`` in place and return it."""
    _dict(model, "model")
    if model.get("version") != MODEL_VERSION:
        _fail("version", f"must be {MODEL_VERSION}")

    course = _dict(model.get("course"), "course")
    _str(course.get("title"), "course.title", MAX_TITLE, empty=False)
    _str(course.get("code", ""), "course.code", MAX_TITLE)
    _str(course.get("term", ""), "course.term", MAX_TITLE)
    _str(course.get("syllabus_html", ""), "course.syllabus_html", MAX_ITEM_HTML_BYTES)
    _bool(course.get("weighted_groups", False), "course.weighted_groups")
    scheme = course.get("grading_scheme")
    if scheme is not None:
        _dict(scheme, "course.grading_scheme")
        _str(scheme.get("title", ""), "course.grading_scheme.title", MAX_TITLE)
        for index, row in enumerate(_list(scheme.get("levels", []), "course.grading_scheme.levels", 50)):
            if (not isinstance(row, dict)):
                _fail(f"course.grading_scheme.levels[{index}]", "must be an object")
            _str(row.get("name"), f"course.grading_scheme.levels[{index}].name", 50, empty=False)
            _num(row.get("min"), f"course.grading_scheme.levels[{index}].min", nullable=False)
    late = course.get("late_policy")
    if late is not None:
        _dict(late, "course.late_policy")
        for key, value in late.items():
            if not isinstance(key, str) or (value is not None and type(value) not in (str, int, float, bool)):
                _fail("course.late_policy", "must hold simple values")

    # Items first: everything else references them.
    items = _list(model.get("items"), "items", MAX_ITEMS)
    item_ids = set()
    total_html = 0
    for index, item in enumerate(items):
        field = f"items[{index}]"
        _dict(item, field)
        item_id = _id(item.get("id"), f"{field}.id")
        if item_id in item_ids:
            _fail(f"{field}.id", "is duplicated")
        item_ids.add(item_id)
        if item.get("kind") not in ITEM_KINDS:
            _fail(f"{field}.kind", "is not a supported item kind")
        _str(item.get("title"), f"{field}.title", MAX_TITLE, empty=False)
        html = _str(item.get("html", ""), f"{field}.html", MAX_ITEM_HTML_BYTES)
        total_html += len(html.encode("utf-8"))
        _opt_str(item.get("module_id"), f"{field}.module_id", MAX_ID)
        _opt_str(item.get("url"), f"{field}.url", 2000)
        _bool(item.get("published", True), f"{field}.published")
    if total_html > MAX_TOTAL_HTML_BYTES:
        _fail("items", "exceeds the 16 MiB total instructional HTML limit")

    modules = _list(model.get("modules"), "modules", MAX_MODULES)
    module_ids = set()
    for index, module in enumerate(modules):
        field = f"modules[{index}]"
        _dict(module, field)
        module_id = _id(module.get("id"), f"{field}.id")
        if module_id in module_ids:
            _fail(f"{field}.id", "is duplicated")
        module_ids.add(module_id)
    for index, module in enumerate(modules):
        field = f"modules[{index}]"
        _str(module.get("title"), f"{field}.title", MAX_TITLE)
        _num(module.get("position"), f"{field}.position")
        _ids(module.get("items", []), f"{field}.items", item_ids, MAX_ITEMS)
        _ids(module.get("prerequisites", []), f"{field}.prerequisites", module_ids, MAX_MODULES)
        _bool(module.get("sequential", False), f"{field}.sequential")
        for r_index, requirement in enumerate(_list(module.get("requirements", []), f"{field}.requirements", MAX_ITEMS)):
            r_field = f"{field}.requirements[{r_index}]"
            _dict(requirement, r_field)
            _id(requirement.get("item_id"), f"{r_field}.item_id")
            if requirement["item_id"] not in item_ids:
                _fail(f"{r_field}.item_id", "references an unknown id")
            _str(requirement.get("type"), f"{r_field}.type", 50, empty=False)
            _num(requirement.get("min_score"), f"{r_field}.min_score")
    for index, item in enumerate(items):
        if item.get("module_id") is not None and item["module_id"] not in module_ids:
            _fail(f"items[{index}].module_id", "references an unknown module")

    groups = _list(model.get("assignment_groups"), "assignment_groups", MAX_GROUPS)
    group_ids = set()
    for index, group in enumerate(groups):
        field = f"assignment_groups[{index}]"
        _dict(group, field)
        group_id = _id(group.get("id"), f"{field}.id")
        if group_id in group_ids:
            _fail(f"{field}.id", "is duplicated")
        group_ids.add(group_id)
        _str(group.get("name"), f"{field}.name", MAX_TITLE)
        _num(group.get("weight"), f"{field}.weight", minimum=0)
        _num(group.get("position"), f"{field}.position")

    rubrics = _dict(model.get("rubrics"), "rubrics")
    if len(rubrics) > MAX_RUBRICS:
        _fail("rubrics", f"exceeds the limit of {MAX_RUBRICS} entries")
    for rubric_id, rubric in rubrics.items():
        field = f"rubrics.{rubric_id}"
        _id(rubric_id, field)
        _dict(rubric, field)
        _str(rubric.get("title"), f"{field}.title", MAX_TITLE)
        _num(rubric.get("points"), f"{field}.points")
        for c_index, criterion in enumerate(_list(rubric.get("criteria", []), f"{field}.criteria", MAX_CRITERIA)):
            c_field = f"{field}.criteria[{c_index}]"
            _dict(criterion, c_field)
            _str(criterion.get("description"), f"{c_field}.description", MAX_TEXT)
            _str(criterion.get("long_description", ""), f"{c_field}.long_description", MAX_TEXT)
            _num(criterion.get("points"), f"{c_field}.points")
            _opt_str(criterion.get("outcome_id"), f"{c_field}.outcome_id", MAX_ID)
            for r_index, rating in enumerate(_list(criterion.get("ratings", []), f"{c_field}.ratings", MAX_RATINGS)):
                r_field = f"{c_field}.ratings[{r_index}]"
                _dict(rating, r_field)
                _str(rating.get("description"), f"{r_field}.description", MAX_TEXT)
                _str(rating.get("long_description", ""), f"{r_field}.long_description", MAX_TEXT)
                _num(rating.get("points"), f"{r_field}.points")

    assignments = _dict(model.get("assignments"), "assignments")
    for item_id, details in assignments.items():
        field = f"assignments.{item_id}"
        if item_id not in item_ids:
            _fail(field, "references an unknown item")
        _dict(details, field)
        _num(details.get("points"), f"{field}.points")
        _opt_str(details.get("due_at"), f"{field}.due_at", 64)
        _ids(details.get("submission_types", []), f"{field}.submission_types", None, 20)
        _opt_str(details.get("grading_type"), f"{field}.grading_type", 50)
        group = details.get("group_id")
        if group is not None and group not in group_ids:
            _fail(f"{field}.group_id", "references an unknown assignment group")
        rubric = details.get("rubric_id")
        if rubric is not None and rubric not in rubrics:
            _fail(f"{field}.rubric_id", "references an unknown rubric")
        _bool(details.get("peer_reviews", False), f"{field}.peer_reviews")
        _bool(details.get("group_work", False), f"{field}.group_work")
        _num(details.get("allowed_attempts"), f"{field}.allowed_attempts")

    quizzes = _dict(model.get("quizzes"), "quizzes")
    question_total = 0
    for item_id, quiz in quizzes.items():
        field = f"quizzes.{item_id}"
        if item_id not in item_ids:
            _fail(field, "references an unknown item")
        _dict(quiz, field)
        _num(quiz.get("time_limit"), f"{field}.time_limit")
        _num(quiz.get("allowed_attempts"), f"{field}.allowed_attempts")
        _num(quiz.get("points"), f"{field}.points")
        _opt_str(quiz.get("quiz_type"), f"{field}.quiz_type", 50)
        questions = _list(quiz.get("questions", []), f"{field}.questions", MAX_QUIZ_QUESTIONS)
        question_total += len(questions)
        for q_index, question in enumerate(questions):
            q_field = f"{field}.questions[{q_index}]"
            _dict(question, q_field)
            _str(question.get("text"), f"{q_field}.text", MAX_TEXT)
            _str(question.get("type"), f"{q_field}.type", 100)
            _num(question.get("points"), f"{q_field}.points")
    if question_total > MAX_QUIZ_QUESTIONS:
        _fail("quizzes", f"exceed the limit of {MAX_QUIZ_QUESTIONS} questions in total")

    bank_questions = 0
    for index, bank in enumerate(_list(model.get("question_banks", []), "question_banks", MAX_RUBRICS)):
        field = f"question_banks[{index}]"
        _dict(bank, field)
        _id(bank.get("id"), f"{field}.id")
        _str(bank.get("title"), f"{field}.title", MAX_TITLE)
        questions = _list(bank.get("questions", []), f"{field}.questions", MAX_QUIZ_QUESTIONS)
        bank_questions += len(questions)
        for q_index, question in enumerate(questions):
            q_field = f"{field}.questions[{q_index}]"
            _dict(question, q_field)
            _str(question.get("text"), f"{q_field}.text", MAX_TEXT)
            _str(question.get("type"), f"{q_field}.type", 100)
            _num(question.get("points"), f"{q_field}.points")
    if bank_questions > MAX_QUIZ_QUESTIONS:
        _fail("question_banks", f"exceed the limit of {MAX_QUIZ_QUESTIONS} questions in total")

    outcomes = _list(model.get("outcomes"), "outcomes", MAX_OUTCOMES)
    outcome_ids = set()
    for index, outcome in enumerate(outcomes):
        field = f"outcomes[{index}]"
        _dict(outcome, field)
        outcome_id = _id(outcome.get("id"), f"{field}.id")
        if outcome_id in outcome_ids:
            _fail(f"{field}.id", "is duplicated")
        outcome_ids.add(outcome_id)
        _str(outcome.get("title"), f"{field}.title", MAX_TITLE, empty=False)
        _str(outcome.get("description", ""), f"{field}.description", MAX_TEXT)
        _num(outcome.get("mastery_points"), f"{field}.mastery_points")
        _ids(outcome.get("item_ids", []), f"{field}.item_ids", item_ids, MAX_ITEMS)
    for rubric_id, rubric in rubrics.items():
        for c_index, criterion in enumerate(rubric.get("criteria", [])):
            ref = criterion.get("outcome_id")
            if ref is not None and ref not in outcome_ids:
                _fail(f"rubrics.{rubric_id}.criteria[{c_index}].outcome_id", "references an unknown outcome")

    for index, media in enumerate(_list(model.get("media_inventory"), "media_inventory", MAX_MEDIA)):
        field = f"media_inventory[{index}]"
        _dict(media, field)
        _str(media.get("path"), f"{field}.path", 1000, empty=False)
        _str(media.get("mime", ""), f"{field}.mime", 200)
        _num(media.get("bytes"), f"{field}.bytes", minimum=0)
        _ids(media.get("referenced_by", []), f"{field}.referenced_by", item_ids, MAX_ITEMS)

    for index, material in enumerate(_list(model.get("materials"), "materials", MAX_MATERIALS)):
        field = f"materials[{index}]"
        _dict(material, field)
        _id(material.get("id"), f"{field}.id")
        _str(material.get("name"), f"{field}.name", MAX_TITLE, empty=False)
        if material.get("role") not in MATERIAL_ROLES:
            _fail(f"{field}.role", "is not a supported material role")
        _str(material.get("text"), f"{field}.text", MAX_MATERIAL_CHARS)

    provenance = _dict(model.get("provenance"), "provenance")
    if provenance.get("source") not in SOURCES:
        _fail("provenance.source", "is not a supported source")
    _opt_str(provenance.get("imported_at"), "provenance.imported_at", 64)
    counts = _dict(provenance.get("counts", {}), "provenance.counts")
    for key, value in counts.items():
        if not isinstance(key, str) or type(value) is not int or value < 0:
            _fail("provenance.counts", "must map names to non-negative integers")
    for index, warning in enumerate(_list(provenance.get("warnings", []), "provenance.warnings", MAX_WARNINGS)):
        _str(warning, f"provenance.warnings[{index}]", 1000)
    return model
