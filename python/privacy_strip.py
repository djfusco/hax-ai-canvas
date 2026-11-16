"""Remove likely student information from a course model.

Runs on the server for every intake path, before the model is stored or any
excerpt is sent to an AI provider. It is deliberately conservative: it
removes identifiers everywhere, contact details everywhere except where they
belong to staff and offices (the syllabus and contact or support pages), and
student names only in patterns that clearly refer to a person in a class
context. It is a safeguard, not a guarantee, and the interface says so.

Emails and phone numbers in the syllabus and on pages such as "Contact your
instructor" or "Getting help" are the instructor's, the help desk's and
support offices'. Removing them made a revised syllabus read "[email removed]",
so they are kept there and removed everywhere else.

Not covered: file names in the media inventory (a name inside a file name
cannot be told from any other word).

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

# Pages whose emails and phone numbers are staff and office contacts, not students'.
CONTACT_PAGE = re.compile(
    r"(?i)\b(contact|instructors?|professor|teacher|teaching assistants?|office hours|staff|syllabus|"
    r"welcome|start here|getting started|course info(?:rmation)?|help|support|resources|accessibility|"
    r"tutoring|advising|library)\b")

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

    def text(self, value, *, names=True, contacts=True):
        """Strip one text. ``contacts=False`` keeps emails and phone numbers (staff contact pages)."""
        if not isinstance(value, str) or not value:
            return value
        if contacts:
            value = self._sub(EMAIL, "emails", value, lambda m: REPLACEMENTS["emails"])
        value = self._sub(USER_LINK, "user_links", value, lambda m: m.group(1) + REPLACEMENTS["user_links"])
        value = self._sub(STUDENT_ID, "student_ids", value, lambda m: m.group(1) + REPLACEMENTS["student_ids"])
        if contacts:
            value = self._sub(PHONE, "phones", value, lambda m: REPLACEMENTS["phones"])
        if names:
            for pattern in NAME_PATTERNS:
                value = self._sub(pattern, "names", value, self._name)
        return value

    def field(self, owner, key, item_id=None, *, names=True, contacts=True):
        before = owner.get(key)
        after = self.text(before, names=names, contacts=contacts)
        if after != before:
            owner[key] = after
            if item_id and item_id not in self.touched:
                self.touched.append(item_id)


def strip_course(model: dict, instructor_names=()) -> tuple[dict, dict]:
    """Strip likely student information from ``model`` in place."""
    stripper = _Stripper(instructor_names)
    stripper.field(model["course"], "syllabus_html", "syllabus", contacts=False)
    kept_contacts = []
    for item in model.get("items", []):
        staff_page = item.get("kind") == "page" and bool(CONTACT_PAGE.search(item.get("title") or ""))
        if staff_page and (EMAIL.search(item.get("html") or "") or PHONE.search(item.get("html") or "")):
            kept_contacts.append(item["id"])
        stripper.field(item, "html", item["id"], contacts=not staff_page)
        stripper.field(item, "title", item["id"])
    for module in model.get("modules", []):
        stripper.field(module, "title", module.get("id"))
    for item_id, quiz in model.get("quizzes", {}).items():
        for question in quiz.get("questions", []):
            stripper.field(question, "text", item_id)
    for bank in model.get("question_banks", []):
        for question in bank.get("questions", []):
            stripper.field(question, "text", bank.get("id"))
    for rubric_id, rubric in model.get("rubrics", {}).items():
        for criterion in rubric.get("criteria", []):
            stripper.field(criterion, "description", rubric_id, names=False)
            stripper.field(criterion, "long_description", rubric_id, names=False)
            for rating in criterion.get("ratings", []):
                stripper.field(rating, "description", rubric_id, names=False)
                stripper.field(rating, "long_description", rubric_id, names=False)
    for outcome in model.get("outcomes", []):
        stripper.field(outcome, "title", outcome.get("id"), names=False)
        stripper.field(outcome, "description", outcome.get("id"), names=False)
    for material in model.get("materials", []):
        # An uploaded syllabus holds staff contacts, like the Canvas syllabus.
        syllabus = "syllabus" in (material.get("name") or "").lower()
        stripper.field(material, "text", material["id"], contacts=not syllabus)
    report = {
        "counts": stripper.counts,
        "total": sum(stripper.counts.values()),
        "touched": stripper.touched,
        "kept_contacts": kept_contacts,
        "notes": [
            "Email addresses and phone numbers are kept in the syllabus and on contact and support pages, "
            "where they belong to staff and offices; they are removed everywhere else.",
            "Discussion replies are never imported; only the instructor's prompt is kept.",
            "Grades, submissions and class rosters are never imported.",
            "Automatic removal is a safeguard, not a guarantee. Review content before sharing it.",
        ],
    }
    return model, report
