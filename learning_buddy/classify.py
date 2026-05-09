"""
Phase A intake helpers: fingerprint a file, then ask the LLM to classify it
(dedup judgment + title/authors/kind/category + ToC).

Outputs flow into Catalog.add_resource() in the agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .catalog import Catalog
from .config import LLMConfig
from .epub import load_epub


KIND_VALUES = ("book", "paper", "blog", "slides", "note", "article", "transcript", "other")
TYPE_BUCKETS = ("books", "papers", "blogs", "slides", "notes", "transcripts", "other")

_BOOK_PAGE_THRESHOLD = 35
_BOOK_HEAD_PAGES = 15
_BOOK_HEAD_CHARS = 12_000
_SHORT_HEAD_PAGES = 1
_SHORT_HEAD_CHARS = 4_000
_MAX_OUTLINE_ENTRIES = 60


@dataclass
class Fingerprint:
    sha256: str
    size: int
    page_count: int
    outline: list[str] | None
    head_text: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "size_bytes": self.size,
            "page_count": self.page_count,
            "outline": self.outline,
            "head_text": self.head_text,
        }


@dataclass
class ClassificationResult:
    duplicate_of: str | None
    title: str
    authors: list[str]
    kind: str
    category: list[str]
    toc: list[str]
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_of)


# ---------------------------------------------------------------------------
# Fingerprint (cheap, no LLM)
# ---------------------------------------------------------------------------


def compute_fingerprint(path: Path) -> Fingerprint:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
    page_count, outline, head_text = _extract_doc_signals(path)
    return Fingerprint(
        sha256=sha.hexdigest(),
        size=size,
        page_count=page_count,
        outline=outline,
        head_text=head_text,
    )


def _extract_doc_signals(path: Path) -> tuple[int, list[str] | None, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_signals(path)
    if suffix == ".epub":
        return _extract_epub_signals(path)
    return 0, None, f"[unsupported suffix: {suffix}]"


def _extract_pdf_signals(path: Path) -> tuple[int, list[str] | None, str]:
    try:
        with fitz.open(str(path)) as doc:
            page_count = len(doc)
            outline = _pdf_outline_titles(doc)
            head_text = _read_pdf_head_text(doc, page_count)
            return page_count, outline, head_text
    except Exception as exc:
        return 0, None, f"[unable to read PDF: {exc}]"


def _pdf_outline_titles(doc) -> list[str] | None:
    raw = doc.get_toc()
    if not raw:
        return None
    titles: list[str] = []
    for entry in raw:
        if len(entry) < 2:
            continue
        title = str(entry[1]).strip()
        if title:
            titles.append(title)
    if len(titles) < 3:
        return None
    return titles[:_MAX_OUTLINE_ENTRIES]


def _read_pdf_head_text(doc, page_count: int) -> str:
    is_book = page_count >= _BOOK_PAGE_THRESHOLD
    pages_to_read = _BOOK_HEAD_PAGES if is_book else _SHORT_HEAD_PAGES
    char_cap = _BOOK_HEAD_CHARS if is_book else _SHORT_HEAD_CHARS
    chunks: list[str] = []
    total = 0
    for idx in range(min(pages_to_read, page_count)):
        try:
            text = doc.load_page(idx).get_text() or ""
        except Exception:
            continue
        chunks.append(text)
        total += len(text)
        if total >= char_cap:
            break
    return _cap_text("\n\n".join(chunks).strip(), char_cap)


def _extract_epub_signals(path: Path) -> tuple[int, list[str] | None, str]:
    try:
        book = load_epub(path)
    except Exception as exc:
        return 0, None, f"[unable to read EPUB: {exc}]"
    page_count = book.estimated_pages
    section_titles = [s.title.strip() for s in book.sections if s.title and s.title.strip()]
    outline = section_titles[:_MAX_OUTLINE_ENTRIES] if len(section_titles) >= 3 else None
    is_book = page_count >= _BOOK_PAGE_THRESHOLD
    sections_to_read = _BOOK_HEAD_PAGES if is_book else _SHORT_HEAD_PAGES
    char_cap = _BOOK_HEAD_CHARS if is_book else _SHORT_HEAD_CHARS
    chunks: list[str] = []
    if book.title:
        chunks.append(book.title)
    total = sum(len(c) for c in chunks)
    for section in book.sections[:sections_to_read]:
        chunks.append(section.text)
        total += len(section.text)
        if total >= char_cap:
            break
    return page_count, outline, _cap_text("\n\n".join(chunks).strip(), char_cap)


def _cap_text(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[…truncated]"
    return text


# ---------------------------------------------------------------------------
# Classification (one LLM call → dup judgment + all metadata, including ToC)
# ---------------------------------------------------------------------------


def classify_file(
    *,
    file_path: Path,
    fingerprint: Fingerprint,
    catalog: Catalog,
    llm: LLMConfig,
) -> ClassificationResult:
    payload = {
        "new_file": {
            "filename": file_path.name,
            **fingerprint.as_payload(),
        },
        "existing_taxonomy": [list(c) for c in catalog.categories_in_use()],
        "existing_resources": catalog.existing_summary(),
    }

    system = (
        "You are a librarian classifying a new file for a personal NotebookLM workspace.\n\n"
        "Inputs you receive: filename, page_count, optional outline (chapter/section titles "
        "from the file's embedded ToC), head_text (rendered text from page 1 for short docs, "
        "or first ~15 pages for books — covers the title-page region and any ToC pages).\n\n"
        "Decide three things:\n"
        "(1) Duplicate. Is this file a duplicate of any existing resource? Use these signals "
        "in order: outline overlap (if both have outlines, ≥ ~70% chapter-title match is a "
        "strong same-work signal — different scans/editions/formats of the same work count as "
        "duplicates); title and authors visible in head_text matching an existing resource's "
        "title and authors. The filename is unreliable — do NOT match on filename alone.\n"
        "(2) Title and authors. Extract from head_text (the rendered first-page region — that's "
        "the title page). Do NOT use the filename as the title. If head_text contains an "
        "edition/version label (e.g. '2nd Edition', 'v1.3'), include it in the title.\n"
        "(3) Kind, category, and ToC. Pick from the existing taxonomy when reasonable; invent a "
        "new sub-node when nothing fits. "
        f"Last category segment must be a type bucket from {list(TYPE_BUCKETS)}. "
        f"kind must be one of {list(KIND_VALUES)}. "
        "If the input outline is null, extract a ToC from head_text (a flat list of chapter or "
        "major section titles). If the input outline is non-null, you may return it unchanged "
        "or refine it.\n"
    )

    user_prompt = (
        "Decide based on the JSON below. Return JSON ONLY, matching this schema exactly:\n"
        "{\n"
        '  "duplicate_of": <existing resource id, or null>,\n'
        '  "title":        <string>,\n'
        '  "authors":      <string array, may be empty>,\n'
        '  "kind":         <one of the kind values>,\n'
        '  "category":     <array of strings, last is a type bucket>,\n'
        '  "toc":          <array of strings — chapter/section titles, may be empty>,\n'
        '  "reason":       <one short sentence>\n'
        "}\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    raw_text = _call_openai_json(model=llm.model, system=system, user=user_prompt)
    parsed = _safe_parse_json(raw_text)
    return _coerce_result(parsed, payload=payload, fingerprint=fingerprint)


def _coerce_result(parsed: dict[str, Any], payload: dict[str, Any], fingerprint: Fingerprint) -> ClassificationResult:
    dup_raw = parsed.get("duplicate_of")
    duplicate_of = dup_raw if isinstance(dup_raw, str) and dup_raw.strip() else None

    title = str(parsed.get("title") or payload["new_file"]["filename"]).strip()

    authors_raw = parsed.get("authors") or []
    authors = [str(a).strip() for a in authors_raw if str(a).strip()] if isinstance(authors_raw, list) else []

    kind = str(parsed.get("kind") or "other").strip().lower()
    if kind not in KIND_VALUES:
        kind = "other"

    category_raw = parsed.get("category") or []
    if isinstance(category_raw, list):
        category = [str(seg).strip() for seg in category_raw if str(seg).strip()]
    else:
        category = []
    if not category or category[-1].lower() not in TYPE_BUCKETS:
        category.append(_default_bucket_for_kind(kind))

    toc_raw = parsed.get("toc")
    if isinstance(toc_raw, list):
        toc = [str(t).strip() for t in toc_raw if str(t).strip()]
    elif fingerprint.outline:
        toc = list(fingerprint.outline)
    else:
        toc = []

    reason = str(parsed.get("reason") or "").strip()

    return ClassificationResult(
        duplicate_of=duplicate_of,
        title=title,
        authors=authors,
        kind=kind,
        category=category,
        toc=toc,
        reason=reason,
        raw=parsed,
    )


def _default_bucket_for_kind(kind: str) -> str:
    mapping = {
        "book": "books",
        "paper": "papers",
        "blog": "blogs",
        "article": "blogs",
        "slides": "slides",
        "note": "notes",
        "transcript": "transcripts",
    }
    return mapping.get(kind, "other")


def _call_openai_json(*, model: str, system: str, user: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is required for classification. Install with `pip install openai`."
        ) from exc
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"


def _safe_parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}
