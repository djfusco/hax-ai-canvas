"""HAX elements Course Consultant may put in a page, and how each is written.

HAX's rule (PRAW, https://github.com/haxtheweb/praw, ``hax-content-authoring``): a page only uses
web components that exist and that the HAX editor can author; never invent a tag name. So this
module is a closed list. Each entry was read from the element's own source in
https://github.com/haxtheweb/webcomponents on 2026-11-16 (attribute spellings, slot names, how
answers are marked), not from memory:

  multiple-choice, true-false-question   elements/multiple-choice/lib/QuestionElement.js
  self-check                             elements/self-check/self-check.js
  stop-note                              elements/stop-note/stop-note.js
  a11y-collapse                          elements/a11y-collapse/a11y-collapse.js
  oer-schema                             elements/oer-schema/oer-schema.js

Two things follow from that:

- **The app writes the markup, the AI does not.** The AI returns plain data (a question, its
  options, which are correct, an explanation) and the builders here turn it into markup, so a
  page can never contain a misspelled tag or attribute.
- **The sanitizer knows the list.** ``ELEMENTS`` says which attributes and slots each element
  may carry; anything else on them is removed, and every other custom element is unwrapped.

HAX Lite stores pages through HAXcms's own sanitizer, which keeps custom elements, ``input`` and
any attribute except ``style``/``srcdoc``/event handlers, so these survive publishing.

``to_plain`` rewrites the same elements as ordinary HTML for places that cannot run HAX's
JavaScript: the plain preview, Word documents and the Canvas import file.
"""

from __future__ import annotations

from html import escape
import re

from bs4 import BeautifulSoup

# OER Schema classes used for ``oer-schema typeof`` (http://oerschema.org; never invent a class).
OER_CLASSES = ("LearningObjective", "Lesson", "Unit", "Assessment", "Quiz", "Practice", "Activity", "Course")
STOP_NOTE_STATUS = ("stop", "warning", "success", "info", "tip", "new")
QUESTION_SLOTS = ("feedbackCorrect", "feedbackIncorrect", "hint", "evidence", "content")
QUESTIONS = ("multiple-choice", "true-false-question")

# tag -> {"attributes": {name: allowed values, or None for free text}, "slots": slot names its children may use}
ELEMENTS = {
    "multiple-choice": {
        "attributes": {"question": None, "single-option": ("",), "randomize": ("",), "max-attempts": None,
                       "has-learning-objective": None},
        "slots": QUESTION_SLOTS},
    "true-false-question": {
        "attributes": {"question": None, "max-attempts": None, "has-learning-objective": None},
        "slots": QUESTION_SLOTS},
    "self-check": {"attributes": {"title": None, "has-learning-objective": None}, "slots": ("question",)},
    "stop-note": {"attributes": {"title": None, "status": STOP_NOTE_STATUS}, "slots": ("message",)},
    # heading-button makes the whole heading the control (PRAW rule WQoGOB824VGW5HECMh08ew).
    "a11y-collapse": {"attributes": {"heading-button": ("",)}, "slots": ("heading", "content")},
    "oer-schema": {"attributes": {"typeof": OER_CLASSES, "oer-property": ("name", "description"), "text": None},
                   "slots": ()},
}
# An answer option of a question: <input type="checkbox" value="..." data-correct="true" correct="correct">.
# Both markers are written on purpose. The element's current source reads ``data-correct`` and
# falls back to ``correct``; the build on HAX's CDN (checked in a browser on 2026-11-16) reads only
# ``correct``. With one marker, every answer was "incorrect" on one version or the other.
INPUT_ATTRIBUTES = {"type": ("checkbox",), "value": None, "data-correct": ("true",), "correct": ("correct",)}
CORRECT = ' data-correct="true" correct="correct"'


def clean_element(node):
    """Keep only what the element is known to accept. Returns False if ``node`` is not a HAX element."""
    spec = ELEMENTS.get(node.name)
    if spec is None:
        return False
    _keep_attributes(node, spec["attributes"])
    if node.name == "a11y-collapse":
        node["heading-button"] = ""
    return True


def clean_input(node):
    """An ``input`` is kept only as an answer option, directly inside a question."""
    if node.parent is None or node.parent.name not in QUESTIONS:
        return False
    _keep_attributes(node, INPUT_ATTRIBUTES)
    node["type"] = "checkbox"
    return bool(str(node.get("value") or "").strip())


def slot_allowed(node):
    """A ``slot`` attribute is kept only on a direct child of a HAX element that has that slot."""
    spec = ELEMENTS.get(node.parent.name) if node.parent is not None else None
    return bool(spec) and node.get("slot") in spec["slots"]


def _keep_attributes(node, allowed):
    for name in list(node.attrs):
        values = allowed.get(name.lower(), False)
        if values is False or (values is not None and str(node[name]) not in values):
            del node[name]


# ── writing the markup (the only place it is written) ────────────────────────

def _attr(value, limit=600):
    return escape(re.sub(r"\s+", " ", str(value)).strip()[:limit], quote=True)


def stop_note(title, message_html, status="info"):
    status = status if status in STOP_NOTE_STATUS else "info"
    return f'<stop-note title="{_attr(title, 120)}" status="{status}"><p slot="message">{message_html}</p></stop-note>'


