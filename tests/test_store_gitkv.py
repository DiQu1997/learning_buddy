from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import gitkv  # noqa: F401

    HAVE_GITKV = True
except Exception:  # pragma: no cover - import guard
    HAVE_GITKV = False

from learning_buddy import catalog as cat
from learning_buddy.catalog import Catalog, ResourceFile, make_source_record
from learning_buddy.store import Store, meta_key, queue_key


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@unittest.skipUnless(HAVE_GITKV, "gitkv not installed")
class GitKVIntegrationTests(unittest.TestCase):
    """End-to-end against a real gitkv repo with a local bare `origin`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        origin = self.tmp / "origin.git"
        self.clone = self.tmp / "clone"
        _git("init", "-q", "--bare", str(origin))
        _git("clone", "-q", str(origin), str(self.clone))
        _git("-C", str(self.clone), "config", "user.email", "test@example.com")
        _git("-C", str(self.clone), "config", "user.name", "test")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_catalog_and_resource_roundtrip(self) -> None:
        store = Store.open(str(self.clone))
        catalog = Catalog.load(store)
        entry = catalog.add_resource(
            sha256="deadbeef", title="Deep Learning", authors=["Goodfellow"], kind="book",
            category=["CS", "AI", "books"], library_path="CS/AI/books/DL.pdf",
            toc=["Ch 1"], page_count=800,
        )
        rid = entry["id"]

        # meta blob exists and a fresh Catalog (derived index) sees it.
        self.assertIsNotNone(store.read(meta_key(rid)))
        self.assertEqual(Catalog.load(store).find_by_id(rid)["title"], "Deep Learning")

        # queue blob round-trips.
        sources = [make_source_record(idx=1, title="Ch 1", library_path="p.pdf",
                                      page_range=[1, 10], artifact_types=["note"])]
        ResourceFile.create(store, rid, notebook_id="nb_1", sources=sources)
        self.assertTrue(ResourceFile.exists(store, rid))
        self.assertEqual(store.resource_ids(), [rid])

        catalog.set_overall_status(entry, cat.DONE)
        self.assertEqual(Catalog.load(store).find_by_id(rid)["overall_status"], cat.DONE)

    def test_keys_land_at_expected_git_paths(self) -> None:
        store = Store.open(str(self.clone))
        store.write(meta_key("f_a1b2c3d4ef"), "{}")
        store.write(queue_key("f_a1b2c3d4ef"), "{}")
        keys = store._backend.list_keys("resources/")  # type: ignore[attr-defined]
        self.assertIn("resources/f_a1b2c3d4ef/meta", keys)
        self.assertIn("resources/f_a1b2c3d4ef/queue", keys)


if __name__ == "__main__":
    unittest.main()
