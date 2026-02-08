from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from learning_buddy.db import LearningBuddyDB


class DBTests(unittest.TestCase):
    def test_job_source_artifact_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = LearningBuddyDB(Path(tmp) / "workflow.db")

            notebook_ref = db.create_notebook(
                name="demo",
                description="demo notebook",
                doc_type="book",
                tags=["ml"],
            )
            job_id = db.create_job(
                notebook_ref=notebook_ref,
                input_paths=["/tmp/a.pdf"],
                config={"artifact_types": ["report"]},
            )
            db.update_notebook_remote_id(notebook_ref, "nb-123")

            source_ref = db.create_source(
                notebook_id=None,
                title="Chapter 1",
                source_index=1,
                file_path="/tmp/ch1.pdf",
                original_pdf="/tmp/a.pdf",
                page_range="1-50",
                is_chunk=True,
            )
            upload_task = db.ensure_task(job_id, "UPLOAD", source_ref, status="QUEUED")
            db.update_task_status(upload_task, "IN_PROGRESS", increment_attempt=True)
            db.update_source_uploaded(source_ref, "nb-123", "src-1")
            db.update_task_status(upload_task, "COMPLETED")

            artifact_ref = db.create_artifact(
                notebook_id="nb-123",
                source_id="src-1",
                artifact_type="report",
                format_detail="Study Guide",
            )
            gen_task = db.ensure_task(job_id, "GENERATE", artifact_ref, status="QUEUED")
            db.update_task_status(gen_task, "IN_PROGRESS", increment_attempt=True)
            db.update_artifact_remote_id(artifact_ref, "art-1")
            db.update_task_status(gen_task, "COMPLETED")

            dl_task = db.ensure_task(job_id, "DOWNLOAD", artifact_ref, status="QUEUED")
            db.update_task_status(dl_task, "COMPLETED")
            db.update_artifact_download_path(artifact_ref, "/tmp/report.md")

            job = db.get_job(job_id)
            self.assertIsNotNone(job)
            self.assertEqual(db.count_tasks(job_id, "UPLOAD", "COMPLETED"), 1)
            self.assertEqual(db.count_tasks(job_id, "GENERATE", "COMPLETED"), 1)
            self.assertEqual(db.count_tasks(job_id, "DOWNLOAD", "COMPLETED"), 1)

            artifact = db.find_artifact("art-1")
            self.assertIsNotNone(artifact)
            self.assertEqual(str(artifact["download_path"]), "/tmp/report.md")
            db.close()


if __name__ == "__main__":
    unittest.main()
