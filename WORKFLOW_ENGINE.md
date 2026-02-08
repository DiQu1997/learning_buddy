# Learning Buddy — Workflow Engine Design

## Overview

A state-machine-driven workflow engine that processes PDF resources into NotebookLM learning artifacts. LLM reasoning is used only at specific decision points; the rest is deterministic orchestration.

---

## Pipeline Stages

```
INPUT → CLASSIFY → CHUNK (if needed) → UPLOAD → GENERATE → POLL → DOWNLOAD → DONE
```

### Stage 1: INPUT
- Accept one or more PDF file paths
- Validate files exist and are readable
- Create a **Job** record in SQLite

### Stage 2: CLASSIFY
- Determine document type and chunking need
- Heuristic-first approach:
  - Page count < 50 → skip chunking
  - Page count >= 50 → run chunker
- Optional LLM classification for borderline cases or to detect content type (article vs textbook vs paper)
- Output: `needs_chunking: bool`, `doc_type: str`

### Stage 3: CHUNK (conditional)
- Runs `book_chunker.py` on documents that need splitting
- Produces N PDF parts in a local directory
- Records each chunk as a **Task** in the database
- Skipped for short documents (the original PDF becomes the single "chunk")

### Stage 4: UPLOAD
- Create a NotebookLM notebook (one per job)
- Upload each chunk/document as a source using `source_add(source_type="file")`
- Use `--wait` to block until each source is processed
- Record `notebook_id` and `source_id` for each chunk in the database

### Stage 5: GENERATE
- For each source, queue artifact generation requests
- Use `studio_create` with `source_ids=[source_id]` to scope to that chunk
- Respect concurrency limit (max 2-3 simultaneous generations)
- Default artifact set per chunk:
  - `report` (Study Guide or Briefing Doc)
  - `slide_deck`
- Extended set (configurable):
  - `audio` (podcast)
  - `video`
  - `quiz`
  - `flashcards`
  - `mind_map`
  - `infographic`

### Stage 6: POLL
- Poll `studio_status` for each pending artifact
- Update database records as artifacts complete
- Backoff strategy: poll every 30s, max wait 10 min per artifact
- On failure: mark artifact as `FAILED`, continue with others

### Stage 7: DOWNLOAD
- Download completed artifacts using `download_artifact`
- Organize into output directory structure (see below)
- Record download paths in database

### Stage 8: DONE
- Mark job as complete
- Print summary of what was generated and where files are

---

## Data Model (SQLite)

The database serves **two purposes**:
1. **Task queue** — tracks workflow state during processing (status fields)
2. **Permanent registry** — stores all NotebookLM IDs, file paths, and metadata for future lookup and operations

After a job completes, records are **never deleted**. They become the catalog you query to find notebooks, re-download artifacts, generate additional materials, share, or query your sources later.

### `notebooks` table (permanent registry)

Central registry of all NotebookLM notebooks created by the system.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key (local) |
| notebook_id | TEXT | **NotebookLM notebook UUID** |
| name | TEXT | Human-readable name |
| description | TEXT | What this notebook contains |
| doc_type | TEXT | book, paper, article, batch, etc. |
| tags | TEXT (JSON) | User-defined tags for search/filtering |
| public_url | TEXT | Public share link (if shared) |
| created_at | TIMESTAMP | |

### `sources` table (permanent registry)

Every source uploaded to NotebookLM, linked to its notebook and local file.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key (local) |
| notebook_id | TEXT | FK → notebooks.notebook_id |
| source_id | TEXT | **NotebookLM source UUID** |
| title | TEXT | Chapter/section title |
| source_index | INT | Order within the notebook |
| file_path | TEXT | Local path to the PDF/file |
| original_pdf | TEXT | Path to the original (pre-chunked) PDF |
| page_range | TEXT | e.g. "45-92" (null if not chunked) |
| is_chunk | BOOL | Whether this is a chunk of a larger document |
| created_at | TIMESTAMP | |

### `artifacts` table (permanent registry)

Every artifact generated, with its NotebookLM ID and local download path.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key (local) |
| notebook_id | TEXT | FK → notebooks.notebook_id |
| source_id | TEXT | FK → sources.source_id (null if whole-notebook artifact) |
| artifact_type | TEXT | report, slide_deck, audio, video, quiz, etc. |
| artifact_id | TEXT | **NotebookLM artifact UUID** |
| format_detail | TEXT | e.g. "Study Guide", "deep_dive", "detailed_deck" |
| download_path | TEXT | Local path after download |
| created_at | TIMESTAMP | |

### `jobs` table (task queue)

