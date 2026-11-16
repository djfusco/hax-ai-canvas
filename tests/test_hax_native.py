"""HAX-native pages: verified elements only, written by the app, grounded in the course."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import canvas_package
import course_build as cb
import course_site
import hax_elements as hax
import hax_preview
import revised_documents as rd
from canvas_api_import import import_api_bundle
from imscc_content import sanitize_import_html
from test_course_build import BUNDLE, FakeAI

# HAXcms's storage sanitizer keeps a custom element when its name matches this (its policy file,
# haxcms-nodejs src/lib/haxcmsSanitizePolicy.json, 2026-11-16), and drops these attributes.
HAXCMS_CUSTOM_ELEMENT = r"^[a-z][a-z0-9._-]*-[a-z0-9._-]*$"
HAXCMS_FORBIDDEN_ATTRIBUTES = {"srcdoc", "style", "is", "attributename", "xmlns"}


class ElementTests(unittest.TestCase):
    def page(self):
        return (hax.stop_note("Essential question", "Why do cells divide?")
                + hax.objectives(["Describe <cell> parts", ""])
                + hax.question("multiple_choice", 'Which makes "ATP"?', [("Mitochondrion", True), ("Nucleus", False),
                                                                         ("Ribosome", False)],
                               correct_html="Yes.", incorrect_html="Reread.", evidence_html="From the page")
                + hax.question("true_false", "Cells are alive.", [("True", True), ("False", False)], correct_html="Yes.")
                + hax.self_check("Explain osmosis.", "<p>Water moves.</p>", objective="Describe")
                + hax.collapse("Tips", "<p>Start early.</p>"))

    def test_the_app_writes_markup_the_sanitizer_accepts_unchanged(self):
        from bs4 import BeautifulSoup
        page = self.page()
        once = sanitize_import_html(page)
        self.assertEqual(sanitize_import_html(once), once, "sanitizing is stable")
        before, after = BeautifulSoup(page, "html.parser"), BeautifulSoup(once, "html.parser")
        self.assertEqual([(t.name, dict(t.attrs)) for t in before.find_all(True)],
                         [(t.name, dict(t.attrs)) for t in after.find_all(True)], "nothing is removed or added")
        self.assertIn('data-correct="true"', once)
        self.assertIn('single-option=""', once)
        self.assertEqual(once.count("<input"), 5)
        self.assertNotIn("<cell>", once, "text in attributes is escaped")

    def test_only_listed_elements_and_attributes_survive(self):
        hostile = ('<multiple-choice question="q" onclick="x()" style="a" evil="1">'
                   '<input type="text" value="A" data-correct="true" onfocus="x()"><input value=" ">'
                   '<p slot="feedbackCorrect" onclick="x()">ok</p><p slot="bogus">b</p></multiple-choice>'
                   '<input value="loose"><fake-element a="1">kept text</fake-element><p slot="message">stray</p>'
                   '<stop-note status="evil" title="t"><p slot="message">m</p></stop-note>'
                   '<video-player source="https://e.org/v.mp4"></video-player><a11y-collapse><p slot="heading">h</p></a11y-collapse>')
        clean = sanitize_import_html(hostile)
        for gone in ("onclick", "onfocus", "style=", "evil", "fake-element", "video-player", 'slot="bogus"',
                     'value="loose"', 'status="evil"', 'type="text"'):
            self.assertNotIn(gone, clean, gone)
        self.assertEqual(clean.count("<input"), 1, "an option needs a value and must sit inside a question")
        self.assertIn("kept text", clean)
        self.assertIn("<p>stray</p>", clean, "a slot outside a HAX element is removed")
        self.assertIn('<a11y-collapse heading-button="">', clean, "the whole heading is always clickable")

    def test_every_element_passes_haxcms_own_sanitizer_rules(self):
        import re
        from bs4 import BeautifulSoup
        for tag, spec in hax.ELEMENTS.items():
            self.assertRegex(tag, HAXCMS_CUSTOM_ELEMENT)
            self.assertFalse(set(spec["attributes"]) & HAXCMS_FORBIDDEN_ATTRIBUTES, tag)
        for node in BeautifulSoup(self.page(), "html.parser").find_all(True):
            for name in node.attrs:
                self.assertTrue(re.fullmatch(r"^[a-z_:][a-z0-9_.:-]*$", name), name)
                self.assertNotIn(name, HAXCMS_FORBIDDEN_ATTRIBUTES)

    def test_plain_form_keeps_everything_but_the_interaction(self):
        plain = hax.to_plain(self.page())
        for tag in hax.ELEMENTS:
            self.assertNotIn("<" + tag, plain)
        self.assertNotIn("<input", plain)
        for text in ("Why do cells divide?", "Describe <cell> parts".replace("<", "&lt;").replace(">", "&gt;"),
                     "Mitochondrion", "Nucleus", "Answer: Mitochondrion", "Answer: True", "Water moves.", "Start early.",
                     "<summary>Show answer</summary>", "<summary>Tips</summary>"):
            self.assertIn(text, plain)
        self.assertEqual(sanitize_import_html(plain), plain.replace("<br>", "<br/>"), "the plain form is ordinary safe HTML")
        self.assertEqual(hax.to_plain("<p>nothing to do</p>"), "<p>nothing to do</p>")


class PracticeAI(FakeAI):
    """Adds an essential question and practice questions (one good of each kind, several bad)."""

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        answer = json.loads(super().call_structured(system, prompt, schema, max_tokens))
        if "overview_html" in answer:
            good = {"explanation": "Because the page says so.", "hint": "Reread the welcome page.",
                    "objective": "Describe cell parts.", "source_item": "page-11",
                    "evidence": "The cell is the basic unit of life"}
            answer["essential_question"] = "What makes something alive?"
            answer["practice"] = [
                dict(good, kind="multiple_choice", question="What is the basic unit of life?",
                     options=[{"text": "The cell", "correct": True}, {"text": "The atom", "correct": False},
                              {"text": "The organ", "correct": False}]),
                dict(good, kind="true_false", question="The cell is the basic unit of life.",
                     options=[{"text": "True", "correct": True}, {"text": "False", "correct": False}]),
                dict(good, kind="self_check", question="Explain why cells matter.", options=[]),
                dict(good, kind="multiple_choice", question="Invented fact about mitochondria?", evidence="Mitochondria were found in 1857 by Kolliker",
                     options=[{"text": "A", "correct": True}, {"text": "B", "correct": False}, {"text": "C", "correct": False}]),
                dict(good, kind="multiple_choice", question="A question with no wrong option?",
                     options=[{"text": "A", "correct": True}, {"text": "B", "correct": True}, {"text": "C", "correct": True}]),
                dict(good, kind="true_false", question="From a quiz item, never allowed.", source_item="quiz-31",
                     options=[{"text": "True", "correct": True}, {"text": "False", "correct": False}]),
                dict(good, kind="essay", question="Unknown kind of question here", options=[]),
            ]
        return json.dumps(answer)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        welcome = next(i for i in self.model["items"] if i["id"] == "page-11")
        welcome["html"] = "<p>Welcome. The cell is the basic unit of life, and every organism is made of cells.</p>"
        self.state = {"proposals": []}
        self.plan = {"vision": "v", "outcomes": [], "ai_policy": ""}

    def built(self, ai):
        build = cb.blueprint(cb.new_build("essential"), self.model, self.state, self.plan, [], ai)
        while cb.steps(build, self.model, []):
            build = cb.run_step(build, cb.steps(build, self.model, [])[0]["id"], self.model, self.state, self.plan, [], ai)
        return build

    def test_week_page_gets_grounded_hax_blocks(self):
        ai = PracticeAI()
        build = self.built(ai)
        pages = {p["id"]: p for p in build["pages"]}
        week = pages["week-1"]
        html = week["html"]
        self.assertEqual((week["practice"], week["practice_dropped"]), (3, 4))
        self.assertIn('<stop-note title="Essential question" status="info">', html)
        self.assertIn('<oer-schema typeof="LearningObjective" oer-property="name" text="Describe cell parts.">', html)
        self.assertEqual((html.count("<multiple-choice"), html.count("<true-false-question"), html.count("<self-check")), (1, 1, 1))
        self.assertIn('has-learning-objective="Describe cell parts."', html)
        self.assertIn("The cell is the basic unit of life", html, "the quoted passage is shown as evidence")
        for dropped in ("Kolliker", "no wrong option", "never allowed", "Unknown kind"):
            self.assertNotIn(dropped, html)
        self.assertLess(html.index("<stop-note"), html.index("Check your understanding"))
        self.assertLess(html.index("Check your understanding"), html.index("This week's checklist"))
        self.assertIn("3 practice questions written by AI", pages["week-1-notes"]["html"])
        self.assertIn("4 more were discarded", pages["week-1-notes"]["html"])
        self.assertEqual(pages["week-2"]["practice"], 0, "no source text in week 2 supports the quoted passage")
        prompt = next(p for p in ai.prompts if "essential_question" in p)
        self.assertIn("Text: Welcome. The cell is the basic unit of life", prompt)
        self.assertIn("Never use quiz items as a source", prompt)

    def test_a_model_that_returns_only_the_text_still_builds(self):
        week = {p["id"]: p for p in self.built(FakeAI())["pages"]}["week-1"]
        self.assertEqual(week["practice"], 0)
        self.assertNotIn("<stop-note", week["html"])
        self.assertIn("<oer-schema", week["html"], "objectives come from the structure, not this answer")

    def test_each_output_gets_the_right_form(self):
        build = self.built(PracticeAI())
        workspace = {"build": build, "documents": [], "consultation": self.state}
        items = course_site.course_items(self.model, workspace, students_only=True)
        addresses = course_site.slugs(items)
        self.assertEqual(addresses["module--week-1"], "week-1-cells")
        self.assertEqual(len(set(addresses.values())), len(addresses))
        # HAX site: the real elements, readable addresses, links by address.
        with tempfile.TemporaryDirectory() as temp:
            result = course_site.package("w", "Bio", items, Path(temp) / "site")
            with zipfile.ZipFile(result["path"]) as archive:
                manifest = json.loads(archive.read("site.json"))
                by_slug = {i["slug"]: i for i in manifest["items"]}
                week = archive.read(by_slug["week-1-cells"]["location"]).decode()
                plain = archive.read("preview/" + course_site.locations(items)["module--week-1"]).decode()
        self.assertIn("<multiple-choice", week)
        self.assertIn('href="welcome"', week)
        self.assertTrue(all(i["location"].startswith("pages/") and i["location"].endswith("/index.html")
                            for i in manifest["items"]))
        self.assertNotIn("x", by_slug)
        # The offline preview, the Canvas file and the documents ZIP cannot run HAX: plain form.
        self.assertNotIn("<multiple-choice", plain)
        self.assertIn("Show answer", plain)
        package, _ = canvas_package.build_course("Bio", self.model, [], items)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            canvas = "".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist() if n.endswith(".html"))
        self.assertNotIn("<multiple-choice", canvas)
        self.assertIn("Answer: The cell", canvas)
        with zipfile.ZipFile(io.BytesIO(rd.zip_package("Bio", [], [], items))) as archive:
            text = "".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist() if n.endswith(".html"))
        self.assertNotIn("<stop-note", text)
        self.assertIn("What makes something alive?", text)
        self.assertNotIn("<multiple-choice", course_site.render("Bio", items, course_site.locations(items)["module--week-1"]))

    def test_slugs_are_safe_and_unique(self):
        items = [{"id": str(n), "title": title} for n, title in enumerate(["Week 1: Cells!", "Week 1: Cells?", "X", "", "É"])]
        self.assertEqual(list(course_site.slugs(items).values()), ["week-1-cells", "week-1-cells-2", "page", "page-2", "page-3"])


if __name__ == "__main__":
    unittest.main()
