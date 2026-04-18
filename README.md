# Learning Buddy

Learning Buddy is a state-machine workflow engine for turning source documents into NotebookLM learning artifacts.

It accepts local `PDF` and `EPUB` files, decides whether each document should be chunked, uploads the resulting sources to NotebookLM, generates artifacts per source, downloads completed outputs, and records everything in a local SQLite registry.

## What It Does

- Processes one document or a batch of documents in a single job
- Supports `PDF` and `EPUB` inputs
- Chunks long documents before upload
- Uploads sources to NotebookLM through the `nlm` CLI
- Generates artifacts such as study guides, reading notes, slide decks, videos, audio, quizzes, and more
- Stores job, source, and artifact metadata in `.learning_buddy/workflow.db`
- Supports resuming interrupted jobs

## Current Input Behavior

### PDF

- Short PDFs are uploaded directly
- Large PDFs are chunked with `book_chunker.py`
- If the PDF chunker fails, Learning Buddy falls back to fixed-size PDF splitting

### EPUB

- EPUBs are parsed as text-first documents
- Learning Buddy uses EPUB navigation metadata if available
  - EPUB 3 nav document first
  - EPUB 2 NCX as fallback
- If no usable ToC exists, it falls back to spine order and detected headings
- EPUB chunks are materialized as local `.txt` files before upload
- EPUB images are ignored
- There is no OCR or image-reading path for EPUB

## Prerequisites

You need:

- Python `>=3.10`
- The `nlm` CLI available on your `PATH`
- A valid NotebookLM login for the `nlm` CLI

Before running jobs, verify NotebookLM auth:

```bash
nlm login --check
```

If that fails, authenticate first with your installed `nlm` client.

## Installation

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

The package exposes the `learning-buddy` command and also supports module execution:

```bash
python -m learning_buddy --help
learning-buddy --help
```

## Quick Start

### Run the Whole Pipeline for an EPUB

This is the simplest end-to-end command:

```bash
learning-buddy process /absolute/path/to/book.epub
```

That will:

1. Validate the EPUB
2. Estimate whether it should be chunked
3. Parse ToC or fall back to spine/heading structure
4. Materialize source files as `.txt`
5. Upload them to NotebookLM
6. Generate the default artifacts
7. Download completed outputs into `output/<job-name>/`

### Common EPUB Examples

Process one EPUB with a custom job name:

```bash
learning-buddy process /absolute/path/to/book.epub --name "deep_learning_textbook"
```

Lower the chunk size target for a large EPUB:

```bash
learning-buddy process /absolute/path/to/book.epub --max-pages 30
```

Request extra artifact types:

```bash
learning-buddy process /absolute/path/to/book.epub --artifacts report,slide_deck,audio,quiz
```

Add tags to the notebook registry entry:

```bash
learning-buddy process /absolute/path/to/book.epub --tags ml,textbook,epub
```

Process a mixed batch of PDFs and EPUBs:

```bash
learning-buddy process ./paper.pdf ./notes.epub ./book.epub --name "rl_batch"
```

## How Chunking Works

### Threshold

By default:

- Documents below `50` units are uploaded as a single source
- Documents at or above `50` units are chunked first

For PDFs, the unit is real page count.

For EPUBs, the unit is an estimated page count derived from extracted word count.

### EPUB Chunking Rules

Learning Buddy uses this fallback order:

1. Use EPUB ToC if present and usable
2. Fall back to spine order
3. Fall back to headings found inside content documents

If a single EPUB section is still too large, it is split into `Part N` text chunks.

## Default Artifacts

By default, each uploaded source gets:

- `report`
- `slide_deck`
- `video`
- `note`

Additional supported artifact types:

- `audio`
- `quiz`
- `flashcards`
- `mind_map`
- `infographic`

Example:

```bash
learning-buddy process ./book.epub --artifacts report,slide_deck,video,note,audio,flashcards
```

