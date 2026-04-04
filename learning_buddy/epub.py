from __future__ import annotations

import math
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


CONTENT_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
HEADING_TAGS = {"h1", "h2", "h3"}
PARAGRAPH_TAGS = {
    "blockquote",
    "dd",
    "div",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "td",
}
WORDS_PER_ESTIMATED_PAGE = 300
MIN_TOC_DOCUMENTS = 2


@dataclass(frozen=True)
class EpubSection:
    title: str
    paragraphs: list[str]
    estimated_pages: int

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs).strip()


@dataclass(frozen=True)
class EpubBook:
    title: str
    sections: list[EpubSection]
    estimated_pages: int


@dataclass(frozen=True)
class _ManifestItem:
    item_id: str
    href: str
    media_type: str
    properties: set[str]
    full_path: str


@dataclass(frozen=True)
class _Package:
    title: str | None
    manifest: dict[str, _ManifestItem]
    spine_ids: list[str]
    nav_path: str | None
    ncx_path: str | None


@dataclass(frozen=True)
class _TocEntry:
    title: str
    href: str
    base_path: str


@dataclass(frozen=True)
class _DocumentInfo:
    title: str
    paragraphs: list[str]
    word_count: int


def load_epub(path: Path) -> EpubBook:
    with zipfile.ZipFile(path) as archive:
        rootfile = _read_rootfile_path(archive)
        package = _read_package(archive, rootfile)
        spine_documents = _spine_documents(package)
        document_infos = {
            doc_path: _read_document_info(archive, doc_path)
            for doc_path in spine_documents
        }
        sections = _build_sections(archive, package, spine_documents, document_infos)
        if not sections:
            raise RuntimeError(f"EPUB contains no readable text sections: {path}")
        estimated_pages = max(1, sum(section.estimated_pages for section in sections))
        title = package.title or path.stem
        return EpubBook(title=title, sections=sections, estimated_pages=estimated_pages)


def estimate_epub_pages(path: Path) -> int:
    return load_epub(path).estimated_pages


def render_book_text(book: EpubBook) -> str:
    parts: list[str] = [book.title.strip() or "Untitled EPUB"]
    for section in book.sections:
        parts.append(_render_section(section.title, section.paragraphs))
    return "\n\n".join(part for part in parts if part).strip() + "\n"


def render_section_text(title: str, paragraphs: list[str]) -> str:
    return _render_section(title, paragraphs)


def split_section(section: EpubSection, max_pages: int) -> list[EpubSection]:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if section.estimated_pages <= max_pages:
        return [section]

    max_words = max_pages * WORDS_PER_ESTIMATED_PAGE
    parts: list[EpubSection] = []
    current: list[str] = []
    current_words = 0
    part_index = 1

    for paragraph in _expand_paragraphs(section.paragraphs, max_words=max_words):
        words = _count_words(paragraph)
        if current and current_words + words > max_words:
            parts.append(
                EpubSection(
                    title=f"{section.title} Part {part_index}",
                    paragraphs=list(current),
                    estimated_pages=_estimate_pages_from_words(current_words),
                )
            )
            current = []
            current_words = 0
            part_index += 1
        current.append(paragraph)
        current_words += words

    if current:
        parts.append(
            EpubSection(
                title=f"{section.title} Part {part_index}",
                paragraphs=list(current),
                estimated_pages=_estimate_pages_from_words(current_words),
            )
        )
    return parts


def _read_rootfile_path(archive: zipfile.ZipFile) -> str:
    raw = archive.read("META-INF/container.xml")
    root = ET.fromstring(raw)
    rootfile = root.find(".//{*}rootfile")
    if rootfile is None:
        raise RuntimeError("EPUB container is missing META-INF/container.xml rootfile entry")
    full_path = (rootfile.attrib.get("full-path") or "").strip()
    if not full_path:
        raise RuntimeError("EPUB rootfile entry is missing full-path")
    return full_path


