"""Page revisions with instructions, tutor kit, version history, comparison."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import course_site
import improvement_plan as ip
import revised_documents as rd
from test_plan_documents import Client, setup


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.model, self.map, state, self.essay = setup()
        self.state = ip.decide(state, "p-1", "accepted")
        self.welcome = next(i["id"] for i in self.model["items"] if i["title"] == "Welcome")

    def test_page_revision_with_instruction(self):
        client = Client({"title": "Welcome", "html": "<h2>Welcome</h2><p>Hello, scientists.</p>",
                         "summary_of_changes": "Friendlier.", "applied_proposals": []})
        document = rd.generate("page", self.model, self.map, self.state, None, client, item_id=self.welcome,
                               instruction="Make it friendlier.")
        prompt = client.prompts[0][0]
        self.assertIn("INSTRUCTOR'S REQUEST (follow it): Make it friendlier.", prompt)
        self.assertIn("CURRENT PAGE (HTML)", prompt)
        self.assertEqual((document["kind"], document["item_id"]), ("page", self.welcome))
        with self.assertRaisesRegex(rd.DocumentError, "4000"):
            rd.generate("page", self.model, self.map, self.state, None, Client(), item_id=self.welcome,
                        instruction="x" * 5000)
        # An approved page revision replaces the original in the course site.
        documents = rd.add([], {**document, "status": "approved"})
        page = next(i for i in course_site.build_items(self.model, documents) if i["id"] == self.welcome)
        self.assertTrue(page["revised"])
        self.assertIn("Hello, scientists.", page["html"])

    def test_tutor_kit(self):
        client = Client({"title": "Course AI tutor kit", "html": "<h2>Tutor instructions</h2><pre>You are...</pre>",
                         "summary_of_changes": "", "applied_proposals": []})
        document = rd.generate("tutor", self.model, self.map, self.state, {"ai_policy": "Cite AI."}, client)
        self.assertEqual((document["kind"], document["item_id"]), ("tutor", None))
        self.assertIn("system prompt", client.prompts[0][0])
        self.assertIn("Cite AI.", client.prompts[0][0])
        self.assertIn("tutor", [t["kind"] for t in rd.targets(self.model, self.state)])

    def test_document_versions_and_restore(self):
        base = {"kind": "syllabus", "item_id": None, "title": "Syllabus", "summary": "", "applied_proposals": [],
                "status": "approved", "created_at": "", "updated_at": None, "html": "<p>Version one</p>"}
        documents = rd.add([], base)
        documents = rd.edit(documents, "doc-1", {"html": "<p>Version two</p>", "title": "Syllabus v2"})
        documents = rd.replace(documents, "doc-1", {**base, "html": "<p>Version three</p>"})
        doc = documents[0]
        self.assertEqual([v["html"] for v in doc["versions"]], ["<p>Version one</p>", "<p>Version two</p>"])
        self.assertEqual(doc["versions"][0]["title"], "Syllabus", "snapshot taken before the change")
        documents = rd.restore(documents, "doc-1", 0)
        doc = documents[0]
        self.assertEqual((doc["html"], doc["status"]), ("<p>Version one</p>", "draft"))
        self.assertEqual(doc["versions"][-1]["html"], "<p>Version three</p>", "undo is itself undoable")
        with self.assertRaises(rd.DocumentError):
            rd.restore(documents, "doc-1", 9)
        for n in range(15):
            documents = rd.edit(documents, "doc-1", {"html": f"<p>v{n}</p>"})
        self.assertEqual(len(documents[0]["versions"]), rd.MAX_VERSIONS)

    def test_plan_versions(self):
        plan = ip.edit_plan(ip.new_plan(), {"vision": "First vision."})
        self.assertNotIn("history", plan, "an empty plan leaves no version")
        plan = ip.edit_plan(plan, {"vision": "Second vision."})
        self.assertEqual(plan["history"][0]["vision"], "First vision.")
        plan = ip.restore_plan(plan, 0)
        self.assertEqual(plan["vision"], "First vision.")
        self.assertEqual(plan["history"][-1]["vision"], "Second vision.")
        with self.assertRaises(ip.PlanError):
            ip.restore_plan(plan, 5)

    def test_compare(self):
        document = {"kind": "assignment", "item_id": self.essay, "title": "Essay",
                    "html": "<p>Argue a position on cell theory using two sources.</p>"}
        result = rd.compare(document, self.model)
        self.assertTrue(result["has_original"])
        self.assertIn("<ins>theory using two sources.</ins>", result["diff_html"])
        self.assertIn("<del>theory.</del>", result["diff_html"])
        self.assertGreater(result["words_added"], 0)
        hostile = rd.compare({**document, "html": "<p>&lt;script&gt;x</p>"}, self.model)
        self.assertNotIn("<script>", hostile["diff_html"])
        tutor = rd.compare({"kind": "tutor", "item_id": None, "title": "Kit", "html": "<p>New</p>"}, self.model)
        self.assertFalse(tutor["has_original"])
        rubric = rd.compare({"kind": "rubric", "item_id": self.essay, "title": "R", "html": "<p>New rubric</p>"}, self.model)
        self.assertIn("Thesis and evidence", rubric["original_html"])


if __name__ == "__main__":
    unittest.main()
