# Learning Buddy

Drop PDFs / EPUBs into an inbox folder. Learning Buddy classifies them, organizes them into a library hierarchy, uploads them to NotebookLM, and tracks artifact generation in a git-backed catalog.

Full design: [DESIGN_V2.md](DESIGN_V2.md). Storage layout & schema: [GITKV_MIGRATION.md](GITKV_MIGRATION.md).

## Prerequisites

- Python ≥ 3.10
- The `nlm` CLI installed and authenticated (`nlm login`)
- `OPENAI_API_KEY` in `~/.env` (used for classification; loaded automatically on import)
- A [`gitkv`](https://github.com/DiQu1997/Git-KV-store) clone for the catalog — a local git clone whose `origin` remote is set (every write is committed and pushed there). Installed automatically as a dependency.

## Install

```bash
cd /path/to/learning_buddy
pip install -e .
```

This installs the `learning-buddy` command.

## Configure (one-time)

Two folders plus a gitkv clone, all configurable:

| location | what's in it |
|---|---|
| `inbox/` | where YOU drop files (PDFs / EPUBs) |
| `library/` | where the agent organizes files into a hierarchy. Agent owns this — never put files in here yourself. |
| `kv.repo` | a local `gitkv` clone (with `origin` set) holding the catalog as `resources/<id>/meta` + `resources/<id>/queue`. Every write is a commit pushed to the remote. |

Set them up (clone your KV repo first, e.g. `git clone <kv-remote> ~/learning_buddy_kv`):

```bash
learning-buddy config set inbox   "/path/to/inbox"
learning-buddy config set library "/path/to/library"
learning-buddy config set kv.repo "/path/to/learning_buddy_kv"

# (optional) tune split thresholds and the artifact set
learning-buddy config set split.min_pages_to_split  35
learning-buddy config set split.max_pages_per_chunk 25
learning-buddy config set artifacts                 note,slide_deck,video,audio,mind_map

learning-buddy config show     # sanity-check
learning-buddy config path     # location of the config file
```

The config file lives at `~/.config/learning-buddy/config.json` (or override via the `LEARNING_BUDDY_CONFIG` env var).

## Run

```bash
# Drop one or more files into your inbox folder
cp ~/Downloads/some_paper.pdf "/path/to/inbox/"

# One-shot pass: intake new files, advance unfinished tasks, exit
learning-buddy run
```

That's it. The agent will:
1. **Verify NotebookLM auth** (`nlm login --check`). If expired and you're at a terminal, it spawns `nlm login` for you to complete, then re-checks. Under cron / launchd (no TTY) it prints a clear error and exits non-zero — log in manually before the next run.
2. Walk `inbox/`, fingerprint + classify each new file via OpenAI, dedup against the catalog, move accepted files into `library/`, and add a row to `catalog.json`.
3. For each unfinished resource: split if needed (book ≥ `min_pages_to_split` pages), upload chunks to NotebookLM, kick off artifact generation (note / slide_deck / video / audio / mind_map), and verify any tasks already in flight.

Each task gets advanced **at most one step per run**. Slow NotebookLM artifacts (video, slides) stay `PROCESSING` across many runs without burning the retry budget. After 5 NLM-reported failures a task moves to `FAILED`.

Print a summary anytime:

```bash
learning-buddy status
```

## Always-on (cron)

`run` is one-shot by design. To keep the agent caught up automatically, schedule it. Example macOS user crontab:

```cron
*/5 * * * * /usr/local/bin/learning-buddy run >> ~/learning-buddy.log 2>&1
```

A flock at `<kv.repo>/.git/.learning-buddy.lock` prevents two simultaneous invocations on the same machine. Files dropped into the inbox between runs get picked up on the next one. (Across machines, gitkv's fast-forward-push CAS keeps the shared remote consistent.)

## Inspect / troubleshoot

```bash
# Counts + recent resources
learning-buddy status
learning-buddy status --json

# What's the agent decided about each file?
cat <inbox>/_log.txt

# Catalog and per-resource state — browse the gitkv clone's history
git -C <kv.repo> log --oneline                 # audit trail (every write is a commit)

# Direct check against NotebookLM (uses the same nlm CLI)
nlm notebook list --json
nlm studio status <notebook-id> --json
```

Duplicates of files you've already processed go to `<inbox>/_dups/<sha-or-id>__<filename>`.

Migrating from the old plain-JSON `metadata/` store? Import it once:

```bash
learning-buddy migrate --from /path/to/old/metadata
```

## CLI surface

```text
learning-buddy run                 # one-shot pass
learning-buddy status              # catalog summary
learning-buddy migrate --from DIR  # import a legacy JSON metadata dir
learning-buddy config show|path|set
```

That's the entire user-facing surface.
