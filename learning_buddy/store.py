"""
Git-backed metadata store (gitkv).

One gitkv table (`learning_buddy`) holds every record. Keys map verbatim to git
blob paths, grouped one directory per knowledge file:

    resources/<id>/meta    -> the catalog row  (index fields + overall_status)
    resources/<id>/queue   -> the task queue   (notebook_id + sources[] + tasks[])

Every write is a commit that gitkv fast-forward-pushes to `origin`, so the git
remote is the cross-machine source of truth and `git log` is the audit trail.

The storage backend is abstracted behind `KVBackend` so the catalog layer can be
unit-tested against an in-memory store without a real git repo. `gitkv` is
imported lazily inside `GitKVBackend`, so importing this module never requires it.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol


TABLE = "learning_buddy"
RESOURCE_PREFIX = "resources/"
_META_SUFFIX = "/meta"
_QUEUE_SUFFIX = "/queue"


def meta_key(resource_id: str) -> str:
    return f"{RESOURCE_PREFIX}{resource_id}{_META_SUFFIX}"


def queue_key(resource_id: str) -> str:
    return f"{RESOURCE_PREFIX}{resource_id}{_QUEUE_SUFFIX}"


# ---------------------------------------------------------------------------
# Backend protocol + implementations
# ---------------------------------------------------------------------------


class KVBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str = "") -> list[str]: ...
    def list_items(self, prefix: str = "") -> list[tuple[str, str]]: ...


class MemoryBackend:
    """In-memory backend with gitkv-compatible (lexicographically sorted) listing."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))

    def list_items(self, prefix: str = "") -> list[tuple[str, str]]:
        return [(k, self._data[k]) for k in self.list_keys(prefix)]


class GitKVBackend:
    """Backend backed by a real gitkv repo. Requires the clone to have `origin` set."""

    def __init__(self, repo_path: str, table: str = TABLE) -> None:
        import gitkv  # lazy: importing learning_buddy.store must not require gitkv

        self._db = gitkv.open(str(repo_path))
        if table not in self._db:
            self._db.create_table(table)
        self._tbl = self._db[table]

    def get(self, key: str) -> str | None:
        return self._tbl.get(key)  # explicit get → None on miss

    def set(self, key: str, value: str) -> None:
        self._tbl[key] = value  # one commit on the log branch (+ push to origin)

    def delete(self, key: str) -> None:
        try:
            del self._tbl[key]
        except KeyError:
            pass

    def list_keys(self, prefix: str = "") -> list[str]:
        return list(self._tbl.list_keys(prefix))

    def list_items(self, prefix: str = "") -> list[tuple[str, str]]:
        return [(k, v) for k, v in self._tbl.list_items(prefix)]


# ---------------------------------------------------------------------------
# Store facade
# ---------------------------------------------------------------------------


class Store:
    """Thin facade over a KVBackend. Centralizes the no-op-write guard and key scan."""

    def __init__(self, backend: KVBackend) -> None:
        self._backend = backend

    @classmethod
    def open(cls, repo_path: str, table: str = TABLE) -> "Store":
        return cls(GitKVBackend(repo_path, table))

    @classmethod
    def memory(cls) -> "Store":
        return cls(MemoryBackend())

    def read(self, key: str) -> str | None:
        return self._backend.get(key)

    def write(self, key: str, text: str) -> None:
        # Skip no-op commits: a save that doesn't change content makes no commit.
        if self._backend.get(key) == text:
            return
        self._backend.set(key, text)

    def delete(self, key: str) -> None:
        self._backend.delete(key)

    def iter_meta(self) -> Iterator[str]:
        """Yield the JSON text of every resources/<id>/meta blob."""
        for key, value in self._backend.list_items(RESOURCE_PREFIX):
            if key.endswith(_META_SUFFIX):
                yield value

    def resource_ids(self) -> list[str]:
        out: list[str] = []
        for key in self._backend.list_keys(RESOURCE_PREFIX):
            if key.endswith(_META_SUFFIX):
                out.append(key[len(RESOURCE_PREFIX) : -len(_META_SUFFIX)])
        return out
