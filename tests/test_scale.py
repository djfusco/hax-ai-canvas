"""A course at the 500-item limit stays fast and fits small and large models."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import consultation
from course_map import build_course_map
from model_capabilities import plan_request, resolve_capabilities
from privacy_strip import strip_course
from slim_import import import_slim
from test_slim_import import NS, resource

PAGES, ASSIGNMENTS, QUIZZES, MODULES = 400, 80, 20, 15


def big_export(path):
    resources, files, items_by_module = [], {}, [[] for _ in range(MODULES)]
    criteria = "".join(f"<criterion><points>5</points><description>Criterion {c}</description><ratings>"
                       f"<rating><description>Full</description><points>5</points></rating></ratings></criterion>"
                       for c in range(4))
    rubrics = "".join(f'<rubric identifier="r{n}"><title>Rubric {n}</title><points_possible>20</points_possible>'
                      f"<criteria>{criteria}</criteria></rubric>" for n in range(ASSIGNMENTS))
    groups = "".join(f'<assignmentGroup identifier="g{n}"><title>Group {n}</title><group_weight>25</group_weight></assignmentGroup>'
                     for n in range(4))
    for n in range(PAGES):
        files[f"wiki_content/p{n}.html"] = (f"<html><head><title>Week {n % MODULES} reading {n}</title></head><body>"
                                             + "<p>Discussion of cell biology, evidence and lab safety. </p>" * 30
                                             + "</body></html>")
        resources.append(resource(f"p{n}", "webcontent", f"wiki_content/p{n}.html", [f"wiki_content/p{n}.html"]))
        items_by_module[n % MODULES].append((f"p{n}", "WikiPage"))
    for n in range(ASSIGNMENTS):
        files[f"a{n}/a.html"] = f"<html><head><title>Assignment {n}</title></head><body><p>Write a lab report {n}.</p></body></html>"
        files[f"a{n}/assignment_settings.xml"] = (
            f'<assignment {NS} identifier="a{n}"><title>Assignment {n}</title><points_possible>20</points_possible>'
            f"<due_at>2026-{9 + n // 30:02d}-{1 + n % 28:02d}T23:59:00Z</due_at>"
            f"<assignment_group_identifierref>g{n % 4}</assignment_group_identifierref>"
            f"<rubric_identifierref>r{n}</rubric_identifierref></assignment>")
        resources.append(resource(f"a{n}", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
                                  f"a{n}/a.html", [f"a{n}/a.html", f"a{n}/assignment_settings.xml"]))
        items_by_module[n % MODULES].append((f"a{n}", "Assignment"))
    for n in range(QUIZZES):
        questions = "".join(
            f'<item ident="q{n}-{k}" title="Q"><itemmetadata><qtimetadata><qtimetadatafield><fieldlabel>question_type'
            f"</fieldlabel><fieldentry>multiple_choice_question</fieldentry></qtimetadatafield></qtimetadata>"
            f"</itemmetadata><presentation><material><mattext>Question {k} about cells?</mattext></material>"
            f"</presentation></item>" for k in range(10))
        files[f"q{n}/assessment_qti.xml"] = (f'<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2">'
                                             f'<assessment ident="q{n}" title="Quiz {n}"><section ident="s">{questions}'
                                             f"</section></assessment></questestinterop>")
        files[f"q{n}/assessment_meta.xml"] = f'<quiz {NS} identifier="q{n}"><title>Quiz {n}</title></quiz>'
        resources.append(resource(f"q{n}", "imsqti_xmlv1p2/imscc_xmlv1p1/assessment", "",
                                  [f"q{n}/assessment_qti.xml", f"q{n}/assessment_meta.xml"]))
    modules = "".join(
        f'<module identifier="m{m}"><title>Week {m}</title><position>{m}</position><items>'
        + "".join(f'<item identifier="mi{ref}"><content_type>{kind}</content_type><title>{ref}</title>'
                  f"<identifierref>{ref}</identifierref></item>" for ref, kind in items)
        + "</items></module>" for m, items in enumerate(items_by_module))
    files.update({
        "imsmanifest.xml": ('<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" identifier="big">'
                            "<organizations/><resources>" + "".join(resources) + "</resources></manifest>"),
        "course_settings/course_settings.xml": (f"<course {NS}><title>Large course</title>"
                                                "<group_weighting_scheme>percent</group_weighting_scheme></course>"),
        "course_settings/rubrics.xml": f"<rubrics {NS}>{rubrics}</rubrics>",
        "course_settings/assignment_groups.xml": f"<assignmentGroups {NS}>{groups}</assignmentGroups>",
        "course_settings/module_meta.xml": f"<modules {NS}>{modules}</modules>",
        "media_inventory.json": json.dumps([{"path": f"web_resources/v{n}.mp4", "bytes": 300_000_000, "mime": "video/mp4"}
                                            for n in range(30)]),
    })
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


class Client:
    """Preflight with the real request planner for a given capability profile."""
    def __init__(self, capabilities):
        self.capabilities, self.prompts = capabilities, []

    def preflight_structured(self, system, prompt, schema, max_tokens=4096):
        encoded = json.dumps(schema, separators=(",", ":"))
        return plan_request("openai", self.capabilities["model"], system,
                            prompt + "\nRESPONSE JSON SCHEMA:\n" + encoded, max_tokens, self.capabilities)

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        self.prompts.append(prompt)
        return json.dumps({"reply": "Here is what I see.", "topic_updates": [], "proposed_changes": [], "next_topic": ""})


class ScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "big.zip"
        big_export(path)
        started = time.monotonic()
        model = import_slim(path)
        cls.model, _ = strip_course(model)
        cls.course_map = build_course_map(cls.model)
        cls.seconds = time.monotonic() - started
        cls.package_bytes = path.stat().st_size

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_import_completes_quickly_with_everything(self):
        counts = self.model["provenance"]["counts"]
        self.assertEqual((counts["pages"], counts["assignments"], counts["quizzes"]), (PAGES, ASSIGNMENTS, QUIZZES))
        self.assertEqual(counts["rubrics"], ASSIGNMENTS)
        self.assertEqual(sum(len(q["questions"]) for q in self.model["quizzes"].values()), QUIZZES * 10)
        self.assertEqual(len(self.course_map["assessments"]), ASSIGNMENTS)
        self.assertAlmostEqual(sum(a["grade_share"] for a in self.course_map["assessments"]), 100, places=0)
        self.assertLess(self.seconds, 30, f"import + strip + map took {self.seconds:.1f}s")
        self.assertLess(self.package_bytes, 3.5 * 1024 * 1024, "fits in one upload piece")

    def test_consultation_fits_a_small_and_a_large_model(self):
        for model_id, label in (("small-unverified-model", "32k fallback"), ("gpt-4.1", "1M context")):
            with self.subTest(model=label):
                client = Client(resolve_capabilities("openai", model_id))
                started = time.monotonic()
                consultation.respond(consultation.new_state(), [], self.model, self.course_map, client,
                                     message="How should I rebalance the assignments and their rubrics?")
                self.assertLess(time.monotonic() - started, 10)
                self.assertIn("RELEVANT COURSE ITEMS", client.prompts[0])


if __name__ == "__main__":
    unittest.main()
