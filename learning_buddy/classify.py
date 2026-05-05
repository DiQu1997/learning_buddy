from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .catalog import Catalog
from .config import LLMConfig
from .epub import load_epub


KIND_VALUES = ("book", "paper", "blog", "slides", "note", "article", "transcript", "other")
TYPE_BUCKETS = ("books", "papers", "blogs", "slides", "notes", "transcripts", "other")


@dataclass
class ClassificationResult:
    duplicate_of: str | None
    title: str
    authors: list[str]
    kind: str
    category: list[str]
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_of)


def compute_fingerprint(path: Path, *, excerpt_pages: int) -> dict[str, Any]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)

    excerpt = extract_excerpt(path, pages=excerpt_pages)
    title_norm = _normalize_title(path.stem)
    return {
        "sha256": sha.hexdigest(),
        "size": size,
        "title_norm": title_norm,
        "first_pages_excerpt": excerpt,
    }


def extract_excerpt(path: Path, *, pages: int = 3, max_chars: int = 4000) -> str:
    suffix = path.suffix.lower()
    text = ""
    if suffix == ".pdf":
        try:
            with fitz.open(str(path)) as doc:
                buf: list[str] = []
                for idx in range(min(pages, len(doc))):
                    buf.append(doc.load_page(idx).get_text())
                text = "\n\n".join(buf)
        except Exception as exc:
            text = f"[unable to extract PDF text: {exc}]"
    elif suffix == ".epub":
        try:
            book = load_epub(path)
            buf: list[str] = []
            for section in book.sections[: max(1, pages)]:
                buf.append(section.text)
                joined = "\n\n".join(buf)
                if len(joined) >= max_chars:
                    text = joined
                    break
            else:
                text = "\n\n".join(buf)
        except Exception as exc:
            text = f"[unable to extract EPUB text: {exc}]"
    else:
        text = f"[unsupported suffix: {suffix}]"

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[…truncated]"
    return text


def _normalize_title(value: str) -> str:
    text = re.sub(r"[_\-]+", " ", value).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _existing_files_summary(catalog: Catalog) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in catalog.files:
        if entry.get("status") == "duplicate":
            continue
        out.append(
            {
                "id": entry["id"],
                "title": entry.get("title") or entry.get("original_filename"),
                "authors": entry.get("authors") or [],
                "kind": entry.get("kind"),
                "category": entry.get("category") or [],
                "library_path": entry.get("library_path"),
                "title_norm": entry.get("fingerprint", {}).get("title_norm"),
            }
        )
    return out


def classify_file(
    *,
    file_path: Path,
    fingerprint: dict[str, Any],
    catalog: Catalog,
    llm: LLMConfig,
) -> ClassificationResult:
    payload = {
        "new_file": {
            "filename": file_path.name,
            "size_bytes": fingerprint.get("size"),
            "title_norm": fingerprint.get("title_norm"),
            "first_pages_excerpt": fingerprint.get("first_pages_excerpt"),
        },
        "existing_taxonomy": [list(cat) for cat in catalog.categories_in_use()],
        "existing_files": _existing_files_summary(catalog),
    }

    system = (
        "You are a librarian classifying a new file for a personal NotebookLM workspace. "
        "Decide: (1) is the new file a duplicate of any existing file, judged by title and "
        "first-page text (allow different scans/editions/file-formats of the same work to count as duplicates); "
        "(2) if not duplicate, give a clean title, authors, kind, and a hierarchical category. "
        "Reuse existing taxonomy when reasonable; you may invent new sub-nodes when nothing fits. "
        "The last category segment must be a type bucket: one of "
        f"{list(TYPE_BUCKETS)}. "
        f"kind must be one of {list(KIND_VALUES)}."
    )

    user_prompt = (
        "Decide based on the JSON below. Return JSON ONLY, matching this schema exactly:\n"
        "{\n"
        '  "duplicate_of": <existing file id or null>,\n'
        '  "title": <string>,\n'
        '  "authors": <string array, may be empty>,\n'
        '  "kind": <one of the kind values>,\n'
        '  "category": <array of strings, last is a type bucket>,\n'
        '  "reason": <one short sentence>\n'
        "}\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    raw_text = _call_openai_json(model=llm.model, system=system, user=user_prompt)
    parsed = _safe_parse_json(raw_text)
    return _coerce_result(parsed, payload=payload)


def _coerce_result(parsed: dict[str, Any], payload: dict[str, Any]) -> ClassificationResult:
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

    reason = str(parsed.get("reason") or "").strip()

    return ClassificationResult(
        duplicate_of=duplicate_of,
        title=title,
        authors=authors,
        kind=kind,
        category=category,
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
