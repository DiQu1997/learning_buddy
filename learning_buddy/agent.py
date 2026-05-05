from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog, commit_catalog, ensure_repo, push
from .chunking import (
    ChunkSpec,
    chunk_document,
    count_document_units,
    input_type,
    materialize_single_source,
)
from .classify import ClassificationResult, classify_file, compute_fingerprint
from .config import AppConfig, normalize_artifact_type
from .nlm_client import NLMAuthError, NLMCLI, NLMError
from .render import render_outline
from .utils import utc_now_iso


SUPPORTED_INPUT_SUFFIXES = {".pdf", ".epub"}

DUPS_DIRNAME = "_dups"
INBOX_LOG_FILE = "_log.txt"


@dataclass
class RunSummary:
    discovered: int = 0
    classified: int = 0
    duplicates: int = 0
    chunked: int = 0
    uploaded: int = 0
    artifacts_completed: int = 0
    artifacts_failed: int = 0
    files_done: int = 0
    files_failed: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "classified": self.classified,
            "duplicates": self.duplicates,
            "chunked": self.chunked,
            "uploaded": self.uploaded,
            "artifacts_completed": self.artifacts_completed,
            "artifacts_failed": self.artifacts_failed,
            "files_done": self.files_done,
            "files_failed": self.files_failed,
            "notes": list(self.notes),
        }


Logger = Callable[[str], None]