def _read_package(archive: zipfile.ZipFile, rootfile: str) -> _Package:
    raw = archive.read(rootfile)
    root = ET.fromstring(raw)
    manifest: dict[str, _ManifestItem] = {}
    root_dir = posixpath.dirname(rootfile)

    for item in root.findall(".//{*}manifest/{*}item"):
        item_id = (item.attrib.get("id") or "").strip()
        href = (item.attrib.get("href") or "").strip()
        if not item_id or not href:
            continue
        properties = {token.strip() for token in (item.attrib.get("properties") or "").split() if token.strip()}
        full_path = _resolve_href(root_dir, href)
        manifest[item_id] = _ManifestItem(
            item_id=item_id,
            href=href,
            media_type=(item.attrib.get("media-type") or "").strip(),
            properties=properties,
            full_path=full_path,
        )

    spine = root.find(".//{*}spine")
    spine_ids = [(item.attrib.get("idref") or "").strip() for item in root.findall(".//{*}spine/{*}itemref")]
    nav_path: str | None = None
    ncx_path: str | None = None
    for item in manifest.values():
        if "nav" in item.properties and item.media_type in CONTENT_MEDIA_TYPES:
            nav_path = item.full_path
        if item.media_type == "application/x-dtbncx+xml":
            ncx_path = item.full_path

    if spine is not None:
        toc_id = (spine.attrib.get("toc") or "").strip()
        if toc_id and toc_id in manifest:
            candidate = manifest[toc_id]
            if candidate.media_type == "application/x-dtbncx+xml":
                ncx_path = candidate.full_path

    title = None
    title_node = root.find(".//{*}metadata/{*}title")
    if title_node is not None:
        title = _normalize_space("".join(title_node.itertext()))

    return _Package(
        title=title,
        manifest=manifest,
        spine_ids=[item_id for item_id in spine_ids if item_id],
        nav_path=nav_path,
        ncx_path=ncx_path,
    )


def _spine_documents(package: _Package) -> list[str]:
    docs: list[str] = []
    for item_id in package.spine_ids:
        item = package.manifest.get(item_id)
        if not item:
            continue
        if item.media_type not in CONTENT_MEDIA_TYPES:
            continue
        if "nav" in item.properties:
            continue
        docs.append(item.full_path)
    return docs


def _build_sections(
    archive: zipfile.ZipFile,
    package: _Package,
    spine_documents: list[str],
    document_infos: dict[str, _DocumentInfo],
) -> list[EpubSection]:
    toc_titles = _resolve_toc_titles(archive, package, spine_documents)
    ordered_toc_docs = [doc_path for doc_path in spine_documents if doc_path in toc_titles]

    if len(ordered_toc_docs) >= MIN_TOC_DOCUMENTS:
        sections: list[EpubSection] = []
        current_docs: list[str] = []
        current_title: str | None = None

        for doc_path in spine_documents:
            info = document_infos.get(doc_path)
            if not info or not info.paragraphs:
                continue
            if doc_path in toc_titles:
                if current_docs:
                    sections.append(_combine_documents(current_title, current_docs, document_infos))
                current_docs = [doc_path]
                current_title = toc_titles[doc_path]
            else:
                current_docs.append(doc_path)

        if current_docs:
            sections.append(_combine_documents(current_title, current_docs, document_infos))
        return [section for section in sections if section.paragraphs]

    sections: list[EpubSection] = []
    for doc_path in spine_documents:
        info = document_infos.get(doc_path)
        if not info or not info.paragraphs:
            continue
        sections.append(
            EpubSection(
                title=info.title,
                paragraphs=list(info.paragraphs),
                estimated_pages=_estimate_pages_from_words(info.word_count),
            )
        )
    return sections


def _resolve_toc_titles(
    archive: zipfile.ZipFile,
    package: _Package,
    spine_documents: list[str],
) -> dict[str, str]:
    entries: list[_TocEntry] = []
    if package.nav_path:
        entries = _read_nav_entries(archive, package.nav_path)
    if not entries and package.ncx_path:
        entries = _read_ncx_entries(archive, package.ncx_path)
    if not entries:
        return {}

    spine_set = set(spine_documents)
    titles: dict[str, str] = {}
    for entry in entries:
        resolved = _resolve_href(posixpath.dirname(entry.base_path), entry.href)
        resolved = resolved.split("#", 1)[0]
        if resolved not in spine_set or resolved in titles:
            continue
        if entry.title:
            titles[resolved] = entry.title
    return titles


def _read_nav_entries(archive: zipfile.ZipFile, nav_path: str) -> list[_TocEntry]:
    try:
        root = ET.fromstring(archive.read(nav_path))
    except ET.ParseError:
        return []

    nav_nodes = [node for node in root.iter() if _local_name(node.tag) == "nav"]
    if not nav_nodes:
        return []

    toc_node = None
    for node in nav_nodes:
        attr_values = [value for key, value in node.attrib.items() if _local_name(key) == "type"]
        if any("toc" in value.lower() for value in attr_values):
            toc_node = node
            break
    if toc_node is None:
        toc_node = nav_nodes[0]

    entries: list[_TocEntry] = []
    for anchor in toc_node.iter():
        if _local_name(anchor.tag) != "a":
            continue
        href = (anchor.attrib.get("href") or "").strip()
        title = _normalize_space("".join(anchor.itertext()))
        if href and title:
            entries.append(_TocEntry(title=title, href=href, base_path=nav_path))
    return entries