def objectives(items):
    """Learning objectives as a list, each one marked up as an OER Schema LearningObjective."""
    rows = "".join(f'<li><oer-schema typeof="LearningObjective" oer-property="name" text="{_attr(o, 300)}">'
                   f"</oer-schema></li>" for o in items if str(o).strip())
    return f"<ul>{rows}</ul>" if rows else ""


def collapse(heading, content_html):
    return (f'<a11y-collapse heading-button=""><p slot="heading">{escape(heading)}</p>'
            f'<div slot="content">{content_html}</div></a11y-collapse>')


def self_check(question, answer_html, title="Check your understanding", objective=""):
    goal = f' has-learning-objective="{_attr(objective, 300)}"' if objective else ""
    return (f'<self-check title="{_attr(title, 120)}"{goal}><p slot="question">{escape(question)}</p>'
            f"{answer_html}</self-check>")


def question(kind, text, options, *, correct_html="", incorrect_html="", evidence_html="", objective=""):
    """A multiple-choice or true/false question.

    ``options`` is [(text, is_correct)]. Feedback goes in the slots, because the built-in
    "Correct" / "Incorrect" alone is the weakest kind of feedback (PRAW, hax-content-authoring).
    """
    tag = "true-false-question" if kind == "true_false" else "multiple-choice"
    attributes = f' question="{_attr(text)}"'
    if tag == "multiple-choice":
        if sum(1 for _, right in options if right) == 1:
            attributes += ' single-option=""'
        attributes += ' randomize=""'
    if objective:
        attributes += f' has-learning-objective="{_attr(objective, 300)}"'
    inputs = "".join(f'<input type="checkbox" value="{_attr(label, 300)}"' + (CORRECT if right else "") + ">"
                     for label, right in options)
    slots = "".join(f'<p slot="{name}">{html}</p>' for name, html in (
        ("feedbackCorrect", correct_html), ("feedbackIncorrect", incorrect_html), ("evidence", evidence_html)) if html)
    return f"<{tag}{attributes}>{inputs}{slots}</{tag}>"


# ── the same content without JavaScript ──────────────────────────────────────

def has_elements(html):
    return any(f"<{tag}" in (html or "") for tag in ELEMENTS)


def to_plain(html):
    """Rewrite HAX elements as ordinary HTML that reads well with no JavaScript.

    Questions become the prompt, the options and a "Show answer" disclosure; notes become a
    titled block; collapsible sections become <details>. Nothing is lost, only the interaction.
    """
    if not has_elements(html):
        return html
    soup = BeautifulSoup(html, "html.parser")

    def new(name, text=None, **attrs):
        tag = soup.new_tag(name, **attrs)
        if text is not None:
            tag.string = text
        return tag

    def children(node, slot=None):
        """Direct children in a slot (or with no slot), moved out of the element."""
        found = [c for c in list(node.children)
                 if getattr(c, "name", None) != "input"
                 and ((getattr(c, "get", None) and c.get("slot")) or None) == slot
                 and (getattr(c, "name", None) or str(c).strip())]
        for child in found:
            child.extract()
            if getattr(child, "attrs", None) is not None:
                child.attrs.pop("slot", None)
        return found

    for node in soup.find_all(list(ELEMENTS)):
        if node.name in QUESTIONS:
            box = new("div")
            prompt = new("p")
            prompt.append(new("strong", "Check your understanding: "))
            prompt.append(node.get("question", ""))
            box.append(prompt)
            options, right = new("ul"), []
            for option in node.find_all("input", recursive=False):
                label = str(option.get("value", ""))
                options.append(new("li", label))
                if option.get("data-correct") == "true" or option.has_attr("correct"):
                    right.append(label)
            box.append(options)
            answer = new("details")
            answer.append(new("summary", "Show answer"))
            answer.append(new("p", "Answer: " + "; ".join(right)))
            for slot in ("feedbackCorrect", "evidence"):
                for child in children(node, slot):
                    answer.append(child)
            children(node, "feedbackIncorrect")
            children(node, "hint")
            for child in children(node, "content"):
                box.append(child)
            box.append(answer)
            node.replace_with(box)
        elif node.name == "self-check":
            box = new("div")
            prompt = new("p")
            prompt.append(new("strong", (node.get("title") or "Check your understanding") + ": "))
            for child in children(node, "question"):
                prompt.append(child.get_text(" ", strip=True) if getattr(child, "get_text", None) else str(child))
            box.append(prompt)
            answer = new("details")
            answer.append(new("summary", "Show answer"))
            for child in children(node):
                answer.append(child)
            box.append(answer)
            node.replace_with(box)
        elif node.name == "stop-note":
            box = new("blockquote")
            title = new("p")
            title.append(new("strong", node.get("title") or "Note"))
            box.append(title)
            for child in children(node, "message") + children(node):
                box.append(child)
            node.replace_with(box)
        elif node.name == "a11y-collapse":
            box = new("details")
            heading = children(node, "heading")
            box.append(new("summary", " ".join(
                c.get_text(" ", strip=True) if getattr(c, "get_text", None) else str(c) for c in heading) or "More"))
            for child in children(node, "content") + children(node):
                box.append(child)
            node.replace_with(box)
        else:  # oer-schema: metadata around text
            node.replace_with(new("span", (node.get("text") or "") + node.get_text(" ", strip=True)))
    return str(soup)
