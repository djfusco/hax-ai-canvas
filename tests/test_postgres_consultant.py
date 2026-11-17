"""Course Consultant storage against a real PostgreSQL server.

Runs only when CC_TEST_POSTGRES_DSN points at a disposable database, e.g.
postgresql://postgres@127.0.0.1:55432/postgres. Every table it touches is
dropped first.
"""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]

DSN = os.environ.get("CC_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "Set CC_TEST_POSTGRES_DSN to a disposable PostgreSQL database")
class PostgresConsultantTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from hosted_data import PostgresMentorStore
        with psycopg.connect(DSN) as conn:
            for table in ("mentor_workspaces", "mentor_course_models", "mentor_intake_chunks", "mentor_course_files"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        # Local test server has no TLS; production always forces sslmode=require.
        self.store = PostgresMentorStore(DSN, connect=lambda dsn, **_: psycopg.connect(dsn))

    def test_course_model_chunks_and_consultation(self):
        from canvas_api_import import import_api_bundle
        import consultation
        from mentor_store import WorkspaceConflict
        model = import_api_bundle(json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text()))
        workspace = self.store.create("alice", "Bio")
        saved = self.store.save_course_model("alice", workspace["id"], workspace["revision"], model,
                                             {"counts": {}, "total": 0, "notes": []})
        self.assertEqual(saved["source"]["course_model"]["title"], "Biology 101: Cells")
        self.assertEqual(self.store.get_course_model("alice", workspace["id"]), model)
        self.assertIsNone(self.store.get_course_model("alice", self.store.create("alice", "Empty")["id"]))
        # Replacing the course upserts.
        replaced = copy.deepcopy(model)
        replaced["course"]["title"] = "Biology 102"
        self.store.save_course_model("alice", workspace["id"], saved["revision"], replaced,
                                     {"counts": {}, "total": 0, "notes": []})
        self.assertEqual(self.store.get_course_model("alice", workspace["id"])["course"]["title"], "Biology 102")

        # Chunked intake with binary data, including NUL bytes.
        pieces = [bytes(range(256)) * 10, b"\x00\x01\x02tail"]
        for index, piece in reversed(list(enumerate(pieces))):
            self.store.put_intake_chunk("alice", workspace["id"], "f" * 32, index, 2, piece)
        self.assertEqual(self.store.take_intake_upload("alice", workspace["id"], "f" * 32), b"".join(pieces))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.store.take_intake_upload("alice", workspace["id"], "f" * 32)

        # Consultation lease and state.
        self.store.begin_consult("alice", workspace["id"])
        with self.assertRaises(WorkspaceConflict):
            self.store.begin_consult("alice", workspace["id"])
        state = consultation.new_state()
        state["turns"] = 1
        done = self.store.finish_consult("alice", workspace["id"], state, [("user", "Hi"), ("assistant", "Hello")])
        self.assertIsNone(done["consult_busy"])
        self.assertEqual([m["kind"] for m in done["messages"][-2:]], ["consult", "consult"])
        self.assertEqual(self.store.get("alice", workspace["id"])["consultation"]["turns"], 1)

    def test_pictures_replacement_and_deletion(self):
        """What was added in November 2026, on the real database: pictures, replacing, deleting."""
        from canvas_api_import import import_api_bundle
        from mentor_store import WorkspaceConflict, WorkspaceNotFound
        import consultation
        model = import_api_bundle(json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text()))
        privacy = {"counts": {}, "total": 0, "notes": []}
        picture = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40  # binary, with NUL bytes
        files = [{"name": "cell.png", "mime": "image/png", "data": picture},
                 {"name": "leaf.jpg", "mime": "image/jpeg", "data": b"\xff\xd8\xff\x00tail"}]
        workspace = self.store.create("alice", "Bio")
        saved = self.store.save_course_model("alice", workspace["id"], workspace["revision"], model, privacy, files=files)
        self.assertEqual(self.store.course_file("alice", workspace["id"], "cell.png"), ("image/png", picture))
        self.assertEqual([(name, mime) for name, mime, _ in self.store.course_files("alice", workspace["id"])],
                         [("cell.png", "image/png"), ("leaf.jpg", "image/jpeg")])
        self.assertIsNone(self.store.course_file("alice", workspace["id"], "missing.png"))
        self.assertIsNone(self.store.course_file("mallory", workspace["id"], "cell.png"), "another account sees nothing")

        # Work made for the course, then the course is replaced without pictures.
        state = consultation.new_state()
        state["turns"] = 2
        state["proposals"] = [{"id": "p-1", "status": "accepted", "title": "t", "change": "c", "rationale": "r",
                               "evidence": ["page-11", "course-map"], "affected_items": ["page-11"]}]
        self.store.save_consultant_fields("alice", workspace["id"], consultation=state, plan={"vision": "Keep me"},
                                          documents=[{"id": "doc-1", "kind": "page", "item_id": "page-11"}],
                                          build={"blueprint": {"weeks": []}, "pages": []},
                                          design={"understandings": ["Students will understand that x"],
                                                  "evidence": [{"item_id": "assignment-201"}, {"item_id": "gone"}]})
        replacement = copy.deepcopy(model)
        replacement["items"] = [i for i in replacement["items"] if i["id"] != "page-11"]
        for module in replacement["modules"]:
            module["items"] = [i for i in module["items"] if i != "page-11"]
            module["requirements"] = [r for r in module.get("requirements", []) if r["item_id"] != "page-11"]
        for outcome in replacement["outcomes"]:
            outcome["item_ids"] = [i for i in outcome["item_ids"] if i != "page-11"]
        for media in replacement["media_inventory"]:
            media["referenced_by"] = [i for i in media["referenced_by"] if i != "page-11"]
        current = self.store.get("alice", workspace["id"])["revision"]
        after = self.store.save_course_model("alice", workspace["id"], current, replacement, privacy)
        self.assertEqual(self.store.course_files("alice", workspace["id"]), [], "the old course's pictures are gone")
        self.assertEqual((after["build"], after["documents"], after["plan"]), (None, [], {"vision": "Keep me"}))
        self.assertEqual(after["consultation"]["proposals"][0]["affected_items"], [])
        self.assertEqual(after["design"]["evidence"], [{"item_id": "assignment-201"}])

        # A course cannot be replaced while the AI is working on it.
        self.store.begin_consult("alice", workspace["id"])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_course_model("alice", workspace["id"], after["revision"], model, privacy)
        self.store.release_consult("alice", workspace["id"])

        # Counting and deleting.
        other = self.store.create("alice", "Chem")
        self.store.save_course_model("alice", other["id"], other["revision"], model, privacy, files=files)
        bob = self.store.create("bob", "Physics")
        self.assertEqual((self.store.count("alice"), self.store.count("bob")), (2, 1))
        with self.assertRaises(WorkspaceNotFound):
            self.store.delete("mallory", other["id"])
        self.store.delete("alice", other["id"])
        with self.assertRaises(WorkspaceNotFound):
            self.store.get("alice", other["id"])
        self.assertIsNone(self.store.course_file("alice", other["id"], "cell.png"))
        self.assertEqual(self.store.delete_owner("alice"), 1)
        self.assertEqual((self.store.count("alice"), self.store.count("bob")), (0, 1))
        self.assertEqual(self.store.get("bob", bob["id"])["title"], "Physics")

    def test_owner_isolation(self):
        from mentor_store import WorkspaceNotFound
        from canvas_api_import import import_api_bundle
        model = import_api_bundle(json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text()))
        workspace = self.store.create("alice", "Bio")
        self.store.save_course_model("alice", workspace["id"], workspace["revision"], model,
                                     {"counts": {}, "total": 0, "notes": []})
        with self.assertRaises(WorkspaceNotFound):
            self.store.get_course_model("mallory", workspace["id"])
        with self.assertRaises(WorkspaceNotFound):
            self.store.put_intake_chunk("mallory", workspace["id"], "a" * 32, 0, 1, b"x")


if __name__ == "__main__":
    unittest.main()
