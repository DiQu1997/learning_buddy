from __future__ import annotations

import random
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import normalize_artifact_type, remote_artifact_type
from .utils import try_load_json


class NLMError(RuntimeError):
    pass


class NLMAuthError(NLMError):
    pass


@dataclass
class StudioArtifact:
    artifact_id: str
    artifact_type: str
    status: str
    source_ids: list[str]
    raw: dict[str, Any]


class NLMCLI:
    AUTH_MARKERS = (
        "cookies have expired",
        "authentication may have expired",
        "unauthorized",
        "http 401",
        "401 unauthorized",
    )
    RATE_MARKERS = ("rate limit exceeded", "http 429", "too many requests", "429 too many requests")
    NOT_FOUND_MARKERS = ("not found", "http 404", "404 not found", "does not exist", "missing")
    ID_LINE_PATTERNS = (
        re.compile(r"Artifact ID:\s*([A-Za-z0-9_\-]+)", re.IGNORECASE),
        re.compile(r"Source ID:\s*([A-Za-z0-9_\-]+)", re.IGNORECASE),
        re.compile(r"Notebook ID:\s*([A-Za-z0-9_\-]+)", re.IGNORECASE),
        re.compile(r"^\s*ID:\s*([A-Za-z0-9_\-]+)\s*$", re.IGNORECASE | re.MULTILINE),
    )

    def __init__(
        self,
        *,
        command: str = "nlm",
        backoff_base_seconds: int = 30,
        backoff_multiplier: int = 2,
        max_retries: int = 3,
        request_interval_range: tuple[float, float] = (0.5, 1.5),
        logger: Callable[[str], None] | None = None,
    ):
        self.command = command
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_retries = max_retries
        lo, hi = request_interval_range
        self._request_interval_range = (max(0.0, float(lo)), max(0.0, float(hi)))
        self._last_command_finished_at: float | None = None
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def ensure_authenticated(self) -> None:
        self._log("Checking authentication via 'login --check'.")
        self._run(["login", "--check"], retries=0)

    def create_notebook(self, title: str) -> str:
        output = self._run(["notebook", "create", title], retries=0)
        notebook_id = self._extract_id(output, allow_generic_id=True)
        if notebook_id:
            return notebook_id

        notebooks = self.list_notebooks()
        for row in notebooks:
            label = str(row.get("title") or row.get("name") or "").strip()
            if label == title:
                ident = self._extract_any_id_from_row(row)
                if ident:
                    return ident
        raise NLMError(f"Could not determine notebook ID after creation. Output:\n{output}")

    def list_notebooks(self) -> list[dict[str, Any]]:
        output = self._run(["notebook", "list", "--json"], retries=0)
        data = try_load_json(output)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("items") or data.get("notebooks") or []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def get_notebook_public_url(self, notebook_id: str) -> str | None:
        try:
            output = self._run(["share", "status", notebook_id, "--json"], retries=0)
            payload = try_load_json(output)
        except Exception:
            return None

        candidates: list[str] = []
        if isinstance(payload, dict):
            for key in ("public_url", "publicUrl", "url", "share_url", "shareUrl"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
        return candidates[0] if candidates else None

    def add_file_source(self, notebook_id: str, file_path: Path) -> str:
        before_ids = self._source_ids(notebook_id)
        output = self._run(
            ["source", "add", notebook_id, "--file", str(file_path), "--wait"],
            retries=self.max_retries,
        )
        source_id = self._extract_id(output, allow_generic_id=True)
        if source_id:
            return source_id

        after = self.list_sources(notebook_id)
        after_ids = {item.get("source_id") for item in after if item.get("source_id")}
        new_ids = [sid for sid in after_ids if sid not in before_ids]
        if len(new_ids) == 1:
            return str(new_ids[0])

        normalized_path = str(file_path.resolve())
        for source in after:
            known_path = str(source.get("file_path") or source.get("path") or "").strip()
            if known_path and Path(known_path).expanduser().resolve().as_posix() == Path(normalized_path).as_posix():
                sid = source.get("source_id")
                if sid:
                    return str(sid)

        raise NLMError(f"Could not determine source ID for uploaded file: {file_path}")

    def list_sources(self, notebook_id: str) -> list[dict[str, Any]]:
        output = self._run(["source", "list", notebook_id, "--json"], retries=0)
        payload = try_load_json(output)
        rows: list[dict[str, Any]] = []
        if isinstance(payload, list):
            rows = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            items = payload.get("items") or payload.get("sources") or []
            if isinstance(items, list):
                rows = [item for item in items if isinstance(item, dict)]

        normalized: list[dict[str, Any]] = []
        for row in rows:
            source_id = self._extract_any_id_from_row(row, keys=("id", "source_id", "sourceId"))
            normalized.append(
                {
                    "source_id": source_id,
                    "title": row.get("title") or row.get("name"),
                    "file_path": row.get("file_path") or row.get("path") or row.get("source_path"),
                    "raw": row,
                }
            )
        return normalized

    def create_artifact(
        self,
        notebook_id: str,
        artifact_type: str,
        source_id: str,
        *,
        report_format: str,
        note_prompt: str,
    ) -> str | None:
        normalized_type = normalize_artifact_type(artifact_type)
        remote_type = remote_artifact_type(normalized_type)
        before = self.list_studio_status(notebook_id)
        before_ids = {item.artifact_id for item in before}

        cmd = self._build_create_command(
            notebook_id=notebook_id,
            artifact_type=normalized_type,
            source_id=source_id,
            report_format=report_format,
            note_prompt=note_prompt,
        )
        output = self._run(cmd, retries=self.max_retries)
        artifact_id = self._extract_id(output, allow_generic_id=False)
        if artifact_id:
            return artifact_id

        after = self.list_studio_status(notebook_id)
        candidate = self._find_matching_artifact(
            artifacts=after,
            artifact_type=remote_type,
            source_id=source_id,
            exclude_ids=before_ids,
        )
        return candidate.artifact_id if candidate else None

    def list_studio_status(self, notebook_id: str) -> list[StudioArtifact]:
        output = self._run(
            ["studio", "status", notebook_id, "--json"],
            retries=self.max_retries,
        )
        payload = try_load_json(output)
        rows = self._extract_items(payload, preferred_keys=("artifacts", "items", "data"))

        results: list[StudioArtifact] = []
        for row in rows:
            artifact_id = self._extract_any_id_from_row(row, keys=("artifact_id", "artifactId", "id"))
            if not artifact_id:
                continue
            raw_type = str(row.get("artifact_type") or row.get("type") or row.get("kind") or "").strip()
            artifact_type = normalize_artifact_type(raw_type) if raw_type else ""
            status_raw = str(row.get("status") or row.get("state") or "").strip().lower()

            source_ids = row.get("source_ids") or row.get("sourceIds") or row.get("source_id") or row.get("sourceId")
            source_list: list[str]
            if isinstance(source_ids, list):
                source_list = [str(item) for item in source_ids if item]
            elif isinstance(source_ids, str):
                source_list = [part.strip() for part in source_ids.split(",") if part.strip()]
            else:
                source_list = []

            results.append(
                StudioArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    status=status_raw,
                    source_ids=source_list,
                    raw=row,
                )
            )
        return results

    def download_artifact(
        self,
        notebook_id: str,
        artifact_type: str,
        artifact_id: str,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        dl_type = self._download_type(artifact_type)
        args = ["download", dl_type, notebook_id, "--id", artifact_id, "--output", str(output_path)]
        self._run(args, retries=min(self.max_retries, 2))

    def delete_artifact(self, notebook_id: str, artifact_id: str, *, ignore_missing: bool = True) -> bool:
        try:
            self._run(
                ["studio", "delete", notebook_id, artifact_id, "--confirm"],
                retries=self.max_retries,
            )
            return True
        except NLMError as exc:
            if ignore_missing and self._is_missing_error(str(exc)):
                return False
            raise

    def query_notebook(self, notebook_id: str, question: str) -> str:
        return self._run(["notebook", "query", notebook_id, question], retries=0)

    def notebook_url(self, notebook_id: str) -> str:
        return f"https://notebooklm.google.com/notebook/{notebook_id}"

    def _run(self, args: list[str], *, retries: int) -> str:
        command = [self.command] + args
        cmd_text = " ".join(command)
        self._log(f"Running: {cmd_text}")
        attempt = 0
        while True:
            self._throttle_request()
            proc = subprocess.run(command, capture_output=True, text=True)
            self._last_command_finished_at = time.monotonic()
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            merged = (stdout + "\n" + stderr).strip()

            if proc.returncode == 0:
                if self._is_auth_error(merged):
                    raise NLMAuthError("NotebookLM authentication expired. Run `nlm login` and retry.")
                return stdout.strip()

            if self._is_auth_error(merged):
                self._log(f"Authentication error from command: {cmd_text}")
                raise NLMAuthError("NotebookLM authentication expired. Run `nlm login` and retry.")

            if self._is_rate_limited(merged) and attempt < retries:
                sleep_for = int(self.backoff_base_seconds * (self.backoff_multiplier**attempt))
                self._log(
                    f"Rate limited on command '{cmd_text}'. Backing off for {sleep_for}s before retry {attempt + 1}/{retries}."
                )
                time.sleep(sleep_for)
                attempt += 1
                continue

            self._log(f"Command failed: {cmd_text}")
            raise NLMError(f"Command failed: {' '.join(command)}\n{merged}")

    def _extract_id(self, text: str, *, allow_generic_id: bool) -> str | None:
        if not text:
            return None

        if "Task ID:" in text and "Artifact ID:" not in text:
            allow_generic_id = False

        for pattern in self.ID_LINE_PATTERNS:
            match = pattern.search(text)
            if match:
                value = match.group(1).strip()
                if value:
                    return value

        if not allow_generic_id:
            return None

        # Last-resort fallback: capture a token-like ID.
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{7,}", text):
            if token.lower() not in {"successfully", "authenticated"}:
                return token
        return None

    def _source_ids(self, notebook_id: str) -> set[str]:
        try:
            rows = self.list_sources(notebook_id)
        except Exception:
            return set()
        return {str(item["source_id"]) for item in rows if item.get("source_id")}

    def _find_matching_artifact(
        self,
        *,
        artifacts: list[StudioArtifact],
        artifact_type: str,
        source_id: str,
        exclude_ids: set[str],
    ) -> StudioArtifact | None:
        matching = []
        for item in artifacts:
            if item.artifact_id in exclude_ids:
                continue
            if item.artifact_type and remote_artifact_type(item.artifact_type) != remote_artifact_type(artifact_type):
                continue
            if item.source_ids and source_id not in item.source_ids:
                continue
            matching.append(item)
        if not matching:
            return None
        return matching[-1]

    def _build_create_command(
        self,
        *,
        notebook_id: str,
        artifact_type: str,
        source_id: str,
        report_format: str,
        note_prompt: str,
    ) -> list[str]:
        if artifact_type == "report":
            return [
                "report",
                "create",
                notebook_id,
                "--format",
                report_format,
                "--source-ids",
                source_id,
                "--confirm",
            ]
        if artifact_type == "note":
            return [
                "report",
                "create",
                notebook_id,
                "--format",
                "Create Your Own",
                "--prompt",
                note_prompt,
                "--source-ids",
                source_id,
                "--confirm",
            ]
        if artifact_type == "slide_deck":
            return ["slides", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "audio":
            return ["audio", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "video":
            return ["video", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "quiz":
            return ["quiz", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "flashcards":
            return ["flashcards", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "mind_map":
            return ["mindmap", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        if artifact_type == "infographic":
            return ["infographic", "create", notebook_id, "--source-ids", source_id, "--confirm"]
        raise NLMError(f"Unsupported artifact type: {artifact_type}")

    def _download_type(self, artifact_type: str) -> str:
        token = remote_artifact_type(artifact_type)
        if token == "slide_deck":
            return "slide-deck"
        if token == "mind_map":
            return "mind-map"
        return token

    @staticmethod
    def _extract_items(payload: Any, preferred_keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in preferred_keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            # Fallback: if the dict itself looks like a row collection wrapper.
            if all(isinstance(v, dict) for v in payload.values()):
                return list(payload.values())
        return []

    @staticmethod
    def _extract_any_id_from_row(row: dict[str, Any], keys: tuple[str, ...] = ("id", "notebook_id", "notebookId")) -> str | None:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _is_auth_error(self, text: str) -> bool:
        token = text.lower()
        return any(marker in token for marker in self.AUTH_MARKERS)

    def _is_rate_limited(self, text: str) -> bool:
        token = text.lower()
        return any(marker in token for marker in self.RATE_MARKERS)

    def _is_missing_error(self, text: str) -> bool:
        token = text.lower()
        return any(marker in token for marker in self.NOT_FOUND_MARKERS)

    def _throttle_request(self) -> None:
        lo, hi = self._request_interval_range
        if hi <= 0:
            return
        if self._last_command_finished_at is None:
            return
        min_gap = random.uniform(lo, hi)
        elapsed = time.monotonic() - self._last_command_finished_at
        if elapsed >= min_gap:
            return
        sleep_for = min_gap - elapsed
        self._log(f"Throttling NLM command cadence: sleeping {sleep_for:.2f}s.")
        time.sleep(sleep_for)
