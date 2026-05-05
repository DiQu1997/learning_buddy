from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .utils import utc_now_iso


CATALOG_FILENAME = "catalog.json"
OUTLINE_FILENAME = "outline.html"
CATALOG_VERSION = 1


# File status lifecycle:
#   pending → classifying → chunking → uploading → generating → done
#   any stage → failed   (with reason in last log line)
#   classifying → duplicate (set dup_of, no further work)


@dataclass
class Catalog:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, database_dir: Path) -> "Catalog":
        database_dir = Path(database_dir).expanduser()
        path = database_dir / CATALOG_FILENAME
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {
                "version": CATALOG_VERSION,
                "updated_at": utc_now_iso(),
                "files": [],
                "notebooks": [],
            }
        return cls(path=path, data=data)

    @property
    def database_dir(self) -> Path:
        return self.path.parent

    @property
    def files(self) -> list[dict[str, Any]]:
        return self.data.setdefault("files", [])

    @property
    def notebooks(self) -> list[dict[str, Any]]:
        return self.data.setdefault("notebooks", [])

    def save(self) -> None:
        self.data["updated_at"] = utc_now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def find_by_fingerprint_sha(self, sha256: str) -> dict[str, Any] | None:
        for entry in self.files:
            if entry.get("fingerprint", {}).get("sha256") == sha256:
                return entry
        return None

    def find_by_id(self, file_id: str) -> dict[str, Any] | None:
        for entry in self.files:
            if entry.get("id") == file_id:
                return entry
        return None

    def add_file(
        self,
        *,
        original_filename: str,
        fingerprint: dict[str, Any],
        inbox_path: str,
    ) -> dict[str, Any]:
        entry = {
            "id": "f_" + uuid.uuid4().hex[:10],
            "original_filename": original_filename,
            "title": None,
            "authors": [],
            "kind": None,
            "category": [],
            "library_path": None,
            "inbox_path": inbox_path,
            "fingerprint": dict(fingerprint),
            "notebook_id": None,
            "resources": [],
            "status": "pending",
            "dup_of": None,
            "log": [{"ts": utc_now_iso(), "msg": f"discovered in inbox as {original_filename}"}],
        }
        self.files.append(entry)
        return entry

    def log(self, file_entry: dict[str, Any], message: str) -> None:
        file_entry.setdefault("log", []).append({"ts": utc_now_iso(), "msg": message})

    def set_status(self, file_entry: dict[str, Any], status: str, message: str | None = None) -> None:
        file_entry["status"] = status
        if message:
            self.log(file_entry, message)

    def categories_in_use(self) -> list[list[str]]:
        seen: list[list[str]] = []
        seen_keys: set[str] = set()
        for entry in self.files:
            cat = entry.get("category") or []
            if not cat:
                continue
            key = "/".join(cat)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            seen.append(list(cat))
        return seen

    def bucket_notebooks(self, category: list[str]) -> list[dict[str, Any]]:
        key = list(category)
        return [nb for nb in self.notebooks if nb.get("role") == "bucket" and nb.get("category") == key]

    def add_notebook(self, *, notebook_id: str, title: str, role: str, category: list[str]) -> dict[str, Any]:
        record = {
            "id": notebook_id,
            "title": title,
            "role": role,
            "category": list(category),
            "created_at": utc_now_iso(),
        }
        self.notebooks.append(record)
        return record

    def find_notebook(self, notebook_id: str) -> dict[str, Any] | None:
        for nb in self.notebooks:
            if nb.get("id") == notebook_id:
                return nb
        return None

    def upsert_resource(
        self,
        file_entry: dict[str, Any],
        *,
        idx: int,
        title: str,
        library_path: str | None,
        page_range: list[int] | str | None,
    ) -> dict[str, Any]:
        for res in file_entry.get("resources", []):
            if res.get("idx") == idx:
                res["title"] = title
                if library_path is not None:
                    res["library_path"] = library_path
                if page_range is not None:
                    res["page_range"] = page_range
                return res
        record = {
            "idx": idx,
            "title": title,
            "library_path": library_path,
            "page_range": page_range,
            "nlm_source_id": None,
            "artifacts": {},
        }
        file_entry.setdefault("resources", []).append(record)
        return record

    def set_resource_source(self, resource: dict[str, Any], nlm_source_id: str) -> None:
        resource["nlm_source_id"] = nlm_source_id

    def update_artifact(
        self,
        resource: dict[str, Any],
        artifact_type: str,
        *,
        status: str,
        nlm_id: str | None = None,
        url: str | None = None,
        error: str | None = None,
    ) -> None:
        artifacts = resource.setdefault("artifacts", {})
        record = artifacts.setdefault(artifact_type, {})
        record["status"] = status
        record["updated_at"] = utc_now_iso()
        if nlm_id is not None:
            record["nlm_artifact_id"] = nlm_id
        if url is not None:
            record["url"] = url
        if error is not None:
            record["error"] = error
        elif status not in {"failed"}:
            record.pop("error", None)

    def counts(self) -> dict[str, int]:
        files = self.files
        return {
            "files_total": len(files),
            "files_done": sum(1 for f in files if f.get("status") == "done"),
            "files_in_progress": sum(1 for f in files if f.get("status") in {"classifying", "chunking", "uploading", "generating"}),
            "files_failed": sum(1 for f in files if f.get("status") == "failed"),
            "files_duplicate": sum(1 for f in files if f.get("status") == "duplicate"),
            "notebooks": len(self.notebooks),
            "resources": sum(len(f.get("resources") or []) for f in files),
        }


# ---- git operations ----------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def ensure_repo(database_dir: Path, *, remote: str | None = None) -> None:
    database_dir = Path(database_dir).expanduser()
    database_dir.mkdir(parents=True, exist_ok=True)
    if not (database_dir / ".git").exists():
        _git(["init", "-q"], cwd=database_dir)
        _git(["checkout", "-q", "-b", "main"], cwd=database_dir, check=False)

    if remote:
        existing = _git(["remote", "get-url", "origin"], cwd=database_dir, check=False)
        if existing.returncode != 0:
            _git(["remote", "add", "origin", remote], cwd=database_dir, check=False)


def commit_catalog(database_dir: Path, message: str) -> bool:
    database_dir = Path(database_dir).expanduser()
    add = _git(["add", CATALOG_FILENAME, OUTLINE_FILENAME], cwd=database_dir, check=False)
    if add.returncode != 0:
        # one of the files may not exist yet
        _git(["add", CATALOG_FILENAME], cwd=database_dir, check=False)
    status = _git(["status", "--porcelain"], cwd=database_dir, check=False)
    if not status.stdout.strip():
        return False
    _git(["commit", "-q", "-m", message], cwd=database_dir)
    return True


def push(database_dir: Path) -> tuple[bool, str]:
    database_dir = Path(database_dir).expanduser()
    has_remote = _git(["remote", "get-url", "origin"], cwd=database_dir, check=False)
    if has_remote.returncode != 0:
        return False, "no remote configured"
    result = _git(["push", "-u", "origin", "HEAD"], cwd=database_dir, check=False)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "push failed").strip()
    return True, "pushed"


def files_iter(catalog: Catalog, statuses: Iterable[str] | None = None) -> Iterable[dict[str, Any]]:
    if statuses is None:
        yield from catalog.files
        return
    wanted = set(statuses)
    for entry in catalog.files:
        if entry.get("status") in wanted:
            yield entry
