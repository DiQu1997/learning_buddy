# Learning Buddy — Design v2

A local agent that ingests files dropped into an inbox folder, classifies them with an LLM, organizes them into a library hierarchy, and reconciles the result into NotebookLM. All bookkeeping lives in plain JSON files. No git. No daemon. One CLI verb.

## Three folders, configured once

| folder | who writes | who reads from disk |
|---|---|---|
| `inbox/` | user only | agent (walks every run) |
| `library/` | agent only | NLM upload reads files; **agent never re-walks library** |
| `metadata/` | agent only | agent (catalog scan every run) |

Configured in `~/.config/learning-buddy/config.json` (path overridable via `LEARNING_BUDDY_CONFIG`):

```json
{
  "inbox":    "/Users/qudi/Google Drive/My Drive/knowledge/knowledge_dropbox",
  "library":  "/Users/qudi/Google Drive/My Drive/knowledge/knowledge_source",
  "metadata": "/Users/qudi/knowledge_metadata",
  "split":    { "min_pages_to_split": 35, "max_pages_per_chunk": 25 },
  "bucket_capacity": 25,
  "artifacts": ["note", "slide_deck", "video", "audio", "mind_map"],
  "llm":      { "model": "gpt-5-mini" },
  "nlm":      { "verify_interval_seconds": 30, "max_retries": 5 }
}
```

The catalog is the single source of truth for "what's in library". Library on disk is just bytes; the catalog tells you it exists.

## Metadata file layout

```
metadata/
├── catalog.json
└── resources/
    └── <resource_id>.json    (one file per resource — book, paper, blog)
```

No git. Atomic writes via `tmp + os.replace`.

## `catalog.json` — small index, scanned every run

One entry per resource. All fields populated by the end of Phase A (intake).

```jsonc
{
  "version": 1,
  "updated_at": "2026-05-08T…",
  "resources": [
    {
      "id":              "f_a1b2c3d4",
      "sha256":          "…",
      "title":           "Deep Learning",
      "authors":         ["Goodfellow", "Bengio", "Courville"],
      "kind":            "book",                                     // book | paper | blog | slides | note | article | transcript | other
      "category":        ["CS", "AI", "DL", "books"],
      "library_path":    "CS/AI/DL/books/Deep Learning.pdf",         // RELATIVE to library root
      "toc":             ["Ch 1 Introduction", "Ch 2 …", …],
      "page_count":      802,
      "overall_status":  "IN_PROGRESS",                              // NEW | IN_PROGRESS | DONE | FAILED
      "created_at":      "2026-05-08T…",
      "updated_at":      "2026-05-08T…"
    }
  ]
}
```

`head_text` (the LLM input) is **not stored** here — it is transient, computed during Phase A and discarded after the LLM call.

## `resources/<id>.json` — task queue for ONE resource

```jsonc
{
  "resource_id": "f_a1b2c3d4",
  "notebook_id": "nb_xxx",                                           // NLM notebook for this resource
  "sources": [                                                       // one entry per NLM source (chunk; or just one if not split)
    {
      "idx":           1,
      "title":         "Chapter 1 Introduction",
      "library_path":  "CS/AI/DL/books/Deep Learning/01_introduction.pdf",   // relative
      "page_range":    [1, 24],
      "nlm_source_id": "src_xxx",                                    // populated when upload task is DONE
      "tasks": [
        {"type": "upload",     "state": "DONE",        "retry_count": 0, "last_error": null},
        {"type": "note",       "state": "DONE",        "retry_count": 0, "nlm_artifact_id": "art_a", "url": "https://notebooklm.google.com/…"},
        {"type": "slide_deck", "state": "PROCESSING",  "retry_count": 1, "nlm_artifact_id": "art_b", "last_error": "transient timeout"},
        {"type": "video",      "state": "NOT_STARTED", "retry_count": 0},
        {"type": "audio",      "state": "DONE",        "retry_count": 0, "nlm_artifact_id": "art_c", "url": "…"},
        {"type": "mind_map",   "state": "FAILED",      "retry_count": 5, "last_error": "permanent error"}
      ]
    }
  ]
}
```

