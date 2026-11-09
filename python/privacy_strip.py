"""Remove likely student information from a course model.

Runs on the server for every intake path, before the model is stored or any
excerpt is sent to an AI provider. It is deliberately conservative: it
removes contact details and identifiers everywhere, and student names only
in patterns that clearly refer to a person in a class context. It is a
safeguard, not a guarantee, and the interface says so.

``strip_course(model, instructor_names=())`` returns ``(model, report)``.
The model is modified in place. The report counts what was removed by
category and lists the item ids touched; it never contains removed text.
"""

from __future__ import annotations

import re

EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
PHONE = re.compile(r"(?<![\w/.-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w/-])")
STUDENT_ID = re.compile(
    r"(?i)\b((?:student|user|banner|psu|university|school)?\s*id(?:\s*(?:number|#|no\.?))?\s*[:#]?\s*)\d{5,10}\b")
USER_LINK = re.compile(r"(?i)(/(?:api/v1/)?(?:courses/\d+/)?users/)\d+")

_NAME = r"[A-Z][a-z'’-]{1,20}(?:\s+[A-Z][a-z'’-]{1,20}){0,2}"
NAME_PATTERNS = [
    # "Thanks to Jane Smith", "Congratulations, Jane", "Kudos to Sam"
    re.compile(r"(?<!\w)((?:[Tt]hanks|[Tt]hank you|[Cc]ongratulations|[Cc]ongrats|[Kk]udos|[Ss]hout[- ]?out)"
               r"(?:\s+to)?[,:]?\s+)(" + _NAME + r")"),
    # "posted by Jane Smith", "submitted by Sam", "replied by ..."
    re.compile(r"(?<!\w)((?:[Pp]osted|[Ss]ubmitted|[Rr]eplied|[Ss]hared)\s+by\s+)(" + _NAME + r")"),
    # "Jane's post", "Sam Lee's reply"
    re.compile(r"(?<!\w)()(" + _NAME + r")(?=['’]s\s+(?:post|reply|response|comment|question|submission|answer|example)\b)"),
    # "Great point, Jane." / "Good question, Sam!"
    re.compile(r"(?<!\w)((?:[Gg]reat|[Gg]ood|[Nn]ice|[Ee]xcellent)\s+(?:point|question|answer|work|job|catch),\s+)(" + _NAME + r")(?=[.!?])"),
    # "@Jane" mentions
    re.compile(r"(?<![\w@])(@)([A-Z][a-z'’-]{1,20}(?:\s+[A-Z][a-z'’-]{1,20})?)"),
]
# Words that follow the trigger phrases but are not people.
NOT_NAMES = frozenset("""
Everyone All You Class The This That Our Your Students Team Group Week Module Chapter Canvas
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June
July August September October November December God Professor Dr Mr Ms Mrs
""".split())

REPLACEMENTS = {
    "emails": "[email removed]",
    "phones": "[phone removed]",
    "student_ids": "[id removed]",
    "user_links": "[user]",
    "names": "[student]",
}


class _Stripper:
    def __init__(self, instructor_names=()):
        self.counts = dict.fromkeys(REPLACEMENTS, 0)
        self.touched = []
        keep = set()
        for name in instructor_names:
            if isinstance(name, str):
                keep.update(part for part in name.split() if part)
        self.keep = keep | NOT_NAMES

    def _sub(self, pattern, category, text, replace):
        def handle(match):
            replacement = replace(match)
            if replacement != match.group(0):
                self.counts[category] += 1
            return replacement
        return pattern.sub(handle, text)

    def _name(self, match):
        prefix, name = match.group(1), match.group(2)
        if any(part in self.keep for part in name.split()):
            return match.group(0)
        return prefix + REPLACEMENTS["names"]

    def text(self, value, *, names=True):
        if not isinstance(value, str) or not value:
            return value
        value = self._sub(EMAIL, "emails", value, lambda m: REPLACEMENTS["emails"])
        value = self._sub(USER_LINK, "user_links", value, lambda m: m.group(1) + REPLACEMENTS["user_links"])
        value = self._sub(STUDENT_ID, "student_ids", value, lambda m: m.group(1) + REPLACEMENTS["student_ids"])
        value = self._sub(PHONE, "phones", value, lambda m: REPLACEMENTS["phones"])
        if names:
            for pattern in NAME_PATTERNS:
                value = self._sub(pattern, "names", value, self._name)
        return value

    def field(self, owner, key, item_id=None, *, names=True):
        before = owner.get(key)
        after = self.text(before, names=names)
        if after != before:
            owner[key] = after
            if item_id and item_id not in self.touched:
                self.touched.append(item_id)


def strip_course(model: dict, instructor_names=()) -> tuple[dict, dict]:
    """Strip likely student information from ``model`` in place."""
    stripper = _Stripper(instructor_names)
    stripper.field(model["course"], "syllabus_html", "syllabus")
    for item in model.get("items", []):
        stripper.field(item, "html", item["id"])
    for item_id, quiz in model.get("quizzes", {}).items():
        for question in quiz.get("questions", []):
            stripper.field(question, "text", item_id)
    for rubric_id, rubric in model.get("rubrics", {}).items():
        for criterion in rubric.get("criteria", []):
            stripper.field(criterion, "description", rubric_id, names=False)
            stripper.field(criterion, "long_description", rubric_id, names=False)
    for material in model.get("materials", []):
        stripper.field(material, "text", material["id"])
    report = {
        "counts": stripper.counts,
        "total": sum(stripper.counts.values()),
        "touched": stripper.touched,
        "notes": [
            "Discussion replies are never imported; only the instructor's prompt is kept.",
            "Grades, submissions and class rosters are never imported.",
            "Automatic removal is a safeguard, not a guarantee. Review content before sharing it.",
        ],
    }
    return model, report
