# Learning Buddy — gitkv Storage Migration & Data Schema

Status: **plan / design doc** (no application code changed yet).

This document specifies how Learning Buddy's metadata moves from the current
plain-JSON-on-disk store to a Git-backed key-value store
([`gitkv`](https://github.com/DiQu1997/Git-KV-store)), what the full data
schema is, and exactly how every record is addressed inside the gitkv repo
(branch + tree path).

---

## 1. Why

The original `DESIGN.md` wanted git to be the cross-machine source of truth
(`git add catalog.json && git commit && git push`). The v2 rewrite dropped that
in favour of plain JSON + `tmp+os.replace` ("No git", `catalog.py:4`). `gitkv`
brings git back, but as a real KV abstraction instead of hand-rolled git calls.

Goals (confirmed):

- **Reuse existing infra** — the `gitkv` store already exists and is maintained.
- **Cross-machine sync** — every write is a commit; the git remote is the shared
  source of truth.
- **Versioned history** — full audit trail of catalog/resource changes for free.

All persistence in Learning Buddy is already isolated behind two dataclasses in
`learning_buddy/catalog.py` (`Catalog` and `ResourceFile`, each with
`load()` / `save()`), so the swap is contained to that seam.

---

## 2. gitkv primer

### 2.1 Python API

```python
import gitkv

db = gitkv.open(repo_path)            # arg → GITKV_REPO env → config cascade
db.create_table("learning_buddy")     # idempotent table creation
tbl = db["learning_buddy"]            # high-level table handle

tbl["catalog"] = json_text            # set  → one git commit (+ auto-push)
text = tbl["catalog"]                 # get  → raises on miss (dict semantics)
"catalog" in tbl                      # membership
del tbl["resources/f_abc"]            # delete (tombstone write)
```

Lower-level / explicit, returns `None` on miss instead of raising:

```python
store = gitkv.GitKVStore(repo_path)
store.table("learning_buddy").set(key, value)
store.table("learning_buddy").get(key)        # -> str | None
```

### 2.2 Iteration (recently added — confirmed in `gitkv/_store.py`)

On `GitKVTable`:

| method | returns | notes |
|---|---|---|
| `list_keys(prefix="", limit=None, after=None)` | sorted `list[str]` | tree-only read (no blobs), cheap; prefix filter + `after` cursor |
| `list_items(prefix="", limit=None, after=None)` | `list[(key, value)]`, values decoded `str` | fetches blobs too |
| `__iter__()` | iterator over all keys | delegates to `list_keys()` |
| `__contains__(key)` | `bool` | `get(key) is not None` |

This is what makes a `resources/`-prefix scan and "rebuild the index from
resources" possible (see §5, Option B).

### 2.3 Value type

Values are **strings** (str/bytes accepted on write, `str` returned on read).
There is **no built-in JSON handling** — Learning Buddy serializes with
`json.dumps(...)` on write and `json.loads(...)` on read, exactly as it does for
the on-disk files today.

### 2.4 Physical git layout (the "full path" of a value)

`gitkv` stores keys as git blobs at tree paths matching the key verbatim, on
per-table branches. For a table whose **prefix** is `learning_buddy`:

| git object | name / path | purpose |
|---|---|---|
| registry branch | `main` | tracks tables; each table is an empty blob at tree path `tables/<prefix>` |
| genesis branch | `learning_buddy_main` | per-table genesis ref |
| active log branch | `learning_buddy_log_<hex16>` | where live data commits land |
| value blob | tree path == key, on the active log branch | the JSON string for that key |

- The prefix must match `^[a-z0-9_]{1,63}$` — `learning_buddy` is valid.
- Keys map **verbatim** to tree paths (no hashing/encoding): key
  `resources/f_a1b2c3d4` → blob at tree path `resources/f_a1b2c3d4`.
- **Log rotation:** when a log branch's commit count crosses the rotation
  threshold, gitkv writes a *tombstone* commit (trailers
  `Tombstone-Next-Branch` / `-Next-Sha` / `-Snapshot-Tree`) closing the old log
  branch and pointing at a fresh `learning_buddy_log_<newhex>`. Readers walk the
  tombstone chain to find the current active branch. This is internal — callers
  always address values by key.
- **Sync:** `repo_path` is a normal clone with `origin` configured; writes
  commit and fast-forward-push (compare-and-swap). A non-fast-forward push
  (another machine wrote first) triggers gitkv's CAS retry.

So the fully-qualified address of one value is:

```
<kv_repo clone>  →  branch learning_buddy_log_<hex>  →  tree path <key>  →  blob (JSON string)
                    (origin remote = cross-machine source of truth)
```

---

## 3. Full data schema

Two record types. Each is stored as `json.dumps(record)` under one gitkv key.

### 3.1 Catalog index

The top-level index — one entry per resource, fully populated by the end of
Phase A (intake).

```jsonc
{
  "version": 1,                    // int, schema version (currently 1)
  "updated_at": "2026-05-08T…Z",   // ISO-8601 UTC, rewritten on every save
  "resources": [ CatalogEntry, … ]
}
```

**CatalogEntry**

| field | type | required | values / notes |
|---|---|---|---|
| `id` | string | ✓ | `"f_" + uuid4().hex[:10]`, e.g. `f_a1b2c3d4ef` |
| `sha256` | string | ✓ | whole-file hash; primary (exact) dedup key |
| `title` | string | ✓ | LLM-extracted |
| `authors` | string[] | ✓ | may be `[]` |
| `kind` | enum string | ✓ | `book \| paper \| blog \| slides \| note \| article \| transcript \| other` |
| `category` | string[] | ✓ | hierarchy path; last segment is a type bucket, e.g. `["CS","AI","DL","books"]` |
| `library_path` | string | ✓ | **relative** to library root, e.g. `CS/AI/DL/books/Deep Learning.pdf` |
| `toc` | string[] | ✓ | chapter titles; `[]` if none |
| `page_count` | int | ✓ | real (PDF) / estimated (EPUB) |
| `overall_status` | enum string | ✓ | `NEW \| IN_PROGRESS \| DONE \| FAILED` |
| `created_at` | string | ✓ | ISO-8601 UTC |
| `updated_at` | string | ✓ | ISO-8601 UTC |

> `head_text` (the LLM classification input) is intentionally **never** stored —
> it is transient in Phase A and discarded.

### 3.2 Resource task queue

One per resource; created lazily in Phase B the first time the entry is drained.

```jsonc
{
  "resource_id": "f_a1b2c3d4",     // == CatalogEntry.id
  "notebook_id": "nb_xxx",         // string | null — NLM notebook for this resource
  "created_at": "2026-05-08T…Z",
  "updated_at": "2026-05-08T…Z",
  "sources": [ Source, … ]         // one per NLM source (chunk; or one if unsplit)
}
```

**Source**

| field | type | required | notes |
|---|---|---|---|
| `idx` | int | ✓ | 1-based ordinal within the resource |
| `title` | string | ✓ | chapter / source title |
| `library_path` | string | ✓ | **relative**, e.g. `CS/AI/DL/books/Deep Learning/01_introduction.pdf` |
| `page_range` | [int,int] \| string \| null | ✓ | `[1, 24]`; null/string when unsplit |
| `nlm_source_id` | string \| null | ✓ | set when the `upload` task reaches DONE |
| `tasks` | Task[] | ✓ | exactly one `upload` + one per artifact type |

**Task** — shared state machine. Base fields always present; the last two appear
at runtime on artifact tasks.

| field | type | required | notes |
|---|---|---|---|
| `type` | enum string | ✓ | `upload \| note \| slide_deck \| video \| audio \| mind_map` (artifact set driven by config `artifacts[]`) |
| `state` | enum string | ✓ | `NOT_STARTED \| PROCESSING \| DONE \| FAILED` |
| `retry_count` | int | ✓ | 0–5; increments **only** on NLM-reported failure |
| `last_error` | string \| null | ✓ | last failure reason |
| `nlm_artifact_id` | string | – | artifact tasks only; set on kick-off |
| `url` | string | – | artifact tasks only; set on DONE (NotebookLM URL) |

### 3.3 Enums

```
kind            : book | paper | blog | slides | note | article | transcript | other
overall_status  : NEW | IN_PROGRESS | DONE | FAILED      (unfinished = {NEW, IN_PROGRESS})
task.type       : upload | note | slide_deck | video | audio | mind_map
task.state      : NOT_STARTED | PROCESSING | DONE | FAILED
```

### 3.4 Invariants

- `CatalogEntry.id` ↔ `ResourceFile.resource_id` is 1:1.
- `overall_status` rollup: all tasks DONE → `DONE`; any task FAILED → `FAILED`;
  otherwise `IN_PROGRESS`.
- `retry_count` increments only on an NLM-reported failure; a long-rendering
  artifact stays `PROCESSING` with the counter unchanged. At `5` → `FAILED`.
- `DONE` and `FAILED` are terminal.

---

## 4. Schema organization inside gitkv — full paths

Single table, prefix **`learning_buddy`**. Two key namespaces:

| record | gitkv key | git tree path (on `learning_buddy_log_<hex>`) | written by |
|---|---|---|---|
| catalog index | `catalog` | `catalog` | `Catalog.save()` |
| resource queue | `resources/<id>` | `resources/<id>` | `ResourceFile.save()` |

Concrete example — a single book with id `f_a1b2c3d4`:

```
kv_repo (e.g. ~/learning_buddy_kv, origin → github.com/<you>/<kv-repo>)
└── branch: learning_buddy_log_9f3c1a77b0e2d4a6   (active log)
    ├── catalog                                   ← JSON: full CatalogEntry index
    └── resources/
        └── f_a1b2c3d4                            ← JSON: notebook_id + sources[] + tasks[]

(registry) branch: main
└── tables/
    └── learning_buddy                            ← empty blob, registers the table
```

Read/write addressing in code:

```python
db   = gitkv.open(cfg.kv_repo)            # resolves the clone + remote
tbl  = db["learning_buddy"]

# catalog
tbl["catalog"]                           = json.dumps(catalog_dict)
catalog_dict = json.loads(tbl["catalog"])

# one resource
tbl[f"resources/{rid}"]                  = json.dumps(resource_dict)
resource_dict = json.loads(tbl[f"resources/{rid}"])

# enumerate all resources (Option B / recovery)
for key, value in tbl.list_items("resources/"):
    rec = json.loads(value)
```

Every assignment above is one commit on `learning_buddy_log_<hex>` and an
auto-push to `origin`. History (`git log` of that branch) is the audit trail.

---

## 5. Two layout options

Both use the table/key scheme above; they differ only in whether the `catalog`
key exists as a stored document.

- **Option A — monolithic `catalog` doc (recommended to start).** Keep
  `catalog` as a single stored value. One read loads the whole index; all
  existing query methods (`find_by_sha`, `find_by_id`, `list_unfinished`,
  `categories_in_use`, `existing_summary`, `counts`) run in-memory, unchanged.
  Smallest diff.

- **Option B — derive the index from `resources/*`.** Drop the stored `catalog`
  key; rebuild the in-memory index each run via `tbl.list_items("resources/")`.
  More git-native (two machines writing different resources never contend on a
  shared `catalog` blob), at the cost of N reads per run. Enabled by the new
  iteration API.

Recommendation: ship **A**, keep **B** as a clean follow-up. Regardless of
choice, `tbl.list_keys("resources/")` gives a free "rebuild `catalog` from
resources" recovery/repair path.

---

## 6. Implementation plan (file by file)

1. **`pyproject.toml`** — add dependency
   `gitkv @ git+https://github.com/DiQu1997/Git-KV-store`.

2. **`learning_buddy/store.py`** *(new, ~40 lines)* — thin wrapper:
   `open_store(kv_repo) -> db` (calls `gitkv.open`, ensures the
   `learning_buddy` table exists), plus `read(key) -> str | None`,
   `write(key, text)`, `delete(key)`. Centralizes the table name and key scheme.

3. **`learning_buddy/catalog.py`** *(reworked; dataclasses + all query/mutation
   logic unchanged)* — replace the three filesystem ops with store calls:
   - `_atomic_write(path, text)`            → `store.write(key, text)`
   - `path.read_text()` / `path.exists()`   → `store.read(key)` (`None` = missing)
   - `Catalog.load/save`, `ResourceFile.load/save/create/exists` switch from
     `metadata_dir` paths to gitkv keys (`catalog`, `resources/<id>`).

4. **`learning_buddy/config.py`** — replace the `metadata` path with `kv_repo`
   (path to the local clone, which must have `origin` set) and an optional
   `table` name (default `learning_buddy`). Keep tolerant loading: accept legacy
   configs that still carry `metadata` (warn + ignore).

5. **`learning_buddy/agent.py` / `cli.py`** — open the store once per run and
   pass it into `Catalog` / `ResourceFile`. Keep the `fcntl` lock as a cheap
   local guard against two concurrent runs on one machine (gitkv CAS handles the
   cross-machine case).

6. **One-time migration — `learning-buddy migrate`** — read any existing
   `catalog.json` + `resources/*.json` from the old `metadata` dir and `write`
   them into gitkv. Idempotent; safe to re-run.

7. **Docs** — update `DESIGN_V2.md` / `README.md`: replace the "No git"
   statements with "git-backed persistence via gitkv (every write is a commit;
   the remote is the cross-machine source of truth)".

---

## 7. Tradeoffs & open items

**Tradeoffs**

- **Commit volume:** each `save()` is a commit + push, so a `run` advancing N
  tasks produces N commits. This is gitkv's model and gives the audit trail we
  want; it is chattier than today's single JSON write. Per-write commits are
  kept deliberately so a mid-run crash stays recoverable. Batching (write once
  at end of run) is a possible later optimization.
- **Network on the hot path:** auto-push means a `run` needs the remote
  reachable. A non-fast-forward push retries via CAS; worst case a run errors and
  is simply re-run.
- **`gitkv.open` cost:** repo resolution is arg → `GITKV_REPO` env → config. We
  always pass `kv_repo` explicitly for deterministic behaviour.

**Open items to decide before building**

1. The local clone path to bake into config defaults (or leave required, no
   default).
2. Keep or drop the `fcntl` lock once gitkv CAS is in place (recommendation:
   keep initially).
3. `learning-buddy migrate` as a one-shot, or auto-import on first run when
   gitkv is empty but an old `metadata` dir exists.
4. Layout Option A vs B (recommendation: A first).