def _default_logger(message: str) -> None:
    print(message, flush=True)


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
        self.database = paths["database"]
        self.nlm = nlm_client or NLMCLI(
            backoff_base_seconds=config.nlm.backoff_base_seconds,
            backoff_multiplier=config.nlm.backoff_multiplier,
            max_retries=config.nlm.max_retries,
            request_interval_range=tuple(config.nlm.request_interval_range),
            logger=self.logger,
        )

    # ---- top-level run ------------------------------------------------------

    def run(self, *, limit: int | None = None, skip_push: bool = False) -> RunSummary:
        summary = RunSummary()
        ensure_repo(self.database, remote=self.config.git_remote or None)
        catalog = Catalog.load(self.database)

        self._discover_inbox_files(catalog, summary)
        catalog.save()

        active_ids = self._select_active_files(catalog, limit=limit)
        for file_id in active_ids:
            entry = catalog.find_by_id(file_id)
            if entry is None:
                continue
            try:
                self._process_file(catalog, entry, summary)
            except NLMAuthError as exc:
                self.logger(f"[{entry['id']}] auth error: {exc}")
                catalog.set_status(entry, "failed", f"auth error: {exc}")
                summary.files_failed += 1
                catalog.save()
                summary.notes.append(str(exc))
                break
            except Exception as exc:  # pragma: no cover - last-resort safety net
                self.logger(f"[{entry['id']}] unexpected error: {exc}")
                catalog.set_status(entry, "failed", f"unexpected error: {exc}")
                summary.files_failed += 1
                catalog.save()

        # render outline + commit
        render_outline(catalog, output_path=self.database / "outline.html")
        catalog.save()
        committed = commit_catalog(self.database, message=f"learning-buddy run @ {utc_now_iso()}")
        if committed and self.config.git_remote and self.config.auto_push and not skip_push:
            ok, msg = push(self.database)
            self.logger(f"git push: {msg}")
            if not ok:
                summary.notes.append(f"push failed: {msg}")
        return summary

    # ---- discovery ----------------------------------------------------------

    def _discover_inbox_files(self, catalog: Catalog, summary: RunSummary) -> None:
        for path in _walk_inbox(self.inbox):
            existing = self._find_by_inbox_path(catalog, path)
            if existing is not None:
                continue

            self.logger(f"discovering {path.name}")
            try:
                fingerprint = compute_fingerprint(path, excerpt_pages=self.config.llm.classify_excerpt_pages)
            except Exception as exc:
                self.logger(f"failed to fingerprint {path.name}: {exc}")
                continue

            existing_by_sha = catalog.find_by_fingerprint_sha(fingerprint["sha256"])
            if existing_by_sha is not None and existing_by_sha.get("status") not in {"failed"}:
                self.logger(f"  exact sha match → marking as duplicate of {existing_by_sha['id']}")
                entry = catalog.add_file(
                    original_filename=path.name,
                    fingerprint=fingerprint,
                    inbox_path=str(path),
                )
                entry["dup_of"] = existing_by_sha["id"]
                catalog.set_status(entry, "duplicate", f"sha256 matches {existing_by_sha['id']}")
                self._move_to_dups(path, entry)
                self._append_inbox_log(f"{path.name}: duplicate (sha256 match) of {existing_by_sha['id']}")
                summary.duplicates += 1
                continue

            entry = catalog.add_file(
                original_filename=path.name,
                fingerprint=fingerprint,
                inbox_path=str(path),
            )
            summary.discovered += 1

    def _select_active_files(self, catalog: Catalog, *, limit: int | None) -> list[str]:
        wanted = {"pending", "classifying", "chunking", "uploading", "generating"}
        ids = [f["id"] for f in catalog.files if f.get("status") in wanted]
        if limit is not None:
            ids = ids[:limit]
        return ids

    # ---- per-file pipeline --------------------------------------------------

    def _process_file(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        if entry["status"] in {"pending", "classifying"}:
            self._stage_classify(catalog, entry, summary)
            catalog.save()
            commit_catalog(self.database, message=f"classify {entry['id']}: {entry.get('title') or entry.get('original_filename')}")
            if entry["status"] in {"failed", "duplicate"}:
                return

        if entry["status"] == "chunking":
            self._stage_chunk_and_move(catalog, entry, summary)
            catalog.save()
            commit_catalog(self.database, message=f"chunk {entry['id']}: {entry.get('title')}")
            if entry["status"] == "failed":
                return

        if entry["status"] == "uploading":
            self._stage_upload(catalog, entry, summary)
            catalog.save()
            commit_catalog(self.database, message=f"upload {entry['id']}: {entry.get('title')}")
            if entry["status"] == "failed":
                return

        if entry["status"] == "generating":
            self._stage_generate(catalog, entry, summary)
            catalog.save()
            commit_catalog(self.database, message=f"generate {entry['id']}: {entry.get('title')}")

    # ---- stage: classify ---------------------------------------------------

    def _stage_classify(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        catalog.set_status(entry, "classifying", "classifying via LLM")
        catalog.save()

        path = Path(entry["inbox_path"])
        if not path.exists():
            catalog.set_status(entry, "failed", f"inbox file missing: {path}")
            summary.files_failed += 1
            return

        try:
            result = classify_file(
                file_path=path,
                fingerprint=entry["fingerprint"],
                catalog=catalog,
                llm=self.config.llm,
            )
        except Exception as exc:
            catalog.set_status(entry, "failed", f"classify failed: {exc}")
            summary.files_failed += 1
            return

        if result.is_duplicate:
            entry["dup_of"] = result.duplicate_of
            entry["title"] = result.title
            entry["authors"] = result.authors
            entry["kind"] = result.kind
            catalog.set_status(entry, "duplicate", f"LLM dup judgement: {result.reason or result.duplicate_of}")
            self._move_to_dups(path, entry)
            self._append_inbox_log(
                f"{path.name}: duplicate of {result.duplicate_of} → {result.reason or '(no reason)'}"
            )
            summary.duplicates += 1
            return

        entry["title"] = result.title
        entry["authors"] = result.authors
        entry["kind"] = result.kind
        entry["category"] = result.category
        catalog.log(entry, f"classified as {result.kind} → {'/'.join(result.category)} ({result.reason or 'no reason'})")
        catalog.set_status(entry, "chunking")
        self._append_inbox_log(
            f"{path.name}: classified to {'/'.join(result.category)} ({result.kind})"
        )
        summary.classified += 1

    # ---- stage: chunk + move into library ----------------------------------

    def _stage_chunk_and_move(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        path = Path(entry["inbox_path"])
        if not path.exists():
            catalog.set_status(entry, "failed", f"inbox file missing before chunk: {path}")
            summary.files_failed += 1
            return

        try:
            page_count = count_document_units(path)
        except Exception as exc:
            catalog.set_status(entry, "failed", f"page count failed: {exc}")
            summary.files_failed += 1
            return

        category = entry.get("category") or ["uncategorized", "other"]
        title = entry.get("title") or path.stem
        kind = entry.get("kind") or "other"

        should_split = (
            kind == "book"
            and page_count >= self.config.split.min_pages_to_split
        )

        try:
            if should_split:
                book_dir = self._book_dir(category, title)
                book_dir.mkdir(parents=True, exist_ok=True)

                # Chunk first while the original is still in inbox, so the chunker's
                # output dir starts empty (avoids globbing the original alongside chunks).
                specs = chunk_document(
                    path,
                    self.config.split.max_pages_per_chunk,
                    book_dir,
                )

                original_in_library = book_dir / path.name
                shutil.move(str(path), str(original_in_library))
                entry["library_path"] = str(original_in_library)
                catalog.log(entry, f"moved original to {original_in_library}")

                for index, spec in enumerate(specs, start=1):
                    catalog.upsert_resource(
                        entry,
                        idx=index,
                        title=spec.title,
                        library_path=str(spec.file_path),
                        page_range=spec.page_range,
                    )
                catalog.log(entry, f"split into {len(specs)} resources")
                summary.chunked += 1
            else:
                target_path = self._library_file_path(category, path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target_path))
                entry["library_path"] = str(target_path)

                if input_type(target_path) == "epub":
                    upload_path = target_path.with_suffix(".txt")
                    spec = materialize_single_source(target_path, upload_path)
                    library_path_for_upload = str(upload_path)
                else:
                    spec = ChunkSpec(
                        title=title,
                        file_path=target_path,
                        original_pdf=target_path,
                        page_range=None,
                        is_chunk=False,
                    )
                    library_path_for_upload = str(target_path)

                catalog.upsert_resource(
                    entry,
                    idx=1,
                    title=spec.title,
                    library_path=library_path_for_upload,
                    page_range=spec.page_range,
                )
                catalog.log(entry, f"moved to library as single resource")
        except Exception as exc:
            catalog.set_status(entry, "failed", f"chunking/move failed: {exc}")
            summary.files_failed += 1
            return

        catalog.set_status(entry, "uploading")

    # ---- stage: upload ------------------------------------------------------

    def _stage_upload(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        notebook_id = entry.get("notebook_id")
        if not notebook_id:
            try:
                notebook_id = self._assign_notebook(catalog, entry)
            except NLMError as exc:
                catalog.set_status(entry, "failed", f"notebook assignment failed: {exc}")
                summary.files_failed += 1
                return
            entry["notebook_id"] = notebook_id
            catalog.log(entry, f"assigned to notebook {notebook_id}")
            catalog.save()

        for resource in entry.get("resources", []):
            if resource.get("nlm_source_id"):
                continue
            upload_path = Path(resource["library_path"])
            if not upload_path.exists():
                catalog.set_status(entry, "failed", f"resource file missing: {upload_path}")
                summary.files_failed += 1
                return
            try:
                source_id = self.nlm.add_file_source(notebook_id, upload_path)
            except NLMError as exc:
                catalog.set_status(entry, "failed", f"upload failed for {upload_path.name}: {exc}")
                summary.files_failed += 1
                return
            catalog.set_resource_source(resource, source_id)
            catalog.log(entry, f"uploaded resource #{resource['idx']} → source {source_id}")
            summary.uploaded += 1
            catalog.save()

        catalog.set_status(entry, "generating")

    def _assign_notebook(self, catalog: Catalog, entry: dict[str, Any]) -> str:
        kind = entry.get("kind") or "other"
        title = entry.get("title") or Path(entry["inbox_path"]).stem
        category = entry.get("category") or ["uncategorized", "other"]

        if kind == "book":
            notebook_id = self.nlm.create_notebook(title)
            catalog.add_notebook(notebook_id=notebook_id, title=title, role="book", category=category)
            return notebook_id

        return self._find_or_create_bucket(catalog, category)

    def _find_or_create_bucket(self, catalog: Catalog, category: list[str]) -> str:
        bucket_label = self._bucket_label(category)
        existing = catalog.bucket_notebooks(category)
        for nb in existing:
            try:
                count = len(self.nlm.list_sources(nb["id"]))
            except NLMError:
                continue
            if count < self.config.bucket_capacity:
                return nb["id"]

        slot = len(existing) + 1
        title = f"{bucket_label} {slot}" if slot > 1 else bucket_label
        notebook_id = self.nlm.create_notebook(title)
        catalog.add_notebook(notebook_id=notebook_id, title=title, role="bucket", category=category)
        return notebook_id

    def _bucket_label(self, category: list[str]) -> str:
        if len(category) >= 2:
            label_root = category[-2]
            type_bucket = category[-1]
        else:
            label_root = category[0] if category else "Misc"
            type_bucket = "Items"
        word = type_bucket.title()
        return f"{label_root} {word}"

    # ---- stage: generate (kick off + poll) ---------------------------------

    def _stage_generate(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        notebook_id = entry.get("notebook_id")
        if not notebook_id:
            catalog.set_status(entry, "failed", "no notebook_id at generate stage")
            summary.files_failed += 1
            return

        # 1. kick off any artifact that's not started
        for resource in entry.get("resources", []):
            source_id = resource.get("nlm_source_id")
            if not source_id:
                continue
            for artifact_type in self.config.artifacts:
                normalized = normalize_artifact_type(artifact_type)
                record = (resource.get("artifacts") or {}).get(normalized) or {}
                state = record.get("status")
                if state in {"done", "in_progress", "pending"}:
                    continue
                # absent or "failed" → kick off
                try:
                    artifact_id = self.nlm.create_artifact(
                        notebook_id,
                        normalized,
                        source_id,
                        report_format="Study Guide",
                        note_prompt=self.config.note_prompt,
                    )
                except NLMError as exc:
                    catalog.update_artifact(resource, normalized, status="failed", error=str(exc))
                    catalog.save()
                    continue
                if artifact_id:
                    catalog.update_artifact(
                        resource,
                        normalized,
                        status="in_progress",
                        nlm_id=artifact_id,
                    )
                else:
                    catalog.update_artifact(resource, normalized, status="failed", error="no artifact id returned")
                catalog.save()

        # 2. poll until all targeted artifacts are done or failed
        if not self._has_pending(entry):
            self._finalize_generation(catalog, entry, summary)
            return

        deadline = time.monotonic() + self.config.nlm.poll_max_wait_seconds
        while True:
            if not self._has_pending(entry):
                break
            if time.monotonic() >= deadline:
                catalog.log(entry, "polling deadline reached; leaving in_progress for next run")
                return
            time.sleep(self.config.nlm.poll_interval_seconds)
            self._poll_once(catalog, entry, summary)

        self._finalize_generation(catalog, entry, summary)

    def _poll_once(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        notebook_id = entry["notebook_id"]
        try:
            statuses = self.nlm.list_studio_status(notebook_id)
        except NLMError as exc:
            self.logger(f"poll failed for {notebook_id}: {exc}")
            return

        by_id = {item.artifact_id: item for item in statuses}
        notebook_url = self.nlm.notebook_url(notebook_id)

        for resource in entry.get("resources", []):
            for art_type, record in (resource.get("artifacts") or {}).items():
                if record.get("status") != "in_progress":
                    continue
                art_id = record.get("nlm_artifact_id")
                if not art_id:
                    continue
                live = by_id.get(art_id)
                if live is None:
                    continue
                state = (live.status or "").lower()
                if state in {"completed", "ready", "succeeded", "success", "done"}:
                    catalog.update_artifact(
                        resource,
                        art_type,
                        status="done",
                        url=notebook_url,
                    )
                    summary.artifacts_completed += 1
                elif state in {"failed", "error", "errored"}:
                    catalog.update_artifact(
                        resource,
                        art_type,
                        status="failed",
                        error=f"NLM reported state={state}",
                    )
                    summary.artifacts_failed += 1
        catalog.save()

    def _has_pending(self, entry: dict[str, Any]) -> bool:
        for resource in entry.get("resources", []):
            for art_type in self.config.artifacts:
                normalized = normalize_artifact_type(art_type)
                record = (resource.get("artifacts") or {}).get(normalized) or {}
                state = record.get("status")
                if state in {"in_progress", "pending"}:
                    return True
        return False

    def _finalize_generation(self, catalog: Catalog, entry: dict[str, Any], summary: RunSummary) -> None:
        any_failed = False
        for resource in entry.get("resources", []):
            for art_type in self.config.artifacts:
                normalized = normalize_artifact_type(art_type)
                record = (resource.get("artifacts") or {}).get(normalized) or {}
                if record.get("status") == "failed":
                    any_failed = True
        if any_failed:
            catalog.set_status(entry, "failed", "one or more artifacts failed")
            summary.files_failed += 1
        else:
            catalog.set_status(entry, "done", "all artifacts done")
            summary.files_done += 1

    # ---- helpers -----------------------------------------------------------

    def _book_dir(self, category: list[str], title: str) -> Path:
        cat_dir = self.library
        for segment in category:
            cat_dir = cat_dir / _safe_segment(segment)
        return cat_dir / _safe_segment(title)

    def _library_file_path(self, category: list[str], original: Path) -> Path:
        cat_dir = self.library
        for segment in category:
            cat_dir = cat_dir / _safe_segment(segment)
        return cat_dir / original.name

    def _move_to_dups(self, path: Path, entry: dict[str, Any]) -> None:
        if not path.exists():
            return
        dups_dir = self.inbox / DUPS_DIRNAME
        dups_dir.mkdir(parents=True, exist_ok=True)
        target = dups_dir / f"{entry['id']}__{path.name}"
        shutil.move(str(path), str(target))
        entry["inbox_path"] = str(target)

    def _append_inbox_log(self, message: str) -> None:
        log_path = self.inbox / INBOX_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{utc_now_iso()}] {message}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _find_by_inbox_path(self, catalog: Catalog, path: Path) -> dict[str, Any] | None:
        target = str(path)
        for entry in catalog.files:
            if entry.get("inbox_path") == target:
                return entry
        return None


def _walk_inbox(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    found: list[Path] = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            continue
        # skip the dups folder and log
        try:
            rel_parts = path.relative_to(inbox).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] == DUPS_DIRNAME:
            continue
        found.append(path)
    return found


def _safe_segment(value: str) -> str:
    text = value.strip()
    text = text.replace("/", "_").replace("\\", "_")
    text = text.strip(". ")
    return text or "untitled"
