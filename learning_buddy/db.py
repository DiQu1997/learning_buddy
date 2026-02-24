from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable


class LearningBuddyDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS notebooks (
                id TEXT PRIMARY KEY,
                notebook_id TEXT UNIQUE,
                name TEXT NOT NULL,
                description TEXT,
                doc_type TEXT,
                tags TEXT,
                public_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                notebook_id TEXT,
                source_id TEXT UNIQUE,
                title TEXT,
                source_index INTEGER,
                file_path TEXT,
                original_pdf TEXT,
                page_range TEXT,
                is_chunk INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                notebook_id TEXT,
                source_id TEXT,
                artifact_type TEXT,
                artifact_id TEXT UNIQUE,
                format_detail TEXT,
                download_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                notebook_ref TEXT,
                status TEXT,
                input_paths TEXT,
                config TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                task_type TEXT,
                target_ref TEXT,
                status TEXT,
                attempts INTEGER DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_unique
            ON tasks (job_id, task_type, target_ref);

            CREATE INDEX IF NOT EXISTS idx_tasks_job
            ON tasks (job_id, task_type, status);

            CREATE INDEX IF NOT EXISTS idx_sources_notebook
            ON sources (notebook_id, source_index);

            CREATE INDEX IF NOT EXISTS idx_artifacts_notebook
            ON artifacts (notebook_id, artifact_type);
            """
        )
        self.conn.commit()

    @staticmethod
    def _id() -> str:
        return str(uuid.uuid4())

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def create_notebook(self, name: str, description: str, doc_type: str, tags: list[str]) -> str:
        notebook_ref = self._id()
        self.conn.execute(
            """
            INSERT INTO notebooks (id, notebook_id, name, description, doc_type, tags, public_url)
            VALUES (?, NULL, ?, ?, ?, ?, NULL)
            """,
            (notebook_ref, name, description, doc_type, json.dumps(tags, ensure_ascii=True)),
        )
        self.conn.commit()
        return notebook_ref

    def update_notebook_remote_id(self, notebook_ref: str, notebook_id: str) -> None:
        self.conn.execute(
            "UPDATE notebooks SET notebook_id = ? WHERE id = ?",
            (notebook_id, notebook_ref),
        )
        self.conn.commit()

    def update_notebook_public_url(self, notebook_ref: str, public_url: str | None) -> None:
        self.conn.execute(
            "UPDATE notebooks SET public_url = ? WHERE id = ?",
            (public_url, notebook_ref),
        )
        self.conn.commit()

    def get_notebook_by_ref(self, notebook_ref: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM notebooks WHERE id = ?", (notebook_ref,))

    def find_notebook(self, identifier: str) -> sqlite3.Row | None:
        row = self._fetchone(
            "SELECT * FROM notebooks WHERE notebook_id = ? OR id = ?",
            (identifier, identifier),
        )
        if row:
            return row
        return self._fetchone(
            "SELECT * FROM notebooks WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (identifier,),
        )

    def list_notebooks(self, tag: str | None = None, doc_type: str | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[str] = []
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if doc_type:
            clauses.append("doc_type = ?")
            params.append(doc_type)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(
            f"""
            SELECT * FROM notebooks
            {where_sql}
            ORDER BY created_at DESC
            """,
            tuple(params),
        )

    def create_job(self, notebook_ref: str, input_paths: list[str], config: dict) -> str:
        job_id = self._id()
        self.conn.execute(
            """
            INSERT INTO jobs (id, notebook_ref, status, input_paths, config, error)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (job_id, notebook_ref, "PENDING", json.dumps(input_paths, ensure_ascii=True), json.dumps(config, ensure_ascii=True)),
        )
        self.conn.commit()
        return job_id

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def list_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT * FROM jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def update_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, error, job_id),
        )
        self.conn.commit()

    def create_source(
        self,
        notebook_id: str | None,
        title: str,
        source_index: int,
        file_path: str,
        original_pdf: str,
        page_range: str | None,
        is_chunk: bool,
    ) -> str:
        source_ref = self._id()
        self.conn.execute(
            """
            INSERT INTO sources (
                id, notebook_id, source_id, title, source_index, file_path, original_pdf, page_range, is_chunk
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_ref,
                notebook_id,
                title,
                source_index,
                file_path,
                original_pdf,
                page_range,
                1 if is_chunk else 0,
            ),
        )
        self.conn.commit()
        return source_ref

    def update_source_uploaded(self, source_ref: str, notebook_id: str, source_id: str) -> None:
        self.conn.execute(
            """
            UPDATE sources
            SET notebook_id = ?, source_id = ?
            WHERE id = ?
            """,
            (notebook_id, source_id, source_ref),
        )
        self.conn.commit()

    def get_source_by_ref(self, source_ref: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM sources WHERE id = ?", (source_ref,))

    def get_source_by_remote_id(self, source_id: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM sources WHERE source_id = ?", (source_id,))

    def list_sources_for_notebook(self, notebook_id: str) -> list[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT * FROM sources
            WHERE notebook_id = ?
            ORDER BY source_index ASC, created_at ASC
            """,
            (notebook_id,),
        )

    def list_sources_for_job(self, job_id: str) -> list[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT s.*, t.id AS upload_task_id, t.status AS upload_task_status
            FROM tasks t
            JOIN sources s ON s.id = t.target_ref
            WHERE t.job_id = ? AND t.task_type = 'UPLOAD'
            ORDER BY s.source_index ASC
            """,
            (job_id,),
        )

    def create_artifact(
        self,
        notebook_id: str,
        source_id: str | None,
        artifact_type: str,
        format_detail: str | None,
    ) -> str:
        existing = self._fetchone(
            """
            SELECT id FROM artifacts
            WHERE notebook_id = ? AND source_id IS ? AND artifact_type = ? AND COALESCE(format_detail, '') = COALESCE(?, '')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (notebook_id, source_id, artifact_type, format_detail),
        )
        if existing:
            return str(existing["id"])

        artifact_ref = self._id()
        self.conn.execute(
            """
            INSERT INTO artifacts (id, notebook_id, source_id, artifact_type, artifact_id, format_detail, download_path)
            VALUES (?, ?, ?, ?, NULL, ?, NULL)
            """,
            (artifact_ref, notebook_id, source_id, artifact_type, format_detail),
        )
        self.conn.commit()
        return artifact_ref

    def get_artifact_by_ref(self, artifact_ref: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_ref,))

    def find_artifact(self, identifier: str) -> sqlite3.Row | None:
        row = self._fetchone(
            "SELECT * FROM artifacts WHERE id = ? OR artifact_id = ?",
            (identifier, identifier),
        )
        if row:
            return row
        return None

    def list_artifacts(self, artifact_type: str | None = None, notebook_id: str | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[str] = []
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if notebook_id:
            clauses.append("notebook_id = ?")
            params.append(notebook_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        return self._fetchall(
            f"""
            SELECT * FROM artifacts
            {where_sql}
            ORDER BY created_at DESC
            """,
            tuple(params),
        )

    def list_artifacts_for_job(self, job_id: str) -> list[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT a.*
            FROM tasks t
            JOIN artifacts a ON a.id = t.target_ref
            WHERE t.job_id = ? AND t.task_type = 'GENERATE'
            ORDER BY a.created_at ASC
            """,
            (job_id,),
        )

    def update_artifact_remote_id(self, artifact_ref: str, artifact_id: str) -> None:
        self.conn.execute(
            "UPDATE artifacts SET artifact_id = ? WHERE id = ?",
            (artifact_id, artifact_ref),
        )
        self.conn.commit()

    def clear_artifact_remote_id(self, artifact_ref: str) -> None:
        self.conn.execute(
            "UPDATE artifacts SET artifact_id = NULL WHERE id = ?",
            (artifact_ref,),
        )
        self.conn.commit()

    def update_artifact_download_path(self, artifact_ref: str, download_path: str) -> None:
        self.conn.execute(
            "UPDATE artifacts SET download_path = ? WHERE id = ?",
            (download_path, artifact_ref),
        )
        self.conn.commit()

    def ensure_task(self, job_id: str, task_type: str, target_ref: str, status: str = "QUEUED") -> str:
        existing = self._fetchone(
            """
            SELECT id FROM tasks
            WHERE job_id = ? AND task_type = ? AND target_ref = ?
            """,
            (job_id, task_type, target_ref),
        )
        if existing:
            return str(existing["id"])

        task_id = self._id()
        self.conn.execute(
            """
            INSERT INTO tasks (id, job_id, task_type, target_ref, status, attempts, error)
            VALUES (?, ?, ?, ?, ?, 0, NULL)
            """,
            (task_id, job_id, task_type, target_ref, status),
        )
        self.conn.commit()
        return task_id

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list_tasks(
        self,
        job_id: str,
        task_type: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        clauses = ["job_id = ?"]
        params: list[str] = [job_id]
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if statuses:
            tokens = list(statuses)
            placeholders = ",".join("?" for _ in tokens)
            clauses.append(f"status IN ({placeholders})")
            params.extend(tokens)
        return self._fetchall(
            f"""
            SELECT * FROM tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC
            """,
            tuple(params),
        )

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        if increment_attempt:
            self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, task_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, task_id),
            )
        self.conn.commit()

    def reset_upload_stage(self, job_id: str) -> int:
        rows = self._fetchall(
            """
            SELECT target_ref
            FROM tasks
            WHERE job_id = ? AND task_type = 'UPLOAD'
            """,
            (job_id,),
        )
        source_refs = sorted({str(row["target_ref"]) for row in rows})

        self.conn.execute(
            "DELETE FROM tasks WHERE job_id = ? AND task_type = 'UPLOAD'",
            (job_id,),
        )
        if source_refs:
            placeholders = ",".join("?" for _ in source_refs)
            self.conn.execute(
                f"DELETE FROM sources WHERE id IN ({placeholders})",
                tuple(source_refs),
            )
        self.conn.commit()
        return len(source_refs)

    def count_tasks(self, job_id: str, task_type: str, status: str) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS c FROM tasks
            WHERE job_id = ? AND task_type = ? AND status = ?
            """,
            (job_id, task_type, status),
        )
        return int(row["c"]) if row else 0

    def task_counts(self, job_id: str) -> dict[str, int]:
        rows = self._fetchall(
            """
            SELECT task_type || ':' || status AS k, COUNT(*) AS c
            FROM tasks
            WHERE job_id = ?
            GROUP BY task_type, status
            """,
            (job_id,),
        )
        return {str(row["k"]): int(row["c"]) for row in rows}

    def known_artifact_ids(self, notebook_id: str) -> set[str]:
        rows = self._fetchall(
            "SELECT artifact_id FROM artifacts WHERE notebook_id = ? AND artifact_id IS NOT NULL",
            (notebook_id,),
        )
        return {str(row["artifact_id"]) for row in rows}