## CLI Reference

Top-level commands:

```text
process
status
inspect
resume
jobs
library
info
artifacts
download
generate
open
query
```

### `process`

Run a full job from local input files.

```bash
learning-buddy process INPUT [INPUT ...] [--name NAME] [--max-pages N] [--artifacts LIST] [--tags LIST]
```

### `status`

Show raw job status:

```bash
learning-buddy status <job-id>
```

### `inspect`

Show structured progress, pending tasks, and failure information:

```bash
learning-buddy inspect
learning-buddy inspect <job-id>
learning-buddy inspect <job-id> --pending-limit 50
learning-buddy inspect --include-completed
```

### `resume`

Resume a failed or interrupted job:

```bash
learning-buddy resume <job-id>
```

### `jobs`

List recent jobs:

```bash
learning-buddy jobs
learning-buddy jobs --limit 20
```

### `library`

List notebook registry entries:

```bash
learning-buddy library
learning-buddy library --tag textbook
learning-buddy library --type book
```

### `info`

Show notebook details from the local registry:

```bash
learning-buddy info <notebook-name-or-id>
```

### `artifacts`

List generated artifacts from the local registry:

```bash
learning-buddy artifacts
learning-buddy artifacts --type report
learning-buddy artifacts --notebook <notebook-name-or-id>
```

### `download`

Re-download an artifact by local or remote artifact ID:

```bash
learning-buddy download <artifact-id>
```

### `generate`

Generate additional artifacts for an existing source:

```bash
learning-buddy generate <source-id> --artifacts audio,quiz
```

### `open`

Open a notebook in the browser:

```bash
learning-buddy open <notebook-name-or-id>
```

### `query`

Ask NotebookLM a question against a stored notebook:

```bash
learning-buddy query <notebook-name-or-id> "What are the core arguments in chapter 3?"
```

## Output Layout

Jobs write into:

```text
output/<job-name>/
```

Typical structure:

```text
output/<job-name>/
├── chunks/
│   ├── 01_<source>.pdf|txt
│   ├── 02_<source>.pdf|txt
├── artifacts/
│   ├── 01_<source-title>/
│   │   ├── 01_<resource-file>__<source-title>__study_guide.md
│   │   ├── 01_<resource-file>__<source-title>__reading_note.md
│   │   ├── 01_<resource-file>__<source-title>__slides.txt
│   │   ├── 01_<resource-file>__<source-title>__video.mp4
│   │   └── ...
└── job_summary.json
```

`job_summary.json` contains:

- job ID and status
- notebook IDs
- output directory
- source counts
- artifact counts
- task counts

## Local State

Learning Buddy stores persistent workflow state in:

```text
.learning_buddy/workflow.db
```

That registry tracks:

- notebooks
- uploaded sources
- generated artifacts
- jobs
- tasks

This is what makes `resume`, `library`, `info`, `artifacts`, and `download` work.

## Example End-to-End Session

Run a job:

```bash
learning-buddy process ~/Books/transformers.epub --name transformers_book --artifacts report,slide_deck,audio
```

Inspect progress:

```bash
learning-buddy inspect
```

Resume later if needed:

```bash
learning-buddy resume <job-id>
```

Open the notebook:

```bash
learning-buddy open transformers_book
```

Query the notebook:

```bash
learning-buddy query transformers_book "Summarize the key ideas in the attention chapters."
```

## Limitations

- EPUB processing is text-based only
- EPUB images are not extracted or interpreted
- NotebookLM upload support is driven through the installed `nlm` CLI
- EPUBs are uploaded as generated `.txt` sources, not as raw `.epub` files
- Very malformed EPUB archives may fail during parsing
- PDF chunking remains more advanced than EPUB chunking because PDFs still use the dedicated `book_chunker.py` flow

## Development

Run tests:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

See CLI help:

```bash
learning-buddy --help
learning-buddy process --help
```
