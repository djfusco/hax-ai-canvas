import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import consultation as c
from canvas_api_import import import_api_bundle
from course_map import build_course_map

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


def response(**overrides):
    value = {"reply": "Thanks. Tell me about your students.", "topic_updates": [], "proposed_changes": [],
             "next_topic": ""}
    value.update(overrides)
    return value


def update(topic="purpose", status="in_progress", facts=(), goals=(), questions=(), resolved=()):
    return {"topic": topic, "status": status, "facts": list(facts), "goals": list(goals),
            "open_questions": list(questions), "resolved_questions": list(resolved)}


class Client:
    def __init__(self, *responses, limit=None):
        self.responses = list(responses)
        self.limit = limit
        self.calls = []

    def preflight_structured(self, system, prompt, schema, max_tokens=4096):
        if self.limit is not None and len(prompt) > self.limit:
            raise ValueError("too large")

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value) if isinstance(value, dict) else value


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.map = build_course_map(self.model)
        self.state = c.new_state()
        self.items = {i["title"]: i["id"] for i in self.model["items"]}

    def turn(self, client, message="Our students are mostly first-years.", state=None, history=()):
        return c.respond(state or self.state, list(history), self.model, self.map, client, message=message)

    def test_opening_uses_snapshot_and_asks(self):
        client = Client(response(reply="Here's what I see: two modules, weighted grading...",
                                 topic_updates=[update(facts=[{"text": "Grade is weighted: Essays 40%",
                                                               "evidence": ["course-map"]}])]))
        reply, state = c.respond(self.state, [], self.model, self.map, client, mode="opening")
        prompt = client.calls[0]["prompt"]
        self.assertIn("first session", prompt)
        self.assertIn("weighted groups (Essays 40%", prompt)
        self.assertIn("Lab [lab]: 30% of grade", prompt)
        self.assertIn("backward design", client.calls[0]["system"])
        self.assertEqual(state["topics"]["purpose"]["status"], "in_progress")
        self.assertEqual(state["topics"]["purpose"]["facts"][0]["evidence"], ["course-map"])
        self.assertEqual(state["turns"], 1)
        self.assertEqual(self.state["turns"], 0, "input state is not mutated")

    def test_merge_dedupes_and_resolves(self):
        first = response(topic_updates=[update(facts=[{"text": "Mostly first-year students", "evidence": ["instructor"]}],
                                               goals=["More active learning"], questions=["How big is the class?"])])
        second = response(topic_updates=[update(status="covered",
                                                facts=[{"text": "mostly first-year students.", "evidence": []},
                                                       {"text": "About 120 students", "evidence": ["instructor"]}],
                                                resolved=["How big is the class?"])], next_topic="outcomes")
        _, state = self.turn(Client(first))
        _, state = self.turn(Client(second), state=state)
        purpose = state["topics"]["purpose"]
        self.assertEqual([f["text"] for f in purpose["facts"]], ["Mostly first-year students", "About 120 students"])
        self.assertEqual(purpose["open_questions"], [])
        self.assertEqual(purpose["status"], "covered")
        self.assertEqual(state["current_topic"], "outcomes")
        self.assertEqual(state["topics"]["outcomes"]["status"], "in_progress")
        # A covered topic is not reopened by a stray in_progress update.
        _, state = self.turn(Client(response(topic_updates=[update(status="in_progress")])), state=state)
        self.assertEqual(state["topics"]["purpose"]["status"], "covered")

    def test_proposals_are_validated_and_never_accepted(self):
        essay = self.items["Essay"]
        good = {"topic": "assessment", "title": "Add a draft checkpoint to the essay",
                "change": "Introduce a graded outline in week 1.", "rationale": "Spreads stakes; feedback loop.",
                "principle": "Formative assessment", "evidence": [essay], "affected_items": [essay],
                "effort": "small", "ai_dimension": "resilient_assessment"}
        _, state = self.turn(Client(response(proposed_changes=[good, dict(good)])))
        self.assertEqual(len(state["proposals"]), 1, "duplicate titles are skipped")
        proposal = state["proposals"][0]
        self.assertEqual((proposal["id"], proposal["status"]), ("p-1", "proposed"))
        bad = dict(good, title="Other", affected_items=["invented-item"])
        with self.assertRaisesRegex(c.ConsultationError, "does not exist"):
            self.turn(Client(response(proposed_changes=[bad])))
        with self.assertRaisesRegex(c.ConsultationError, "invalid effort"):
            self.turn(Client(response(proposed_changes=[dict(good, title="X", effort="huge")])))

    def test_rejects_invalid_responses(self):
        cases = [("{not json", "not valid JSON"), (response(reply=""), "reply"),
                 (response(topic_updates=[update(topic="vibes")]), "unknown consultation topic"),
                 (response(topic_updates=[update(facts=[{"text": "x", "evidence": ["nope"]}])]), "does not exist"),
                 (response(next_topic="dessert"), "unknown next topic")]
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(c.ConsultationError, message):
                    self.turn(Client(raw))

    def test_provider_failures_are_safe(self):
        with self.assertRaisesRegex(c.ConsultationError, "provider request failed"):
            self.turn(Client(RuntimeError("secret-key-sk-123 leaked")))
        with self.assertRaisesRegex(c.ConsultationError, "Choose an AI provider"):
            self.turn(None)
        with self.assertRaisesRegex(c.ConsultationError, "1-6000"):
            self.turn(Client(response()), message="")

    def test_relevant_items_and_history_grounding(self):
        client = Client(response())
        history = [{"role": "user", "content": "Earlier I said the lab is too long."},
                   {"role": "assistant", "content": "Noted."}]
        state = c.choose_topic(self.state, "assessment")
        self.turn(client, "What do you think of the Essay rubric?", state=state, history=history)
        prompt = client.calls[0]["prompt"]
        self.assertIn(f"[{self.items['Essay']}] Essay", prompt)
        self.assertIn("rubric: Thesis and evidence", prompt)
        self.assertIn("INSTRUCTOR: Earlier I said the lab is too long.", prompt)
        self.assertIn("Current topic: assessment", prompt)

    def test_prompt_shrinks_to_fit_and_fails_clearly(self):
        model = self.model
        for n in range(300):
            model["items"].append({"id": f"p{n}", "kind": "page", "title": f"Exam review {n}",
                                   "html": "<p>" + "exam rubric feedback " * 300 + "</p>",
                                   "module_id": None, "published": True})
        big_map = build_course_map(model)
        client = Client(response(), limit=14000)
        c.respond(self.state, [], model, big_map, client, message="Let's talk about exams.")
        self.assertLessEqual(len(client.calls[0]["prompt"]), 14000)
        with self.assertRaisesRegex(c.ConsultationError, "too small"):
            c.respond(self.state, [], model, big_map, Client(response(), limit=500), message="hi")

    def test_directness_topic_choice_and_resume(self):
        state = c.set_directness(self.state, "direct")
        client = Client(response())
        self.turn(client, state=state)
        self.assertIn("ADVICE STYLE: direct", client.calls[0]["prompt"])
        with self.assertRaises(c.ConsultationError):
            c.set_directness(state, "brutal")
        state = c.choose_topic(state, "resources")
        self.assertEqual(state["current_topic"], "resources")
        state["topics"]["purpose"]["status"] = "covered"
        state["topics"]["resources"]["open_questions"] = ["Is the textbook required?"]
        state["proposals"] = [{"id": "p-1", "title": "x", "status": "proposed"}]
        summary = c.resume_summary(state)
        self.assertIn("Covered so far: Purpose and learners", summary)
        self.assertIn("**Resources (texts and technology)**", summary)
        self.assertIn("Is the textbook required?", summary)
        self.assertIn("1 awaiting your decision", summary)
        self.assertIn("Still to discuss: Outcomes", summary)


if __name__ == "__main__":
    unittest.main()
