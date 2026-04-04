from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from .epub import EpubSection, estimate_epub_pages, load_epub, render_book_text, render_section_text, split_section
from .utils import slugify


SUPPORTED_INPUT_SUFFIXES = {".pdf", ".epub"}


@dataclass
class ChunkSpec:
    title: str
    file_path: Path
    original_pdf: Path
    page_range: str | None
    is_chunk: bool


def input_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(f"Unsupported input type: {path}")
    return suffix.lstrip(".")


def upload_extension_for_source(path: Path) -> str:
    return ".pdf" if input_type(path) == "pdf" else ".txt"


def count_document_units(path: Path) -> int:
    kind = input_type(path)
    if kind == "pdf":
        with fitz.open(str(path)) as doc:
            return len(doc)
    return estimate_epub_pages(path)


def classify_document(path: Path, threshold: int) -> tuple[int, bool]:
    pages = count_document_units(path)
    return pages, pages >= threshold


def run_book_chunker(pdf_path: Path, max_pages: int, output_dir: Path) -> list[ChunkSpec]:
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve().parent.parent / "book_chunker.py"
    if not script_path.exists():
        raise RuntimeError(f"book_chunker.py not found at {script_path}")

    command = [
        sys.executable,
        str(script_path),
        str(pdf_path),
        "--max-pages",
        str(max_pages),
        "--output-dir",
        str(output_dir),
    ]
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.stdout:
        print(proc.stdout, flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"book_chunker.py failed for {pdf_path}:\n{(proc.stdout or '').strip()}")

    chunks = sorted(output_dir.glob("*.pdf"))
    if not chunks:
        raise RuntimeError(f"book_chunker.py produced no chunk PDFs for {pdf_path}")

    return _build_specs_from_files(chunks, original_pdf=pdf_path, is_chunk=True)


def fallback_fixed_size_split(pdf_path: Path, max_pages: int, output_dir: Path) -> list[ChunkSpec]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    with fitz.open(str(pdf_path)) as src:
        total_pages = len(src)
        if total_pages == 0:
            raise RuntimeError(f"PDF has no pages: {pdf_path}")

        part_index = 1
        for start in range(0, total_pages, max_pages):
            end = min(total_pages, start + max_pages)
            part = fitz.open()
            part.insert_pdf(src, from_page=start, to_page=end - 1)
            title = f"Part_{part_index:02d}_{slugify(pdf_path.stem, fallback='document')}"
            filename = output_dir / f"{title}.pdf"
            part.save(str(filename))
            part.close()
            generated.append(filename)
            part_index += 1

    return _build_specs_from_files(generated, original_pdf=pdf_path, is_chunk=True)


def chunk_document(source_path: Path, max_pages: int, output_dir: Path) -> list[ChunkSpec]:
    kind = input_type(source_path)
    if kind == "pdf":
        return _chunk_pdf(source_path, max_pages, output_dir)
    return _chunk_epub(source_path, max_pages, output_dir)


def materialize_single_source(source_path: Path, output_path: Path) -> ChunkSpec:
    kind = input_type(source_path)
    if kind == "pdf":
        return copy_as_single_chunk(source_path, output_path)
    return _write_epub_single_source(source_path, output_path)


def copy_as_single_chunk(pdf_path: Path, output_path: Path) -> ChunkSpec:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_path, output_path)
    return ChunkSpec(
        title=pdf_path.stem,
        file_path=output_path,
        original_pdf=pdf_path,
        page_range=None,
        is_chunk=False,
    )


def classify_pdf(path: Path, threshold: int) -> tuple[int, bool]:
    return classify_document(path, threshold)


def count_pdf_pages(path: Path) -> int:
    return count_document_units(path)


def _chunk_pdf(pdf_path: Path, max_pages: int, output_dir: Path) -> list[ChunkSpec]:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        return run_book_chunker(pdf_path, max_pages, output_dir)
    except Exception:
        return fallback_fixed_size_split(pdf_path, max_pages, output_dir)