A **task is a unit of work with a state**. Each NLM source has exactly one `upload` task plus one task per artifact type. All tasks share the same state machine.

## State machine — same for every task

```
                      ┌── NLM reports DONE ─────────────────────────► DONE       (terminal)
                      │
NOT_STARTED ── run ──►│ PROCESSING ── NLM reports still in flight ──► stays PROCESSING (no counter change)
   (kick off)         │
                      └── NLM reports FAILED ──► retry_count += 1
                                                 ├── if < 5: re-issue this run, stays PROCESSING
                                                 └── if = 5: FAILED                   (terminal)
```

Hard rules:

- A counter only increments when NLM **reports a failure**. A 30-min video still rendering does NOT burn a retry — it stays at PROCESSING with `retry_count` unchanged.
- **Maximum one retry per `learning-buddy run` per task.** The next retry waits for the next run.
- DONE and FAILED are terminal. No further attempts on those tasks.

## Per-run flow

`learning-buddy run` does exactly the two phases below in order, then exits.

### Phase A — Intake (inbox → catalog)

```
for each PDF/EPUB in inbox/ (skipping _dups/, _log.txt, .DS_Store):

    fingerprint:
        sha256       = whole-file hash
        page_count   = real page count (PDF) / estimated (EPUB)
        toc          = embedded outline (doc.get_toc / EPUB nav)  OR  null
        head_text    = first 1 page (page_count < 35)  OR  first ~15 pages (>= 35), capped at 12 KB
                       (transient — not stored anywhere durable)

    LLM classify, given the fingerprint and the current catalog:
        → {is_dup_of, title, authors, kind, category, toc_extracted}
        - if embedded toc was null, the LLM extracts one from head_text

    if is_dup_of is set:
        move file to inbox/_dups/<id>__<filename>
        append a line to inbox/_log.txt
        continue

    library_path = "/".join(category) + "/" + filename                # RELATIVE
    move file from inbox/ to library/<library_path>
    append a row to catalog.json with overall_status = NEW
    atomic save catalog.json
```

Classification happens **before** the catalog add. Only non-duplicate files become catalog entries. All persistent metadata (sha256, title, authors, kind, category, toc, page_count, library_path) is populated in this single pass.

### Phase B — Drain (catalog → tasks)

```
for each entry in catalog.json where overall_status in {NEW, IN_PROGRESS}:

    if resources/<id>.json does not exist:
        decide chunking:
            if kind == "book" and page_count >= 35:
                split via book_chunker into chapters of <= 25 pages each
                materialize chunks under library/<book_folder>/01_*.pdf, 02_*.pdf, …
            else:
                single source — uses library_path from catalog directly
        write resources/<id>.json:
            sources[]: one per chunk (or one entry if not split)
            each source.tasks[]: [upload, note, slide_deck, video, audio, mind_map] all at NOT_STARTED
        atomic save resources/<id>.json
        flip catalog.overall_status to IN_PROGRESS

    open resources/<id>.json
    for each source in sources:
        advance the upload task by one step (see below)
        if upload-task state == DONE:
            for each artifact-task:
                advance one step

    recompute resource overall_status:
        all tasks DONE     → DONE
        any task FAILED    → FAILED
        otherwise          → IN_PROGRESS
    update catalog.json overall_status if it changed
    atomic save
```

"Advance one step" for a task:

| current state | action this run |
|---|---|
| NOT_STARTED | issue the kick-off command (`nlm source add` for upload, `nlm <type> create` for an artifact). On success → state = PROCESSING, store `nlm_source_id` / `nlm_artifact_id`. On error → handle as a "PROCESSING with retry" path below. |
| PROCESSING | verify (`nlm studio status` for an artifact; for upload, the `nlm source add --wait` either succeeded synchronously or returned an error). If verify says DONE → state = DONE. If still in flight → no change. If NLM reports failed → `retry_count += 1`; if `< 5` re-issue once and stay PROCESSING; if `== 5` state = FAILED. |
| DONE | no-op |
| FAILED | no-op |

