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
            for table in ("mentor_workspaces", "mentor_course_models", "mentor_intake_chunks"):
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
