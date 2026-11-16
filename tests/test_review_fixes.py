"""Fixes from the November 2026 review: what must never reach students or run in a browser."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import course_build as cb
import course_site
import revised_documents as rd
from canvas_api_import import import_api_bundle
from imscc_content import sanitize_import_html
from test_course_build import BUNDLE, FakeAI


class SanitizerTests(unittest.TestCase):
    def test_markup_hidden_in_comments_and_cdata_is_removed(self):
        # A browser ends "<!-->" and "<![CDATA[>" at the first ">", so what follows would be live.
        for hostile in ("<![CDATA[><img src=x onerror=alert(1)>]]>",
                        "<!--><img src=x onerror=alert(1)>-->",
                        "<!---><img src=x onerror=alert(1)>-->",
                        "<!-- note --><p>kept</p><?php x ?><!DOCTYPE html>"):
            clean = sanitize_import_html("<p>before</p>" + hostile)
            for marker in ("onerror", "<!--", "CDATA", "<?", "<!"):
                self.assertNotIn(marker, clean, hostile)
            self.assertIn("before", clean)

    def test_ordinary_content_survives(self):
        clean = sanitize_import_html('<h2>Title</h2><p>Use a &lt; b.</p><pre>x &lt;!-- y</pre><a href="https://e.org">e</a>')
        self.assertIn("<h2>Title</h2>", clean)
        self.assertIn("a &lt; b", clean)
        self.assertIn('href="https://e.org"', clean)


class BuiltCourse(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        welcome = next(i for i in self.model["items"] if i["id"] == "page-11")
        self.model["items"].append(dict(welcome, id="page-key", title="Answer key: cell quiz",
                                        html="<p>SECRET-ANSWERS</p>"))
        self.state = {"proposals": [{"id": "p-1", "status": "accepted", "title": "Essay outline",
                                     "change": "Add an outline.", "rationale": "Early feedback.",
                                     "affected_items": ["assignment-201"]}]}
        self.plan = {"vision": "v", "outcomes": [], "ai_policy": ""}

    def built(self, edit=None):
        ai = FakeAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, self.state, self.plan, [], ai)
        if edit:
            build = dict(build, blueprint=edit(build["blueprint"]))
        while cb.steps(build, self.model, []):
            build = cb.run_step(build, cb.steps(build, self.model, [])[0]["id"], self.model, self.state,
                                self.plan, [], ai)
        return build


class AudienceTests(BuiltCourse):
    def test_instructor_pages_never_leave_the_private_view(self):
        workspace = {"build": self.built(), "documents": [], "consultation": self.state}
        everything = course_site.course_items(self.model, workspace)
        students = course_site.course_items(self.model, workspace, students_only=True)
        self.assertTrue(any(i["audience"] == "instructor" for i in everything), "the instructor still sees them")
        self.assertFalse([i["id"] for i in students if i.get("audience") == "instructor"])
        ids = {i["id"] for i in students}
        for hidden in ("page-key", "module--instructor", "design-decisions", "week-1-notes"):
            self.assertNotIn(hidden, ids)
        self.assertIn("module--week-1", ids)
        with tempfile.TemporaryDirectory() as temp:
            result = course_site.package("w", "Bio", students, Path(temp) / "site")
            with zipfile.ZipFile(result["path"]) as archive:
                text = "".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist())
        self.assertNotIn("SECRET-ANSWERS", text)
        self.assertNotIn("Prepare examples", text, "weekly instructor notes are not published")
        self.assertNotIn('"audience": "instructor"', text)

    def test_original_course_also_hides_answer_keys(self):
        workspace = {"build": None, "documents": [], "consultation": self.state}
        self.model["modules"][0]["items"].append("page-key")
        students = course_site.course_items(self.model, workspace, students_only=True)
        self.assertNotIn("page-key", {i["id"] for i in students})
        self.assertIn("page-key", {i["id"] for i in course_site.course_items(self.model, workspace)})

    def test_a_mostly_unpublished_course_is_not_treated_as_instructor_only(self):
        for item in self.model["items"]:
            item["published"] = False
        self.assertFalse(cb.unpublished_is_a_signal(self.model))
        self.assertEqual(cb.instructor_only_ids(self.model), {"page-key"}, "only the title rule applies")
        self.assertNotIn("UNPUBLISHED", cb._blueprint_prompt(self.model, self.state, self.plan, []))
        bp = cb.validate_blueprint({"summary": "s", "weeks": [{"title": "One", "items": []}]}, self.model)
        self.assertEqual(bp["instructor_items"], ["page-key"])
        self.assertIn("page-11", bp["unplaced"])
        # A few unpublished items in a published course are still kept from students.
        for item in self.model["items"]:
            item["published"] = item["id"] != "page-11"
        self.assertEqual(cb.instructor_only_ids(self.model), {"page-key", "page-11"})


class HoursTests(BuiltCourse):
    def test_ai_hours_are_for_the_instructor_until_confirmed(self):
        build = self.built()
        week = build["blueprint"]["weeks"][0]
        self.assertEqual((week["hours"], week["hours_shown"]), (6.0, False))
        pages = {p["id"]: p for p in build["pages"]}
        self.assertNotIn("Expected time", pages["week-1"]["html"])
        self.assertIn("About 6 hours", pages["week-1-notes"]["html"])
        self.assertIn("Students do not see it", pages["week-1-notes"]["html"])
        schedule = next(i for i in cb.site_items(build, self.model, [], self.state) if i["id"] == "schedule")
        self.assertNotIn("Hours", schedule["html"])

    def test_the_ai_cannot_mark_hours_as_confirmed(self):
        raw = {"summary": "s", "weeks": [{"title": "One", "items": [], "hours": 5, "hours_shown": True}]}
        self.assertFalse(cb.validate_blueprint(raw, self.model)["weeks"][0]["hours_shown"])
        self.assertTrue(cb.validate_blueprint(raw, self.model, by_instructor=True)["weeks"][0]["hours_shown"])

    def test_confirmed_hours_are_shown_to_students(self):
        def confirm(blueprint):
            weeks = [dict(w, hours=4, hours_shown=w["number"] == 1) for w in blueprint["weeks"]]
            return cb.validate_blueprint(dict(blueprint, weeks=weeks), self.model, by_instructor=True)
        build = self.built(confirm)
        pages = {p["id"]: p for p in build["pages"]}
        self.assertIn("about 4 hours", pages["week-1"]["html"])
        self.assertNotIn("Expected time", pages["week-2"]["html"])
        schedule = next(i for i in cb.site_items(build, self.model, [], self.state) if i["id"] == "schedule")
        self.assertIn("<th scope=col>Hours</th>", schedule["html"])


class ZipTests(unittest.TestCase):
    def test_course_pages_in_the_documents_zip_are_cleaned(self):
        pages = [{"id": "a", "title": "A", "html": "<p>ok</p><script>steal()</script><!--><img src=x onerror=x()>-->"}]
        with zipfile.ZipFile(__import__("io").BytesIO(rd.zip_package("Bio", [], [], pages))) as archive:
            page = archive.read(next(n for n in archive.namelist() if n.endswith("A.html"))).decode()
        self.assertIn("ok", page)
        for marker in ("<script", "onerror", "steal"):
            self.assertNotIn(marker, page)


if __name__ == "__main__":
    unittest.main()


class StoreTests(unittest.TestCase):
    """Replacing and deleting courses (through the real store and web API)."""

    def setUp(self):
        import io
        import os
        from unittest.mock import patch
        import app as web
        from test_slim_intake import slim_package
        self.web, self.io, self.package = web, io, slim_package
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        self.workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{self.workspace['id']}"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            self.web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            self.web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def upload(self, expected=200):
        revision = self.call("GET", self.base)["revision"]
        response = self.client.post(self.base + "/intake/export", data={
            "package": (self.io.BytesIO(self.package()), "s.zip"), "revision": str(revision)},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def store(self):
        from mentor_store import MentorStore
        return MentorStore(Path(self.temp.name) / "workspaces.sqlite")

    def owner(self):
        import sqlite3
        with sqlite3.connect(Path(self.temp.name) / "workspaces.sqlite") as db:
            return db.execute("SELECT owner FROM mentor_workspaces WHERE id = ?", (self.workspace["id"],)).fetchone()[0]

    def test_replacing_a_course_keeps_the_talk_and_clears_work_on_old_pages(self):
        import consultation
        self.upload()
        owner, store = self.owner(), self.store()
        model = store.get_course_model(owner, self.workspace["id"])
        old_id = model["items"][0]["id"]
        state = consultation.new_state()
        state["turns"] = 2
        state["topics"]["purpose"]["facts"] = [{"text": "Large intro course", "evidence": [old_id, "instructor"]}]
        state["proposals"] = [{"id": "p-1", "topic": "assessment", "title": "Outline first", "change": "c",
                               "rationale": "r", "principle": "TILT", "evidence": [old_id, "course-map"],
                               "affected_items": [old_id], "effort": "small", "ai_dimension": "none",
                               "status": "accepted"}]
        build = cb.validate_blueprint({"summary": "s", "weeks": [{"title": "One", "items": [old_id]}]}, model)
        store.save_consultant_fields(owner, self.workspace["id"], consultation=state, plan={"vision": "Keep me"},
                                     documents=[{"id": "doc-1", "kind": "page", "item_id": old_id, "title": "t",
                                                 "html": "<p>x</p>", "status": "approved"}],
                                     build=dict(cb.new_build("essential"), blueprint=build))
        self.upload()  # an export import gives every item a new ID
        after = store.get(owner, self.workspace["id"])
        new_ids = {i["id"] for i in store.get_course_model(owner, self.workspace["id"])["items"]}
        self.assertNotIn(old_id, new_ids)
        self.assertEqual(after["plan"], {"vision": "Keep me"})
        self.assertEqual((after["build"], after["documents"]), (None, []))
        kept = after["consultation"]
        self.assertEqual(kept["turns"], 2)
        self.assertEqual(kept["topics"]["purpose"]["facts"], [{"text": "Large intro course", "evidence": ["instructor"]}])
        self.assertEqual((kept["proposals"][0]["evidence"], kept["proposals"][0]["affected_items"]), (["course-map"], []))
        # Nothing breaks afterwards: the build view, the preview and the downloads all answer.
        self.call("GET", self.base + "/build")
        self.assertEqual(self.client.get(self.base + "/site/index.html").status_code, 200)
        self.assertEqual(self.client.get(self.base + "/documents/export?format=hax").status_code, 200)

    def test_a_course_cannot_be_replaced_while_the_ai_is_working(self):
        self.upload()
        self.store().begin_consult(self.owner(), self.workspace["id"])
        self.assertIn("still working", self.upload(expected=409)["error"])

    def test_delete_a_course_and_everything(self):
        self.upload()
        self.call("POST", self.base + "/share", {})
        other = self.call("POST", "/mentor/api/workspaces", {"title": "Chem"}, expected=201)
        self.assertEqual(self.call("DELETE", self.base), {"deleted": True})
        self.call("GET", self.base, expected=404)
        self.assertEqual([w["id"] for w in self.call("GET", "/mentor/api/workspaces")], [other["id"]])
        import sqlite3
        with sqlite3.connect(Path(self.temp.name) / "workspaces.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mentor_course_models").fetchone()[0], 0)
        self.call("DELETE", "/mentor/api/account", {}, expected=400)  # needs the typed confirmation
        self.assertEqual(self.call("DELETE", "/mentor/api/account", {"confirm": "DELETE"})["courses"], 1)
        self.assertEqual(self.call("GET", "/mentor/api/workspaces"), [])

    def test_courses_per_account_are_capped(self):
        import mentor_routes
        with unittest.mock.patch.object(mentor_routes, "MAX_WORKSPACES", 2):
            self.call("POST", "/mentor/api/workspaces", {"title": "Two"}, expected=201)
            self.assertIn("up to 2 courses", self.call("POST", "/mentor/api/workspaces", {"title": "Three"},
                                                        expected=409)["error"])


class ConsultationEvidenceTests(unittest.TestCase):
    """The consultant reads the syllabus and the instructor's documents, and flags hand-work."""

    def setUp(self):
        import consultation
        from course_map import build_course_map
        self.consultation = consultation
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.model["course"]["syllabus_html"] = "<h1>BIO 101</h1><p>SYLLABUS-PURPOSE: for first-year majors.</p>" + "<p>x</p>" * 4000
        self.model["materials"] = [{"id": "material-1", "name": "Program outcomes.docx", "role": "reference",
                                    "text": "PROGRAM-OUTCOME: reason from evidence."}]
        self.map = build_course_map(self.model)

    def prompt(self, mode="opening", message=None, scale=1.0):
        state = self.consultation.new_state("direct")
        return self.consultation._build_prompt(state, [], self.model, self.map, mode, message, scale)

    def test_syllabus_and_documents_reach_the_consultant(self):
        prompt = self.prompt()
        self.assertIn("SYLLABUS-PURPOSE: for first-year majors.", prompt)
        self.assertIn("[material-1] Program outcomes.docx", prompt)
        self.assertIn("PROGRAM-OUTCOME: reason from evidence.", prompt)
        self.assertIn("more characters not shown", prompt, "a cut is stated, never silent")
        self.assertIn("ADVICE STYLE: direct (say what you would change first", prompt)
        self.assertLess(len(self.prompt(scale=0.2)), len(prompt), "a smaller model still gets a smaller prompt")

    def test_documents_only_course_is_not_advised_blind(self):
        import course_model as cm
        from course_map import build_course_map
        model = cm.empty_course("documents", "Bio")
        model["materials"] = [{"id": "material-1", "name": "Syllabus.txt", "role": "reference",
                               "text": "Objectives: explain osmosis."}]
        model["course"]["syllabus_html"] = "<pre>Objectives: explain osmosis.</pre>"
        prompt = self.consultation._build_prompt(self.consultation.new_state(), [], model, build_course_map(model),
                                                 "opening", None, 1.0)
        self.assertEqual(prompt.count("Objectives: explain osmosis."), 1, "included once, not twice")

    def response(self, **change):
        proposal = {"topic": "assessment", "title": "Split the final into milestones", "change": "c",
                    "rationale": "r", "principle": "Scaffolding", "evidence": [], "affected_items": [],
                    "effort": "medium", "ai_dimension": "none", **change}
        return {"reply": "ok", "topic_updates": [], "proposed_changes": [proposal], "next_topic": ""}

    def test_manual_work_is_recorded_and_inferred(self):
        import improvement_plan as ip
        known = self.consultation.evidence_ids(self.model)
        _, state = self.consultation.apply_response(self.consultation.new_state(), self.response(manual_work="new_item"),
                                                    known, item_ids=set())
        self.assertEqual(state["proposals"][0]["manual_work"], "new_item")
        self.assertIn("manual_work", self.consultation._schema(known)["properties"]["proposed_changes"]["items"]["required"])
        # A model that leaves it out (or a proposal from before the field existed) is inferred from the wording.
        _, state = self.consultation.apply_response(self.consultation.new_state(), self.response(), known, item_ids=set())
        self.assertNotIn("manual_work", state["proposals"][0])
        for text, expected in (("Add a low-stakes weekly quiz", "new_item"),
                               ("Reweight the final exam to 20% of the grade", "grading_change"),
                               ("Replace the midterm with a project worth 100 points", "both"),
                               ("Clarify the essay instructions with a purpose statement", "none")):
            self.assertEqual(ip.manual_work({"title": text, "change": ""}), expected, text)

    def test_hand_work_is_shown_in_the_plan_and_the_built_course(self):
        import improvement_plan as ip
        state = {"proposals": [
            {"id": "p-1", "status": "accepted", "topic": "assessment", "title": "Add a peer review step",
             "change": "Students review a draft.", "rationale": "Feedback.", "principle": "Formative assessment",
             "effort": "medium", "ai_dimension": "none", "manual_work": "new_item", "affected_items": []},
            {"id": "p-2", "status": "accepted", "topic": "content", "title": "Clarify instructions",
             "change": "Add purpose.", "rationale": "TILT.", "principle": "TILT", "effort": "small",
             "ai_dimension": "none", "manual_work": "none", "affected_items": []},
            {"id": "p-3", "status": "proposed", "topic": "assessment", "title": "Reweight exams", "change": "c",
             "rationale": "r", "principle": "p", "effort": "small", "ai_dimension": "none",
             "manual_work": "grading_change", "affected_items": []}]}
        self.assertEqual([t["id"] for t in ip.manual_tasks(state)], ["p-1"], "only accepted changes are tasks")
        html = ip.render_html("Bio", ip.new_plan(), state)
        self.assertIn("To do by hand in Canvas", html)
        self.assertEqual(html.count("To do by hand:"), 1)
        ip.render_docx("Bio", ip.new_plan(), state)
        ai = FakeAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, state, {"vision": "v"}, [], ai)
        self.assertIn("INSTRUCTOR MUST DO BY HAND IN CANVAS", ai.prompts[0])
        for step in ("front-home", "front-start", "front-guide"):
            build = cb.run_step(build, step, self.model, state, {"vision": "v"}, [], ai)
        items = {i["id"]: i for i in cb.site_items(build, self.model, [], state)}
        self.assertEqual(items["manual-work"]["audience"], "instructor")
        self.assertIn("Add a peer review step", items["manual-work"]["html"])
        self.assertNotIn("Clarify instructions", items["manual-work"]["html"])
