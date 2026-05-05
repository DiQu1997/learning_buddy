from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path
from typing import Any

from .catalog import Catalog


STATUS_BADGE = {
    "done": ("done", "#4caf50"),
    "duplicate": ("dup", "#90a4ae"),
    "pending": ("pending", "#9e9e9e"),
    "classifying": ("classifying", "#03a9f4"),
    "chunking": ("chunking", "#03a9f4"),
    "uploading": ("uploading", "#03a9f4"),
    "generating": ("generating", "#ff9800"),
    "failed": ("failed", "#f44336"),
}


_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; max-width: 1080px; margin: 24px auto; padding: 0 24px; }
h1 { margin-bottom: 4px; }
.meta { color: #888; margin-bottom: 24px; font-size: 13px; }
.counts { display: flex; gap: 16px; flex-wrap: wrap; padding: 12px 16px; background: rgba(120,120,120,0.08); border-radius: 8px; margin-bottom: 24px; }
.counts span b { font-weight: 600; }
details.cat { margin: 6px 0; }
details.cat > summary { cursor: pointer; padding: 6px 8px; border-radius: 6px; }
details.cat > summary:hover { background: rgba(120,120,120,0.1); }
details.cat[open] > summary { font-weight: 600; }
.cat-children { padding-left: 20px; border-left: 1px dashed rgba(120,120,120,0.3); margin-left: 8px; }
.file { padding: 8px 12px; margin: 6px 0; border-left: 3px solid rgba(120,120,120,0.4); }
.file.done { border-left-color: #4caf50; }
.file.failed { border-left-color: #f44336; }
.file.duplicate { border-left-color: #90a4ae; opacity: 0.7; }
.file .title { font-weight: 600; }
.file .authors { color: #888; font-size: 13px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; color: white; font-size: 11px; font-weight: 600; vertical-align: middle; margin-left: 6px; }
.notebook-link { font-size: 12px; color: #607d8b; }
.resources { margin-top: 6px; padding-left: 20px; }
.resource { padding: 4px 0; font-size: 13px; }
.resource .res-title { font-weight: 500; }
.artifacts { display: inline-flex; gap: 6px; margin-left: 8px; }
.artifact { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border-radius: 999px; font-size: 11px; color: white; }
.artifact.done { background: #4caf50; }
.artifact.in_progress { background: #ff9800; }
.artifact.pending { background: #9e9e9e; }
.artifact.failed { background: #f44336; }
.artifact a { color: white; text-decoration: none; }
.section-title { margin-top: 32px; }
"""


def render_outline(catalog: Catalog, *, output_path: Path) -> None:
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append("<html lang=\"en\"><head><meta charset=\"utf-8\">")
    parts.append("<title>Learning Buddy — Outline</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append("</head><body>")
    parts.append("<h1>Learning Buddy</h1>")
    parts.append(f"<div class=\"meta\">Generated {html.escape(catalog.data.get('updated_at') or '')}</div>")
    parts.append(_render_counts(catalog))

    parts.append("<h2 class=\"section-title\">Library</h2>")
    parts.append(_render_library_tree(catalog))

    parts.append("<h2 class=\"section-title\">Notebooks</h2>")
    parts.append(_render_notebook_list(catalog))

    failed = [f for f in catalog.files if f.get("status") == "failed"]
    if failed:
        parts.append("<h2 class=\"section-title\">Failures</h2>")
        parts.append(_render_failures(failed))

    parts.append("</body></html>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")


def _render_counts(catalog: Catalog) -> str:
    counts = catalog.counts()
    items = [
        ("files", counts["files_total"]),
        ("done", counts["files_done"]),
        ("in progress", counts["files_in_progress"]),
        ("failed", counts["files_failed"]),
        ("duplicates", counts["files_duplicate"]),
        ("notebooks", counts["notebooks"]),
        ("resources", counts["resources"]),
    ]
    inner = " ".join(f"<span><b>{html.escape(label)}</b>: {value}</span>" for label, value in items)
    return f"<div class=\"counts\">{inner}</div>"


def _render_library_tree(catalog: Catalog) -> str:
    tree = _build_category_tree(catalog.files)
    out: list[str] = []
    _render_tree_node(tree, out)
    return "".join(out)


def _build_category_tree(files: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {"_children": {}, "_files": []}
    for entry in files:
        if entry.get("status") == "duplicate":
            continue
        cat = entry.get("category") or ["uncategorized", "other"]
        node = root
        for segment in cat:
            children = node.setdefault("_children", {})
            node = children.setdefault(segment, {"_children": {}, "_files": []})
        node.setdefault("_files", []).append(entry)
    return root


def _render_tree_node(node: dict[str, Any], out: list[str]) -> None:
    children = node.get("_children") or {}
    files = node.get("_files") or []

    for entry in sorted(files, key=lambda e: (e.get("title") or "").lower()):
        out.append(_render_file(entry))

    for name in sorted(children.keys(), key=lambda s: s.lower()):
        child = children[name]
        descendant_count = _count_files(child)
        if descendant_count == 0:
            continue
        out.append(f"<details class=\"cat\" open><summary>{html.escape(name)} ({descendant_count})</summary>")
        out.append("<div class=\"cat-children\">")
        _render_tree_node(child, out)
        out.append("</div></details>")


def _count_files(node: dict[str, Any]) -> int:
    total = sum(1 for _ in node.get("_files") or [])
    for child in (node.get("_children") or {}).values():
        total += _count_files(child)
    return total


def _render_file(entry: dict[str, Any]) -> str:
    status = entry.get("status") or "pending"
    cls = "file"
    if status in {"done", "failed", "duplicate"}:
        cls += f" {status}"
    title = entry.get("title") or entry.get("original_filename") or entry["id"]
    authors = ", ".join(entry.get("authors") or [])
    notebook_id = entry.get("notebook_id")
    notebook_link = ""
    if notebook_id:
        url = f"https://notebooklm.google.com/notebook/{html.escape(notebook_id)}"
        notebook_link = f' <a class="notebook-link" href="{url}" target="_blank">notebook ↗</a>'

    badge_label, badge_color = STATUS_BADGE.get(status, (status, "#9e9e9e"))
    badge = f'<span class="badge" style="background:{badge_color}">{html.escape(badge_label)}</span>'

    parts: list[str] = []
    parts.append(f'<div class="{cls}">')
    parts.append(f'<div><span class="title">{html.escape(title)}</span>{badge}{notebook_link}</div>')
    if authors:
        parts.append(f'<div class="authors">by {html.escape(authors)}</div>')

    resources = entry.get("resources") or []
    if resources:
        parts.append('<div class="resources">')
        for resource in resources:
            parts.append(_render_resource(resource, notebook_id))
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def _render_resource(resource: dict[str, Any], notebook_id: str | None) -> str:
    title = resource.get("title") or f"Resource #{resource.get('idx')}"
    page_range = resource.get("page_range")
    page_text = ""
    if isinstance(page_range, (list, tuple)) and len(page_range) == 2:
        page_text = f" (p. {page_range[0]}–{page_range[1]})"
    elif isinstance(page_range, str) and page_range:
        page_text = f" ({html.escape(page_range)})"

    artifacts_html: list[str] = []
    artifacts = resource.get("artifacts") or {}
    for art_type in sorted(artifacts.keys()):
        record = artifacts[art_type] or {}
        state = record.get("status") or "pending"
        url = record.get("url")
        label = f"{art_type}"
        if url:
            artifacts_html.append(
                f'<span class="artifact {state}"><a href="{html.escape(url)}" target="_blank">{html.escape(label)}</a></span>'
            )
        else:
            artifacts_html.append(f'<span class="artifact {state}">{html.escape(label)}</span>')

    parts = [
        '<div class="resource">',
        f'<span class="res-title">{html.escape(title)}</span>{html.escape(page_text)}',
    ]
    if artifacts_html:
        parts.append('<span class="artifacts">' + "".join(artifacts_html) + '</span>')
    parts.append("</div>")
    return "".join(parts)


def _render_notebook_list(catalog: Catalog) -> str:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for nb in catalog.notebooks:
        key = "/".join(nb.get("category") or [])
        by_category[key].append(nb)

    if not by_category:
        return "<p>(no notebooks yet)</p>"

    parts: list[str] = ["<ul>"]
    for key in sorted(by_category.keys()):
        for nb in sorted(by_category[key], key=lambda x: x.get("title") or ""):
            url = f"https://notebooklm.google.com/notebook/{html.escape(nb['id'])}"
            role = nb.get("role") or "notebook"
            parts.append(
                f'<li><a href="{url}" target="_blank">{html.escape(nb.get("title") or nb["id"])}</a>'
                f' <span class="meta">({html.escape(role)} · {html.escape(key) or "uncategorized"})</span></li>'
            )
    parts.append("</ul>")
    return "".join(parts)


def _render_failures(failed: list[dict[str, Any]]) -> str:
    parts: list[str] = ["<ul>"]
    for entry in failed:
        log = entry.get("log") or []
        last = log[-1] if log else {}
        msg = last.get("msg") or "(no message)"
        title = entry.get("title") or entry.get("original_filename") or entry["id"]
        parts.append(
            f"<li><b>{html.escape(title)}</b> — {html.escape(msg)}</li>"
        )
    parts.append("</ul>")
    return "".join(parts)
