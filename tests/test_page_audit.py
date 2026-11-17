"""Videos kept as HAX players, spaced review, and HAX's page audits by rule."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import course_build as cb
import course_site
import hax_elements as hax
import hax_preview
import page_audit
from canvas_api_import import import_api_bundle
from imscc_content import sanitize_import_html
from test_course_build import BUNDLE, FakeAI
from test_hax_native import PracticeAI


class VideoTests(unittest.TestCase):
    def test_embedded_videos_become_players_and_other_frames_go(self):
        html = sanitize_import_html(
            '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ?rel=0" title="Cell division"></iframe>'
            '<iframe src="//player.vimeo.com/video/123456789"></iframe>'
            '<iframe src="https://www.youtube-nocookie.com/embed/abcDEF12345"></iframe>'
            '<iframe src="https://evil.example/embed/x"></iframe><iframe srcdoc="<script>x</script>"></iframe>')
        self.assertEqual(html.count("<video-player"), 3)
        self.assertIn('source="https://www.youtube.com/watch?v=dQw4w9WgXcQ"', html)
        self.assertIn('media-title="Cell division"', html)
        self.assertIn('source="https://vimeo.com/123456789"', html)
        self.assertNotIn("iframe", html)
        self.assertIn('<a href="https://evil.example/embed/x">Embedded content (evil.example)</a>', html,
                      "another site's embed is never shown; it becomes a link the reader can choose to open")
        self.assertEqual(sanitize_import_html(html), html)

    def test_a_player_only_takes_a_known_video_address(self):
        for bad in ("javascript:alert(1)", "https://evil.example/watch?v=abcdefgh", "https://youtube.com.evil.example/embed/abcdefgh",
                    "data:text/html,x", ""):
            self.assertEqual(sanitize_import_html(f'<video-player source="{bad}" onclick="x()"></video-player><p>ok</p>'), "<p>ok</p>", bad)
            self.assertEqual(hax.video(bad), "")
        self.assertEqual(hax.video_source("https://youtu.be/abcdefg1234?t=10"), "https://www.youtube.com/watch?v=abcdefg1234")

    def test_outputs_that_cannot_run_hax_get_a_link(self):
        model = import_api_bundle(copy.deepcopy(BUNDLE))
        next(i for i in model["items"] if i["id"] == "page-11")["html"] = (
            '<p>Watch first.</p><iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Mitosis"></iframe>')
        items = course_site.build_items(model, [])
        welcome = next(i for i in items if i["id"] == "page-11")
        self.assertIn("<video-player", hax_preview.page_html(welcome, course_site.slugs(items)))
        plain = course_site.render("Bio", items, course_site.locations(items)["page-11"])
        self.assertNotIn("video-player", plain)
        self.assertIn("Watch the video: Mitosis", plain)
        self.assertIn('href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"', plain)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        next(i for i in self.model["items"] if i["id"] == "page-11")["html"] = (
            "<p>Welcome. The cell is the basic unit of life, and every organism is made of cells.</p>")
        ai = PracticeAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, {"proposals": []}, {"vision": "v"}, [], ai)
        while cb.steps(build, self.model, []):
            build = cb.run_step(build, cb.steps(build, self.model, [])[0]["id"], self.model, {"proposals": []},
                                {"vision": "v"}, [], ai)
        self.build = build

    def items(self, build=None):
        return course_site.course_items(self.model, {"build": build or self.build, "documents": [],
                                                     "consultation": {"proposals": []}}, students_only=True)

    def test_week_two_brings_back_a_question_from_week_one(self):
        items = {i["id"]: i for i in self.items()}
        first, second = items["module--week-1"]["html"], items["module--week-2"]["html"]
        self.assertNotIn("Review from earlier weeks", first, "nothing came before week 1")
        self.assertIn("Review from earlier weeks", second)
        self.assertIn("What is the basic unit of life?", second)
        self.assertLess(second.index("Review from earlier weeks"), second.index("This week's checklist"))
        self.assertEqual(second.count("Review from earlier weeks"), 1)
        self.assertNotIn("module--review", items, "a course review needs practice in at least two weeks")

    def test_course_review_samples_every_week(self):
        build = json.loads(json.dumps(self.build))
        pages = {p["id"]: p for p in build["pages"]}
        pages["week-2"]["practice_blocks"] = [hax.self_check("Why does evidence matter?", "<p>Because.</p>")]
        items = {i["id"]: i for i in self.items(build)}
        review = items["module--review"]["html"]
        self.assertIn("From week 1", review)
        self.assertIn("Why does evidence matter?", review)
        self.assertEqual(review.count("<h2>"), 2)


class AuditTests(ReviewTests):
    def test_chunking_uses_praws_thresholds_and_reports_the_numbers(self):
        dense = ("<p>" + "word " * 230 + "</p>" + "<p>" + "word " * 190 + "</p>"
                 + "<ul>" + "<li>item</li>" * 9 + "</ul>")
        found = {f["rule"]: f["detail"] for f in page_audit.chunking(dense)}
        self.assertIn("1 paragraph over 200 words (the longest is 230)", found["wall-of-text"])
        self.assertIn("A run of 429 words with no heading", found["heading-gap"])
        self.assertIn("the longest has 9", found["list-fatigue"])
        self.assertEqual(page_audit.chunking("<h2>A</h2><p>" + "word " * 150 + "</p><h2>B</h2><p>" + "word " * 150 + "</p>"), [])
        inside = hax.self_check("Q", "<p>" + "word " * 300 + "</p>")
        self.assertEqual(page_audit.chunking(inside), [], "text revealed on request is not a wall of text")

    def test_weeks_are_rated_by_the_events_of_instruction(self):
        report = page_audit.audit(self.items(), self.model, self.build)
        weeks = {w["number"]: w for w in report["weeks"]}
        self.assertEqual(weeks[1]["rating"], "Partial")
        self.assertNotIn("Ask the learner to do something", weeks[1]["missing"])
        self.assertNotIn("Help it last and transfer", weeks[1]["missing"], "week 1's questions come back in week 2")
        self.assertIn("Recall earlier learning", weeks[1]["missing"])
        self.assertNotIn("Recall earlier learning", weeks[2]["missing"], "week 2 reviews week 1")
        self.assertTrue(weeks[1]["udl"]["action"])
        self.assertEqual(report["summary"]["no_response"], 0, "week 2's review still asks students to respond")
        self.assertTrue(report["not_checked"])
        self.assertIn("PRAW", report["source"])

    def test_a_course_that_only_presents_is_named(self):
        ai = FakeAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, {"proposals": []}, {"vision": "v"}, [], ai)
        while cb.steps(build, self.model, []):
            build = cb.run_step(build, cb.steps(build, self.model, [])[0]["id"], self.model, {"proposals": []},
                                {"vision": "v"}, [], ai)
        report = page_audit.audit(self.items(build), self.model, build)
        self.assertTrue(report["overall"].startswith("Single-path"))
        self.assertEqual(report["summary"]["no_response"], 2)
        self.assertTrue(all("Ask the learner to do something" in w["missing"] for w in report["weeks"]))

    def test_before_a_build_only_pages_are_checked(self):
        report = page_audit.audit(course_site.build_items(self.model, []), self.model, None)
        self.assertEqual(report["weeks"], [])
        self.assertIn("Build the course", report["overall"])


if __name__ == "__main__":
    unittest.main()


class EmbeddedFrameTests(unittest.TestCase):
    def test_other_embedded_content_becomes_a_link_not_nothing(self):
        html = sanitize_import_html(
            '<iframe src="https://psu.app.box.com/embed/s/abc" title="Lecture slides" onload="x()"></iframe>'
            '<iframe src="//docs.google.com/presentation/d/1/embed"></iframe>'
            '<iframe src="javascript:alert(1)"></iframe><iframe src="http://insecure.example/x"></iframe>'
            '<iframe srcdoc="<script>x</script>"></iframe>')
        self.assertIn('<a href="https://psu.app.box.com/embed/s/abc">Lecture slides (psu.app.box.com)</a>', html)
        self.assertIn('<a href="https://docs.google.com/presentation/d/1/embed">Embedded content (docs.google.com)</a>', html)
        for gone in ("iframe", "javascript", "insecure.example", "onload", "script"):
            self.assertNotIn(gone, html)
        self.assertEqual(sanitize_import_html(html), html)


class ModuleLinkTests(ReviewTests):
    def test_a_link_to_an_old_module_goes_to_the_week_that_holds_it(self):
        module = self.model["modules"][0]
        home = next(w for w in self.build["blueprint"]["weeks"] if set(w["items"]) & set(module["items"]))
        page = next(i for i in self.model["items"] if i["id"] == "page-11")
        page["html"] = f'<p>Back to <a href="$IMSCC_ITEM_REFERENCE$/module--{module["id"]}">the module</a>.</p>'
        items = self.items()
        welcome = next(i for i in items if i["id"] == "page-11")
        html = hax_preview.page_html(welcome, course_site.slugs(items))
        self.assertIn(f'href="{course_site.slugs(items)["module--week-%d" % home["number"]]}"', html)
        self.assertNotIn("REFERENCE", html)
