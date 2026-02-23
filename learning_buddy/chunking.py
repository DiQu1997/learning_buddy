from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from .utils import slugify


@dataclass
class ChunkSpec:
    title: str
    file_path: Path
    original_pdf: Path
    page_range: str | None
    is_chunk: bool


def count_pdf_pages(path: Path) -> int:
    with fitz.open(str(path)) as doc:
        return len(doc)


def classify_pdf(path: Path, threshold: int) -> tuple[int, bool]:
    pages = count_pdf_pages(path)
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


def _build_specs_from_files(chunk_files: list[Path], original_pdf: Path, is_chunk: bool) -> list[ChunkSpec]:
    specs: list[ChunkSpec] = []
    cursor = 1
    for path in chunk_files:
        pages = count_pdf_pages(path)
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