Tracks processing workflow state. References the permanent registry tables.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key |
| notebook_ref | TEXT | FK → notebooks.id |
| status | TEXT | PENDING, CLASSIFYING, CHUNKING, UPLOADING, GENERATING, POLLING, DOWNLOADING, DONE, FAILED |
| input_paths | TEXT (JSON) | List of input PDF paths |
| config | TEXT (JSON) | Artifact config, max_pages, etc. |
| error | TEXT | Error message if FAILED |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

### `tasks` table (task queue)

Individual work items within a job (upload a chunk, generate an artifact, download).

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key |
| job_id | TEXT | FK → jobs.id |
| task_type | TEXT | UPLOAD, GENERATE, DOWNLOAD |
| target_ref | TEXT | FK → sources.id or artifacts.id depending on task_type |
| status | TEXT | QUEUED, IN_PROGRESS, COMPLETED, FAILED |
| attempts | INT | Retry count |
| error | TEXT | Error message if FAILED |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

### Relationship Diagram

```
notebooks (permanent)
  │
  ├── sources (permanent)        ← uploaded chunks/docs with NotebookLM source_ids
  │
  ├── artifacts (permanent)      ← generated artifacts with NotebookLM artifact_ids
  │
  └── jobs (task queue)          ← processing workflow state
       │
       └── tasks (task queue)    ← individual work items (upload/generate/download)
```

### Lookup Examples

```sql
-- Find the notebook for a book I processed
SELECT * FROM notebooks WHERE name LIKE '%Reinforcement Learning%';

-- List all sources in a notebook
SELECT * FROM sources WHERE notebook_id = 'abc123' ORDER BY source_index;

-- Find all slide decks I've generated
SELECT a.*, n.name as notebook_name, s.title as source_title
FROM artifacts a
JOIN notebooks n ON a.notebook_id = n.notebook_id
LEFT JOIN sources s ON a.source_id = s.source_id
WHERE a.artifact_type = 'slide_deck';

-- Get everything for a specific book chapter
SELECT * FROM artifacts
WHERE source_id = (SELECT source_id FROM sources WHERE title LIKE '%Chapter 3%' AND notebook_id = 'abc123');

-- Find all notebooks tagged with "machine-learning"
SELECT * FROM notebooks WHERE tags LIKE '%machine-learning%';
```

---

## Concurrency & Rate Limiting

### Architecture

```
                    ┌─────────────────────┐
                    │   Artifact Queue     │
                    │  (SQLite: QUEUED)    │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────────┐
                    │   Worker Loop       │
                    │  max_concurrent = 3 │
                    │  courtesy_delay = 3s│
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         ┌─────────┐   ┌─────────┐   ┌─────────┐
         │ Gen #1  │   │ Gen #2  │   │ Gen #3  │
         │ (poll)  │   │ (poll)  │   │ (poll)  │
         └─────────┘   └─────────┘   └─────────┘
```

- Worker loop pulls QUEUED artifacts from the database
- Fires `studio_create`, marks as GENERATING
- Polls `studio_status` with backoff (30s intervals)
- On completion: marks COMPLETED, queues download
- On failure: marks FAILED, logs error, moves on
- Max 3 concurrent generations to avoid rate limits

### Rate Limiting Strategy

No hard-coded throttle. Instead, use **reactive backoff** — go as fast as the API allows, slow down only when told to.

**Uploads** (`source_add --wait`):
- No explicit delay needed. The `--wait` flag blocks until NotebookLM finishes processing each source (several seconds per source), which acts as a natural gap between requests.

**Artifact generation** (`studio_create`):
- These are fast fire-and-forget calls (polling is separate), so they can burst quickly.
- Use a **courtesy delay** of 2-3 seconds between `studio_create` calls as cheap insurance against burst detection. This barely affects total runtime since actual generation takes minutes anyway.

**On rate limit error (HTTP 429 or "Rate limit exceeded")**:
```
attempt 1: request
  → 429? wait 30s, retry
attempt 2: retry
  → 429? wait 60s, retry
attempt 3: retry
  → 429? wait 120s, retry
attempt 4: retry
  → 429? mark FAILED, log, move on to next item
```

**Summary**:
| Operation | Base Delay | Backoff on 429 | Max Retries |
|-----------|-----------|----------------|-------------|
| Source upload | None (--wait is natural gap) | 30s → 60s → 120s | 3 |
| Artifact generation | 2-3s courtesy delay | 30s → 60s → 120s | 3 |
| Status polling | 30s between polls | 60s → 120s | 3 |
| Download | None | 30s → 60s | 2 |

