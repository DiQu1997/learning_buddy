from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from learning_buddy.engine import WorkflowEngine


class EngineArtifactNamingTests(unittest.TestCase):
    def test_artifact_output_path_includes_origin_resource_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            engine = WorkflowEngine(workspace=workspace, show_progress=False)
            try:
                notebook_ref = engine.db.create_notebook(
                    name="demo",
                    description="demo notebook",
                    doc_type="paper",
                    tags=[],
                )
                engine.db.update_notebook_remote_id(notebook_ref, "nb-123")

                source_ref = engine.db.create_source(
                    notebook_id="nb-123",
                    title="Chapter 1",
                    source_index=1,
                    file_path="/tmp/01_chapter-1.pdf",
                    original_pdf="/tmp/Deep Learning Systems.pdf",
                    page_range="1-20",
                    is_chunk=True,
                )
                engine.db.update_source_uploaded(source_ref, "nb-123", "src-123")

                artifact_ref = engine.db.create_artifact(
                    notebook_id="nb-123",
                    source_id="src-123",
                    artifact_type="note",
                    format_detail="Create Your Own",
                )
                artifact = dict(engine.db.get_artifact_by_ref(artifact_ref))

                output_path = engine._artifact_output_path(output_dir=workspace / "output", artifact=artifact)

                self.assertEqual(
                    output_path,
                    workspace
                    / "output"
                    / "artifacts"
                    / "01_Chapter_1"
                    / "01_Deep_Learning_Systems__Chapter_1__reading_note.md",
                )
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
