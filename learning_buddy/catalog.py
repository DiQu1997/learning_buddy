"""
Metadata store, backed by gitkv. Two blobs per resource:

    resources/<id>/meta    -> catalog row (index fields + overall_status)  → `Catalog`
    resources/<id>/queue   -> task queue  (notebook_id + sources[] + tasks) → `ResourceFile`

The `Catalog` index is *derived* — there is no monolithic catalog document. It is
rebuilt in memory from the per-resource `meta` blobs, and each mutation writes the
affected `meta` blob immediately (every write is a gitkv commit). Ordering is not
implied by the keys (gitkv lists lexicographically by random id), so `resources`
is sorted by `created_at`. See GITKV_MIGRATION.md for the full schema.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .store import Store, meta_key, queue_key
from .utils import utc_now_iso


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


def _dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def new_resource_id() -> str:
    return "f_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Catalog (the derived top-level index)
# ---------------------------------------------------------------------------


@dataclass
class Catalog:
    store: Store
    _by_id: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, store: Store) -> "Catalog":
        by_id: dict[str, dict[str, Any]] = {}
        for text in store.iter_meta():
            entry = json.loads(text)
            rid = entry.get("id")
            if rid:
                by_id[rid] = entry
        return cls(store=store, _by_id=by_id)

    @property
    def resources(self) -> list[dict[str, Any]]:
        # Keys carry no creation order (random ids), so sort explicitly.
        return sorted(
            self._by_id.values(),
            key=lambda e: (e.get("created_at") or "", e.get("id") or ""),
        )

    def _persist(self, entry: dict[str, Any]) -> None:
        self._by_id[entry["id"]] = entry
        self.store.write(meta_key(entry["id"]), _dump_json(entry))

    # ---- queries ----

    def find_by_sha(self, sha256: str) -> dict[str, Any] | None:
        for entry in self._by_id.values():
            if entry.get("sha256") == sha256:
                return entry
        return None

    def find_by_id(self, resource_id: str) -> dict[str, Any] | None:
        return self._by_id.get(resource_id)

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
        rs = list(self._by_id.values())
        return {
            "total": len(rs),
            "new": sum(1 for r in rs if r.get("overall_status") == NEW),
            "in_progress": sum(1 for r in rs if r.get("overall_status") == IN_PROGRESS),
            "done": sum(1 for r in rs if r.get("overall_status") == DONE),
            "failed": sum(1 for r in rs if r.get("overall_status") == FAILED),
        }

    # ---- mutations (each writes its meta blob immediately) ----

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
        self._persist(entry)
        return entry

    def set_overall_status(self, entry: dict[str, Any], status: str) -> None:
        if status not in OVERALL_STATUSES:
            raise ValueError(f"unknown overall_status: {status}")
        entry["overall_status"] = status
        entry["updated_at"] = utc_now_iso()
        self._persist(entry)

    def touch(self, entry: dict[str, Any]) -> None:
        """Persist an in-place mutation of `entry` (e.g. a rewritten library_path)."""
        entry["updated_at"] = utc_now_iso()
        self._persist(entry)


# ---------------------------------------------------------------------------
# ResourceFile (per-resource task queue)
# ---------------------------------------------------------------------------


@dataclass
class ResourceFile:
    store: Store
    resource_id: str
    data: dict[str, Any]

    @classmethod
    def exists(cls, store: Store, resource_id: str) -> bool:
        return store.read(queue_key(resource_id)) is not None

    @classmethod
    def load(cls, store: Store, resource_id: str) -> "ResourceFile":
        text = store.read(queue_key(resource_id))
        if text is None:
            raise FileNotFoundError(f"resource queue does not exist: {resource_id}")
        return cls(store=store, resource_id=resource_id, data=json.loads(text))

    @classmethod
    def create(
        cls,
        store: Store,
        resource_id: str,
        *,
        notebook_id: str | None,
        sources: list[dict[str, Any]],
    ) -> "ResourceFile":
        now = utc_now_iso()
        data = {
            "resource_id": resource_id,
            "notebook_id": notebook_id,
            "created_at": now,
            "updated_at": now,
            "sources": sources,
        }
        rf = cls(store=store, resource_id=resource_id, data=data)
        rf.save()
        return rf

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
        self.store.write(queue_key(self.resource_id), _dump_json(self.data))

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
