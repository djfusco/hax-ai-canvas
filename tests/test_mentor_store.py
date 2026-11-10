"""Workspace store contract (SQLite); the Postgres store runs the same tests."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]

from canvas_api_import import import_api_bundle
from mentor_store import MentorStore, WorkspaceConflict, WorkspaceNotFound

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())
PRIVACY = {"counts": {}, "total": 0, "notes": []}


class MentorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mentor.sqlite3"
        self.store = MentorStore(self.path)
        self.owner = "browser-one"
        self.workspace = self.store.create(self.owner, "Evidence and inquiry")
        self.id = self.workspace["id"]

    def get(self):
        return self.store.get(self.owner, self.id)

    def other_instance(self):
        return MentorStore(self.path)

    def with_course(self):
        model = import_api_bundle(copy.deepcopy(BUNDLE))
        return self.store.save_course_model(self.owner, self.id, self.get()["revision"], model, PRIVACY)

    def test_initial_shape(self):
        state = self.get()
        self.assertEqual((state["revision"], state["source_type"], state["messages"]), (1, "blank", []))
        self.assertIsNone(state["active_job"])
        self.assertNotIn("owner", state)
        self.assertEqual(state["created_at"], state["updated_at"])
        json.dumps(state, allow_nan=False)

    def test_owner_is_scoped_for_every_workspace_operation(self):
        calls = (
            lambda: self.store.get("other", self.id),
            lambda: self.store.get_course_model("other", self.id),
            lambda: self.store.add_message("other", self.id, "user", "Text"),
            lambda: self.store.begin_consult("other", self.id),
            lambda: self.store.finish_consult("other", self.id, {}, []),
            lambda: self.store.save_consultant_fields("other", self.id, plan={}),
            lambda: self.store.set_share("other", self.id, True),
            lambda: self.store.put_intake_chunk("other", self.id, "a" * 32, 0, 1, b"x"),
            lambda: self.store.take_intake_upload("other", self.id, "a" * 32),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(WorkspaceNotFound):
                call()
        self.assertEqual(self.store.list_workspaces("other"), [])
        self.assertEqual(self.get(), self.workspace)

    def test_missing_and_nonowned_workspaces_have_same_error(self):
        messages = []
        for owner, id in (("other", self.id), (self.owner, "missing")):
            with self.assertRaises(WorkspaceNotFound) as error:
                self.store.get(owner, id)
            messages.append(str(error.exception))
        self.assertEqual(messages[0], messages[1])

    def test_sql_metacharacters_are_not_executed(self):
        hostile_owner = "' OR 1=1; DROP TABLE mentor_workspaces; --"
        hostile = self.store.create(hostile_owner, "Literal string")
        self.assertEqual(self.store.get(hostile_owner, hostile["id"]), hostile)
        self.assertEqual(self.store.list_workspaces(self.owner), [self.workspace])
        with self.assertRaises(WorkspaceNotFound):
            self.store.get(self.owner, "' OR 1=1 --")

    def test_course_model_replaces_and_bumps_revision(self):
        state = self.with_course()
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["source"]["course_model"]["title"], self.store.get_course_model(
            self.owner, self.id)["course"]["title"])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_course_model(self.owner, self.id, 1, self.store.get_course_model(self.owner, self.id),
                                         PRIVACY)

    def test_consult_lease_is_exclusive_and_released(self):
        with self.assertRaises(WorkspaceConflict):
            self.store.begin_consult(self.owner, self.id)  # no course yet
        self.with_course()
        self.store.begin_consult(self.owner, self.id)
        with self.assertRaises(WorkspaceConflict):
            self.store.begin_consult(self.owner, self.id)
        with self.assertRaises(WorkspaceConflict):
            self.store.save_consultant_fields(self.owner, self.id, plan={"vision": "x"})
        state = self.store.finish_consult(self.owner, self.id, {"topics": {}},
                                          [("user", "Hello"), ("assistant", "Welcome")])
        self.assertEqual([m["kind"] for m in state["messages"]], ["consult", "consult"])
        self.store.begin_consult(self.owner, self.id)
        state = self.store.save_consultant_fields(self.owner, self.id, release=True, plan={"vision": "x"})
        self.assertIsNone(state["consult_busy"])
        with self.assertRaises(ValueError):
            self.store.save_consultant_fields(self.owner, self.id, stage="built")

    def test_simultaneous_consult_turns_admit_only_one(self):
        self.with_course()
        barrier = Barrier(2)

        def start(store):
            barrier.wait(timeout=5)
            try:
                return store.begin_consult(self.owner, self.id)
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, (self.store, self.other_instance())))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_share_versions_change_on_every_toggle(self):
        self.assertEqual(self.store.set_share(self.owner, self.id, True)["share"]["version"], 1)
        self.assertEqual(self.store.set_share(self.owner, self.id, True)["share"]["version"], 1)
        state = self.store.set_share(self.owner, self.id, False)
        self.assertEqual((state["share"]["version"], state["share"]["enabled"]), (2, False))
        self.assertEqual(self.store.set_share(self.owner, self.id, True)["share"]["version"], 3)

    def test_intake_chunks_assemble_once(self):
        upload = "b" * 32
        self.store.put_intake_chunk(self.owner, self.id, upload, 1, 2, b"world")
        self.store.put_intake_chunk(self.owner, self.id, upload, 0, 2, b"hello ")
        self.assertEqual(self.store.take_intake_upload(self.owner, self.id, upload), b"hello world")
        with self.assertRaises(ValueError):
            self.store.take_intake_upload(self.owner, self.id, upload)
        self.store.put_intake_chunk(self.owner, self.id, "c" * 32, 0, 2, b"half")
        with self.assertRaises(ValueError):
            self.store.take_intake_upload(self.owner, self.id, "c" * 32)
        for bad in (("bad", 0, 1, b"x"), ("d" * 32, 0, 99, b"x"), ("d" * 32, 2, 2, b"x"), ("d" * 32, 0, 1, b"")):
            with self.subTest(bad=bad[:3]), self.assertRaises(ValueError):
                self.store.put_intake_chunk(self.owner, self.id, *bad)

    def test_returned_and_input_dicts_are_detached_from_database(self):
        plan = {"vision": "Original", "outcomes": ["Explain"]}
        state = self.store.save_consultant_fields(self.owner, self.id, plan=plan)
        plan["vision"] = "changed"
        state["plan"]["outcomes"].append("changed")
        self.assertEqual(self.get()["plan"], {"vision": "Original", "outcomes": ["Explain"]})

    def test_multiple_instances_share_durable_state(self):
        second = self.other_instance()
        updated = second.add_message(self.owner, self.id, "assistant", "Hello.", "consult")
        self.assertEqual(self.get(), updated)
        self.assertEqual(second.list_workspaces(self.owner), [updated])

    def test_failed_database_write_rolls_back_transition(self):
        before = self.get()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_fixture_update BEFORE UPDATE ON mentor_workspaces "
                "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.set_share(self.owner, self.id, True)
        self.assertEqual(self.get(), before)

    def test_json_and_credentials_rejected_without_persistence_or_payload_errors(self):
        cyclic = {}
        cyclic["cycle"] = cyclic
        values = (
            {"nested": [{"API_KEY": "private fixture"}]},
            {"Authorization": "private fixture"},
            {"canvas_token": "private fixture"},
            {"provider_credentials": {"password": "private fixture"}},
            {"not_json": object()},
            {"bad_number": float("nan")},
            {1: "not a string key"},
            cyclic,
        )
        for value in values:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as error:
                self.store.save_consultant_fields(self.owner, self.id, plan=value)
            self.assertNotIn("private fixture", str(error.exception))
        self.assertEqual(self.get(), self.workspace)
        with sqlite3.connect(self.path) as conn:
            self.assertNotIn("private fixture", conn.execute(
                "SELECT state FROM mentor_workspaces").fetchone()[0])

    def test_invalid_values_do_not_mutate_state(self):
        calls = (
            lambda: self.store.create("", "Title"),
            lambda: self.store.create(self.owner, ""),
            lambda: self.store.create(self.owner, "Title", "unknown"),
            lambda: self.store.add_message(self.owner, self.id, "unknown", "Text"),
            lambda: self.store.add_message(self.owner, self.id, "user", ""),
            lambda: self.store.add_message(self.owner, self.id, "user", "Text", "unknown"),
            lambda: self.store.finish_consult(self.owner, self.id, {}, [("system", "x")]),
            lambda: MentorStore(":memory:"),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.get(), self.workspace)


if __name__ == "__main__":
    unittest.main()
