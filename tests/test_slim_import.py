"""Slim package import: full Canvas metadata into the course model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import course_model as cm
import slim_import

NS = 'xmlns="http://canvas.instructure.com/xsd/cccv1p0"'


def resource(identifier, kind, href="", files=(), dependencies=()):
    return (f'<resource identifier="{identifier}" type="{kind}" href="{href}">'
            + "".join(f'<file href="{f}"/>' for f in files)
            + "".join(f'<dependency identifierref="{d}"/>' for d in dependencies)
            + "</resource>")


MANIFEST = f"""<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" identifier="m">
<metadata><lom xmlns="http://ltsc.ieee.org/xsd/LOM"><general><title><string>Biology 101</string></title>
</general></lom></metadata><organizations/><resources>
{resource("settings", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
          "course_settings/canvas_export.txt",
          ["course_settings/course_settings.xml", "course_settings/module_meta.xml",
           "course_settings/assignment_groups.xml", "course_settings/rubrics.xml",
           "course_settings/learning_outcomes.xml", "course_settings/syllabus.html",
           "course_settings/canvas_export.txt"])}
{resource("gwelcome", "webcontent", "wiki_content/welcome.html", ["wiki_content/welcome.html"])}
{resource("gessay", "associatedcontent/imscc_xmlv1p1/learning-application-resource", "gessay/essay.html",
          ["gessay/essay.html", "gessay/assignment_settings.xml"])}
{resource("glab", "associatedcontent/imscc_xmlv1p1/learning-application-resource", "glab/lab.html",
          ["glab/lab.html", "glab/assignment_settings.xml"])}
{resource("gproject", "associatedcontent/imscc_xmlv1p1/learning-application-resource", "gproject/project.html",
          ["gproject/project.html", "gproject/assignment_settings.xml"])}
{resource("gquiz", "imsqti_xmlv1p2/imscc_xmlv1p1/assessment", "",
          ["gquiz/assessment_qti.xml", "gquiz/assessment_meta.xml"])}
{resource("gvideo", "webcontent", "web_resources/lecture%20one.mp4", ["web_resources/lecture one.mp4"])}
{resource("gpdf", "webcontent", "web_resources/reading.pdf", ["web_resources/reading.pdf"])}
</resources></manifest>"""

COURSE_SETTINGS = f"""<course {NS} identifier="c"><title>Biology 101: Cells</title>
<course_code>BIO-101</course_code><start_at>2026-08-25T00:00:00Z</start_at>
<group_weighting_scheme>percent</group_weighting_scheme></course>"""

GROUPS = f"""<assignmentGroups {NS}>
<assignmentGroup identifier="gg-essays"><title>Essays</title><position>1</position><group_weight>40.0</group_weight></assignmentGroup>
<assignmentGroup identifier="gg-labs"><title>Labs</title><position>2</position><group_weight>30.0</group_weight></assignmentGroup>
<assignmentGroup identifier="gg-quizzes"><title>Quizzes</title><position>3</position><group_weight>30.0</group_weight></assignmentGroup>
</assignmentGroups>"""

OUTCOMES = f"""<learningOutcomes {NS}>
<learningOutcomeGroup identifier="og"><title>Program</title><learningOutcomes>
<learningOutcome identifier="o-argue"><title>Construct a scientific argument</title>
<description>&lt;p&gt;Use evidence.&lt;/p&gt;</description><mastery_points>3</mastery_points></learningOutcome>
</learningOutcomes></learningOutcomeGroup>
<learningOutcome identifier="o-lab"><title>Use lab equipment safely</title><mastery_points>2</mastery_points></learningOutcome>
</learningOutcomes>"""

RUBRICS = f"""<rubrics {NS}><rubric identifier="r-essay"><title>Essay rubric</title>
<points_possible>20.0</points_possible><criteria>
<criterion><criterion_id>_1</criterion_id><points>12.0</points><description>Thesis and evidence</description>
<long_description>Claims are supported</long_description>
<learning_outcome_identifierref>o-argue</learning_outcome_identifierref>
<ratings><rating><description>Strong</description><points>12.0</points></rating>
<rating><description>Weak</description><points>4.0</points></rating></ratings></criterion>
<criterion><criterion_id>_2</criterion_id><points>8.0</points><description>Writing</description>
<ratings><rating><description>Clear</description><points>8.0</points></rating></ratings></criterion>
</criteria></rubric></rubrics>"""

MODULES = f"""<modules {NS}>
<module identifier="mod1"><title>Week 1</title><position>1</position>
<require_sequential_progress>true</require_sequential_progress>
<completionRequirements><completionRequirement type="must_view"><identifierref>mi-welcome</identifierref></completionRequirement>
<completionRequirement type="min_score"><min_score>7</min_score><identifierref>mi-quiz</identifierref></completionRequirement>
</completionRequirements><items>
<item identifier="mi-welcome"><content_type>WikiPage</content_type><title>Welcome</title><identifierref>gwelcome</identifierref><position>1</position></item>
<item identifier="mi-essay"><content_type>Assignment</content_type><title>Essay</title><identifierref>gessay</identifierref><position>2</position></item>
<item identifier="mi-quiz"><content_type>Quizzes::Quiz</content_type><title>Quiz</title><identifierref>gquiz</identifierref><position>3</position></item>
<item identifier="mi-link"><content_type>ExternalUrl</content_type><title>NCBI</title><url>https://www.ncbi.nlm.nih.gov/</url><position>4</position></item>
</items></module>
<module identifier="mod2"><title>Week 2</title><position>2</position>
<prerequisites><prerequisite type="context_module"><title>Week 1</title><identifierref>mod1</identifierref></prerequisite></prerequisites>
<items><item identifier="mi-lab"><content_type>Assignment</content_type><title>Lab</title><identifierref>glab</identifierref><position>1</position></item>
<item identifier="mi-project"><content_type>Assignment</content_type><title>Project</title><identifierref>gproject</identifierref><position>2</position></item>
</items></module></modules>"""


def assignment(identifier, title, points, group, rubric="", extra=""):
    return f"""<assignment {NS} identifier="{identifier}"><title>{title}</title>