def _chunk_epub(epub_path: Path, max_pages: int, output_dir: Path) -> list[ChunkSpec]:
    output_dir.mkdir(parents=True, exist_ok=True)
    book = load_epub(epub_path)
    sections: list[tuple[EpubSection, bool]] = []
    for section in book.sections:
        if section.estimated_pages > max_pages:
            sections.extend((piece, True) for piece in split_section(section, max_pages=max_pages))
        else:
            sections.append((section, False))

    drafts: list[tuple[str, str, int]] = []
    current_titles: list[str] = []
    current_blocks: list[str] = []
    current_pages = 0
    for section, force_standalone in sections:
        if force_standalone and current_blocks:
            drafts.append(
                (
                    _combine_titles(current_titles),
                    "\n\n".join(current_blocks).strip() + "\n",
                    current_pages,
                )
            )
            current_titles = []
            current_blocks = []
            current_pages = 0

        if current_blocks and current_pages + section.estimated_pages > max_pages:
            drafts.append(
                (
                    _combine_titles(current_titles),
                    "\n\n".join(current_blocks).strip() + "\n",
                    current_pages,
                )
            )
            current_titles = []
            current_blocks = []
            current_pages = 0

        current_titles.append(section.title)
        current_blocks.append(render_section_text(section.title, section.paragraphs))
        current_pages += section.estimated_pages

        if force_standalone:
            drafts.append(
                (
                    _combine_titles(current_titles),
                    "\n\n".join(current_blocks).strip() + "\n",
                    current_pages,
                )
            )
            current_titles = []
            current_blocks = []
            current_pages = 0

    if current_blocks:
        drafts.append(
            (
                _combine_titles(current_titles),
                "\n\n".join(current_blocks).strip() + "\n",
                current_pages,
            )
        )

    specs: list[ChunkSpec] = []
    cursor = 1
    for index, (title, content, pages) in enumerate(drafts, start=1):
        filename = output_dir / f"Part_{index:02d}_{slugify(title, fallback='section')}.txt"
        filename.write_text(content, encoding="utf-8")
        end = cursor + pages - 1
        page_range = f"{cursor}-{end} (est.)" if pages > 0 else None
        specs.append(
            ChunkSpec(
                title=title,
                file_path=filename,
                original_pdf=epub_path,
                page_range=page_range,
                is_chunk=True,
            )
        )
        cursor = end + 1
    return specs


def _write_epub_single_source(epub_path: Path, output_path: Path) -> ChunkSpec:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    book = load_epub(epub_path)
    output_path.write_text(render_book_text(book), encoding="utf-8")
    return ChunkSpec(
        title=epub_path.stem,
        file_path=output_path,
        original_pdf=epub_path,
        page_range=None,
        is_chunk=False,
    )


def _build_specs_from_files(chunk_files: list[Path], original_pdf: Path, is_chunk: bool) -> list[ChunkSpec]:
    specs: list[ChunkSpec] = []
    cursor = 1
    for path in chunk_files:
        pages = count_document_units(path)
        start = cursor
        end = cursor + pages - 1
        page_range = f"{start}-{end}" if pages > 0 else None
        cursor = end + 1

        title = _title_from_filename(path)
        specs.append(
            ChunkSpec(
                title=title,
                file_path=path,
                original_pdf=original_pdf,
                page_range=page_range,
                is_chunk=is_chunk,
            )
        )
    return specs


def _title_from_filename(path: Path) -> str:
    text = path.stem
    text = re.sub(r"^Part[_-]?\d+[_-]?", "", text, flags=re.IGNORECASE)
    text = text.replace("_", " ").strip()
    return text or path.stem


def _combine_titles(titles: list[str]) -> str:
    unique: list[str] = []
    for title in titles:
        if not unique or unique[-1] != title:
            unique.append(title)
    if not unique:
        return "Section"
    if len(unique) == 1:
        return unique[0]
    return f"{unique[0]} - {unique[-1]}"
