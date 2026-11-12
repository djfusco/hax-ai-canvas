"""Building the finished course (AI calls are scripted)."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import course_build as cb
import course_site
from canvas_api_import import import_api_bundle

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


class FakeAI:
    """Answers each kind of build request; records prompts."""

    def __init__(self, weeks=None):
        self.prompts = []
        self.weeks = weeks

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        self.prompts.append(prompt)
        keys = set(schema["properties"])
        if "weeks" in keys:
            return json.dumps({"summary": "Two themed weeks building to the essay.", "weeks": self.weeks or [
                {"title": "Cells", "theme": "What cells are.", "objectives": ["Describe cell parts."],
                 "items": ["page-11", "quiz-31", "discussion-61", "invented-id", "page-11"], "hours": 6,
                 "instructor_focus": "Check prior knowledge."},
                {"title": "Arguing from evidence", "theme": "Writing.", "objectives": ["Construct an argument."],
                 "items": ["assignment-202", "assignment-201"], "hours": 8, "instructor_focus": "Feedback."}]})
        if "home_html" in keys:
            return json.dumps({"home_html": '<h2>Welcome</h2><p>Start with <a href="item:module--start">Start here</a>.</p>'})
        if "start_here_html" in keys:
            return json.dumps({"start_here_html": "<h2>How this course works</h2><p>Each week has an overview.</p>"})
        if "instructor_guide_html" in keys:
            return json.dumps({"instructor_guide_html": "<h2>Design</h2><p>Weekly rhythm.</p>"})
        if "overview_html" in keys:
            return json.dumps({"overview_html": '<p>This week: read <a href="item:page-11">Welcome</a>, '
                                                '<a href="item:not-real">ghost</a>.</p><script>x()</script>',
                               "instructor_notes_html": "<p>Prepare examples.</p>"})
        if "pages" in keys:
            ids = [line.split()[1] for line in prompt.splitlines() if line.startswith("ITEM ")]
            return json.dumps({"pages": [{"item_id": i, "html": f"<h2>Purpose</h2><p>Why {i} matters.</p>"} for i in ids]})
        if keys == {"html"}:
            return json.dumps({"html": "<p>Revised page.</p><h2>This week's checklist</h2><p>AI copy</p>"})
        raise AssertionError(keys)


class CourseBuildTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.plan = {"vision": "Evidence first.", "outcomes": [{"text": "Argue from evidence.", "note": "new"}],
                     "ai_policy": "Cite AI use."}
        self.state = {"proposals": [{"id": "p-1", "status": "accepted", "title": "Essay outline",
                                     "change": "Add an outline.", "rationale": "Early feedback.",
                                     "affected_items": ["assignment-201"]}]}

    def build(self, mode, ai=None):
        ai = ai or FakeAI()
        build = cb.blueprint(cb.new_build(mode), self.model, self.state, self.plan, [], ai)
        while cb.steps(build, self.model, []):
            step = cb.steps(build, self.model, [])[0]
            build = cb.run_step(build, step["id"], self.model, self.state, self.plan, [], ai)
        return build, ai

    def test_blueprint_places_every_item_once(self):
        ai = FakeAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, self.state, self.plan, [], ai)
        weeks = build["blueprint"]["weeks"]
        self.assertEqual([w["number"] for w in weeks], [1, 2])
        self.assertEqual(weeks[0]["items"], ["page-11", "quiz-31", "discussion-61"], "unknown and repeated IDs dropped")
        self.assertEqual(build["blueprint"]["unplaced"], ["assignment-203", "assignment-205", "link-1004"])
        self.assertIn("Essay outline", ai.prompts[0])
        self.assertIn("Argue from evidence.", ai.prompts[0])
        self.assertIn("due: ", ai.prompts[0])
        with self.assertRaises(cb.BuildError):
            cb.validate_blueprint({"weeks": []}, self.model)

    def test_placement_rules_keep_instructor_material_from_students(self):
        welcome = next(i for i in self.model["items"] if i["id"] == "page-11")
        solutions = dict(welcome, id="page-sol", title="For Instructors: Lab Solutions", html="<p>Answers</p>")
        hidden = dict(welcome, id="page-hid", title="Draft notes", published=False, html="<p>Draft</p>")
        self.model["items"] += [solutions, hidden]
        aliases = {v: k for k, v in cb._aliases(self.model).items()}
        raw = {"summary": "s", "start_items": [aliases["page-11"]], "instructor_items": [],
               "weeks": [{"title": "Week 1: Cells", "theme": "", "objectives": [], "hours": 5, "instructor_focus": "",
                          "items": [aliases["assignment-201"], "page-sol", aliases["quiz-31"]]}]}
        bp = cb.validate_blueprint(raw, self.model)
        self.assertEqual(bp["weeks"][0]["title"], "Cells", "the week number is not repeated")
        self.assertEqual(bp["weeks"][0]["items"], ["assignment-201", "quiz-31"], "labels map to IDs; solutions removed")
        self.assertEqual(bp["start_items"], ["page-11"])
        self.assertIn("page-sol", bp["instructor_items"], "instructor titles always go to the instructor section")
        self.assertIn("page-hid", bp["instructor_items"], "unplaced unpublished items stay hidden")
        self.assertNotIn("page-sol", bp["unplaced"])
        self.assertIn("i1", cb._blueprint_prompt(self.model, self.state, self.plan, []))
        self.assertIn("UNPUBLISHED in Canvas", cb._blueprint_prompt(self.model, self.state, self.plan, []))
        build = dict(cb.new_build("essential"), blueprint=bp)
        for step in ("front-home", "front-start", "front-guide"):
            build = cb.run_step(build, step, self.model, self.state, self.plan, [], FakeAI())
        items = {i["id"]: i for i in cb.site_items(build, self.model, [], self.state)}
        self.assertEqual((items["page-11"]["module"], items["page-11"]["audience"]), ("Start here", "student"))
        self.assertEqual(items["page-sol"]["audience"], "instructor")
        self.assertEqual(items["page-hid"]["audience"], "instructor")
        self.assertTrue(all(i["audience"] == "instructor" for i in items.values()
                            if i["module"] == "Instructor (not shown to students)"))

    def test_essential_build_and_site(self):
        build, ai = self.build("essential")
        self.assertEqual(build["done"], ["front-home", "front-start", "front-guide", "week-1", "week-2"])
        self.assertEqual(len(ai.prompts), 6)
        self.assertIn("Length: 250-400 words", ai.prompts[1])
        week = next(p for p in build["pages"] if p["id"] == "week-1")
        self.assertNotIn("<script", week["html"])
        self.assertIn("$IMSCC_ITEM_REFERENCE$/page-11", week["html"])
        self.assertNotIn("not-real", week["html"])
        self.assertIn("This week's checklist", week["html"])
        items = cb.site_items(build, self.model, [], self.state)
        ids = [i["id"] for i in items]
        self.assertEqual(ids[:5], ["home", "module--start", "syllabus", "schedule", "module--week-1"])
        self.assertLess(ids.index("module--week-2"), ids.index("assignment-201"))
        self.assertIn("module--resources", ids)
        instructor = [i for i in items if i["audience"] == "instructor"]
        self.assertEqual([i["id"] for i in instructor],
                         ["module--instructor", "design-decisions", "week-1-notes", "week-2-notes"])
        essay = next(i for i in items if i["id"] == "assignment-201")
        self.assertIn("<strong>Points:</strong>", essay["html"])
        # Every output renders it.
        places = course_site.locations(items)
        home = course_site.render("Bio", items, "index.html")
        self.assertIn("Start here", home)
        self.assertIn(places["module--start"], home, "home links to Start here")
        page = course_site.render("Bio", items, places["module--week-1"])
        self.assertIn(places["page-11"].split("/")[1], page, "item links resolve to page addresses")

    def test_full_build_rewrites_pages_and_keeps_wording_score(self):
        build, ai = self.build("full")
        self.assertIn("week-2-items-1", build["done"])
        page = next(p for p in build["pages"] if p["id"] == "item-assignment-201")
        self.assertEqual(page["source"], "assignment-201")
        self.assertIn("kept", page)
        self.assertIn("KEEP the instructor's original wording", ai.prompts[-1])
        items = cb.site_items(build, self.model, [], self.state)
        essay = next(i for i in items if i["id"] == "assignment-201")
        self.assertIn("Why assignment-201 matters", essay["html"])
        self.assertIn("<strong>Points:</strong>", essay["html"], "exact facts are added by the app")

    def test_edit_revise_and_restore(self):
        build, _ = self.build("essential")
        build = cb.edit_page(build, "home", "<p>My own welcome.</p><script>x</script>")
        home = next(p for p in build["pages"] if p["id"] == "home")
        self.assertEqual((home["html"], home["built_by"]), ("<p>My own welcome.</p>", "instructor"))
        build = cb.revise_page(build, "week-1", "Shorter please.", self.model, self.plan, FakeAI())
        week = next(p for p in build["pages"] if p["id"] == "week-1")
        self.assertIn("Revised page.", week["html"])
        self.assertNotIn("AI copy", week["html"], "the checklist is always the app's exact one")
        self.assertEqual(week["html"].count("This week's checklist"), 1)
        build = cb.restore_page(build, "week-1", len(week["history"]) - 1)
        self.assertNotIn("Revised page.", next(p for p in build["pages"] if p["id"] == "week-1")["html"])
        with self.assertRaises(cb.BuildError):
            cb.run_step(build, "week-9", self.model, self.state, self.plan, [], FakeAI())
        self.assertIsNone(cb.site_items(cb.new_build("full"), self.model, [], self.state))

    def test_every_output_uses_the_built_course(self):
        import io
        import tempfile
        import zipfile
        import canvas_package
        import hax_preview
        import revised_documents as rd
        from slim_import import import_slim
        build, _ = self.build("essential")
        items = cb.site_items(build, self.model, [], self.state)
        # Canvas: one module per section, assignments keep points and due dates, pages link to each other.
        data, summary = canvas_package.build_course("Biology", self.model, [], items)
        self.assertEqual(summary["modules"], 5)  # Start here, 2 weeks, resources, instructor
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "course.imscc"
            path.write_bytes(data)
            imported = import_slim(path)
        self.assertEqual([m["title"] for m in imported["modules"]][:3],
                         ["Start here", "Week 1: Cells", "Week 2: Arguing from evidence"])
        essay = next(i for i in imported["items"] if i["title"] == "Essay")
        self.assertEqual(imported["assignments"][essay["id"]]["points"],
                         self.model["assignments"]["assignment-201"]["points"])
        week = next(i for i in imported["items"] if i["title"] == "Week 1: Cells")
        self.assertIn("Welcome", week["html"])
        archive = zipfile.ZipFile(io.BytesIO(data))
        week_file = next(n for n in archive.namelist() if n.startswith("wiki_content/week-1"))
        self.assertIn("$WIKI_REFERENCE$/pages/", archive.read(week_file).decode())
        # Word + HTML ZIP: every page in order, instructor pages apart, links between files.
        package = zipfile.ZipFile(io.BytesIO(rd.zip_package("Biology", [], [], items)))
        names = package.namelist()
        self.assertTrue(names[0].startswith("course/001 "), names[0])
        self.assertTrue(any(n.startswith("course/instructor/") for n in names))
        self.assertIn("course/Whole course.docx", names)
        week_html = package.read(next(n for n in names if "Week-1-Cells" in n)).decode()
        self.assertIn('href="00', week_html)
        # HAX: links point at the other page's address.
        places = course_site.locations(items)
        week_item = next(i for i in items if i["id"] == "module--week-1")
        self.assertIn(f'href="{places["page-11"].split("/")[1]}"', hax_preview.page_html(week_item, places))

    def test_repeated_title_headings_are_removed(self):
        self.assertEqual(cb.without_title_heading("<h2>Welcome to Week 2: Data Models</h2><p>x</p>", "Week 2: Data Models"),
                         "<p>x</p>")
        kept = "<h2>Learning objectives</h2><p>x</p>"
        self.assertEqual(cb.without_title_heading(kept, "Week 2: Data Models"), kept)

    def test_kept_share(self):
        original = "<p>Write a 1500 word essay about cell biology using two sources.</p>"
        self.assertEqual(cb.kept_share(original, "<h2>What to do</h2>" + original), 1.0)
        self.assertLess(cb.kept_share(original, "<p>Compose an essay on cells.</p>"), 0.2)


if __name__ == "__main__":
    unittest.main()


class BuildApiTests(unittest.TestCase):
    """The build routes, with the real app and a scripted AI."""

    def setUp(self):
        import io
        import os
        import tempfile
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "python/app"))
        import app as web
        from test_slim_intake import slim_package
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.addCleanup(lambda: web.app.config.update(MENTOR_DATA_ROOT=old) if old else web.app.config.pop("MENTOR_DATA_ROOT", None))
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{workspace['id']}"
        self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "s.zip"), "revision": str(workspace["revision"])},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.call("POST", "/mentor/api/credentials", {"provider": "openai", "api_key": "sk-test-only-fixture-key",
                                                      "model": "fixture-text"})
        factory = patch("ai_client.AIClient")
        mock = factory.start()
        self.addCleanup(factory.stop)
        items = [i["id"] for i in self.client.get(self.base + "/course-map").json["assessments"]]
        self.ai = FakeAI(weeks=[{"title": "Start", "theme": "t", "objectives": ["o"], "items": items[:2], "hours": 5,
                                 "instructor_focus": "f"},
                                {"title": "Finish", "theme": "t", "objectives": ["o"], "items": items[2:], "hours": 6,
                                 "instructor_focus": "f"}])
        mock.return_value.call_structured.side_effect = self.ai.call_structured

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def test_build_flow_and_outputs(self):
        import io
        import zipfile
        estimate = self.call("GET", self.base + "/estimate?action=build_full")
        self.assertGreater(estimate["requests"][1], estimate["requests"][0])
        cheap = self.call("GET", self.base + "/estimate?action=build_essential")
        self.assertLess(cheap["requests"][1], estimate["requests"][1])
        view = self.call("GET", self.base + "/build")
        self.assertIsNone(view["build"])
        self.call("POST", self.base + "/build/blueprint", {"provider": "openai", "mode": "huge"}, expected=502)
        view = self.call("POST", self.base + "/build/blueprint", {"provider": "openai", "mode": "full"})
        self.assertEqual([s["id"] for s in view["steps"]][:4], ["front-home", "front-start", "front-guide", "week-1"])
        self.assertFalse(view["ready"])
        before = self.client.get(self.base + "/site/index.html").get_data(as_text=True)
        self.assertNotIn("Course sections", before, "the plain site until the course is built")
        exact = self.call("GET", self.base + "/estimate?action=build_full&structure=current")
        self.assertEqual(exact["requests"][0], exact["requests"][1])
        self.assertEqual(exact["requests"][0], len(view["steps"]))
        for step in view["steps"]:
            view = self.call("POST", self.base + "/build/step", {"provider": "openai", "step": step["id"]})
        self.assertEqual(view["steps"], [])
        self.assertTrue(view["ready"])
        site = self.client.get(self.base + "/site/index.html").get_data(as_text=True)
        self.assertIn("Course sections", site)
        self.assertIn("Instructor only", site)
        # Every download uses the built course.
        cartridge = self.client.get(self.base + "/documents/export?format=imscc")
        self.assertIn("course-for-Canvas", cartridge.headers["Content-Disposition"])
        revised_only = self.client.get(self.base + "/documents/export?format=imscc&scope=revised")
        self.assertEqual(revised_only.status_code, 400, "no approved revisions in this test")
        names = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export").data)).namelist()
        self.assertIn("course/Whole course.docx", names)
        site_zip = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export?format=hax").data))
        self.assertIn("Start here", site_zip.read("site.json").decode())
        # Review: edit, revise, restore, and structure edits clear the pages.
        page = view["build"]["pages"][0]["id"]
        view = self.call("PUT", f"{self.base}/build/pages/{page}", {"html": "<p>Mine.</p>"})
        view = self.call("POST", f"{self.base}/build/pages/{page}/revise", {"provider": "openai", "instruction": "Shorter."})
        view = self.call("POST", f"{self.base}/build/pages/{page}/restore", {"index": 1})
        restored = next(p for p in view["build"]["pages"] if p["id"] == page)
        self.assertEqual(restored["html"], "<p>Mine.</p>")
        weeks = view["build"]["blueprint"]["weeks"]
        weeks[0]["title"] = "Renamed"
        view = self.call("PUT", self.base + "/build/blueprint", {"weeks": weeks})
        self.assertEqual(view["build"]["blueprint"]["weeks"][0]["title"], "Renamed")
        self.assertFalse(view["ready"])
        view = self.call("DELETE", self.base + "/build")
        self.assertIsNone(view["build"])
        # The HAX theme applies to the HAX site (default Learn Two).
        self.assertEqual(view["theme"], "learn-two-theme")
        self.call("PUT", self.base + "/site-theme", {"theme": "nope"}, expected=400)
        self.call("PUT", self.base + "/site-theme", {"theme": "clean-two"})
        self.assertEqual(self.call("GET", self.base + "/build")["theme"], "clean-two")
        site_zip = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export?format=hax").data))
        self.assertEqual(json.loads(site_zip.read("site.json"))["metadata"]["theme"]["element"], "clean-two")
