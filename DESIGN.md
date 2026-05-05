# Learning Buddy — Design

A single command, `learning-buddy run`, that turns a Drive inbox of books / papers / blogs into a tidy NotebookLM workspace, with all bookkeeping in a JSON file under git.

Run it as often as you want. Each run is idempotent: pick up new files, finish anything left in flight, stop.

## Pieces

Three locations, all configurable:

| name | where | what's in it |
|---|---|---|
| `inbox` | Google Drive folder | new files the user drops in; agent reads + moves them out; agent writes a `_log.txt` here |
| `library` | Google Drive folder | classified files in a hierarchy: `<top>/<sub…>/<type>/<filename>`. Agent owns this layout. |
| `database` | local folder, **not** in Drive | a git working tree containing `catalog.json` + `outline.html`. Source of truth across machines is the git remote (e.g., GitHub), not Drive sync. |

Artifacts (mp3, mp4, slides, mindmap, note) **stay on NotebookLM**. Catalog records the NLM URL; nothing is downloaded.

## What `learning-buddy run` does

For each new file in `inbox/`:

1. **Fingerprint + dedup.** LLM looks at filename, title, and first 1–2 pages (cover, author, edition). Compare to existing entries in `catalog.json`. If duplicate → move file to `inbox/_dups/`, log reason, skip.
2. **Classify.** LLM picks a category path. Categories are not a fixed taxonomy: the LLM is shown the existing hierarchy in the catalog, prefers reusing existing nodes, may invent new sub-nodes when nothing fits. Last segment is always a type bucket (`books`, `papers`, `blogs`, …).
3. **Split.** If kind ∈ `{book}` and page count ≥ 35 → split via `book_chunker.py` into chapters of ≤25 pages each. Papers / blogs / short books → single resource.
4. **Move to library.** Physically `mv` the file from `inbox/` to `library/<classified path>/`. Split chapters land alongside as `01_chapter_title.pdf` etc.
5. **Assign to notebook.**
   - Book → its own new notebook, named after the book.
   - Paper / blog → bucket notebook for that category, e.g. `NLP Papers 1`. If current bucket has 25 sources, create `NLP Papers 2`. Source counts are re-queried from NLM each run, not cached in the catalog.
6. **Upload + generate.** For each new resource:
   - `nlm source add` to its notebook
   - Kick off all five artifacts: `note` (using the existing 阅读版本 prompt), `slide_deck`, `video`, `audio`, `mind_map`
   - Poll until each completes; record the NLM URL in the catalog
7. **Commit.** `git add catalog.json && git commit -m "run @ <timestamp>"`. Optionally `git push` if a remote is configured.
8. **Re-render `outline.html`.** Static HTML rendered from `catalog.json`. Top of `database/`, no build tools.

If any step fails for a given file, mark its status in the catalog (`status=failed`, with reason), commit, continue with the rest. Next run retries.

## `catalog.json` schema (sketch)

```json
{
  "version": 1,
  "updated_at": "2026-05-03T16:00:00Z",
  "files": [
    {
      "id": "f_<short-uuid>",
      "title": "Deep Learning",
      "authors": ["Goodfellow", "Bengio", "Courville"],
      "kind": "book",
      "category": ["CS", "AI", "DL", "books"],
      "library_path": "library/CS/AI/DL/books/Deep Learning.pdf",
      "fingerprint": {
        "sha256": "…",
        "size": 12345678,
        "title_norm": "deep learning",
        "first_pages_excerpt": "…"
      },
      "notebook_id": "nb_xxx",
      "resources": [
        {
          "idx": 1,
          "title": "Chapter 1 — Introduction",
          "page_range": [1, 24],
          "library_path": "library/CS/AI/DL/books/Deep Learning/01_introduction.pdf",
          "nlm_source_id": "src_xxx",
          "artifacts": {
            "note":       {"status": "done", "url": "https://notebooklm.google.com/…"},
            "slide_deck": {"status": "done", "url": "…"},
            "video":      {"status": "in_progress"},
            "audio":      {"status": "done", "url": "…"},
            "mind_map":   {"status": "failed", "error": "rate_limited", "retry_after": "2026-05-03T17:00:00Z"}
          }
        }
      ],
      "status": "in_progress",
      "log": [
        {"ts": "2026-05-03T15:30:00Z", "msg": "classified to CS/AI/DL/books"},
        {"ts": "2026-05-03T15:35:00Z", "msg": "split into 18 chapters"}
      ]
    }
  ],
  "notebooks": [
    {
      "id": "nb_xxx",
      "title": "Deep Learning",
      "role": "book",
      "category": ["CS", "AI", "DL", "books"]
    },
    {
      "id": "nb_yyy",
      "title": "NLP Papers 1",
      "role": "bucket",
      "category": ["CS", "AI", "NLP", "papers"]
    }
  ]
}
```

