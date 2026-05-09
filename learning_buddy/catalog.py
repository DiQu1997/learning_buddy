"""
Metadata store: catalog.json (top-level index) + resources/<id>.json (per-resource task queue).

Plain JSON, atomic writes via tmp+rename. No git. See DESIGN_V2.md for the schema and the
state machine.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .utils import utc_now_iso


CATALOG_FILENAME = "catalog.json"
RESOURCES_DIRNAME = "resources"
CATALOG_VERSION = 1

# overall_status values for a catalog row
NEW = "NEW"
IN_PROGRESS = "IN_PROGRESS"
DONE = "DONE"
FAILED = "FAILED"
OVERALL_STATUSES = (NEW, IN_PROGRESS, DONE, FAILED)
UNFINISHED = (NEW, IN_PROGRESS)

# task state values
NOT_STARTED = "NOT_STARTED"
PROCESSING = "PROCESSING"
TASK_DONE = "DONE"
TASK_FAILED = "FAILED"
TASK_STATES = (NOT_STARTED, PROCESSING, TASK_DONE, TASK_FAILED)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def new_resource_id() -> str:
    return "f_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Catalog (the top-level index)
# ---------------------------------------------------------------------------


@dataclass
class Catalog:
    metadata_dir: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, metadata_dir: Path) -> "Catalog":
        metadata_dir = Path(metadata_dir).expanduser()
        path = metadata_dir / CATALOG_FILENAME
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {
                "version": CATALOG_VERSION,
                "updated_at": utc_now_iso(),
                "resources": [],
            }
        return cls(metadata_dir=metadata_dir, data=data)

    @property
    def path(self) -> Path:
        return self.metadata_dir / CATALOG_FILENAME

    @property
    def resources_dir(self) -> Path:
        return self.metadata_dir / RESOURCES_DIRNAME

    @property
    def resources(self) -> list[dict[str, Any]]:
        return self.data.setdefault("resources", [])

    # ---- save ----

    def save(self) -> None:
        self.data["updated_at"] = utc_now_iso()
        _atomic_write(self.path, _dump_json(self.data))

    # ---- queries ----

    def find_by_sha(self, sha256: str) -> dict[str, Any] | None:
        for entry in self.resources:
            if entry.get("sha256") == sha256:
                return entry
        return None

    def find_by_id(self, resource_id: str) -> dict[str, Any] | None:
        for entry in self.resources:
            if entry.get("id") == resource_id:
                return entry
        return None

    def list_unfinished(self) -> list[dict[str, Any]]:
        return [e for e in self.resources if e.get("overall_status") in UNFINISHED]

    def categories_in_use(self) -> list[list[str]]:
        seen: list[list[str]] = []
        seen_keys: set[str] = set()
        for entry in self.resources:
            cat = entry.get("category") or []
            if not cat:
                continue
            key = "/".join(cat)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            seen.append(list(cat))
        return seen

    def existing_summary(self) -> list[dict[str, Any]]:
        """A compact dump of every resource for the LLM dedup prompt."""
        return [
            {
                "id": e["id"],
                "title": e.get("title"),
                "authors": e.get("authors") or [],
                "kind": e.get("kind"),
                "category": e.get("category") or [],
                "library_path": e.get("library_path"),
                "page_count": e.get("page_count"),
                "toc": e.get("toc"),
            }
            for e in self.resources
        ]

    def counts(self) -> dict[str, int]:
        rs = self.resources
        return {
            "total": len(rs),
            "new": sum(1 for r in rs if r.get("overall_status") == NEW),
            "in_progress": sum(1 for r in rs if r.get("overall_status") == IN_PROGRESS),
            "done": sum(1 for r in rs if r.get("overall_status") == DONE),
            "failed": sum(1 for r in rs if r.get("overall_status") == FAILED),
        }

    # ---- mutations ----

    def add_resource(
        self,
        *,
        sha256: str,
        title: str,
        authors: list[str],
        kind: str,
        category: list[str],
        library_path: str,
        toc: list[str] | None,
        page_count: int,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        entry = {
            "id": new_resource_id(),
            "sha256": sha256,
            "title": title,
            "authors": list(authors),
            "kind": kind,
            "category": list(category),
            "library_path": library_path,
            "toc": list(toc) if toc else [],
            "page_count": int(page_count),
            "overall_status": NEW,
            "created_at": now,
            "updated_at": now,
        }
        self.resources.append(entry)
        return entry

    def set_overall_status(self, entry: dict[str, Any], status: str) -> None:
        if status not in OVERALL_STATUSES:
            raise ValueError(f"unknown overall_status: {status}")
        entry["overall_status"] = status
        entry["updated_at"] = utc_now_iso()


# ---------------------------------------------------------------------------
# ResourceFile (per-resource task queue)
# ---------------------------------------------------------------------------


@dataclass
class ResourceFile:
    metadata_dir: Path
    resource_id: str
    data: dict[str, Any]

    @classmethod
    def path_for(cls, metadata_dir: Path, resource_id: str) -> Path:
        return Path(metadata_dir).expanduser() / RESOURCES_DIRNAME / f"{resource_id}.json"

    @classmethod
    def exists(cls, metadata_dir: Path, resource_id: str) -> bool:
        return cls.path_for(metadata_dir, resource_id).exists()

    @classmethod
    def load(cls, metadata_dir: Path, resource_id: str) -> "ResourceFile":
        path = cls.path_for(metadata_dir, resource_id)
        if not path.exists():
            raise FileNotFoundError(f"resource file does not exist: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(metadata_dir=Path(metadata_dir).expanduser(), resource_id=resource_id, data=data)

    @classmethod
    def create(
        cls,
        metadata_dir: Path,
        resource_id: str,
        *,
        notebook_id: str | None,
        sources: list[dict[str, Any]],
    ) -> "ResourceFile":
        data = {
            "resource_id": resource_id,
            "notebook_id": notebook_id,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "sources": sources,
        }
        rf = cls(metadata_dir=Path(metadata_dir).expanduser(), resource_id=resource_id, data=data)
        rf.save()
        return rf

    @property
    def path(self) -> Path:
        return self.path_for(self.metadata_dir, self.resource_id)

    @property
    def notebook_id(self) -> str | None:
        return self.data.get("notebook_id")

    @notebook_id.setter
    def notebook_id(self, value: str) -> None:
        self.data["notebook_id"] = value

    @property
    def sources(self) -> list[dict[str, Any]]:
        return self.data.setdefault("sources", [])

    def save(self) -> None:
        self.data["updated_at"] = utc_now_iso()
        _atomic_write(self.path, _dump_json(self.data))

    # ---- iterators / aggregations ----

    def iter_tasks(self) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield (source, task) pairs for every task across every source."""
        for source in self.sources:
            for task in source.get("tasks", []):
                yield source, task

    def all_tasks_done(self) -> bool:
        for _, task in self.iter_tasks():
            if task.get("state") != TASK_DONE:
                return False
        return True

    def any_task_failed(self) -> bool:
        for _, task in self.iter_tasks():
            if task.get("state") == TASK_FAILED:
                return True
        return False

    def task_state_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in TASK_STATES}
        for _, task in self.iter_tasks():
            state = task.get("state") or NOT_STARTED
            counts[state] = counts.get(state, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Helpers used by callers building source/task records
# ---------------------------------------------------------------------------


def make_task(task_type: str) -> dict[str, Any]:
    return {
        "type": task_type,
        "state": NOT_STARTED,
        "retry_count": 0,
        "last_error": None,
    }


def make_source_record(
    *,
    idx: int,
    title: str,
    library_path: str,
    page_range: list[int] | str | None,
    artifact_types: Iterable[str],
) -> dict[str, Any]:
    tasks = [make_task("upload")]
    for art_type in artifact_types:
        tasks.append(make_task(art_type))
    return {
        "idx": idx,
        "title": title,
        "library_path": library_path,
        "page_range": page_range,
        "nlm_source_id": None,
        "tasks": tasks,
    }
