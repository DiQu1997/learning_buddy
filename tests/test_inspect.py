from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from learning_buddy.engine import WorkflowEngine


class InspectTests(unittest.TestCase):
    def test_inspect_reports_remaining_and_terminal_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            engine = WorkflowEngine(workspace=workspace, show_progress=False)
            try:
                db = engine.db
                notebook_ref = db.create_notebook(
                    name="inspect-demo",
                    description="demo",
                    doc_type="book",
                    tags=["inspect"],
                )
                job_id = db.create_job(
                    notebook_ref=notebook_ref,
                    input_paths=[str(workspace / "demo.pdf")],
                    config={"artifact_types": ["report", "slide_deck"]},
                )
                db.update_job_status(job_id, "POLLING", error=None)
                db.update_notebook_remote_id(notebook_ref, "nb-1")

                source_ref = db.create_source(
                    notebook_id="nb-1",
                    title="Part 1",
                    source_index=1,
                    file_path=str(workspace / "part1.pdf"),
                    original_pdf=str(workspace / "demo.pdf"),
                    page_range="1-10",
                    is_chunk=True,
                )
                upload_task = db.ensure_task(job_id, "UPLOAD", source_ref, status="QUEUED")
                db.update_task_status(upload_task, "COMPLETED", error=None)
                db.update_source_uploaded(source_ref, "nb-1", "src-1")

                artifact_ref_a = db.create_artifact("nb-1", "src-1", "report", "Study Guide")
                artifact_ref_b = db.create_artifact("nb-1", "src-1", "slide_deck", None)
                artifact_ref_c = db.create_artifact("nb-1", "src-1", "audio", None)

                gen_done = db.ensure_task(job_id, "GENERATE", artifact_ref_a, status="QUEUED")
                db.update_task_status(gen_done, "IN_PROGRESS", increment_attempt=True)
                db.update_task_status(gen_done, "COMPLETED", error=None)

                gen_retryable_failed = db.ensure_task(job_id, "GENERATE", artifact_ref_b, status="QUEUED")
                db.update_task_status(gen_retryable_failed, "FAILED", error="temporary failure", increment_attempt=True)

                gen_terminal_failed = db.ensure_task(job_id, "GENERATE", artifact_ref_c, status="QUEUED")
                db.update_task_status(gen_terminal_failed, "FAILED", error="attempt 1", increment_attempt=True)
                db.update_task_status(gen_terminal_failed, "FAILED", error="attempt 2", increment_attempt=True)
                db.update_task_status(gen_terminal_failed, "FAILED", error="attempt 3", increment_attempt=True)

                db.ensure_task(job_id, "DOWNLOAD", artifact_ref_a, status="QUEUED")

                payload = engine.inspect(job_id=job_id, include_task_details=True, pending_limit=10)
                self.assertEqual(payload["jobs_returned"], 1)
                snapshot = payload["jobs"][0]

                self.assertEqual(snapshot["remaining"]["total_actionable_tasks"], 2)
                self.assertEqual(snapshot["remaining"]["terminal_failures"], 1)
                self.assertEqual(snapshot["tasks"]["GENERATE"]["retryable_failed"], 1)
                self.assertEqual(snapshot["tasks"]["GENERATE"]["terminal_failed"], 1)
                self.assertEqual(snapshot["tasks"]["DOWNLOAD"]["queued"], 1)
                self.assertEqual(snapshot["pending_tasks"]["total"], 3)
                self.assertTrue(any("Resume job:" in item for item in snapshot["next_actions"]))
            finally:
                engine.close()

    def test_inspect_list_mode_filters_done_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            engine = WorkflowEngine(workspace=workspace, show_progress=False)
            try:
                db = engine.db
                notebook_ref_a = db.create_notebook(
                    name="done-job",
                    description="done",
                    doc_type="book",
                    tags=["inspect"],
                )
                job_a = db.create_job(
                    notebook_ref=notebook_ref_a,
                    input_paths=[str(workspace / "a.pdf")],
                    config={"artifact_types": ["report"]},
                )
                db.update_job_status(job_a, "DONE", error=None)

                notebook_ref_b = db.create_notebook(
                    name="active-job",
                    description="active",
                    doc_type="book",
                    tags=["inspect"],
                )
                job_b = db.create_job(
                    notebook_ref=notebook_ref_b,
                    input_paths=[str(workspace / "b.pdf")],
                    config={"artifact_types": ["report"]},
                )
                db.update_job_status(job_b, "POLLING", error=None)

                filtered = engine.inspect(limit=10, include_task_details=False)
                returned_ids = {row["job"]["id"] for row in filtered["jobs"]}
                self.assertIn(job_b, returned_ids)
                self.assertNotIn(job_a, returned_ids)

                unfiltered = engine.inspect(limit=10, include_completed=True, include_task_details=False)
                all_ids = {row["job"]["id"] for row in unfiltered["jobs"]}
                self.assertIn(job_a, all_ids)
                self.assertIn(job_b, all_ids)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