def _read_ncx_entries(archive: zipfile.ZipFile, ncx_path: str) -> list[_TocEntry]:
    try:
        root = ET.fromstring(archive.read(ncx_path))
    except ET.ParseError:
        return []

    entries: list[_TocEntry] = []
    for nav_point in root.iter():
        if _local_name(nav_point.tag) != "navPoint":
            continue
        title = ""
        href = ""
        for child in nav_point.iter():
            name = _local_name(child.tag)
            if name == "text" and not title:
                title = _normalize_space("".join(child.itertext()))
            if name == "content" and not href:
                href = (child.attrib.get("src") or "").strip()
        if href and title:
            entries.append(_TocEntry(title=title, href=href, base_path=ncx_path))
    return entries


def _read_document_info(archive: zipfile.ZipFile, doc_path: str) -> _DocumentInfo:
    raw = archive.read(doc_path)
    try:
        root = ET.fromstring(raw)
        body = next((node for node in root.iter() if _local_name(node.tag) == "body"), None)
        title = ""
        for node in root.iter():
            name = _local_name(node.tag)
            if name in HEADING_TAGS:
                title = _normalize_space("".join(node.itertext()))
                if title:
                    break
        if not title:
            title_node = next((node for node in root.iter() if _local_name(node.tag) == "title"), None)
            if title_node is not None:
                title = _normalize_space("".join(title_node.itertext()))
        paragraphs = _paragraphs_from_xml(body) if body is not None else []
        if not paragraphs and body is not None:
            fallback = _normalize_space(" ".join(body.itertext()))
            if fallback:
                paragraphs = [fallback]
    except ET.ParseError:
        decoded = raw.decode("utf-8", errors="ignore")
        decoded = re.sub(r"<(script|style)\b.*?</\1>", " ", decoded, flags=re.IGNORECASE | re.DOTALL)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, flags=re.IGNORECASE | re.DOTALL)
        title = _normalize_space(title_match.group(1)) if title_match else ""
        text = _normalize_space(re.sub(r"<[^>]+>", " ", decoded))
        paragraphs = [text] if text else []

    if not title:
        title = _title_from_path(doc_path)

    word_count = sum(_count_words(paragraph) for paragraph in paragraphs)
    return _DocumentInfo(title=title, paragraphs=paragraphs, word_count=word_count)


def _paragraphs_from_xml(body: ET.Element | None) -> list[str]:
    if body is None:
        return []
    paragraphs: list[str] = []
    for node in body.iter():
        if _local_name(node.tag) not in PARAGRAPH_TAGS:
            continue
        text = _normalize_space(" ".join(node.itertext()))
        if text:
            paragraphs.append(text)
    return _dedupe_adjacent(paragraphs)


def _combine_documents(
    title: str | None,
    doc_paths: list[str],
    document_infos: dict[str, _DocumentInfo],
) -> EpubSection:
    paragraphs: list[str] = []
    word_count = 0
    resolved_title = title or ""
    for doc_path in doc_paths:
        info = document_infos[doc_path]
        if not resolved_title:
            resolved_title = info.title
        paragraphs.extend(info.paragraphs)
        word_count += info.word_count
    return EpubSection(
        title=resolved_title or _title_from_path(doc_paths[0]),
        paragraphs=paragraphs,
        estimated_pages=_estimate_pages_from_words(word_count),
    )


def _expand_paragraphs(paragraphs: list[str], *, max_words: int) -> list[str]:
    expanded: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if len(words) <= max_words:
            expanded.append(paragraph)
            continue
        for start in range(0, len(words), max_words):
            expanded.append(" ".join(words[start : start + max_words]))
    return expanded


def _render_section(title: str, paragraphs: list[str]) -> str:
    body = "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph.strip()).strip()
    if not body:
        return title.strip()
    return f"{title.strip()}\n\n{body}"


def _estimate_pages_from_words(word_count: int) -> int:
    return max(1, math.ceil(word_count / WORDS_PER_ESTIMATED_PAGE))


def _count_words(text: str) -> int:
    return len(text.split())


def _resolve_href(base_dir: str, href: str) -> str:
    combined = posixpath.join(base_dir, href)
    return posixpath.normpath(combined)


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _title_from_path(doc_path: str) -> str:
    stem = Path(doc_path).stem
    return re.sub(r"[_\-]+", " ", stem).strip() or "Untitled Section"


def _dedupe_adjacent(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if result and result[-1] == value:
            continue
        result.append(value)
    return result