`inbox/_log.txt` is appended each run, one line per file action — duplicates, classifications, errors. Plain text, easy to read in the Drive web UI.

## Resumability

Catalog status is the only state. Next run reconstructs everything from JSON + a fresh `nlm notebook list`:

- `artifact.status == "in_progress"` → re-poll NLM; if done, record URL.
- `artifact.status == "failed"` → retry (with `nlm_client`'s existing backoff).
- `artifact.status == "done"` → skip.
- `file.status == "failed"` (e.g., classification crashed) → retry from step 1.

No stage state machine. The status fields ARE the state machine.

## Config file

`~/.config/learning-buddy/config.json` (path overridable via `LEARNING_BUDDY_CONFIG`):

```json
{
  "inbox": "/Users/qudi/Library/CloudStorage/GoogleDrive-…/learning_buddy/inbox",
  "library": "/Users/qudi/Library/CloudStorage/GoogleDrive-…/learning_buddy/library",
  "database": "/Users/qudi/learning_buddy_db",
  "git_remote": "git@github.com:qudi/learning_buddy_db.git",
  "llm": { "provider": "anthropic", "model": "claude-sonnet-4-6" },
  "split": { "min_pages_to_split": 35, "max_pages_per_chunk": 25 },
  "bucket_capacity": 25,
  "artifacts": ["note", "slide_deck", "video", "audio", "mind_map"]
}
```

## CLI surface

Three commands, that's it:

```
learning-buddy run                # one pass: scan, classify, upload, generate, commit, render
learning-buddy status             # quick summary of catalog state (counts of in_progress / failed / done)
learning-buddy config show|set    # read/write the config file
```

Long-running mode is just a shell `while true; learning-buddy run; sleep 1h; done` or a launchd / cron entry. No daemon code.

## What we keep from v1

| file | role in v2 |
|---|---|
| `learning_buddy/nlm_client.py` | unchanged — subprocess wrapper around `nlm` CLI, with backoff |
| `book_chunker.py` | unchanged — LLM-aware PDF chapter splitter |
| `learning_buddy/chunking.py` | dispatch to PDF / EPUB chunkers |
| `learning_buddy/epub.py` | unchanged — EPUB parser |
| `learning_buddy/utils.py` | unchanged — small helpers |
| `learning_buddy/config.py` | trim down: keep `DEFAULT_NOTE_PROMPT`, drop the v1 `EngineConfig` and v0.3 `LibraryConfig` / `PolicyConfig` |

## What we throw away

| file | why |
|---|---|
| `learning_buddy/engine.py` (1355 lines) | 8-stage state machine; replaced by a small `agent.py` orchestrator that follows status fields in the JSON |
| `learning_buddy/db.py` (970 lines, SQLite) | replaced by `catalog.py` (JSON + git) |
| most of `learning_buddy/cli.py` | 12 subcommands → 3 |
| `WORKFLOW_ENGINE.md` | old design doc; this file replaces it |
| `BRAINSTORM.md` | old brainstorm; superseded |
| `README.md` | rewrite once v2 lands |
| uncommitted `learning_buddy/{adapters,controller,domain,ports,workers}/` | abandoned v0.3 reconciler scaffold |
| `learning_buddy.egg-info/` | build artifact |

## New files to write

| file | role | rough size |
|---|---|---|
| `learning_buddy/catalog.py` | load / save / mutate `catalog.json`, run git ops | ~250 lines |
| `learning_buddy/classify.py` | LLM dedup + classification calls | ~200 lines |
| `learning_buddy/agent.py` | the run loop: scan → dedup → classify → split → upload → generate → commit → render | ~400 lines |
| `learning_buddy/render.py` | catalog → `outline.html` (single static file, links into NLM) | ~150 lines |
| `learning_buddy/cli.py` | shrink to `run / status / config` | ~150 lines |
| `learning_buddy/config.py` | replace v1 dataclasses with the simple JSON config above | ~80 lines |

Total v2 footprint: ~1,200 lines net new + the kept primitives (~2,400 lines kept). Roughly half the size of v1.

## Open questions before we code

1. **LLM provider** for classify / dedup. `book_chunker.py` already wires one up — do we reuse the same provider, or pick a separate model for classification?
2. **First-run categories**. The hierarchy starts empty. The very first few files have no existing taxonomy to anchor against. OK to let the LLM seed it from scratch?
3. **Drive sync timing on `mv`**. Moving a 200 MB PDF inside Drive triggers an upload event. Race with the next run? Probably fine since the next run reads `library/` paths from `catalog.json`, not by re-walking the disk.
4. **`outline.html` style**. Single-page collapsible tree, or category index pages? I'll start with a single page; trivial to expand.
