"""Offline mentor state tests using only temporary SQLite fixtures."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from mentor_store import MentorStore, WorkspaceConflict, WorkspaceNotFound


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

    def direction(self):
        state = self.get()
        state = self.store.save_brief(
            self.owner, self.id, "Teach evidence evaluation.", state["revision"])
        return self.store.approve_direction(self.owner, self.id, state["revision"])

    def analyzed(self, review=True):
        state = self.direction()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        state = self.store.finish_analysis(
            self.owner, self.id, state["active_job"]["id"],
            {"title": "Evidence", "pages": [{"title": "Compare sources"}]})
        return self.reviewed(state) if review else state

    def reviewed(self, state=None):
        state = state or self.get()
        state = self.store.begin_review(self.owner, self.id, state["draft_revision"])
        return self.store.finish_review(self.owner, self.id, state["active_job"]["id"], None)

    def built(self):
        state = self.analyzed()
        state = self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.begin_build(self.owner, self.id, state["draft_revision"])
        return self.store.finish_build(
            self.owner, self.id, state["active_job"]["id"], {"path": "exports/course"})

    def test_initial_public_shape(self):
        state = self.get()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["draft_revision"], 0)
        self.assertEqual(state["brief"], "")
        self.assertEqual(state["stage"], "brief")
        self.assertEqual(state["source_type"], "blank")
        self.assertEqual(state["materials"], [])
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["jobs"], [])
        for key in ("direction_revision", "build_revision", "reviewed_draft_revision",
                    "draft", "active_job", "artifact"):
            self.assertIsNone(state[key])
        self.assertNotIn("owner", state)
        self.assertEqual(state["created_at"], state["updated_at"])
        json.dumps(state, allow_nan=False)

    def test_owner_is_scoped_for_every_workspace_operation(self):
        calls = (
            lambda: self.store.get("other", self.id),
            lambda: self.store.set_source("other", self.id, {}, 1),
            lambda: self.store.save_brief("other", self.id, "Text", 1),
            lambda: self.store.add_material("other", self.id, {"text": "Text"}, 1),
            lambda: self.store.remove_material("other", self.id, "material", 1),
            lambda: self.store.add_message("other", self.id, "user", "Text"),
            lambda: self.store.approve_direction("other", self.id, 1),
            lambda: self.store.begin_analysis("other", self.id, 1),
            lambda: self.store.finish_analysis("other", self.id, "job", {"pages": []}),
            lambda: self.store.fail_job("other", self.id, "job", "Error"),
            lambda: self.store.save_draft("other", self.id, {"pages": []}, 0),
            lambda: self.store.approve_build("other", self.id, 0),
            lambda: self.store.begin_build("other", self.id, 0),
            lambda: self.store.finish_build("other", self.id, "job", {"path": "course"}),
            lambda: self.store.begin_review("other", self.id, 0),
            lambda: self.store.finish_review("other", self.id, "job", None),
            lambda: self.store.begin_interview("other", self.id, 1),
            lambda: self.store.finish_interview("other", self.id, "job"),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(WorkspaceNotFound):
                call()
        self.assertEqual(self.store.list_workspaces("other"), [])
        self.assertEqual(self.get(), self.workspace)

    def test_review_is_required_and_pending_review_blocks_all_approval(self):
        state = self.analyzed(review=False)
        with self.assertRaises(WorkspaceConflict):
            self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.begin_review(self.owner, self.id, state["draft_revision"])
        self.assertEqual(state["stage"], "reviewing")
        self.assertEqual(state["active_job"]["kind"], "review")
        self.assertIsNone(state["reviewed_draft_revision"])
        self.store.add_message(self.owner, self.id, "user", "Please review.", "review")
        self.store.add_message(self.owner, self.id, "assistant", "Review response.", "review")
        for call in (
            lambda: self.store.approve_build(self.owner, self.id, state["draft_revision"]),
            lambda: self.store.begin_build(self.owner, self.id, state["draft_revision"]),
            lambda: self.store.approve_direction(self.owner, self.id, state["revision"]),
            lambda: self.store.save_draft(self.owner, self.id, {"title": "Edit"}, state["draft_revision"]),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        state = self.store.finish_review(self.owner, self.id, state["active_job"]["id"], None)
        self.assertEqual(state["reviewed_draft_revision"], state["draft_revision"])
        self.assertEqual(state["draft_revision"], 1)
        self.assertEqual(state["stage"], "draft")
        self.store.approve_build(self.owner, self.id, state["draft_revision"])

    def test_review_revision_is_bound_to_optional_replacement_and_manual_edits(self):
        state = self.analyzed()
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.begin_review(self.owner, self.id, state["draft_revision"])
        self.assertIsNone(state["build_revision"])
        self.assertIsNone(state["artifact"])
        self.assertIsNone(state["reviewed_draft_revision"])
        old_revision = state["draft_revision"]
        state = self.store.finish_review(
            self.owner, self.id, state["active_job"]["id"], {"title": "Review revision"})
        self.assertEqual(state["draft_revision"], old_revision + 1)
        self.assertEqual(state["reviewed_draft_revision"], state["draft_revision"])
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.save_draft(
            self.owner, self.id, {"title": "Manual change"}, state["draft_revision"])
        self.assertIsNone(state["reviewed_draft_revision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.approve_build(self.owner, self.id, state["draft_revision"])

    def test_failed_or_recovered_review_never_enables_build(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                self.id = self.store.create(self.owner, "Review fixture")["id"]
                state = self.analyzed()
                state = self.store.begin_review(self.owner, self.id, state["draft_revision"])
                job_id = state["active_job"]["id"]
                if recovery:
                    self.store.recover_interrupted_jobs()
                    state = self.get()
                else:
                    state = self.store.fail_job(self.owner, self.id, job_id, "Fixture failure")
                self.assertIsNone(state["reviewed_draft_revision"])
                with self.assertRaises(WorkspaceConflict):
                    self.store.approve_build(self.owner, self.id, state["draft_revision"])
                with self.assertRaises(WorkspaceConflict):
                    self.store.finish_review(self.owner, self.id, job_id, None)

    def test_review_rejects_stale_wrong_and_duplicate_completion(self):
        state = self.analyzed(review=False)
        with self.assertRaises(WorkspaceConflict):
            self.store.begin_review(self.owner, self.id, state["draft_revision"] - 1)
        state = self.store.begin_review(self.owner, self.id, state["draft_revision"])
        job_id = state["active_job"]["id"]
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_review(self.owner, self.id, "wrong", None)
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_interview(self.owner, self.id, job_id)
        with self.assertRaises(ValueError):
            self.store.finish_review(self.owner, self.id, job_id, {})
        self.assertEqual(self.get(), state)
        self.store.finish_review(self.owner, self.id, job_id, None)
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_review(self.owner, self.id, job_id, None)

    def test_interview_lease_blocks_changes_and_direction_approval(self):
        state = self.store.save_brief(self.owner, self.id, "Interview brief", 1)
        state = self.store.begin_interview(self.owner, self.id, state["revision"])
        job_id = state["active_job"]["id"]
        self.assertEqual(state["stage"], "interviewing")
        self.assertEqual(state["active_job"]["kind"], "interview")
        self.store.add_message(self.owner, self.id, "user", "Interview question")
        for call in (
            lambda: self.store.approve_direction(self.owner, self.id, state["revision"]),
            lambda: self.store.save_brief(self.owner, self.id, "New brief", state["revision"]),
            lambda: self.store.begin_interview(self.owner, self.id, state["revision"]),
            lambda: self.store.finish_review(self.owner, self.id, job_id, None),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        self.store.add_message(self.owner, self.id, "assistant", "Interview response")
        state = self.store.finish_interview(self.owner, self.id, job_id)
        self.assertEqual(state["stage"], "brief")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["jobs"][-1]["status"], "completed")
        self.store.approve_direction(self.owner, self.id, state["revision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_interview(self.owner, self.id, job_id)

    def test_review_lease_serializes_duplicate_requests_across_instances(self):
        state = self.analyzed(review=False)
        stores = (MentorStore(self.path), MentorStore(self.path))
        barrier = Barrier(2)

        def start(store):
            barrier.wait(timeout=5)
            try:
                return store.begin_review(self.owner, self.id, state["draft_revision"])
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, stores))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.get()["active_job"]["kind"], "review")

    def test_old_database_without_review_field_defaults_to_unreviewed(self):
        state = self.analyzed(review=False)
        del state["reviewed_draft_revision"]
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE mentor_workspaces SET state = ? WHERE id = ?",
                         (json.dumps(state), self.id))
        self.assertIsNone(self.get()["reviewed_draft_revision"])
        self.assertIsNone(self.store.list_workspaces(self.owner)[0]["reviewed_draft_revision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.approve_build(self.owner, self.id, state["draft_revision"])

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

    def test_all_source_types_and_metadata(self):
        for source_type in ("blank", "api", "imscc", "prepared"):
            with self.subTest(source_type=source_type):
                state = self.store.create(
                    self.owner, "Source", source_type, "course-1", "/fixture/export")
                self.assertEqual(state["source_type"], source_type)
                self.assertEqual(state["course_id"], "course-1")
                self.assertEqual(state["export_dir"], "/fixture/export")
        state = self.store.set_source(
            self.owner, self.id,
            {"source_type": "prepared", "course_id": "prepared-1",
             "export_dir": "/fixture/prepared", "inventory": {"pages": 2}}, 1)
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["source"]["inventory"], {"pages": 2})
        self.assertEqual(state["course_id"], "prepared-1")

    def test_invalid_sequence_and_empty_brief(self):
        for call in (
            lambda: self.store.approve_direction(self.owner, self.id, 1),
            lambda: self.store.begin_analysis(self.owner, self.id, 1),
            lambda: self.store.save_draft(self.owner, self.id, {"pages": []}, 0),
            lambda: self.store.approve_build(self.owner, self.id, 0),
            lambda: self.store.begin_build(self.owner, self.id, 0),
            lambda: self.store.finish_analysis(self.owner, self.id, "job", {"pages": []}),
            lambda: self.store.finish_build(self.owner, self.id, "job", {"path": "course"}),
            lambda: self.store.fail_job(self.owner, self.id, "job", "Error"),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        state = self.store.save_brief(self.owner, self.id, "   ", 1)
        with self.assertRaises(WorkspaceConflict):
            self.store.approve_direction(self.owner, self.id, state["revision"])

    def test_full_approved_workflow_and_job_history(self):
        state = self.built()
        self.assertEqual(state["stage"], "built")
        self.assertIsNone(state["active_job"])
        self.assertEqual(state["artifact"], {"path": "exports/course"})
        self.assertEqual(state["draft_revision"], 1)
        self.assertEqual(state["direction_revision"], state["revision"])
        self.assertEqual(state["build_revision"], state["draft_revision"])
        self.assertEqual([job["kind"] for job in state["jobs"]], ["analysis", "review", "build"])
        for job in state["jobs"]:
            self.assertEqual(job["status"], "completed")
            self.assertIsNotNone(job["finished_at"])

    def test_messages_do_not_invalidate_approvals_or_draft(self):
        before = self.built()
        state = self.store.add_message(self.owner, self.id, "instructor", "Revise?", "review")
        for key in ("revision", "draft_revision", "direction_revision",
                    "build_revision", "artifact", "draft", "stage"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["messages"][0]["kind"], "review")

    def test_each_input_change_invalidates_approvals_draft_and_artifact(self):
        for mutation in ("source", "brief", "material", "remove"):
            with self.subTest(mutation=mutation):
                self.workspace = self.store.create(self.owner, mutation)
                self.id = self.workspace["id"]
                state = self.store.add_material(self.owner, self.id, {"text": "Reference"}, 1)
                material_id = state["materials"][0]["id"]
                before = self.built()
                revision = before["revision"]
                if mutation == "source":
                    state = self.store.set_source(self.owner, self.id, {"source_type": "blank"}, revision)
                elif mutation == "brief":
                    state = self.store.save_brief(self.owner, self.id, "New direction", revision)
                elif mutation == "material":
                    state = self.store.add_material(self.owner, self.id, {"text": "Policy"}, revision)
                else:
                    state = self.store.remove_material(self.owner, self.id, material_id, revision)
                self.assertEqual(state["revision"], revision + 1)
                self.assertEqual(state["draft_revision"], before["draft_revision"])
                for key in ("draft", "direction_revision", "build_revision",
                            "reviewed_draft_revision", "artifact"):
                    self.assertIsNone(state[key])
                self.assertEqual(state["stage"], "brief")
                self.assertEqual(len(state["jobs"]), 3)
                with self.assertRaises(WorkspaceConflict):
                    self.store.begin_build(self.owner, self.id, before["draft_revision"])

    def test_stale_input_revision_cannot_change_anything(self):
        state = self.store.save_brief(self.owner, self.id, "New brief", 1)
        for call in (
            lambda: self.store.save_brief(self.owner, self.id, "Stale brief", 1),
            lambda: self.store.set_source(self.owner, self.id, {}, 1),
            lambda: self.store.add_material(self.owner, self.id, {"text": "Stale"}, 1),
            lambda: self.store.approve_direction(self.owner, self.id, 1),
            lambda: self.store.begin_analysis(self.owner, self.id, 1),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        self.assertEqual(self.get(), state)

    def test_edit_draft_requires_new_exact_build_approval(self):
        state = self.analyzed()
        state = self.store.approve_build(self.owner, self.id, state["draft_revision"])
        original_revision = state["draft_revision"]
        state = self.store.save_draft(
            self.owner, self.id, {"title": "Edited"}, original_revision)
        self.assertEqual(state["draft_revision"], original_revision + 1)
        self.assertIsNone(state["build_revision"])
        for call in (
            lambda: self.store.approve_build(self.owner, self.id, original_revision),
            lambda: self.store.save_draft(self.owner, self.id, {"title": "Stale"}, original_revision),
            lambda: self.store.begin_build(self.owner, self.id, original_revision),
            lambda: self.store.begin_build(self.owner, self.id, state["draft_revision"]),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        self.assertEqual(self.get(), state)
        self.reviewed(state)
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        self.assertEqual(
            self.store.begin_build(self.owner, self.id, state["draft_revision"])["stage"],
            "building")

    def test_explicit_recovery_preserves_drafts_and_handles_all_owners(self):
        state = self.analyzed()
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.begin_build(self.owner, self.id, state["draft_revision"])
        other = self.store.create("another-owner", "Another course")
        other = self.store.save_brief("another-owner", other["id"], "Another brief.", 1)
        other = self.store.approve_direction("another-owner", other["id"], other["revision"])
        other = self.store.begin_analysis("another-owner", other["id"], other["revision"])
        recovered = self.store.recover_interrupted_jobs()
        self.assertEqual({item["id"] for item in recovered}, {self.id, other["id"]})
        current = self.get()
        self.assertEqual(current["draft"], state["draft"])
        self.assertEqual(current["draft_revision"], state["draft_revision"])
        self.assertEqual(current["stage"], "build_approved")
        self.assertIsNone(current["artifact"])
        self.assertEqual(current["jobs"][-1]["status"], "failed")
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_build(
                self.owner, self.id, state["active_job"]["id"], {"path": "interrupted"})
        self.assertEqual(len(self.store.list_workspaces(self.owner)), 1)

    def test_invalid_completion_payload_leaves_running_job_unchanged(self):
        state = self.direction()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        for draft in ({}, [], {"data": float("inf")}, {"token": "private fixture"}):
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                self.store.finish_analysis(self.owner, self.id, state["active_job"]["id"], draft)
            self.assertEqual(self.get(), state)

    def test_analysis_completion_rejects_changed_input_revision(self):
        state = self.direction()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        state["revision"] += 1
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE mentor_workspaces SET state = ? WHERE id = ?",
                         (json.dumps(state), self.id))
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_analysis(
                self.owner, self.id, state["active_job"]["id"], {"title": "Stale draft"})
        self.assertEqual(self.get(), state)

    def test_running_jobs_block_edits_but_allow_messages(self):
        state = self.analyzed()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        for call in (
            lambda: self.store.save_brief(self.owner, self.id, "Changed", state["revision"]),
            lambda: self.store.set_source(self.owner, self.id, {}, state["revision"]),
            lambda: self.store.add_material(self.owner, self.id, {"text": "Changed"}, state["revision"]),
            lambda: self.store.remove_material(self.owner, self.id, "missing", state["revision"]),
            lambda: self.store.save_draft(self.owner, self.id, {"title": "Changed"}, state["draft_revision"]),
            lambda: self.store.approve_direction(self.owner, self.id, state["revision"]),
            lambda: self.store.approve_build(self.owner, self.id, state["draft_revision"]),
            lambda: self.store.begin_analysis(self.owner, self.id, state["revision"]),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        messaged = self.store.add_message(self.owner, self.id, "user", "A note")
        self.assertEqual(messaged["active_job"], state["active_job"])
        finished = self.store.finish_analysis(
            self.owner, self.id, state["active_job"]["id"], {"title": "New analysis"})
        self.assertEqual(len(finished["messages"]), 1)

    def test_wrong_job_kind_id_and_duplicate_completion(self):
        state = self.direction()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        job_id = state["active_job"]["id"]
        for call in (
            lambda: self.store.finish_analysis(self.owner, self.id, "wrong", {"title": "Draft"}),
            lambda: self.store.finish_build(self.owner, self.id, job_id, {"path": "bad"}),
            lambda: self.store.fail_job(self.owner, self.id, "wrong", "Error"),
        ):
            with self.subTest(call=call), self.assertRaises(WorkspaceConflict):
                call()
        self.assertEqual(self.get(), state)
        self.store.finish_analysis(self.owner, self.id, job_id, {"title": "Draft"})
        with self.assertRaises(WorkspaceConflict):
            self.store.finish_analysis(self.owner, self.id, job_id, {"title": "Duplicate"})

    def test_completion_checks_both_input_and_draft_revision(self):
        for changed_key in ("revision", "draft_revision"):
            with self.subTest(changed_key=changed_key):
                self.id = self.store.create(self.owner, "Job fixture")["id"]
                state = self.analyzed()
                self.store.approve_build(self.owner, self.id, state["draft_revision"])
                state = self.store.begin_build(self.owner, self.id, state["draft_revision"])
                job_id = state["active_job"]["id"]
                # Simulate incompatible restored state; normal APIs prohibit edits.
                state[changed_key] += 1
                with sqlite3.connect(self.path) as conn:
                    conn.execute("UPDATE mentor_workspaces SET state = ? WHERE id = ?",
                                 (json.dumps(state), self.id))
                with self.assertRaises(WorkspaceConflict):
                    self.store.finish_build(self.owner, self.id, job_id, {"path": "stale"})
                self.assertEqual(self.get(), state)

    def test_duplicate_material_and_missing_removal_are_atomic(self):
        material = {"id": "reference-one", "text": "Reference"}
        state = self.store.add_material(self.owner, self.id, material, 1)
        with self.assertRaises(WorkspaceConflict):
            self.store.add_material(self.owner, self.id, material, state["revision"])
        with self.assertRaises(WorkspaceNotFound):
            self.store.remove_material(self.owner, self.id, "missing", state["revision"])
        self.assertEqual(self.get(), state)
        state = self.store.remove_material(self.owner, self.id, material["id"], state["revision"])
        self.assertEqual(state["materials"], [])

    def test_returned_and_input_dicts_are_detached_from_database(self):
        material = {"text": "Reference", "provenance": {"role": "reference"}}
        state = self.store.add_material(self.owner, self.id, material, 1)
        material["provenance"]["role"] = "changed"
        state["materials"][0]["text"] = "changed"
        self.assertEqual(self.get()["materials"][0]["text"], "Reference")
        self.assertEqual(self.get()["materials"][0]["provenance"]["role"], "reference")
        self.assertNotIn("id", material)

    def test_multiple_instances_share_durable_state(self):
        state = self.built()
        second = MentorStore(self.path)
        self.assertEqual(second.get(self.owner, self.id), state)
        updated = second.add_message(self.owner, self.id, "assistant", "Review complete.", "review")
        self.assertEqual(self.get(), updated)
        self.assertEqual(second.list_workspaces(self.owner), [updated])

    def test_simultaneous_analysis_starts_create_only_one_job(self):
        state = self.direction()
        stores = (MentorStore(self.path), MentorStore(self.path))
        barrier = Barrier(2)

        def start(store):
            barrier.wait(timeout=5)
            try:
                return store.begin_analysis(self.owner, self.id, state["revision"])
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, stores))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.get()["jobs"]), 1)

    def test_simultaneous_input_edits_accept_only_one_revision(self):
        stores = (MentorStore(self.path), MentorStore(self.path))
        barrier = Barrier(2)

        def edit(store):
            barrier.wait(timeout=5)
            try:
                return store.save_brief(self.owner, self.id, "Concurrent brief", 1)
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, stores))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.get()["revision"], 2)

    def test_simultaneous_build_starts_create_only_one_job(self):
        state = self.analyzed()
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        stores = (MentorStore(self.path), MentorStore(self.path))
        barrier = Barrier(2)

        def start(store):
            barrier.wait(timeout=5)
            try:
                return store.begin_build(self.owner, self.id, state["draft_revision"])
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, stores))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.get()["jobs"]), 3)
        self.assertEqual(self.get()["active_job"]["kind"], "build")

    def test_failed_database_write_rolls_back_transition(self):
        before = self.direction()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_fixture_update BEFORE UPDATE ON mentor_workspaces "
                "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.begin_analysis(self.owner, self.id, before["revision"])
        self.assertEqual(self.get(), before)

    def test_restart_preserves_running_until_explicit_recovery(self):
        state = self.direction()
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        old_job = state["active_job"]["id"]
        restarted = MentorStore(self.path)
        self.assertEqual(restarted.get(self.owner, self.id), state)
        recovered = restarted.recover_interrupted_jobs()
        self.assertEqual(len(recovered), 1)
        state = self.get()
        self.assertIsNone(state["active_job"])
        self.assertEqual(state["jobs"][0]["status"], "failed")
        self.assertIn("restart", state["jobs"][0]["error"])
        self.assertEqual(state["stage"], "direction_approved")
        self.assertEqual(restarted.recover_interrupted_jobs(), [])
        with self.assertRaises(WorkspaceConflict):
            restarted.finish_analysis(self.owner, self.id, old_job, {"title": "Stale"})
        new_state = restarted.begin_analysis(self.owner, self.id, state["revision"])
        self.assertNotEqual(new_state["active_job"]["id"], old_job)
        self.assertEqual(len(new_state["jobs"]), 2)


    def test_failed_build_can_be_retried_without_fabricated_artifact(self):
        state = self.analyzed()
        self.store.approve_build(self.owner, self.id, state["draft_revision"])
        state = self.store.begin_build(self.owner, self.id, state["draft_revision"])
        state = self.store.fail_job(self.owner, self.id, state["active_job"]["id"],
                                    "Upstream confidential fixture value")
        self.assertEqual(state["stage"], "build_approved")
        self.assertIsNone(state["artifact"])
        self.assertNotIn("confidential", json.dumps(state))
        self.assertEqual(state["jobs"][-1]["status"], "failed")
        self.assertEqual(
            self.store.begin_build(self.owner, self.id, state["draft_revision"])["stage"],
            "building")

    def test_input_and_draft_revisions_remain_monotonic_after_invalidation(self):
        state = self.analyzed()
        old_draft_revision = state["draft_revision"]
        state = self.store.save_brief(self.owner, self.id, "New course direction.", state["revision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_draft(self.owner, self.id, {"title": "Cannot restore"}, old_draft_revision)
        self.store.approve_direction(self.owner, self.id, state["revision"])
        state = self.store.begin_analysis(self.owner, self.id, state["revision"])
        state = self.store.finish_analysis(
            self.owner, self.id, state["active_job"]["id"], {"title": "Fresh"})
        self.assertGreater(state["draft_revision"], old_draft_revision)

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
                self.store.add_material(self.owner, self.id, value, 1)
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
            lambda: self.store.set_source(self.owner, self.id, {"source_type": []}, 1),
            lambda: self.store.save_brief(self.owner, self.id, None, 1),
            lambda: self.store.save_brief(self.owner, self.id, "Text", True),
            lambda: self.store.save_brief(self.owner, self.id, "Text", -1),
            lambda: self.store.save_brief(self.owner, self.id, "Text", "1"),
            lambda: self.store.add_material(self.owner, self.id, [], 1),
            lambda: self.store.add_material(self.owner, self.id, {"id": ""}, 1),
            lambda: self.store.add_message(self.owner, self.id, "unknown", "Text"),
            lambda: self.store.add_message(self.owner, self.id, "user", ""),
            lambda: self.store.add_message(self.owner, self.id, "user", "Text", "unknown"),
            lambda: MentorStore(":memory:"),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.get(), self.workspace)


if __name__ == "__main__":
    unittest.main()
