"""
The agent's run loop. Two phases per invocation:

  Phase A — INTAKE: walk inbox, classify each new file, dedup, move to library, add to catalog.
  Phase B — DRAIN: walk catalog, for each unfinished resource:
                     - write resources/<id>.json if missing (split decided here)
                     - advance every task by ONE step (kick off / verify / retry-once / fail)

See DESIGN_V2.md for the full spec.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import catalog as cat
from .catalog import Catalog, ResourceFile, make_source_record
from .chunking import (
    ChunkSpec,
    chunk_document,
    input_type,
    materialize_single_source,
)
from .classify import ClassificationResult, classify_file, compute_fingerprint
from .config import AppConfig
from .nlm_client import NLMAuthError, NLMCLI, NLMError
from .utils import utc_now_iso


SUPPORTED_INPUT_SUFFIXES = {".pdf", ".epub"}
DUPS_DIRNAME = "_dups"
INBOX_LOG_FILE = "_log.txt"

# Terminal NLM artifact states (lowercased).
_NLM_DONE_STATES = {"completed", "ready", "succeeded", "success", "done"}
_NLM_FAILED_STATES = {"failed", "error", "errored"}


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    discovered: int = 0
    intake_duplicates: int = 0
    intake_failed: int = 0
    resource_files_created: int = 0
    tasks_kicked_off: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_retried: int = 0
    auth_aborted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "intake_duplicates": self.intake_duplicates,
            "intake_failed": self.intake_failed,
            "resource_files_created": self.resource_files_created,
            "tasks_kicked_off": self.tasks_kicked_off,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_retried": self.tasks_retried,
            "auth_aborted": self.auth_aborted,
            "notes": list(self.notes),
        }


Logger = Callable[[str], None]


def _default_logger(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Logger | None = None,
        nlm_client: NLMCLI | None = None,
    ):
        self.config = config
        self.logger = logger or _default_logger
        paths = config.resolved_paths()
        self.inbox = paths["inbox"]
        self.library = paths["library"]
        self.metadata = paths["metadata"]
        self.nlm = nlm_client or NLMCLI(
            backoff_base_seconds=config.nlm.backoff_base_seconds,
            backoff_multiplier=config.nlm.backoff_multiplier,
            max_retries=0,  # we manage retries; let nlm_client surface errors immediately
            request_interval_range=tuple(config.nlm.request_interval_range),
            logger=self.logger,
        )

    # ---- top-level run -----------------------------------------------------

    def run(self) -> RunSummary:
        summary = RunSummary()
        self.metadata.mkdir(parents=True, exist_ok=True)
        catalog = Catalog.load(self.metadata)

        try:
            self._phase_a_intake(catalog, summary)
            self._phase_b_drain(catalog, summary)
        except NLMAuthError as exc:
            self.logger(f"NLM auth error: {exc}")
            summary.auth_aborted = True
            summary.notes.append(f"NLM auth error: {exc}")

        catalog.save()
        return summary

    # ---- Phase A: inbox → catalog -----------------------------------------

    def _phase_a_intake(self, catalog: Catalog, summary: RunSummary) -> None:
        for path in _walk_inbox(self.inbox):
            try:
                self._intake_one(catalog, path, summary)
            except NLMAuthError:
                raise
            except Exception as exc:
                self.logger(f"intake failed for {path.name}: {exc}")
                summary.intake_failed += 1
                self._append_inbox_log(f"{path.name}: intake failed: {exc}")

    def _intake_one(self, catalog: Catalog, path: Path, summary: RunSummary) -> None:
        self.logger(f"intake: {path.name}")
        fingerprint = compute_fingerprint(path)

        # 1. Cheap exact-byte dedup
        sha_match = catalog.find_by_sha(fingerprint.sha256)
        if sha_match is not None:
            self._move_to_dups(path, suffix_id="sha")
            self._append_inbox_log(
                f"{path.name}: duplicate (sha256 match) of {sha_match['id']}"
            )
            summary.intake_duplicates += 1
            return

        # 2. LLM classify (also asks for dup judgment + ToC + all metadata)
        result = classify_file(
            file_path=path,
            fingerprint=fingerprint,
            catalog=catalog,
            llm=self.config.llm,
        )

        if result.is_duplicate:
            self._move_to_dups(path, suffix_id=result.duplicate_of or "dup")
            self._append_inbox_log(
                f"{path.name}: duplicate of {result.duplicate_of} → {result.reason or 'no reason'}"
            )
            summary.intake_duplicates += 1
            return

        # 3. Decide library-relative path, move file, register catalog row
        rel_path = _build_library_rel_path(result.category, path.name)
        target_abs = self.library / rel_path
        target_abs.parent.mkdir(parents=True, exist_ok=True)
        if target_abs.exists():
            # Defensive: name collision in library. Tag with sha prefix to disambiguate.
            stem = target_abs.stem
            target_abs = target_abs.with_name(f"{stem}__{fingerprint.sha256[:8]}{target_abs.suffix}")
            rel_path = str(target_abs.relative_to(self.library))
        shutil.move(str(path), str(target_abs))

        entry = catalog.add_resource(
            sha256=fingerprint.sha256,
            title=result.title,
            authors=result.authors,
            kind=result.kind,
            category=result.category,
            library_path=rel_path,
            toc=result.toc,
            page_count=fingerprint.page_count,
        )
        catalog.save()
        summary.discovered += 1
        self._append_inbox_log(
            f"{path.name}: classified as {result.kind} → {'/'.join(result.category)} "
            f"(id={entry['id']}, title={result.title!r})"
        )

    # ---- Phase B: catalog → tasks -----------------------------------------

    def _phase_b_drain(self, catalog: Catalog, summary: RunSummary) -> None:
        for entry in list(catalog.list_unfinished()):
            try:
                self._advance_resource(catalog, entry, summary)
            except NLMAuthError:
                raise
            except Exception as exc:
                self.logger(f"[{entry['id']}] drain error: {exc}")
                summary.notes.append(f"{entry['id']} drain error: {exc}")
            catalog.save()

    def _advance_resource(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        # 1. Ensure the resource file exists. Create on first encounter (split if needed).
        if not ResourceFile.exists(self.metadata, entry["id"]):
            rf = self._create_resource_file(entry)
            summary.resource_files_created += 1
            catalog.set_overall_status(entry, cat.IN_PROGRESS)
        else:
            rf = ResourceFile.load(self.metadata, entry["id"])
            if entry["overall_status"] == cat.NEW:
                catalog.set_overall_status(entry, cat.IN_PROGRESS)

        # 2. Walk every source. Advance the upload task; if upload is DONE, advance artifact tasks.
        for source in rf.sources:
            upload_task = _find_task(source, "upload")
            if upload_task is None:
                continue
            self._advance_upload_task(rf, source, upload_task, summary)
            rf.save()
            if upload_task["state"] != cat.TASK_DONE:
                continue
            for task in source.get("tasks", []):
                if task is upload_task:
                    continue
                self._advance_artifact_task(rf, source, task, summary)
                rf.save()

        # 3. Roll up resource overall status
        if rf.all_tasks_done():
            catalog.set_overall_status(entry, cat.DONE)
        elif rf.any_task_failed():
            catalog.set_overall_status(entry, cat.FAILED)
        else:
            catalog.set_overall_status(entry, cat.IN_PROGRESS)

    # ---- creating a resource file (split decision lives here) -------------

    def _create_resource_file(self, entry: dict[str, Any]) -> ResourceFile:
        kind = entry.get("kind") or "other"
        title = entry.get("title") or entry["id"]
        category = entry.get("category") or ["uncategorized", "other"]
        rel_library_path = entry["library_path"]
        original_abs = self.library / rel_library_path
        page_count = int(entry.get("page_count") or 0)

        should_split = (
            kind == "book"
            and page_count >= self.config.split.min_pages_to_split
        )

        # Decide notebook role. We do NOT create the actual NLM notebook here —
        # it gets created lazily when the first upload task runs (so a Phase B with
        # no NLM auth doesn't burn a notebook id).
        # We just remember the policy by leaving notebook_id = None for now.

        sources: list[dict[str, Any]]
        if should_split:
            book_dir = original_abs.parent / _safe_segment(title)
            book_dir.mkdir(parents=True, exist_ok=True)
            specs = chunk_document(
                original_abs,
                self.config.split.max_pages_per_chunk,
                book_dir,
            )
            # Move original alongside the chunks (after chunking, so the chunker's
            # output_dir starts empty).
            new_original_abs = book_dir / original_abs.name
            if original_abs.resolve() != new_original_abs.resolve():
                if not new_original_abs.exists():
                    shutil.move(str(original_abs), str(new_original_abs))
            entry["library_path"] = str(new_original_abs.relative_to(self.library))
            sources = [
                make_source_record(
                    idx=index,
                    title=spec.title,
                    library_path=str(spec.file_path.relative_to(self.library)),
                    page_range=spec.page_range,
                    artifact_types=self.config.artifacts,
                )
                for index, spec in enumerate(specs, start=1)
            ]
        else:
            # Single resource. For EPUB, materialize a .txt sibling for upload.
            if input_type(original_abs) == "epub":
                upload_abs = original_abs.with_suffix(".txt")
                spec = materialize_single_source(original_abs, upload_abs)
                upload_rel = str(upload_abs.relative_to(self.library))
            else:
                spec = ChunkSpec(
                    title=title,
                    file_path=original_abs,
                    original_pdf=original_abs,
                    page_range=None,
                    is_chunk=False,
                )
                upload_rel = rel_library_path
            sources = [
                make_source_record(
                    idx=1,
                    title=spec.title,
                    library_path=upload_rel,
                    page_range=spec.page_range,
                    artifact_types=self.config.artifacts,
                )
            ]

        return ResourceFile.create(
            self.metadata,
            entry["id"],
            notebook_id=None,
            sources=sources,
        )

    # ---- task advancement (single step) -----------------------------------

    def _advance_upload_task(
        self,
        rf: ResourceFile,
        source: dict[str, Any],
        task: dict[str, Any],
        summary: RunSummary,
    ) -> None:
        state = task["state"]
        if state in {cat.TASK_DONE, cat.TASK_FAILED}:
            return

        if state == cat.NOT_STARTED:
            self._set_task_state(task, cat.PROCESSING)
            try:
                notebook_id = self._ensure_notebook(rf)
                upload_path = self.library / source["library_path"]
                source_id = self.nlm.add_file_source(notebook_id, upload_path)
            except NLMError as exc:
                self._fail_task_attempt(task, summary, str(exc))
                return
            source["nlm_source_id"] = source_id
            self._set_task_state(task, cat.TASK_DONE)
            summary.tasks_kicked_off += 1
            summary.tasks_completed += 1
            return

        # state == PROCESSING — one retry per run.
        try:
            notebook_id = self._ensure_notebook(rf)
            upload_path = self.library / source["library_path"]
            source_id = self.nlm.add_file_source(notebook_id, upload_path)
        except NLMError as exc:
            self._fail_task_attempt(task, summary, str(exc))
            return
        source["nlm_source_id"] = source_id
        self._set_task_state(task, cat.TASK_DONE)
        summary.tasks_retried += 1
        summary.tasks_completed += 1

    def _advance_artifact_task(
        self,
        rf: ResourceFile,
        source: dict[str, Any],
        task: dict[str, Any],
        summary: RunSummary,
    ) -> None:
        state = task["state"]
        if state in {cat.TASK_DONE, cat.TASK_FAILED}:
            return

        notebook_id = rf.notebook_id
        source_id = source.get("nlm_source_id")
        if not notebook_id or not source_id:
            # Upload not done yet — should not happen; defensive no-op.
            return

        if state == cat.NOT_STARTED:
            try:
                artifact_id = self.nlm.create_artifact(
                    notebook_id,
                    task["type"],
                    source_id,
                    report_format="Study Guide",
                    note_prompt=self.config.note_prompt,
                )
            except NLMError as exc:
                self._set_task_state(task, cat.PROCESSING)
                self._fail_task_attempt(task, summary, str(exc))
                return
            if artifact_id:
                task["nlm_artifact_id"] = artifact_id
                self._set_task_state(task, cat.PROCESSING)
                summary.tasks_kicked_off += 1
            else:
                self._set_task_state(task, cat.PROCESSING)
                self._fail_task_attempt(task, summary, "no artifact id returned")
            return

        # state == PROCESSING — verify, then either retry once or wait.
        nlm_artifact_id = task.get("nlm_artifact_id")
        try:
            statuses = self.nlm.list_studio_status(notebook_id)
        except NLMError as exc:
            self.logger(f"  verify failed for {task['type']}: {exc}")
            return

        live = next((s for s in statuses if s.artifact_id == nlm_artifact_id), None)
        if live is None:
            # Still scheduling on NLM's side — leave PROCESSING, no counter change.
            return

        live_state = (live.status or "").lower()
        if live_state in _NLM_DONE_STATES:
            self._set_task_state(task, cat.TASK_DONE)
            task["url"] = self.nlm.notebook_url(notebook_id)
            summary.tasks_completed += 1
            return
        if live_state in _NLM_FAILED_STATES:
            self._fail_task_attempt(
                task,
                summary,
                f"NLM reported state={live_state}",
                retry_action=lambda: self.nlm.create_artifact(
                    notebook_id,
                    task["type"],
                    source_id,
                    report_format="Study Guide",
                    note_prompt=self.config.note_prompt,
                ),
                on_retry_artifact_id=lambda new_id: task.update({"nlm_artifact_id": new_id}),
            )
            return
        # live_state is something like "pending" / "in_progress" — keep waiting.

    # ---- helpers for state transitions ------------------------------------

    def _set_task_state(self, task: dict[str, Any], state: str) -> None:
        task["state"] = state
        task["updated_at"] = utc_now_iso()

    def _fail_task_attempt(
        self,
        task: dict[str, Any],
        summary: RunSummary,
        error: str,
        *,
        retry_action: Callable[[], str | None] | None = None,
        on_retry_artifact_id: Callable[[str], None] | None = None,
    ) -> None:
        """
        Record a single NLM-reported failure on this task.

        Increments retry_count. If the new count == max_retries, marks task FAILED.
        Otherwise, optionally re-issues the underlying command this run (one retry per run);
        the task stays PROCESSING.
        """
        max_retries = max(1, int(self.config.nlm.max_retries))
        task["retry_count"] = int(task.get("retry_count") or 0) + 1
        task["last_error"] = error[:500]
        if task["retry_count"] >= max_retries:
            self._set_task_state(task, cat.TASK_FAILED)
            summary.tasks_failed += 1
            return
        if retry_action is not None:
            try:
                new_id = retry_action()
                if new_id and on_retry_artifact_id:
                    on_retry_artifact_id(new_id)
                summary.tasks_retried += 1
            except NLMError as exc:
                # second failure in the same run still counts as one attempt; stay PROCESSING.
                task["last_error"] = f"retry failed: {exc}"[:500]

    def _ensure_notebook(self, rf: ResourceFile) -> str:
        if rf.notebook_id:
            return rf.notebook_id
        entry = Catalog.load(self.metadata).find_by_id(rf.resource_id)
        if entry is None:
            raise RuntimeError(f"catalog entry missing for {rf.resource_id}")
        kind = entry.get("kind") or "other"
        title = entry.get("title") or rf.resource_id
        category = entry.get("category") or ["uncategorized", "other"]
        if kind == "book":
            notebook_id = self.nlm.create_notebook(title)
            self._record_notebook(rf.resource_id, notebook_id, title, "book", category)
        else:
            notebook_id = self._find_or_create_bucket(category)
        rf.notebook_id = notebook_id
        return notebook_id

    def _find_or_create_bucket(self, category: list[str]) -> str:
        # We don't keep a separate "notebooks" registry in v2; instead, scan existing
        # bucket notebooks by querying NLM. We also avoid creating duplicate buckets by
        # listing remote notebooks and matching by title prefix.
        bucket_label = self._bucket_label(category)
        try:
            remote = self.nlm.list_notebooks()
        except NLMError:
            remote = []
        candidates = []
        for nb in remote:
            title = str(nb.get("title") or nb.get("name") or "")
            if title == bucket_label or title.startswith(bucket_label + " "):
                nb_id = nb.get("id") or nb.get("notebook_id") or nb.get("notebookId")
                if nb_id:
                    candidates.append((title, str(nb_id)))
        # Pick the first bucket with capacity.
        for title, nb_id in candidates:
            try:
                count = len(self.nlm.list_sources(nb_id))
            except NLMError:
                continue
            if count < self.config.bucket_capacity:
                return nb_id
        slot = len(candidates) + 1
        new_title = bucket_label if slot == 1 else f"{bucket_label} {slot}"
        return self.nlm.create_notebook(new_title)

    def _bucket_label(self, category: list[str]) -> str:
        if len(category) >= 2:
            label_root = category[-2]
            type_bucket = category[-1]
        else:
            label_root = category[0] if category else "Misc"
            type_bucket = "Items"
        return f"{label_root} {type_bucket.title()}"

    def _record_notebook(
        self,
        resource_id: str,
        notebook_id: str,
        title: str,
        role: str,
        category: list[str],
    ) -> None:
        # v2 doesn't keep a separate notebooks registry; the resource file stores the
        # notebook_id and that's enough. This helper exists in case we want to add a
        # registry later — for now, intentionally a no-op.
        return

    # ---- inbox helpers ----------------------------------------------------

    def _move_to_dups(self, path: Path, *, suffix_id: str) -> None:
        if not path.exists():
            return
        dups_dir = self.inbox / DUPS_DIRNAME
        dups_dir.mkdir(parents=True, exist_ok=True)
        target = dups_dir / f"{suffix_id}__{path.name}"
        # Disambiguate if target exists.
        i = 1
        while target.exists():
            target = dups_dir / f"{suffix_id}__{i}__{path.name}"
            i += 1
        shutil.move(str(path), str(target))

    def _append_inbox_log(self, message: str) -> None:
        log_path = self.inbox / INBOX_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{utc_now_iso()}] {message}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _walk_inbox(inbox: Path) -> list[Path]:
    """Walk inbox/ for supported files. Skip _dups/, _log.txt, and dotfiles."""
    if not inbox.exists():
        return []
    found: list[Path] = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            continue
        try:
            rel_parts = path.relative_to(inbox).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] == DUPS_DIRNAME:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        found.append(path)
    return found


def _build_library_rel_path(category: list[str], filename: str) -> str:
    parts = [_safe_segment(seg) for seg in category]
    parts.append(filename)
    return "/".join(parts)


def _safe_segment(value: str) -> str:
    text = value.strip()
    text = text.replace("/", "_").replace("\\", "_").replace(":", "_")
    text = text.strip(". ")
    return text or "untitled"


def _find_task(source: dict[str, Any], task_type: str) -> dict[str, Any] | None:
    for task in source.get("tasks", []):
        if task.get("type") == task_type:
            return task
    return None
