from __future__ import annotations

import json
import unittest

from learning_buddy import catalog as cat
from learning_buddy.catalog import Catalog, ResourceFile, make_source_record
from learning_buddy.store import MemoryBackend, Store, meta_key, queue_key


def _write_meta(store: Store, *, id: str, created_at: str, status: str = cat.NEW) -> None:
    entry = {
        "id": id, "sha256": id, "title": id, "authors": [], "kind": "paper",
        "category": ["A", "papers"], "library_path": f"A/papers/{id}.pdf",
        "toc": [], "page_count": 1, "overall_status": status,
        "created_at": created_at, "updated_at": created_at,
    }
    store.write(meta_key(id), json.dumps(entry))


class CountingBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.sets = 0

    def set(self, key: str, value: str) -> None:
        self.sets += 1
        super().set(key, value)


class CatalogTests(unittest.TestCase):
    def _new_catalog(self) -> tuple[Catalog, Store]:
        store = Store.memory()
        return Catalog.load(store), store

    def test_add_resource_persists_and_queries(self) -> None:
        catalog, store = self._new_catalog()
        entry = catalog.add_resource(
            sha256="abc", title="Deep Learning", authors=["Goodfellow"], kind="book",
            category=["CS", "AI", "books"], library_path="CS/AI/books/DL.pdf",
            toc=["Ch 1"], page_count=800,
        )
        self.assertEqual(catalog.find_by_sha("abc")["id"], entry["id"])
        self.assertEqual(catalog.find_by_id(entry["id"])["title"], "Deep Learning")
        # Written straight through to the store as a meta blob.
        self.assertIsNotNone(store.read(meta_key(entry["id"])))
        # A freshly loaded catalog sees it (derived index, no save() needed).
        self.assertEqual(Catalog.load(store).find_by_id(entry["id"])["title"], "Deep Learning")

    def test_resources_sorted_by_created_at_not_id(self) -> None:
        store = Store.memory()
        _write_meta(store, id="f_zzz", created_at="2026-01-01T00:00:00Z")
        _write_meta(store, id="f_aaa", created_at="2026-02-01T00:00:00Z")
        catalog = Catalog.load(store)
        self.assertEqual([e["id"] for e in catalog.resources], ["f_zzz", "f_aaa"])

    def test_unfinished_and_status_rollup(self) -> None:
        catalog, store = self._new_catalog()
        entry = catalog.add_resource(
            sha256="s", title="t", authors=[], kind="paper",
            category=["A", "papers"], library_path="A/papers/t.pdf",
            toc=[], page_count=1,
        )
        self.assertEqual([e["id"] for e in catalog.list_unfinished()], [entry["id"]])
        catalog.set_overall_status(entry, cat.DONE)
        self.assertEqual(catalog.list_unfinished(), [])
        self.assertEqual(Catalog.load(store).find_by_id(entry["id"])["overall_status"], cat.DONE)

    def test_counts_and_categories(self) -> None:
        catalog, _ = self._new_catalog()
        catalog.add_resource(sha256="1", title="a", authors=[], kind="paper",
                             category=["CS", "papers"], library_path="x", toc=[], page_count=1)
        b = catalog.add_resource(sha256="2", title="b", authors=[], kind="book",
                                 category=["CS", "books"], library_path="y", toc=[], page_count=1)
        catalog.set_overall_status(b, cat.DONE)
        counts = catalog.counts()
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(sorted(catalog.categories_in_use()), [["CS", "books"], ["CS", "papers"]])

    def test_resourcefile_roundtrip(self) -> None:
        store = Store.memory()
        rid = "f_abc1234567"
        self.assertFalse(ResourceFile.exists(store, rid))
        sources = [make_source_record(idx=1, title="s1", library_path="p.pdf",
                                      page_range=None, artifact_types=["note", "audio"])]
        ResourceFile.create(store, rid, notebook_id=None, sources=sources)
        self.assertTrue(ResourceFile.exists(store, rid))

        rf = ResourceFile.load(store, rid)
        self.assertEqual([t["type"] for t in rf.sources[0]["tasks"]], ["upload", "note", "audio"])
        self.assertFalse(rf.all_tasks_done())

        for _, task in rf.iter_tasks():
            task["state"] = cat.TASK_DONE
        rf.save()
        self.assertTrue(ResourceFile.load(store, rid).all_tasks_done())

    def test_write_noop_guard_skips_identical_content(self) -> None:
        store = Store(CountingBackend())
        backend = store._backend  # type: ignore[attr-defined]
        store.write(queue_key("f_x"), "v1")
        store.write(queue_key("f_x"), "v1")  # identical → no commit
        store.write(queue_key("f_x"), "v2")  # changed → commit
        self.assertEqual(backend.sets, 2)


if __name__ == "__main__":
    unittest.main()