At most one kick-off **plus** one verify + at most one retry per task per run. No internal polling loop.

## Dedup logic (called inside Phase A)

In order:

1. **sha256 exact match** against catalog → duplicate.
2. **LLM semantic match** based on title + authors + ToC overlap (≥ 70% of chapter titles matching counts as the same work even across different scans / editions / file formats) → duplicate.
3. Otherwise → new resource.

Filename is never trusted as identity.

## Notebook assignment (decided when writing `resources/<id>.json` for the first time)

- `kind == "book"` → its own NotebookLM notebook, named after the book's title.
- `kind ∈ {paper, blog, article, slides, note, transcript}` → bucket notebook for that category, e.g. `NLP Papers 1`. If the bucket has reached `bucket_capacity` (25), create the next one (`NLP Papers 2`). Source counts are queried live from NLM each time, never cached in the catalog.

## What the agent NEVER does

- Re-walks `library/` from disk
- Touches files in `library/` after the first move
- Mutates files in `inbox/_dups/`
- Holds a single iteration hostage on long polling — every task gets at most one verify + one retry per run
- Talks to git
- Stores the LLM's `head_text` in the catalog

## Lifecycle of one new book, end to end

```
Run 1: file appears in inbox
  Phase A: fingerprint → ToC pulled from PDF outline (free) → LLM classifies as
           book / CS/AI/DL/books / "Deep Learning" / not a dup
           move file to library/CS/AI/DL/books/Deep Learning.pdf
           catalog.json: + entry, overall_status=NEW, all metadata fields populated

  Phase B: catalog entry at NEW, no resources file yet
           split into 70 chunks via book_chunker
           write resources/f_a1b2c3d4.json — 70 sources × 6 tasks each = 420 tasks all NOT_STARTED
           catalog.overall_status → IN_PROGRESS
           advance each task by one step:
             - upload tasks: kick off `nlm source add --wait` (synchronous, mostly DONE same run)
             - artifact tasks (only those whose upload is DONE): kick off `nlm <type> create` → PROCESSING
           save

Run 2 (cron-driven, minutes later):
  Phase A: nothing new in inbox
  Phase B: entry at IN_PROGRESS
           open resources file
           advance each PROCESSING task one step:
             nlm studio status → DONE if completed; stays PROCESSING if rendering;
             counter logic if NLM-reported failure
           save

Run N: video task has been PROCESSING for 8 hours — NLM still rendering →
       stays PROCESSING, retry_count unchanged

Run N+1: video DONE on NLM → marked DONE in resource file
         all 420 tasks DONE → catalog.overall_status = DONE
         further runs skip this entry entirely
```

## Long-running

`learning-buddy run` is one-shot by design. To run continuously, use the OS:

```cron
*/5 * * * * /usr/local/bin/learning-buddy run >> ~/learning-buddy.log 2>&1
```

A simple file lock at `metadata/.learning-buddy.lock` (acquired via `fcntl.flock`) prevents two simultaneous `run` invocations on one machine. No internal supervisor, no `--loop`, no daemon code.

## CLI surface

Three subcommands:

```
learning-buddy run             # the two-phase flow above
learning-buddy status          # print catalog summary + resource counts by overall_status
learning-buddy config show|set # read / write ~/.config/learning-buddy/config.json
```

## What's gone from earlier designs

- No git, no commits, no push/pull, no remote
- No `--loop` flag, no internal supervisor
- No library walk
- No file moves except `inbox → library` and `inbox → inbox/_dups`
- No mid-stage commits, no audit history beyond `inbox/_log.txt`
- No long-running per-file polling deadline that holds the iteration hostage
- No `head_text` stored in the catalog
