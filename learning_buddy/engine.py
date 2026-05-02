from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chunking import ChunkSpec, SUPPORTED_INPUT_SUFFIXES, chunk_document, classify_document, materialize_single_source, upload_extension_for_source
from .config import DEFAULT_CONFIG, EngineConfig, normalize_artifact_type, remote_artifact_type
from .db import LearningBuddyDB
from .nlm_client import NLMAuthError, NLMCLI, NLMError, StudioArtifact
from .utils import ensure_parent, slugify


TERMINAL_GENERATION_SUCCESS = {"completed", "complete", "done", "ready", "succeeded", "success"}
TERMINAL_GENERATION_FAILURE = {"failed", "error", "cancelled", "canceled"}


@dataclass
class JobContext:
    job_id: str
    notebook_ref: str
    notebook_id: str
    config: EngineConfig
    config_dict: dict[str, Any]
    output_dir: Path


class WorkflowEngine:
    def __init__(
        self,
        *,
        workspace: Path | None = None,
        db_path: Path | None = None,
        output_root: Path | None = None,
        nlm_command: str = "nlm",
        show_progress: bool = True,
    ):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.db_path = Path(db_path or self.workspace / ".learning_buddy" / "workflow.db")
        self.output_root = Path(output_root or self.workspace / "output")
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.show_progress = show_progress

        self.db = LearningBuddyDB(self.db_path)
        self.nlm = NLMCLI(command=nlm_command, logger=lambda message: self._log(f"NLM: {message}"))

    def _log(self, message: str, *, job_id: str | None = None) -> None:
        if not self.show_progress:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{stamp} UTC]"
        if job_id:
            prefix += f" [job {job_id}]"
        print(f"{prefix} {message}", file=sys.stderr, flush=True)

    def close(self) -> None:
        self.db.close()

    def process(
        self,
        *,
        input_paths: list[str],
        name: str | None = None,
        tags: list[str] | None = None,
        artifacts: list[str] | None = None,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        validated_paths = self._validate_inputs(input_paths)
        tags = tags or []
        self._log(
            f"Starting process job for {len(validated_paths)} document(s): "
            + ", ".join(path.name for path in validated_paths)
        )
        self._log(
            "Planned stages: INPUT -> CLASSIFY -> CHUNK (if needed) -> UPLOAD -> GENERATE -> POLL -> DOWNLOAD -> DONE."
        )

        cfg = EngineConfig.from_dict(DEFAULT_CONFIG)
        if max_pages is not None:
            cfg.max_pages_per_chunk = max_pages
        artifact_types = cfg.resolve_artifacts(artifacts)

        page_counts = [classify_document(path, cfg.chunk_threshold)[0] for path in validated_paths]
        doc_type = self._infer_doc_type(validated_paths, page_counts, cfg.chunk_threshold)

        job_name = name or validated_paths[0].stem
        job_name = slugify(job_name, fallback="job")
        output_dir = (self.output_root / job_name).resolve()
        description = f"Learning materials from {len(validated_paths)} document file(s)"
        self._log(
            f"Resolved config: chunk_threshold={cfg.chunk_threshold}, max_pages_per_chunk={cfg.max_pages_per_chunk}, "
            f"artifacts={artifact_types}, output_dir={output_dir}"
        )

        notebook_ref = self.db.create_notebook(
            name=job_name,
            description=description,
            doc_type=doc_type,
            tags=tags,
        )
        config_dict = cfg.to_dict()
        config_dict["artifact_types"] = artifact_types
        config_dict["output_dir"] = str(output_dir)
        config_dict["job_name"] = job_name
        job_id = self.db.create_job(notebook_ref=notebook_ref, input_paths=[str(p) for p in validated_paths], config=config_dict)
        self._log(f"Created job and notebook registry entries (doc_type={doc_type}).", job_id=job_id)

        context = JobContext(
            job_id=job_id,
            notebook_ref=notebook_ref,
            notebook_id="",
            config=cfg,
            config_dict=config_dict,
            output_dir=output_dir,
        )
        self._run_job(context, input_paths=validated_paths, force_from_stage=None)
        return self.get_job_summary(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        self._log("Resuming existing job.", job_id=job_id)

        config_dict = json.loads(job["config"])
        cfg = EngineConfig.from_dict(config_dict)
        notebook_ref = str(job["notebook_ref"])
        notebook = self.db.get_notebook_by_ref(notebook_ref)
        notebook_id = str(notebook["notebook_id"] or "") if notebook else ""
        output_dir = Path(config_dict.get("output_dir") or self.output_root / slugify(job_id))

        context = JobContext(
            job_id=job_id,
            notebook_ref=notebook_ref,
            notebook_id=notebook_id,
            config=cfg,
            config_dict=config_dict,
            output_dir=output_dir,
        )
        input_paths = [Path(p) for p in json.loads(job["input_paths"])]
        self._run_job(context, input_paths=input_paths, force_from_stage=None)
        return self.get_job_summary(job_id)

    def jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.list_jobs(limit=limit)]

    def status(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        payload = dict(job)
        payload["tasks"] = self.db.task_counts(job_id)
        return payload

    def inspect(
        self,
        *,
        job_id: str | None = None,
        limit: int = 10,
        include_completed: bool = False,
        include_task_details: bool = True,
        pending_limit: int = 25,
    ) -> dict[str, Any]:
        rows = [self.db.get_job(job_id)] if job_id else list(self.db.list_jobs(limit=max(1, limit)))
        jobs = [row for row in rows if row is not None]
        if job_id and not jobs:
            raise ValueError(f"Job not found: {job_id}")

        snapshots: list[dict[str, Any]] = []
        for job_row in jobs:
            snapshot = self._build_job_inspection(
                dict(job_row),
                include_task_details=include_task_details,
                pending_limit=pending_limit,
            )
            if not include_completed and not job_id and snapshot["job"]["status"] == "DONE":
                continue
            snapshots.append(snapshot)

        jobs_with_remaining = sum(1 for item in snapshots if item["remaining"]["total_actionable_tasks"] > 0)
        jobs_blocked = sum(1 for item in snapshots if item["remaining"]["terminal_failures"] > 0)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(self.workspace),
            "db_path": str(self.db_path),
            "filters": {
                "job_id": job_id,
                "limit": limit,
                "include_completed": include_completed,
                "include_task_details": include_task_details,
                "pending_limit": pending_limit,
            },
            "jobs_returned": len(snapshots),
            "summary": {
                "jobs_with_remaining_work": jobs_with_remaining,
                "jobs_blocked_by_terminal_failures": jobs_blocked,
            },
            "jobs": snapshots,
        }

    def library(self, *, tag: str | None = None, doc_type: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.list_notebooks(tag=tag, doc_type=doc_type)]

    def _build_job_inspection(
        self,
        job: dict[str, Any],
        *,
        include_task_details: bool,
        pending_limit: int,
    ) -> dict[str, Any]:
        job_id = str(job["id"])
        config_dict = json.loads(job["config"])
        cfg = EngineConfig.from_dict(config_dict)
        max_retries = cfg.max_retries

        notebook = self.db.get_notebook_by_ref(str(job["notebook_ref"])) if job.get("notebook_ref") else None
        sources = [dict(row) for row in self.db.list_sources_for_job(job_id)]
        artifacts = [dict(row) for row in self.db.list_artifacts_for_job(job_id)]
        tasks = [dict(row) for row in self.db.list_tasks(job_id)]

        source_by_ref = {str(row["id"]): row for row in sources}
        artifact_by_ref = {str(row["id"]): row for row in artifacts}

        task_types = ("UPLOAD", "GENERATE", "DOWNLOAD")
        tasks_by_type: dict[str, list[dict[str, Any]]] = {task_type: [] for task_type in task_types}
        for task in tasks:
            task_type = str(task["task_type"])
            tasks_by_type.setdefault(task_type, []).append(task)

        task_stats = {task_type: self._task_breakdown(rows, max_retries=max_retries) for task_type, rows in tasks_by_type.items()}
        task_stats["ALL"] = self._task_breakdown(tasks, max_retries=max_retries)

        remaining = {
            "upload_remaining": task_stats.get("UPLOAD", {}).get("remaining_actionable", 0),
            "generate_remaining": task_stats.get("GENERATE", {}).get("remaining_actionable", 0),
            "download_remaining": task_stats.get("DOWNLOAD", {}).get("remaining_actionable", 0),
            "total_actionable_tasks": task_stats["ALL"]["remaining_actionable"],
            "terminal_failures": task_stats["ALL"]["terminal_failed"],
        }

        next_actions: list[str] = []
        if remaining["total_actionable_tasks"] > 0:
            next_actions.append(f"Resume job: learning-buddy resume {job_id}")
        if remaining["terminal_failures"] > 0:
            next_actions.append("Inspect terminal failures and fix root cause before retrying.")
        if not next_actions:
            next_actions.append("No remaining actionable tasks.")

        pending_rows: list[dict[str, Any]] = []
        pending_total = 0
        pending_truncated = False
        if include_task_details:
            pending_rows, pending_total, pending_truncated = self._pending_task_rows(
                tasks=tasks,
                source_by_ref=source_by_ref,
                artifact_by_ref=artifact_by_ref,
                limit=max(1, pending_limit),
            )

        return {
            "job": {
                "id": job_id,
                "status": str(job["status"]),
                "error": job.get("error"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
            },
            "config": {
                "max_retries": cfg.max_retries,
                "max_concurrent_generations": cfg.max_concurrent_generations,
                "courtesy_delay_seconds": cfg.courtesy_delay_seconds,
                "poll_interval_seconds": cfg.poll_interval_seconds,
                "poll_max_wait_seconds": cfg.poll_max_wait_seconds,
                "nlm_request_interval_range": cfg.nlm_request_interval_range,
            },
            "notebook": {
                "local_id": notebook["id"] if notebook else None,
                "notebook_id": notebook["notebook_id"] if notebook else None,
                "name": notebook["name"] if notebook else None,
                "public_url": notebook["public_url"] if notebook else None,
            },
            "counts": {
                "sources_total": len(sources),
                "sources_uploaded": sum(1 for row in sources if row.get("source_id")),
                "sources_remaining": sum(1 for row in sources if not row.get("source_id")),
                "artifacts_total": len(artifacts),
                "artifacts_with_remote_id": sum(1 for row in artifacts if row.get("artifact_id")),
                "artifacts_downloaded": sum(1 for row in artifacts if row.get("download_path")),
            },
            "tasks": task_stats,
            "remaining": remaining,
            "next_actions": next_actions,
            "pending_tasks": {
                "total": pending_total,
                "truncated": pending_truncated,
                "rows": pending_rows,
            },
        }

    @staticmethod
    def _task_breakdown(tasks: list[dict[str, Any]], *, max_retries: int) -> dict[str, Any]:
        status_counts = {"QUEUED": 0, "IN_PROGRESS": 0, "COMPLETED": 0, "FAILED": 0}
        retryable_failed = 0
        terminal_failed = 0
        for task in tasks:
            status = str(task.get("status") or "").upper()
            if status not in status_counts:
                status_counts[status] = 0
            status_counts[status] += 1
            if status == "FAILED":
                attempts = int(task.get("attempts") or 0)
                if attempts < max_retries:
                    retryable_failed += 1
                else:
                    terminal_failed += 1

        total = len(tasks)
        completed = status_counts.get("COMPLETED", 0)
        remaining_actionable = (
            status_counts.get("QUEUED", 0) + status_counts.get("IN_PROGRESS", 0) + retryable_failed
        )
        completion_ratio = round((completed / total) if total else 1.0, 4)
        return {
            "total": total,
            "queued": status_counts.get("QUEUED", 0),
            "in_progress": status_counts.get("IN_PROGRESS", 0),
            "completed": completed,
            "failed": status_counts.get("FAILED", 0),
            "retryable_failed": retryable_failed,
            "terminal_failed": terminal_failed,
            "remaining_actionable": remaining_actionable,
            "completion_ratio": completion_ratio,
        }

    def _pending_task_rows(
        self,
        *,
        tasks: list[dict[str, Any]],
        source_by_ref: dict[str, dict[str, Any]],
        artifact_by_ref: dict[str, dict[str, Any]],
        limit: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        status_rank = {"IN_PROGRESS": 0, "FAILED": 1, "QUEUED": 2}
        pending = [row for row in tasks if str(row.get("status") or "").upper() in status_rank]
        pending.sort(
            key=lambda row: (
                status_rank.get(str(row.get("status") or "").upper(), 9),
                str(row.get("task_type") or ""),
                str(row.get("updated_at") or ""),
            )
        )

        truncated = len(pending) > limit
        result: list[dict[str, Any]] = []
        for task in pending[:limit]:
            task_type = str(task.get("task_type") or "")
            target_ref = str(task.get("target_ref") or "")
            target: dict[str, Any]
            if task_type == "UPLOAD":
                source = source_by_ref.get(target_ref) or self.db.get_source_by_ref(target_ref)
                target = {
                    "source_ref": target_ref,
                    "title": source["title"] if source else None,
                    "source_id": source["source_id"] if source else None,
                    "file_path": source["file_path"] if source else None,
                    "page_range": source["page_range"] if source else None,
                }
            else:
                artifact = artifact_by_ref.get(target_ref) or self.db.get_artifact_by_ref(target_ref)
                target = {
                    "artifact_ref": target_ref,
                    "artifact_type": artifact["artifact_type"] if artifact else None,
                    "artifact_id": artifact["artifact_id"] if artifact else None,
                    "source_id": artifact["source_id"] if artifact else None,
                    "download_path": artifact["download_path"] if artifact else None,
                }
            result.append(
                {
                    "id": str(task.get("id") or ""),
                    "task_type": task_type,
                    "status": str(task.get("status") or ""),
                    "attempts": int(task.get("attempts") or 0),
                    "error": task.get("error"),
                    "updated_at": task.get("updated_at"),
                    "target": target,
                }
            )
        return result, len(pending), truncated

    def info(self, notebook_identifier: str) -> dict[str, Any]:
        notebook = self._resolve_notebook(notebook_identifier)
        notebook_id = str(notebook["notebook_id"])
        sources = [dict(row) for row in self.db.list_sources_for_notebook(notebook_id)]
        artifacts = [dict(row) for row in self.db.list_artifacts(notebook_id=notebook_id)]
        return {"notebook": dict(notebook), "sources": sources, "artifacts": artifacts}

    def list_artifacts(self, *, artifact_type: str | None = None, notebook_identifier: str | None = None) -> list[dict[str, Any]]:
        notebook_id: str | None = None
        if notebook_identifier:
            notebook = self._resolve_notebook(notebook_identifier)
            notebook_id = str(notebook["notebook_id"])
        if artifact_type:
            artifact_type = normalize_artifact_type(artifact_type)
        return [dict(row) for row in self.db.list_artifacts(artifact_type=artifact_type, notebook_id=notebook_id)]

    def download_artifact(self, artifact_identifier: str) -> dict[str, Any]:
        artifact = self.db.find_artifact(artifact_identifier)
        if not artifact:
            raise ValueError(f"Artifact not found: {artifact_identifier}")

        notebook_id = str(artifact["notebook_id"] or "")
        artifact_id = str(artifact["artifact_id"] or "")
        artifact_type = str(artifact["artifact_type"])
        if not notebook_id or not artifact_id:
            raise ValueError("Artifact is missing notebook or artifact ID in registry.")

        output_path = self._artifact_output_path(
            output_dir=self.output_root / "redownloads" / slugify(notebook_id),
            artifact=dict(artifact),
        )
        self.nlm.ensure_authenticated()
        self.nlm.download_artifact(notebook_id, artifact_type, artifact_id, output_path)
        self.db.update_artifact_download_path(str(artifact["id"]), str(output_path))
        return {"artifact_id": artifact_id, "path": str(output_path)}

    def generate_for_source(self, source_identifier: str, artifact_types: list[str]) -> dict[str, Any]:
        source_row = self.db.get_source_by_remote_id(source_identifier) or self.db.get_source_by_ref(source_identifier)
        if not source_row:
            raise ValueError(f"Source not found: {source_identifier}")
        source = dict(source_row)
        if not source["source_id"]:
            raise ValueError(f"Source has not been uploaded to NotebookLM: {source_identifier}")

        notebook_row = self.db.find_notebook(str(source["notebook_id"]))
        if not notebook_row:
            raise ValueError(f"Notebook not found for source: {source_identifier}")
        notebook = dict(notebook_row)
        if not notebook["notebook_id"]:
            raise ValueError(f"Notebook has no NotebookLM ID: {source_identifier}")

        cfg = EngineConfig.from_dict(DEFAULT_CONFIG)
        artifact_values = cfg.resolve_artifacts(artifact_types)
        job_name = slugify(f"generate_{source.get('title') or source.get('source_id')}", fallback="generate")
        output_dir = (self.output_root / job_name).resolve()
        self._log(
            f"Generating additional artifacts for source {source_identifier}: {artifact_values}"
        )

        config_dict = cfg.to_dict()
        config_dict["artifact_types"] = artifact_values
        config_dict["output_dir"] = str(output_dir)
        config_dict["job_name"] = job_name
        job_id = self.db.create_job(
            notebook_ref=str(notebook["id"]),
            input_paths=[str(source["file_path"] or "")],
            config=config_dict,
        )
        self._log("Created generation-only job.", job_id=job_id)

        context = JobContext(
            job_id=job_id,
            notebook_ref=str(notebook["id"]),
            notebook_id=str(notebook["notebook_id"]),
            config=cfg,
            config_dict=config_dict,
            output_dir=output_dir,
        )
        self._apply_nlm_runtime_config(cfg)

        for artifact_type in artifact_values:
            format_detail = self._format_detail(artifact_type, cfg)
            artifact_ref = self.db.create_artifact(
                notebook_id=str(notebook["notebook_id"]),
                source_id=str(source["source_id"]),
                artifact_type=artifact_type,
                format_detail=format_detail,
            )
            self.db.ensure_task(job_id, "GENERATE", artifact_ref, status="QUEUED")

        self._run_generation_pipeline(context)
        self._stage_download(context)
        self.db.update_job_status(job_id, "DONE", error=None)
        self._log("Generation-only job completed.", job_id=job_id)
        self._write_job_summary(job_id)
        return self.get_job_summary(job_id)

    def open_notebook(self, notebook_identifier: str) -> str:
        notebook = self._resolve_notebook(notebook_identifier)
        notebook_id = str(notebook["notebook_id"])
        url = str(notebook["public_url"] or "") or self.nlm.notebook_url(notebook_id)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return url

    def query_notebook(self, notebook_identifier: str, question: str) -> str:
        notebook = self._resolve_notebook(notebook_identifier)
        notebook_id = str(notebook["notebook_id"])
        self.nlm.ensure_authenticated()
        return self.nlm.query_notebook(notebook_id, question)

    def get_job_summary(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        config = json.loads(job["config"])
        summary_path = Path(config.get("output_dir", "")) / "job_summary.json"
        if summary_path.exists():
            return json.loads(summary_path.read_text(encoding="utf-8"))
        return self._write_job_summary(job_id)

    def _run_job(self, context: JobContext, *, input_paths: list[Path], force_from_stage: str | None) -> None:
        self._apply_nlm_runtime_config(context.config)
        context.output_dir.mkdir(parents=True, exist_ok=True)
        self._log("Entering workflow pipeline.", job_id=context.job_id)

        try:
            if force_from_stage is None:
                self._maybe_stage_classify_chunk(context, input_paths)
                self._stage_upload(context)
            self._run_generation_pipeline(context)
            self._stage_download(context)
            self.db.update_job_status(context.job_id, "DONE", error=None)
            self._log("Workflow completed successfully.", job_id=context.job_id)
        except NLMAuthError as exc:
            self.db.update_job_status(context.job_id, "FAILED", error=str(exc))
            self._log(f"Workflow failed due to authentication: {exc}", job_id=context.job_id)
            self._write_job_summary(context.job_id)
            raise
        except Exception as exc:
            self.db.update_job_status(context.job_id, "FAILED", error=str(exc))
            self._log(f"Workflow failed: {exc}", job_id=context.job_id)
            self._write_job_summary(context.job_id)
            raise
        self._write_job_summary(context.job_id)

    def _maybe_stage_classify_chunk(self, context: JobContext, input_paths: list[Path]) -> None:
        job = self.db.get_job(context.job_id)
        job_status = str(job["status"]) if job else ""
        existing_sources = self.db.list_sources_for_job(context.job_id)
        if existing_sources:
            if job_status in {"CLASSIFYING", "CHUNKING"}:
                reset_count = self.db.reset_upload_stage(context.job_id)
                chunks_dir = context.output_dir / "chunks"
                if chunks_dir.exists():
                    shutil.rmtree(chunks_dir)
                self._log(
                    f"Detected interrupted {job_status} stage; reset {reset_count} partial upload/source record(s).",
                    job_id=context.job_id,
                )
            else:
                self._log(
                    f"Skipping INPUT/CLASSIFY/CHUNK because {len(existing_sources)} source records already exist.",
                    job_id=context.job_id,
                )
                return

        self.db.update_job_status(context.job_id, "CLASSIFYING", error=None)
        self._log("Stage CLASSIFY started.", job_id=context.job_id)
        classified: list[tuple[Path, int, bool]] = []
        for path in input_paths:
            pages, needs_chunking = classify_document(path, context.config.chunk_threshold)
            classified.append((path, pages, needs_chunking))
            decision = "chunk" if needs_chunking else "no-chunk"
            self._log(
                f"Classified '{path.name}': size_units={pages}, threshold={context.config.chunk_threshold}, decision={decision}.",
                job_id=context.job_id,
            )

        self.db.update_job_status(context.job_id, "CHUNKING", error=None)
        self._log("Stage CHUNK started.", job_id=context.job_id)
        chunks_dir = context.output_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)

        source_index = 0
        multi = len(classified) > 1
        for source_path, _, needs_chunking in classified:
            if needs_chunking:
                self._log(
                    f"Chunking '{source_path.name}' using format-specific chunking.",
                    job_id=context.job_id,
                )
                raw_chunks = self._chunk_document(source_path, context)
                self._log(
                    f"Chunking produced {len(raw_chunks)} part(s) for '{source_path.name}'.",
                    job_id=context.job_id,
                )
                for spec in raw_chunks:
                    source_index += 1
                    title = spec.title if not multi else f"{source_path.stem} - {spec.title}"
                    filename = (
                        f"{source_index:02d}_{slugify(title, fallback='chunk')}{spec.file_path.suffix.lower() or '.txt'}"
                    )
                    final_path = chunks_dir / filename
                    ensure_parent(final_path)
                    if spec.file_path.resolve() != final_path.resolve():
                        shutil.move(str(spec.file_path), str(final_path))
                    source_ref = self.db.create_source(
                        notebook_id=None,
                        title=title,
                        source_index=source_index,
                        file_path=str(final_path),
                        original_pdf=str(source_path),
                        page_range=spec.page_range,
                        is_chunk=True,
                    )
                    self.db.ensure_task(context.job_id, "UPLOAD", source_ref, status="QUEUED")
                temp_dir = context.output_dir / "chunks" / f".{slugify(source_path.stem, fallback='source')}_parts"
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
            else:
                self._log(
                    f"Skipping chunking for '{source_path.name}' (single source path).",
                    job_id=context.job_id,
                )
                source_index += 1
                title = source_path.stem
                filename = f"{source_index:02d}_{slugify(title, fallback='document')}{upload_extension_for_source(source_path)}"
                final_path = chunks_dir / filename
                materialize_single_source(source_path, final_path)
                source_ref = self.db.create_source(
                    notebook_id=None,
                    title=title,
                    source_index=source_index,
                    file_path=str(final_path),
                    original_pdf=str(source_path),
                    page_range=None,
                    is_chunk=False,
                )
                self.db.ensure_task(context.job_id, "UPLOAD", source_ref, status="QUEUED")
        upload_tasks = self.db.list_tasks(context.job_id, task_type="UPLOAD")
        self._log(f"Prepared {len(upload_tasks)} UPLOAD task(s).", job_id=context.job_id)

    def _chunk_document(self, source_path: Path, context: JobContext) -> list[ChunkSpec]:
        temp_dir = context.output_dir / "chunks" / f".{slugify(source_path.stem, fallback='source')}_parts"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        if source_path.suffix.lower() == ".pdf":
            self._log(
                f"Running PDF chunker for '{source_path.name}' (max_pages={context.config.max_pages_per_chunk}).",
                job_id=context.job_id,
            )
        else:
            self._log(
                f"Parsing EPUB structure for '{source_path.name}' (max_pages={context.config.max_pages_per_chunk}).",
                job_id=context.job_id,
            )
        return chunk_document(source_path, context.config.max_pages_per_chunk, temp_dir)

    def _stage_upload(self, context: JobContext) -> None:
        self.db.update_job_status(context.job_id, "UPLOADING", error=None)
        self._log("Stage UPLOAD started. Checking NotebookLM authentication.", job_id=context.job_id)
        self.nlm.ensure_authenticated()
        notebook = self.db.get_notebook_by_ref(context.notebook_ref)
        if not notebook:
            raise RuntimeError(f"Notebook registry record missing: {context.notebook_ref}")

        notebook_id = str(notebook["notebook_id"] or "")
        if not notebook_id:
            notebook_id = self.nlm.create_notebook(str(notebook["name"]))
            self.db.update_notebook_remote_id(context.notebook_ref, notebook_id)
            public_url = self.nlm.get_notebook_public_url(notebook_id)
            if public_url:
                self.db.update_notebook_public_url(context.notebook_ref, public_url)
            self._log(f"Created NotebookLM notebook: {notebook_id}.", job_id=context.job_id)
        else:
            self._log(f"Reusing existing NotebookLM notebook: {notebook_id}.", job_id=context.job_id)
        context.notebook_id = notebook_id

        tasks = self.db.list_tasks(context.job_id, task_type="UPLOAD")
        self._log(f"Uploading {len(tasks)} source file(s).", job_id=context.job_id)
        for task in tasks:
            task_id = str(task["id"])
            source_ref = str(task["target_ref"])
            source = self.db.get_source_by_ref(source_ref)
            if not source:
                self.db.update_task_status(task_id, "FAILED", error="Missing source record")
                self._log(f"UPLOAD task {task_id} failed: missing source record.", job_id=context.job_id)
                continue

            if source["source_id"]:
                self.db.update_task_status(task_id, "COMPLETED", error=None)
                self._log(
                    f"UPLOAD task {task_id} already completed (source_id={source['source_id']}).",
                    job_id=context.job_id,
                )
                continue

            if str(task["status"]) == "FAILED" and int(task["attempts"]) >= context.config.max_retries:
                self._log(
                    f"UPLOAD task {task_id} skipped after retry limit.",
                    job_id=context.job_id,
                )
                continue

            file_path = Path(str(source["file_path"]))
            if not file_path.exists():
                self.db.update_task_status(task_id, "FAILED", error=f"Missing chunk file: {file_path}")
                self._log(f"UPLOAD task {task_id} failed: missing file {file_path}.", job_id=context.job_id)
                continue

            self.db.update_task_status(task_id, "IN_PROGRESS", error=None, increment_attempt=True)
            try:
                self._log(
                    f"Uploading source '{source['title']}' from {file_path.name}.",
                    job_id=context.job_id,
                )
                source_id = self.nlm.add_file_source(notebook_id, file_path)
                self.db.update_source_uploaded(source_ref, notebook_id, source_id)
                self.db.update_task_status(task_id, "COMPLETED", error=None)
                self._log(
                    f"UPLOAD task {task_id} completed (source_id={source_id}).",
                    job_id=context.job_id,
                )
            except Exception as exc:
                self.db.update_task_status(task_id, "FAILED", error=str(exc))
                self._log(f"UPLOAD task {task_id} failed: {exc}", job_id=context.job_id)

        uploaded_sources = [row for row in self.db.list_sources_for_job(context.job_id) if row["source_id"]]
        self._log(
            f"Upload stage finished with {len(uploaded_sources)} successful source(s).",
            job_id=context.job_id,
        )
        if not uploaded_sources:
            raise RuntimeError("No sources were uploaded successfully. Job cannot continue.")

    def _run_generation_pipeline(self, context: JobContext) -> None:
        self._log("Stage GENERATE/POLL preparing. Checking NotebookLM authentication.", job_id=context.job_id)
        self.nlm.ensure_authenticated()
        self._stage_prepare_generation(context)
        asyncio.run(self._stage_generate_and_poll(context))

    def _stage_prepare_generation(self, context: JobContext) -> None:
        self.db.update_job_status(context.job_id, "GENERATING", error=None)
        artifact_types = context.config.resolve_artifacts(context.config_dict.get("artifact_types"))
        uploaded_sources = [row for row in self.db.list_sources_for_job(context.job_id) if row["source_id"]]
        self._log(
            f"Stage GENERATE started for {len(uploaded_sources)} source(s) with artifacts={artifact_types}.",
            job_id=context.job_id,
        )

        for source in uploaded_sources:
            source_id = str(source["source_id"])
            for artifact_type in artifact_types:
                fmt = self._format_detail(artifact_type, context.config)
                artifact_ref = self.db.create_artifact(
                    notebook_id=context.notebook_id,
                    source_id=source_id,
                    artifact_type=artifact_type,
                    format_detail=fmt,
                )
                task_id = self.db.ensure_task(context.job_id, "GENERATE", artifact_ref, status="QUEUED")
                task = self.db.get_task(task_id)
                if task and str(task["status"]) == "FAILED" and int(task["attempts"]) < context.config.max_retries:
                    if context.config.cleanup_failed_remote_artifacts:
                        self._cleanup_failed_remote_artifact(context, artifact_ref=artifact_ref, task_id=task_id)
                    self.db.update_task_status(task_id, "QUEUED", error=None)
                    self._log(
                        f"Re-queued GENERATE task {task_id} after previous failure.",
                        job_id=context.job_id,
                    )

        # Crash recovery: if a task was in-progress, keep polling it.
        for task in self.db.list_tasks(context.job_id, task_type="GENERATE", statuses=["IN_PROGRESS"]):
            if int(task["attempts"]) > context.config.max_retries:
                self.db.update_task_status(str(task["id"]), "FAILED", error="Exceeded retry budget")
                self._log(
                    f"Marked stale in-progress task {task['id']} as FAILED (retry budget exceeded).",
                    job_id=context.job_id,
                )

        queued = self.db.count_tasks(context.job_id, "GENERATE", "QUEUED")
        in_progress = self.db.count_tasks(context.job_id, "GENERATE", "IN_PROGRESS")
        completed = self.db.count_tasks(context.job_id, "GENERATE", "COMPLETED")
        self._log(
            f"Generate queue ready: queued={queued}, in_progress={in_progress}, completed={completed}.",
            job_id=context.job_id,
        )

    async def _stage_generate_and_poll(self, context: JobContext) -> None:
        pending = deque(self.db.list_tasks(context.job_id, task_type="GENERATE", statuses=["QUEUED"]))
        inflight = self._seed_inflight(context)
        self._log(
            f"Generation loop started with queued={len(pending)}, inflight={len(inflight)}, "
            f"max_concurrent={context.config.max_concurrent_generations}.",
            job_id=context.job_id,
        )

        while pending or inflight:
            open_slots = context.config.max_concurrent_generations - len(inflight)
            while pending and open_slots > 0:
                started_this_pass = False
                pending_count = len(pending)
                for _ in range(pending_count):
                    task = pending.popleft()
                    if self._has_generation_conflict(context, dict(task), inflight):
                        pending.append(task)
                        continue
                    started = self._start_generate_task(context, dict(task))
                    if started:
                        inflight[str(task["id"])] = time.monotonic()
                        started_this_pass = True
                        open_slots -= 1
                        if pending and open_slots > 0:
                            await asyncio.sleep(context.config.courtesy_delay_seconds)
                    break
                if not started_this_pass:
                    break

            if inflight:
                self.db.update_job_status(context.job_id, "POLLING", error=None)
                completed, failed = self._poll_inflight_once(context, inflight)
                for task_id in completed + failed:
                    inflight.pop(task_id, None)
                if completed or failed:
                    self._log(
                        f"Poll cycle update: completed={len(completed)}, failed={len(failed)}, remaining_inflight={len(inflight)}.",
                        job_id=context.job_id,
                    )
                if inflight:
                    await asyncio.sleep(context.config.poll_interval_seconds)

            # Pick up re-queued work if any.
            if not pending:
                queued_now = self.db.list_tasks(context.job_id, task_type="GENERATE", statuses=["QUEUED"])
                pending = deque(queued_now)
                if pending:
                    self._log(f"Detected {len(pending)} newly queued generation task(s).", job_id=context.job_id)

        self._log("Generation/POLL loop finished.", job_id=context.job_id)

    def _seed_inflight(self, context: JobContext) -> dict[str, float]:
        inflight: dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for task in self.db.list_tasks(context.job_id, task_type="GENERATE", statuses=["IN_PROGRESS"]):
            updated_at = str(task["updated_at"])
            started_at = self._parse_sqlite_timestamp(updated_at) or now
            elapsed = max(0.0, (now - started_at).total_seconds())
            inflight[str(task["id"])] = time.monotonic() - elapsed
        return inflight

    def _has_generation_conflict(
        self,
        context: JobContext,
        task: dict[str, Any],
        inflight: dict[str, float],
    ) -> bool:
        artifact = self.db.get_artifact_by_ref(str(task["target_ref"]))
        if not artifact:
            return False

        source_id = str(artifact["source_id"] or "")
        remote_type = remote_artifact_type(str(artifact["artifact_type"]))
        if not source_id:
            return False

        for inflight_task_id in inflight:
            inflight_task = self.db.get_task(inflight_task_id)
            if not inflight_task:
                continue
            inflight_artifact = self.db.get_artifact_by_ref(str(inflight_task["target_ref"]))
            if not inflight_artifact:
                continue
            if str(inflight_artifact["source_id"] or "") != source_id:
                continue
            if remote_artifact_type(str(inflight_artifact["artifact_type"])) != remote_type:
                continue
            self._log(
                f"Delaying GENERATE task {task['id']} because another {remote_type} artifact for source_id={source_id} is still in progress.",
                job_id=context.job_id,
            )
            return True
        return False

    def _start_generate_task(self, context: JobContext, task: dict[str, Any]) -> bool:
        task_id = str(task["id"])
        artifact_ref = str(task["target_ref"])
        attempts = int(task["attempts"])
        if attempts >= context.config.max_retries:
            self.db.update_task_status(task_id, "FAILED", error="Exceeded retry budget")
            self._log(f"GENERATE task {task_id} failed: exceeded retry budget.", job_id=context.job_id)
            return False

        artifact = self.db.get_artifact_by_ref(artifact_ref)
        if not artifact:
            self.db.update_task_status(task_id, "FAILED", error="Missing artifact record")
            self._log(f"GENERATE task {task_id} failed: missing artifact record.", job_id=context.job_id)
            return False
        source_id = str(artifact["source_id"] or "")
        if not source_id:
            self.db.update_task_status(task_id, "FAILED", error="Missing source ID for artifact")
            self._log(f"GENERATE task {task_id} failed: missing source ID.", job_id=context.job_id)
            return False

        self.db.update_task_status(task_id, "IN_PROGRESS", error=None, increment_attempt=True)
        artifact_type = str(artifact["artifact_type"])
        try:
            self._log(
                f"Starting GENERATE task {task_id}: artifact_type={artifact_type}, source_id={source_id}.",
                job_id=context.job_id,
            )
            artifact_id = self.nlm.create_artifact(
                context.notebook_id,
                artifact_type=artifact_type,
                source_id=source_id,
                report_format=context.config.report_format,
                note_prompt=context.config.note_prompt,
            )
            if artifact_id:
                self.db.update_artifact_remote_id(artifact_ref, artifact_id)
                self._log(
                    f"GENERATE task {task_id} accepted by NotebookLM (artifact_id={artifact_id}).",
                    job_id=context.job_id,
                )
            else:
                self._log(
                    f"GENERATE task {task_id} accepted; awaiting artifact ID from status polling.",
                    job_id=context.job_id,
                )
            return True
        except Exception as exc:
            self.db.update_task_status(task_id, "FAILED", error=str(exc))
            self._log(f"GENERATE task {task_id} failed: {exc}", job_id=context.job_id)
            return False

    def _poll_inflight_once(self, context: JobContext, inflight: dict[str, float]) -> tuple[list[str], list[str]]:
        try:
            statuses = self.nlm.list_studio_status(context.notebook_id)
        except Exception as exc:
            failed = []
            for task_id in inflight:
                self.db.update_task_status(task_id, "FAILED", error=f"Polling failed: {exc}")
                self._log(f"POLL failed for task {task_id}: {exc}", job_id=context.job_id)
                failed.append(task_id)
            return [], failed

        status_by_id = {item.artifact_id: item for item in statuses}
        known_ids = self.db.known_artifact_ids(context.notebook_id)

        completed: list[str] = []
        failed: list[str] = []
        now_mono = time.monotonic()

        for task_id, started_mono in inflight.items():
            task = self.db.get_task(task_id)
            if not task:
                failed.append(task_id)
                continue

            artifact = self.db.get_artifact_by_ref(str(task["target_ref"]))
            if not artifact:
                self.db.update_task_status(task_id, "FAILED", error="Missing artifact record")
                failed.append(task_id)
                continue

            artifact_id = str(artifact["artifact_id"] or "")
            source_id = str(artifact["source_id"] or "")
            artifact_type = str(artifact["artifact_type"])
            elapsed = now_mono - started_mono

            if not artifact_id:
                candidate = self._resolve_artifact_candidate(
                    statuses=statuses,
                    artifact_type=artifact_type,
                    source_id=source_id,
                    used_ids=known_ids,
                )
                if candidate:
                    artifact_id = candidate.artifact_id
                    known_ids.add(artifact_id)
                    self.db.update_artifact_remote_id(str(artifact["id"]), artifact_id)
                    self._log(
                        f"POLL mapped missing artifact ID for task {task_id}: {artifact_id}.",
                        job_id=context.job_id,
                    )

            if not artifact_id:
                if elapsed > context.config.poll_max_wait_seconds:
                    self.db.update_task_status(task_id, "FAILED", error="Timed out waiting for artifact ID")
                    self._log(
                        f"POLL timed out waiting for artifact ID for task {task_id}.",
                        job_id=context.job_id,
                    )
                    failed.append(task_id)
                continue

            status = status_by_id.get(artifact_id)
            if not status:
                if elapsed > context.config.poll_max_wait_seconds:
                    self.db.update_task_status(task_id, "FAILED", error="Timed out waiting for artifact status")
                    self._log(
                        f"POLL timed out waiting for artifact status for task {task_id} (artifact_id={artifact_id}).",
                        job_id=context.job_id,
                    )
                    failed.append(task_id)
                continue

            token = status.status.lower().strip()
            if token in TERMINAL_GENERATION_SUCCESS:
                self.db.update_task_status(task_id, "COMPLETED", error=None)
                self.db.ensure_task(context.job_id, "DOWNLOAD", str(artifact["id"]), status="QUEUED")
                self._log(
                    f"POLL completed task {task_id} (artifact_id={artifact_id}, status={token}).",
                    job_id=context.job_id,
                )
                completed.append(task_id)
                continue
            if token in TERMINAL_GENERATION_FAILURE:
                self.db.update_task_status(task_id, "FAILED", error=f"NotebookLM artifact status: {token}")
                self._log(
                    f"POLL marked task {task_id} FAILED (artifact_id={artifact_id}, status={token}).",
                    job_id=context.job_id,
                )
                failed.append(task_id)
                continue
            if elapsed > context.config.poll_max_wait_seconds:
                self.db.update_task_status(task_id, "FAILED", error=f"Timed out while waiting (status={token or 'unknown'})")
                self._log(
                    f"POLL timed out task {task_id} (artifact_id={artifact_id}, status={token or 'unknown'}).",
                    job_id=context.job_id,
                )
                failed.append(task_id)

        return completed, failed

    def _resolve_artifact_candidate(
        self,
        *,
        statuses: list[StudioArtifact],
        artifact_type: str,
        source_id: str,
        used_ids: set[str],
    ) -> StudioArtifact | None:
        normalized_type = normalize_artifact_type(artifact_type)
        remote_type = remote_artifact_type(normalized_type)
        matches: list[StudioArtifact] = []
        for item in statuses:
            if item.artifact_id in used_ids:
                continue
            if item.artifact_type and remote_artifact_type(item.artifact_type) != remote_type:
                continue
            if item.source_ids and source_id not in item.source_ids:
                continue
            matches.append(item)
        if not matches:
            return None
        return matches[-1]

    def _cleanup_failed_remote_artifact(self, context: JobContext, *, artifact_ref: str, task_id: str) -> None:
        artifact = self.db.get_artifact_by_ref(artifact_ref)
        if not artifact:
            return
        artifact_id = str(artifact["artifact_id"] or "")
        if not artifact_id:
            return

        try:
            removed = self.nlm.delete_artifact(context.notebook_id, artifact_id, ignore_missing=True)
            self.db.clear_artifact_remote_id(artifact_ref)
            if removed:
                self._log(
                    f"Deleted failed remote artifact {artifact_id} before retrying GENERATE task {task_id}.",
                    job_id=context.job_id,
                )
            else:
                self._log(
                    f"Remote artifact {artifact_id} already absent before retrying GENERATE task {task_id}.",
                    job_id=context.job_id,
                )
        except Exception as exc:
            self._log(
                f"Could not delete failed remote artifact {artifact_id} before retrying task {task_id}: {exc}",
                job_id=context.job_id,
            )

    def _stage_download(self, context: JobContext) -> None:
        self.db.update_job_status(context.job_id, "DOWNLOADING", error=None)
        self._log("Stage DOWNLOAD started. Checking NotebookLM authentication.", job_id=context.job_id)
        self.nlm.ensure_authenticated()

        for task in self.db.list_tasks(context.job_id, task_type="GENERATE", statuses=["COMPLETED"]):
            self.db.ensure_task(context.job_id, "DOWNLOAD", str(task["target_ref"]), status="QUEUED")

        download_tasks = self.db.list_tasks(context.job_id, task_type="DOWNLOAD")
        self._log(f"Processing {len(download_tasks)} DOWNLOAD task(s).", job_id=context.job_id)
        for task in download_tasks:
            task_id = str(task["id"])
            status = str(task["status"])
            attempts = int(task["attempts"])

            if status == "FAILED" and attempts >= context.config.max_retries:
                self._log(
                    f"DOWNLOAD task {task_id} skipped after retry limit.",
                    job_id=context.job_id,
                )
                continue

            artifact = self.db.get_artifact_by_ref(str(task["target_ref"]))
            if not artifact:
                self.db.update_task_status(task_id, "FAILED", error="Missing artifact record")
                self._log(f"DOWNLOAD task {task_id} failed: missing artifact record.", job_id=context.job_id)
                continue

            artifact_id = str(artifact["artifact_id"] or "")
            if not artifact_id:
                self.db.update_task_status(task_id, "FAILED", error="Missing remote artifact ID")
                self._log(f"DOWNLOAD task {task_id} failed: missing artifact ID.", job_id=context.job_id)
                continue

            existing_path = str(artifact["download_path"] or "")
            if existing_path and Path(existing_path).exists():
                self.db.update_task_status(task_id, "COMPLETED", error=None)
                self._log(
                    f"DOWNLOAD task {task_id} already complete at {existing_path}.",
                    job_id=context.job_id,
                )
                continue

            output_path = self._artifact_output_path(output_dir=context.output_dir, artifact=dict(artifact))
            self.db.update_task_status(task_id, "IN_PROGRESS", error=None, increment_attempt=True)
            try:
                self._log(
                    f"Downloading artifact {artifact_id} ({artifact['artifact_type']}) to {output_path}.",
                    job_id=context.job_id,
                )
                self.nlm.download_artifact(
                    context.notebook_id,
                    str(artifact["artifact_type"]),
                    artifact_id,
                    output_path,
                )
                self.db.update_artifact_download_path(str(artifact["id"]), str(output_path))
                self.db.update_task_status(task_id, "COMPLETED", error=None)
                self._log(f"DOWNLOAD task {task_id} completed.", job_id=context.job_id)
            except Exception as exc:
                self.db.update_task_status(task_id, "FAILED", error=str(exc))
                self._log(f"DOWNLOAD task {task_id} failed: {exc}", job_id=context.job_id)

    def _artifact_output_path(self, *, output_dir: Path, artifact: dict[str, Any]) -> Path:
        source_id = str(artifact.get("source_id") or "")
        source_label = "notebook"
        source_record: dict[str, Any] | None = None
        if source_id:
            source = self.db.get_source_by_remote_id(source_id)
            if source:
                source_record = dict(source)
                source_label = self._source_output_label(source_record)

        artifact_type = normalize_artifact_type(str(artifact["artifact_type"]))
        filename = self._artifact_filename(artifact_type, source=source_record)
        return output_dir / "artifacts" / source_label / filename

    def _source_output_label(self, source: dict[str, Any]) -> str:
        return f"{int(source['source_index']):02d}_{slugify(str(source['title'] or 'source'))}"

    def _artifact_source_stem(self, source: dict[str, Any] | None) -> str:
        if not source:
            return "notebook"

        original_path = Path(str(source.get("original_pdf") or source.get("file_path") or "source"))
        original_slug = slugify(original_path.stem or "source", fallback="source")
        title_slug = slugify(str(source.get("title") or original_path.stem or "source"), fallback="source")

        prefix = f"{int(source['source_index']):02d}_{original_slug}"
        if title_slug == original_slug:
            return prefix
        return f"{prefix}__{title_slug}"

    def _artifact_filename(self, artifact_type: str, *, source: dict[str, Any] | None = None) -> str:
        base = self._artifact_source_stem(source)
        if artifact_type == "report":
            return f"{base}__study_guide.md"
        if artifact_type == "note":
            return f"{base}__reading_note.md"
        if artifact_type == "slide_deck":
            return f"{base}__slides.txt"
        if artifact_type == "audio":
            return f"{base}__podcast.mp3"
        if artifact_type == "video":
            return f"{base}__video.mp4"
        if artifact_type == "quiz":
            return f"{base}__quiz.json"
        if artifact_type == "flashcards":
            return f"{base}__flashcards.json"
        if artifact_type == "mind_map":
            return f"{base}__mind_map.txt"
        if artifact_type == "infographic":
            return f"{base}__infographic.png"
        return f"{base}__{artifact_type}.bin"

    def _format_detail(self, artifact_type: str, cfg: EngineConfig) -> str | None:
        normalized = normalize_artifact_type(artifact_type)
        if normalized == "report":
            return cfg.report_format
        if normalized == "note":
            return "Create Your Own"
        return None

    def _resolve_notebook(self, identifier: str) -> dict[str, Any]:
        notebook = self.db.find_notebook(identifier)
        if not notebook:
            raise ValueError(f"Notebook not found: {identifier}")
        if not notebook["notebook_id"]:
            raise ValueError(f"Notebook has no NotebookLM ID yet: {identifier}")
        return dict(notebook)

    def _validate_inputs(self, input_paths: list[str]) -> list[Path]:
        if not input_paths:
            raise ValueError("At least one input path is required.")
        result: list[Path] = []
        for item in input_paths:
            path = Path(item).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"Input file not found: {path}")
            if not path.is_file():
                raise ValueError(f"Input path is not a file: {path}")
            if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
                supported = ", ".join(sorted(SUPPORTED_INPUT_SUFFIXES))
                raise ValueError(f"Unsupported input type for {path}. Expected one of: {supported}")
            result.append(path)
        return result

    def _infer_doc_type(self, paths: list[Path], page_counts: list[int], chunk_threshold: int) -> str:
        if len(paths) > 1:
            return "batch"
        pages = page_counts[0]
        if pages >= chunk_threshold:
            return "book"
        if pages <= 15:
            return "article"
        return "paper"

    def _write_job_summary(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        config = json.loads(job["config"])
        output_dir = Path(config.get("output_dir") or self.output_root / slugify(job_id))
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path = output_dir / "job_summary.json"

        notebook = self.db.get_notebook_by_ref(str(job["notebook_ref"])) if job["notebook_ref"] else None
        sources = self.db.list_sources_for_job(job_id)
        artifacts = self.db.list_artifacts_for_job(job_id)
        task_counts = self.db.task_counts(job_id)

        downloaded = sum(1 for row in artifacts if row["download_path"])
        summary = {
            "job_id": job_id,
            "status": str(job["status"]),
            "error": job["error"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "output_dir": str(output_dir),
            "notebook": {
                "local_id": notebook["id"] if notebook else None,
                "notebook_id": notebook["notebook_id"] if notebook else None,
                "name": notebook["name"] if notebook else None,
                "public_url": notebook["public_url"] if notebook else None,
            },
            "sources": {
                "total": len(sources),
                "uploaded": sum(1 for row in sources if row["source_id"]),
            },
            "artifacts": {
                "total": len(artifacts),
                "downloaded": downloaded,
            },
            "task_counts": task_counts,
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
        self._log(f"Wrote job summary to {summary_path}.", job_id=job_id)
        return summary

    @staticmethod
    def _parse_sqlite_timestamp(value: str) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _apply_nlm_runtime_config(self, cfg: EngineConfig) -> None:
        self.nlm.backoff_base_seconds = max(1, int(cfg.backoff_base_seconds))
        self.nlm.backoff_multiplier = max(1, int(cfg.backoff_multiplier))
        self.nlm.max_retries = max(1, int(cfg.max_retries))
        lo, hi = cfg.nlm_request_interval_range
        self.nlm._request_interval_range = (max(0.0, float(lo)), max(0.0, float(hi)))