<due_at>2026-09-10T23:59:00Z</due_at><points_possible>{points}</points_possible>
<grading_type>points</grading_type><submission_types>online_upload</submission_types>
<assignment_group_identifierref>{group}</assignment_group_identifierref>
{f"<rubric_identifierref>{rubric}</rubric_identifierref>" if rubric else ""}{extra}
<workflow_state>published</workflow_state></assignment>"""


QTI = """<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2">
<assessment ident="gquiz" title="Cell quiz"><section ident="s">
<item ident="q1" title="Parts"><itemmetadata><qtimetadata>
<qtimetadatafield><fieldlabel>question_type</fieldlabel><fieldentry>multiple_choice_question</fieldentry></qtimetadatafield>
<qtimetadatafield><fieldlabel>points_possible</fieldlabel><fieldentry>2.0</fieldentry></qtimetadatafield>
</qtimetadata></itemmetadata><presentation><material><mattext>Which is a cell part?</mattext></material>
<response_lid><render_choice><response_label ident="a"><material><mattext>Nucleus</mattext></material></response_label>
</render_choice></response_lid></presentation></item>
<item ident="q2" title="Explain"><itemmetadata><qtimetadata>
<qtimetadatafield><fieldlabel>question_type</fieldlabel><fieldentry>essay_question</fieldentry></qtimetadatafield>
<qtimetadatafield><fieldlabel>points_possible</fieldlabel><fieldentry>8.0</fieldentry></qtimetadatafield>
</qtimetadata></itemmetadata><presentation><material><mattext>Explain osmosis.</mattext></material></presentation></item>
</section></assessment></questestinterop>"""

QUIZ_META = f"""<quiz {NS} identifier="gquiz"><title>Cell quiz</title><description>&lt;p&gt;Check understanding.&lt;/p&gt;</description>
<quiz_type>assignment</quiz_type><points_possible>10.0</points_possible><time_limit>20</time_limit>
<allowed_attempts>2</allowed_attempts><assignment identifier="gquiz-a"><title>Cell quiz</title>
<due_at>2026-09-05T23:59:00Z</due_at><assignment_group_identifierref>gg-quizzes</assignment_group_identifierref>
</assignment></quiz>"""

LATE = f"""<late_policy {NS}><late_submission_deduction_enabled>true</late_submission_deduction_enabled>
<late_submission_deduction>10.0</late_submission_deduction><late_submission_interval>day</late_submission_interval></late_policy>"""

GRADING = f"""<gradingStandards {NS}><gradingStandard identifier="gs" version="2"><title>Letters</title>
<data>[["A", 0.94], ["B", 0.84], ["F", 0.0]]</data></gradingStandard></gradingStandards>"""


def text_entries():
    return {
        "imsmanifest.xml": MANIFEST,
        "course_settings/course_settings.xml": COURSE_SETTINGS,
        "course_settings/assignment_groups.xml": GROUPS,
        "course_settings/learning_outcomes.xml": OUTCOMES,
        "course_settings/rubrics.xml": RUBRICS,
        "course_settings/module_meta.xml": MODULES,
        "course_settings/late_policy.xml": LATE,
        "course_settings/grading_standards.xml": GRADING,
        "course_settings/syllabus.html": "<p>Office hours Tuesday. Textbook: Campbell Biology.</p>",
        "course_settings/canvas_export.txt": "export",
        "wiki_content/welcome.html": ('<html><head><title>Welcome</title></head><body><p>Welcome to cells.</p>'
                                      '<video src="$IMS-CC-FILEBASE$/lecture%20one.mp4"></video>'
                                      '<a href="%24IMS-CC-FILEBASE%24/reading.pdf">Reading</a></body></html>'),
        "gessay/essay.html": "<html><head><title>Essay</title></head><body><p>Argue a position on cell theory.</p></body></html>",
        "gessay/assignment_settings.xml": assignment("gessay", "Essay", "20.0", "gg-essays", "r-essay",
                                                     "<peer_reviews>true</peer_reviews>"),
        "glab/lab.html": "<html><head><title>Lab</title></head><body><p>Microscope lab report.</p></body></html>",
        "glab/assignment_settings.xml": assignment("glab", "Lab", "15.0", "gg-labs", "",
                                                   "<group_category>Lab teams</group_category><allowed_attempts>3</allowed_attempts>"),
        "gproject/project.html": "<html><head><title>Project</title></head><body><p>Final project.</p></body></html>",
        "gproject/assignment_settings.xml": assignment("gproject", "Project", "30.0", "gg-essays"),
        "gquiz/assessment_qti.xml": QTI,
        "gquiz/assessment_meta.xml": QUIZ_META,
    }


class SlimImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "course.zip"

    def tearDown(self):
        self.directory.cleanup()

    def build(self, entries):
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return slim_import.import_slim(self.path)

    def slim(self, extra=None):
        entries = text_entries()
        entries["media_inventory.json"] = json.dumps([
            {"path": "web_resources/lecture one.mp4", "bytes": 4_000_000_000, "mime": "video/mp4"},
            {"path": "web_resources/reading.pdf", "bytes": 2_000_000, "mime": "application/pdf"},
        ])
        entries.update(extra or {})
        return self.build(entries)

    def item(self, model, title):
        return next(i for i in model["items"] if i["title"] == title)

    def test_full_metadata(self):
        model = self.slim()
        cm.validate_course(model)
        course = model["course"]
        self.assertEqual(course["title"], "Biology 101: Cells")
        self.assertEqual(course["code"], "BIO-101")
        self.assertTrue(course["weighted_groups"])
        self.assertIn("Campbell", course["syllabus_html"])
        self.assertEqual(course["grading_scheme"]["levels"][0], {"name": "A", "min": 0.94})
        self.assertEqual(course["late_policy"]["late_submission_deduction"], 10.0)
        self.assertIs(course["late_policy"]["late_submission_deduction_enabled"], True)

        weights = {g["name"]: g["weight"] for g in model["assignment_groups"]}
        self.assertEqual(weights, {"Essays": 40.0, "Labs": 30.0, "Quizzes": 30.0})
        groups = {g["id"]: g["name"] for g in model["assignment_groups"]}

        essay = model["assignments"][self.item(model, "Essay")["id"]]
        self.assertEqual(essay["points"], 20.0)
        self.assertEqual(groups[essay["group_id"]], "Essays")
        self.assertTrue(essay["peer_reviews"])
        rubric = model["rubrics"][essay["rubric_id"]]
        self.assertEqual(rubric["title"], "Essay rubric")
        self.assertEqual([c["description"] for c in rubric["criteria"]], ["Thesis and evidence", "Writing"])
        self.assertEqual(rubric["criteria"][0]["points"], 12.0)
        self.assertEqual([r["points"] for r in rubric["criteria"][0]["ratings"]], [12.0, 4.0])

        lab = model["assignments"][self.item(model, "Lab")["id"]]
        self.assertTrue(lab["group_work"])
        self.assertEqual(lab["allowed_attempts"], 3)
        self.assertIsNone(lab["rubric_id"])

        quiz_item = self.item(model, "Cell quiz")
        quiz = model["quizzes"][quiz_item["id"]]
        self.assertEqual((quiz["time_limit"], quiz["allowed_attempts"], quiz["points"]), (20.0, 2, 10.0))
        self.assertEqual([q["type"] for q in quiz["questions"]], ["multiple_choice_question", "essay_question"])
        self.assertEqual([q["points"] for q in quiz["questions"]], [2.0, 8.0])
        graded = model["assignments"][quiz_item["id"]]
        self.assertEqual(groups[graded["group_id"]], "Quizzes")
        self.assertEqual(graded["due_at"], "2026-09-05T23:59:00Z")

        outcomes = {o["title"]: o for o in model["outcomes"]}
        self.assertEqual(set(outcomes), {"Construct a scientific argument", "Use lab equipment safely"})
        self.assertEqual(outcomes["Construct a scientific argument"]["item_ids"], [self.item(model, "Essay")["id"]])
        self.assertEqual(rubric["criteria"][0]["outcome_id"], outcomes["Construct a scientific argument"]["id"])

    def test_modules_requirements_and_links(self):
        model = self.slim()
        week1, week2 = model["modules"]
        titles = {i["id"]: i["title"] for i in model["items"]}
        self.assertEqual([titles[i] for i in week1["items"]], ["Welcome", "Essay", "Cell quiz", "NCBI"])
        self.assertTrue(week1["sequential"])
        self.assertEqual([(titles[r["item_id"]], r["type"], r["min_score"]) for r in week1["requirements"]],
                         [("Welcome", "must_view", None), ("Cell quiz", "min_score", 7.0)])
        self.assertEqual(week2["prerequisites"], [week1["id"]])
        link = self.item(model, "NCBI")
        self.assertEqual((link["kind"], link["url"], link["module_id"]),
                         ("external_link", "https://www.ncbi.nlm.nih.gov/", week1["id"]))
        self.assertEqual(self.item(model, "Lab")["module_id"], week2["id"])

    def test_media_inventory_and_references(self):
        model = self.slim()
        media = {m["path"]: m for m in model["media_inventory"]}
        welcome = self.item(model, "Welcome")["id"]
        self.assertEqual(media["web_resources/lecture one.mp4"]["bytes"], 4_000_000_000)
        self.assertEqual(media["web_resources/lecture one.mp4"]["referenced_by"], [welcome])
        self.assertEqual(media["web_resources/reading.pdf"]["referenced_by"], [welcome])
        # Media omitted by design is not reported as missing.
        self.assertFalse([w for w in model["provenance"]["warnings"] if "Missing archive reference" in w])
        self.assertFalse([w for w in model["provenance"]["warnings"] if "no usable bundled files" in w])
        self.assertEqual(model["provenance"]["counts"]["assignments"], 3)
        self.assertEqual(model["provenance"]["counts"]["media"], 2)
        self.assertEqual(model["provenance"]["source"], "export")

    def test_full_export_inventories_media_without_reading_it(self):
        entries = text_entries()
        entries["web_resources/lecture one.mp4"] = b"\0" * 2048
        entries["web_resources/reading.pdf"] = b"%PDF-1.4 synthetic"
        model = self.build(entries)
        paths = {m["path"]: m["bytes"] for m in model["media_inventory"]}
        self.assertEqual(paths["web_resources/lecture one.mp4"], 2048)

    def test_included_material(self):
        model = self.slim({"materials/notes.txt": "Students struggle with osmosis."})
        self.assertEqual(model["materials"][0]["name"], "notes.txt")
        self.assertIn("osmosis", model["materials"][0]["text"])

    def test_rejections(self):
        with self.assertRaises(slim_import.SlimImportError):
            self.build({"readme.txt": "no manifest"})
        with self.assertRaisesRegex(slim_import.SlimImportError, "media inventory"):
            entries = text_entries()
            entries["media_inventory.json"] = "{not json"
            self.build(entries)
        self.path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(slim_import.SlimImportError, "readable ZIP"):
            slim_import.import_slim(self.path)

    # Real-world cases found by running Canvas's own export fixtures (Task 12).
    def test_empty_metadata_file_and_mislabelled_encoding(self):
        entries = text_entries()
        entries["course_settings/media_tracks.xml"] = ""
        entries["imsmanifest.xml"] = '<?xml version="1.0" encoding="UTF-16" ?>\n' + MANIFEST
        model = self.build(entries)
        self.assertEqual(model["provenance"]["counts"]["assignments"], 3)

    def test_backslash_references(self):
        entries = text_entries()
        entries["imsmanifest.xml"] = MANIFEST.replace('href="gessay/essay.html"', 'href="gessay\\essay.html"')
        model = self.build(entries)
        self.assertIn("Essay", [i["title"] for i in model["items"]])

    def test_canvas_native_quiz_questions_and_banks(self):
        native = QTI.replace("Which is a cell part?", "Native: which is a cell part?").replace(
            "</section>", '<item ident="q3" title="Extra"><itemmetadata><qtimetadata><qtimetadatafield>'
            "<fieldlabel>question_type</fieldlabel><fieldentry>true_false_question</fieldentry>"
            "</qtimetadatafield></qtimetadata></itemmetadata><presentation><material><mattext>Cells divide.</mattext>"
            "</material></presentation></item></section>")
        bank = ('<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2"><objectbank ident="bank1">'
                "<qtimetadata><qtimetadatafield><fieldlabel>bank_title</fieldlabel><fieldentry>Chapter 3 pool</fieldentry>"
                "</qtimetadatafield></qtimetadata>" + QTI.split("<section ident=\"s\">")[1].split("</section>")[0]
                + "</objectbank></questestinterop>")
        unfiled = ('<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2"><item ident="u1" title="Q">'
                   "<presentation><material><mattext>Unfiled question?</mattext></material></presentation></item>"
                   "</questestinterop>")
        model = self.build({**text_entries(), "non_cc_assessments/gquiz.xml.qti": native,
                            "non_cc_assessments/bank1.xml.qti": bank, "non_cc_assessments/loose.xml.qti": unfiled})
        quiz = next(q for i, q in model["quizzes"].items())
        texts = [q["text"] for q in quiz["questions"]]
        self.assertEqual(len(texts), 3, "native questions replace, not duplicate, the CC copy")
        self.assertIn("Native: which is a cell part?", texts)
        banks = {b["title"]: b for b in model["question_banks"]}
        self.assertEqual(len(banks["Chapter 3 pool"]["questions"]), 2)
        self.assertEqual(banks["Unfiled questions"]["questions"][0]["text"], "Unfiled question?")
        self.assertEqual(model["provenance"]["counts"]["question_banks"], 2)

    def test_minimal_course_without_settings(self):
        model = self.build({
            "imsmanifest.xml": MANIFEST.replace("Biology 101", "Tiny"),
            "wiki_content/welcome.html": "<html><head><title>Welcome</title></head><body><p>Hi there.</p></body></html>",
        })
        self.assertEqual(model["course"]["title"], "Tiny")
        self.assertEqual([i["title"] for i in model["items"]], ["Welcome"])
        self.assertEqual(model["assignment_groups"], [])


if __name__ == "__main__":
    unittest.main()
