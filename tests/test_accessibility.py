import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import accessibility as a11y
from canvas_api_import import import_api_bundle

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


class AccessibilityTests(unittest.TestCase):
    def test_rules(self):
        html = ('<h2>Week</h2><h4>Skipped</h4><img src="a.png"><img src="b.png" alt="IMG_0042.JPG">'
                '<img src="c.png" alt="">'
                '<a href="/x">click here</a><a href="/y">https://example.edu/long</a><a href="/z"></a>'
                '<a href="/w"><img src="d.png"></a><a href="/ok">Syllabus PDF</a>'
                '<table><tr><td>1</td></tr><tr><td>2</td></tr></table><iframe src="https://v"></iframe>'
                '<video src="v.mp4"></video><video src="w.mp4"><track kind="captions" src="w.vtt"></video>')
        self.assertEqual(a11y.check_html(html), {
            "heading-skip": 1, "image-alt-missing": 1, "image-alt-filename": 1, "link-generic": 1,
            "link-url-text": 1, "link-empty": 1, "linked-image-no-text": 1, "table-no-headers": 1,
            "frame-no-title": 1, "media-no-captions": 1})
        clean = ('<h2>A</h2><h3>B</h3><img src="x.png" alt="Diagram of a cell"><a href="/s">Course schedule</a>'
                 '<table><tr><th>Week</th></tr><tr><td>1</td></tr></table><iframe title="Lecture" src="x"></iframe>')
        self.assertEqual(a11y.check_html(clean), {})

    def test_course_report_uses_revisions_and_media(self):
        model = import_api_bundle(copy.deepcopy(BUNDLE))
        welcome = next(i for i in model["items"] if i["title"] == "Welcome")
        welcome["html"] = '<p>See <a href="/r">here</a></p><img src="x.png">'
        report = a11y.course_report(model, [])
        entry = next(e for e in report["items"] if e["id"] == welcome["id"])
        self.assertTrue(entry["fixable"])
        self.assertEqual({i["rule"] for i in entry["issues"]}, {"link-generic", "image-alt-missing"})
        self.assertEqual(report["items"][0]["id"], welcome["id"], "errors sort first")
        self.assertEqual(report["videos_without_captions"], ["lecture one.mp4"])
        self.assertIn("Link text does not say where it goes", a11y.fix_instruction(entry))
        self.assertIn("Instructor to describe", a11y.fix_instruction(entry))
        fixed = [{"kind": "page", "item_id": welcome["id"], "status": "approved", "title": "Welcome",
                  "html": '<p>See the <a href="/r">reading list</a></p><img src="x.png" alt="Cell diagram">'}]
        self.assertFalse(any(e["id"] == welcome["id"] for e in a11y.course_report(model, fixed)["items"]))
        model["media_inventory"].append({"path": "files/900/lecture one.vtt", "mime": "text/vtt", "bytes": 10,
                                         "referenced_by": []})
        self.assertEqual(a11y.course_report(model, [])["videos_without_captions"], [])


if __name__ == "__main__":
    unittest.main()