> **Why reactive, not preemptive**: NotebookLM's rate limits aren't publicly documented, so exact thresholds are unknown. Reactive backoff handles that uncertainty — fast when limits are generous, automatically slows when they're not.

---

## Output Directory Structure

```
output/
└── {job_name}/
    ├── chunks/                          # Raw PDF chunks (if chunked)
    │   ├── 01_Introduction.pdf
    │   ├── 02_Chapter_1.pdf
    │   └── ...
    ├── artifacts/
    │   ├── 01_Introduction/
    │   │   ├── study_guide.md
    │   │   ├── slides.pdf
    │   │   ├── podcast.mp3
    │   │   └── ...
    │   ├── 02_Chapter_1/
    │   │   ├── study_guide.md
    │   │   ├── slides.pdf
    │   │   └── ...
    │   └── ...
    └── job_summary.json                 # Metadata, status, paths
```

---

## Configuration

```python
DEFAULT_CONFIG = {
    "max_pages_per_chunk": 50,
    "chunk_threshold": 50,           # Pages before chunking kicks in
    "default_artifacts": [
        "report",                    # Study Guide
        "slide_deck",
    ],
    "extended_artifacts": [          # User can opt-in
        "audio",
        "video",
        "quiz",
        "flashcards",
        "mind_map",
        "infographic",
    ],
    "report_format": "Study Guide",  # Or "Briefing Doc", "Blog Post"

    # Rate limiting & concurrency
    "max_concurrent_generations": 3,
    "courtesy_delay_seconds": 3,     # Pause between studio_create calls
    "poll_interval_seconds": 30,
    "poll_max_wait_seconds": 600,
    "backoff_base_seconds": 30,      # First retry wait on 429
    "backoff_multiplier": 2,         # Exponential: 30 → 60 → 120
    "max_retries": 3,
}
```

---

## CLI Interface (proposed)

### Processing
```bash
# Process a single PDF
learning-buddy process paper.pdf

# Process a book with custom chunk size
learning-buddy process textbook.pdf --max-pages 40

# Process multiple related PDFs together
learning-buddy process paper1.pdf paper2.pdf paper3.pdf --name "ML Survey"

# Process with extended artifacts
learning-buddy process book.pdf --artifacts report,slide_deck,audio,quiz

# Tag for future lookup
learning-buddy process book.pdf --tags "ml,reinforcement-learning,textbook"
```

### Job Management
```bash
# Check status of a running job
learning-buddy status <job-id>

# Resume a failed/interrupted job
learning-buddy resume <job-id>

# List active/recent jobs
learning-buddy jobs
```

### Registry (permanent catalog)
```bash
# List all processed notebooks
learning-buddy library
learning-buddy library --tag "machine-learning"
learning-buddy library --type book

# Show details for a notebook (sources, artifacts, paths)
learning-buddy info <notebook-name-or-id>

# List all artifacts of a type
learning-buddy artifacts --type slide_deck
learning-buddy artifacts --notebook <id>

# Re-download an artifact
learning-buddy download <artifact-id>

# Generate additional artifacts for an existing source
learning-buddy generate <source-id> --artifacts audio,quiz

# Open NotebookLM in browser for a notebook
learning-buddy open <notebook-name-or-id>

# Query a notebook's sources
learning-buddy query <notebook-name-or-id> "What is policy gradient?"
```

---

## Resumability

The SQLite-backed state machine ensures resumability at every stage:

| Crash Point | Resume Behavior |
|------------|-----------------|
| During chunking | Re-run chunker (idempotent if output dir exists) |
| During upload | Skip already-uploaded chunks (have source_id), upload remaining |
| During generation | Skip COMPLETED artifacts, re-queue GENERATING (may have timed out) |
| During polling | Resume polling for GENERATING artifacts |
| During download | Skip already-downloaded, download remaining |

On resume, the engine reads the database, finds the current state, and picks up from there.

---

## Error Handling

| Error | Strategy |
|-------|----------|
| NotebookLM auth expired | Prompt user to run `nlm login`, pause and retry |
| Source upload fails | Retry once, then mark chunk FAILED and continue |
| Artifact generation fails | Mark FAILED, log error, continue with other artifacts |
| Rate limit hit | Exponential backoff (30s → 60s → 120s) |
| Chunker fails | Fall back to fixed-size page splitting |
| Download fails | Retry once, then mark FAILED |

---

## Key Dependencies

- `book_chunker.py` — PDF splitting (requires PyMuPDF, OpenAI)
- `nlm` CLI / NotebookLM MCP — notebook management and artifact generation
- `sqlite3` — task queue and state persistence (stdlib)
- `asyncio` — concurrent artifact generation and polling
