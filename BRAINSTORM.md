# Learning Buddy — Brainstorm Notes

## Concept

An auto-processing LLM agent that takes PDF resources (articles, blogs, research papers, books), determines if they need chunking, uploads them to NotebookLM, and generates learning artifacts (docs, slide decks, videos, audio, etc.).

## Available Tools

### 1. Book Chunker (`book_chunker.py`)
- Intelligent PDF book splitter using OpenAI GPT models
- Supports text-based and image-based (scanned) PDFs
- Adaptive strategy: tries TOC-based splitting first, falls back to sequential page-by-page chapter detection
- Outputs individual PDF parts (~50 pages max each), split along chapter/section boundaries
- Validates TOC page numbers, calculates physical page offsets, handles edge cases

### 2. NotebookLM MCP / CLI (`nlm`)
- Create notebooks and add sources (PDFs, URLs, text, files)
- Generate learning materials: audio podcasts, videos, quizzes, flashcards, study guides, mind maps, slides, infographics, data tables
- Research topics on the web or Google Drive
- Query/chat with notebook sources
- Download all generated artifacts locally
- `studio_create` accepts `source_ids` — can target specific sources for artifact generation

---

## Key Design Decisions

### Input Modes
- **Single PDF**: one document at a time (article, paper, or book)
- **Batch PDFs**: multiple related documents processed together (e.g., papers on the same topic for comparison/synthesis)
- Both modes should be supported

### Chunking Decision
- The agent's first job is **classification**: what kind of document is this, and does it need chunking?
- Short content (articles, blog posts, papers < ~50 pages): upload directly, no chunking
- Long content (books, large reports): run book_chunker to split into chapter-sized parts
- Could be LLM-based classification or a simpler page-count heuristic

### Notebook Organization
- **One notebook per processing job** — all chunks/documents go into the same notebook
- The notebook serves as the **knowledge base** for the entire resource
- Cross-chunk/cross-document queries are possible since everything is in one notebook
- Individual artifacts are **focused views** into specific chunks via `source_ids`

### Artifact Generation Strategy
- Generate artifacts **per chunk separately**, not one giant artifact for everything
- Rationale: a slide deck covering 500 pages compresses and drops information
- Use `source_ids` parameter in `studio_create` to target specific chunks
- Core artifact set per chunk: slide deck, summary/report (study guide or briefing doc)
- Optional/configurable: audio podcast, video, quiz, flashcards, mind map, infographic

### Async & Task Management
- Artifact generation is async (audio: 1-5 min, video: longer, reports: 30-60s)
- Need a persistent task queue to track state
- **SQLite** is the sweet spot — simple, no dependencies, crash-resilient, supports resume
- Each chunk has its own artifact tracking: notebook_id, source_id, artifact_type, status, artifact_id, download_path
- Rate limiting: can't fire 50 generations simultaneously — need concurrency control (2-3 at a time)

### Architecture Direction
- **Workflow engine** approach (not full autonomous agent)
- Deterministic pipeline with LLM used only where reasoning is needed (classification, chunking strategy)
- More predictable, easier to debug, less prone to weird LLM decisions
- State machine drives the process; LLM assists at specific decision points

---

## Open Questions
- What artifacts should be in the "default" set vs optional?
- Should the agent auto-download everything or let the user pick?
- How to handle NotebookLM rate limits gracefully?
- Naming conventions for notebooks and output files?
- Should there be a "whole-book synthesis" pass after per-chunk artifacts are done (e.g., one overall mind map)?
- How to handle batch mode — one notebook per batch, or one per document within the batch?
