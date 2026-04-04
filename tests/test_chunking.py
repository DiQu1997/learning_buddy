from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from learning_buddy.chunking import chunk_document, classify_document, materialize_single_source, upload_extension_for_source
from learning_buddy.engine import WorkflowEngine


def _make_epub(
    base_dir: Path,
    *,
    with_toc: bool,
    headings: list[str],
    toc_titles: list[str] | None = None,
    words_per_chapter: int = 650,
) -> Path:
    toc_titles = toc_titles or headings
    epub_path = base_dir / ("with_toc.epub" if with_toc else "no_toc.epub")
    manifest_nav = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' if with_toc else ""
    nav_doc = ""
    if with_toc:
        nav_entries = "\n".join(
            f'<li><a href="chapter{index}.xhtml">{title}</a></li>'
            for index, title in enumerate(toc_titles, start=1)
        )
        nav_doc = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body>
    <nav epub:type="toc">
      <ol>
        {nav_entries}
      </ol>
    </nav>
  </body>
</html>
"""

    manifest_items = "\n".join(
        f'<item id="chapter{index}" href="chapter{index}.xhtml" media-type="application/xhtml+xml"/>'
        for index, _ in enumerate(headings, start=1)
    )
    spine_items = "\n".join(
        f'<itemref idref="chapter{index}"/>'
        for index, _ in enumerate(headings, start=1)
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">demo-book</dc:identifier>
    <dc:title>Demo Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    {manifest_nav}
    {manifest_items}
  </manifest>
  <spine>
    {spine_items}
  </spine>
</package>
"""

    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        if with_toc:
            archive.writestr("OEBPS/nav.xhtml", nav_doc)
        for index, heading in enumerate(headings, start=1):
            words = " ".join(f"word{n}" for n in range(words_per_chapter))
            chapter = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>{heading}</title></head>
  <body>
    <h1>{heading}</h1>
    <p>{words}</p>
  </body>
</html>
"""
            archive.writestr(f"OEBPS/chapter{index}.xhtml", chapter)
    return epub_path


class ChunkingTests(unittest.TestCase):
    def test_epub_single_source_materializes_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            epub = _make_epub(
                workspace,
                with_toc=True,
                headings=["Chapter One", "Chapter Two"],
                toc_titles=["Opening Move", "Second Step"],
            )

            pages, needs_chunking = classify_document(epub, threshold=10)
            self.assertGreaterEqual(pages, 1)
            self.assertFalse(needs_chunking)
            self.assertEqual(upload_extension_for_source(epub), ".txt")

            output_path = workspace / "single.txt"
            spec = materialize_single_source(epub, output_path)
            body = output_path.read_text(encoding="utf-8")

            self.assertEqual(spec.file_path, output_path)
            self.assertIn("Demo Book", body)
            self.assertIn("Opening Move", body)
            self.assertIn("Second Step", body)

    def test_epub_chunking_prefers_toc_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            epub = _make_epub(
                workspace,
                with_toc=True,
                headings=["Internal A", "Internal B"],
                toc_titles=["Public Intro", "Deep Concepts"],
                words_per_chapter=450,
            )

            specs = chunk_document(epub, max_pages=2, output_dir=workspace / "chunks")

            self.assertEqual([spec.title for spec in specs], ["Public Intro", "Deep Concepts"])
            self.assertTrue(all(spec.file_path.suffix == ".txt" for spec in specs))
            self.assertIn("Public Intro", specs[0].file_path.read_text(encoding="utf-8"))

    def test_epub_chunking_falls_back_to_spine_headings_without_toc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            epub = _make_epub(
                workspace,
                with_toc=False,
                headings=["Alpha Heading", "Beta Heading"],
                words_per_chapter=450,
            )

            specs = chunk_document(epub, max_pages=2, output_dir=workspace / "chunks")

            self.assertEqual([spec.title for spec in specs], ["Alpha Heading", "Beta Heading"])

    def test_engine_accepts_epub_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            epub = _make_epub(
                workspace,
                with_toc=True,
                headings=["Chapter One", "Chapter Two"],
                words_per_chapter=200,
            )
            engine = WorkflowEngine(workspace=workspace, show_progress=False)
            try:
                validated = engine._validate_inputs([str(epub)])
                self.assertEqual(validated, [epub.resolve()])
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
