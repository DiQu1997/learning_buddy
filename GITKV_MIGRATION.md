# Learning Buddy — gitkv Storage Migration & Data Schema

Status: **implemented** (v0.3.0). Code: `learning_buddy/store.py`,
`learning_buddy/catalog.py`, `config.py`, `agent.py`, `cli.py`; tests in
`tests/test_catalog.py` and `tests/test_store_gitkv.py`. This doc remains the
schema + layout reference.

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

### 3.5 Formal JSON Schemas

The two blobs stored per resource (§4), as JSON Schema (draft 2020-12).

**`resources/<id>/meta`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LearningBuddyResourceMeta",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "sha256", "title", "authors", "kind", "category",
               "library_path", "toc", "page_count", "overall_status",
               "created_at", "updated_at"],
  "properties": {
    "id":            { "type": "string", "pattern": "^f_[0-9a-f]{10}$" },
    "sha256":        { "type": "string", "pattern": "^[0-9a-f]{64}$" },
    "title":         { "type": "string", "minLength": 1 },
    "authors":       { "type": "array", "items": { "type": "string" } },
    "kind":          { "enum": ["book","paper","blog","slides","note",
                                "article","transcript","other"] },
    "category":      { "type": "array", "items": { "type": "string" },
                       "minItems": 1 },
    "library_path":  { "type": "string", "minLength": 1 },
    "toc":           { "type": "array", "items": { "type": "string" } },
    "page_count":    { "type": "integer", "minimum": 0 },
    "overall_status":{ "enum": ["NEW","IN_PROGRESS","DONE","FAILED"] },
    "created_at":    { "type": "string", "format": "date-time" },
    "updated_at":    { "type": "string", "format": "date-time" }
  }
}
```

**`resources/<id>/queue`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LearningBuddyResourceQueue",
  "type": "object",
  "additionalProperties": false,
  "required": ["resource_id", "notebook_id", "created_at", "updated_at", "sources"],
  "properties": {
    "resource_id": { "type": "string", "pattern": "^f_[0-9a-f]{10}$" },
    "notebook_id": { "type": ["string", "null"] },
    "created_at":  { "type": "string", "format": "date-time" },
    "updated_at":  { "type": "string", "format": "date-time" },
    "sources": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["idx", "title", "library_path", "page_range",
                     "nlm_source_id", "tasks"],
        "properties": {
          "idx":           { "type": "integer", "minimum": 1 },
          "title":         { "type": "string" },
          "library_path":  { "type": "string" },
          "page_range":    { "oneOf": [
                               { "type": "array", "items": {"type":"integer"},
                                 "minItems": 2, "maxItems": 2 },
                               { "type": "string" },
                               { "type": "null" } ] },
          "nlm_source_id": { "type": ["string", "null"] },
          "tasks": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["type", "state", "retry_count", "last_error"],
              "properties": {
                "type":            { "enum": ["upload","note","slide_deck",
                                              "video","audio","mind_map"] },
                "state":           { "enum": ["NOT_STARTED","PROCESSING",
                                              "DONE","FAILED"] },
                "retry_count":     { "type": "integer", "minimum": 0, "maximum": 5 },
                "last_error":      { "type": ["string", "null"] },
                "nlm_artifact_id": { "type": "string" },
                "url":             { "type": "string" }
              }
            }
          }
        }
      }
    }
  }
}
```

---

## 4. Schema organization inside gitkv — full paths

Single table, prefix **`learning_buddy`**. **Recommended layout: one directory
per knowledge file.** Because gitkv keys map verbatim to git tree paths, a `/`
in a key *is* a directory — so every original file gets its own directory under
`resources/<id>/`, with its metadata split into a few small blobs:

| blob (gitkv key = git tree path) | holds | written by |
|---|---|---|
| `resources/<id>/meta` | the `CatalogEntry` fields (index: sha256, title, authors, kind, category, library_path, toc, page_count, overall_status, timestamps) | Phase A intake; status rollup |
| `resources/<id>/queue` | `notebook_id` + `sources[]` + their `tasks[]` (the task state machine) | Phase B drain |

Concrete example — a single book with id `f_a1b2c3d4`:

```
kv_repo (e.g. ~/learning_buddy_kv, origin → github.com/<you>/<kv-repo>)
└── branch: learning_buddy_log_9f3c1a77b0e2d4a6   (active log)
    └── resources/
        └── f_a1b2c3d4/              ← one directory per original knowledge file
            ├── meta                 ← JSON: CatalogEntry (index fields + overall_status)
            └── queue                ← JSON: notebook_id + sources[] + tasks[]

(registry) branch: main
└── tables/
    └── learning_buddy               ← empty blob, registers the table
```

This keeps everything about one file in one place (easy to browse in the repo,
delete by prefix, and diff), and lets Phase A (index fields) and the hot-path
task updates (`queue`) commit independently without rewriting each other. Room
to add more per-file blobs later (e.g. `resources/<id>/log`) without schema
churn.

Read/write addressing in code:

```python
db   = gitkv.open(cfg.kv_repo)            # resolves the clone + remote
tbl  = db["learning_buddy"]

# index fields for one resource
tbl[f"resources/{rid}/meta"]   = json.dumps(meta_dict)
meta = json.loads(tbl[f"resources/{rid}/meta"])

# task queue for one resource
tbl[f"resources/{rid}/queue"]  = json.dumps(queue_dict)
queue = json.loads(tbl[f"resources/{rid}/queue"])

# build the in-memory index across all resources
metas = [json.loads(v) for k, v in tbl.list_items("resources/") if k.endswith("/meta")]
metas.sort(key=lambda m: m["created_at"])     # see §4.1 — ordering
```

Every assignment above is one commit on `learning_buddy_log_<hex>` and an
auto-push to `origin`. History (`git log` of that branch) is the audit trail.

### 4.1 Ordering

`gitkv.list_keys()` / `list_items()` return keys in **lexicographic** order. Our
ids are `"f_" + random hex`, so a raw listing is stable but **not** in creation
order. The catalog is therefore *unordered at the key level*; impose order
explicitly:

- **Sort in-memory after listing (recommended).** Each `meta` blob carries
  `created_at` (and `title`, `overall_status`), so we sort the loaded list by
  whatever the caller needs — creation time, title, status. No extra reads (we
  already fetched the blobs). This is what every existing query method wants
  anyway (`list_unfinished` filters, `existing_summary` order is cosmetic).
- **Sortable keys (only if tree-order itself must be meaningful).** Prefix the
  key with a zero-padded counter or timestamp, e.g.
  `resources/00042__f_a1b2c3d4/meta`, so `list_keys` returns creation order
  directly. Costs a secondary index for id/sha lookups, so not worth it here.

For the `sources[]` *within* a resource, order is meaningful (chapter 1, 2, …)
and is preserved by the JSON array inside the single `queue` blob — no key-level
sorting needed. (If sources were ever promoted to their own blobs, name them
`sources/01`, `sources/02`, … zero-padded so `list_keys` stays in order.)

---

## 5. Layout alternatives considered

The per-file-directory layout (§4) is the recommendation. Two simpler variants
were considered:

- **Single blob per resource** — `resources/<id>` holding meta+queue together
  (one JSON doc). Fewer keys, but every task-state update rewrites the index
  fields too, and the two phases can't commit independently. Fine if we want the
  smallest possible diff.
- **Monolithic `catalog` doc** — one `catalog` key holding the whole index array
  (closest to today's `catalog.json`). Preserves array insertion order for free
  and gives an atomic index snapshot, but serializes all writers onto one blob
  (cross-machine contention) and grows unboundedly. Acceptable only for a
  single-machine setup.

Recommendation: **per-file directory (§4)** for the git-native grouping and
independent commits the goals call for. `tbl.list_keys("resources/")` also gives
a free "rebuild the index from the per-file blobs" recovery/repair path.

---

## 6. Implementation plan (file by file)

1. **`pyproject.toml`** — add dependency
   `gitkv @ git+https://github.com/DiQu1997/Git-KV-store`.

2. **`learning_buddy/store.py`** *(new, ~40 lines)* — thin wrapper:
   `open_store(kv_repo) -> db` (calls `gitkv.open`, ensures the
   `learning_buddy` table exists), plus `read(key) -> str | None`,
   `write(key, text)`, `delete(key)`. Centralizes the table name and key scheme.

3. **`learning_buddy/catalog.py`** *(reworked; dataclasses + all query/mutation
   logic unchanged)* — replace the three filesystem ops with store calls and
   adopt the per-file key scheme:
   - `_atomic_write(path, text)`            → `store.write(key, text)`
   - `path.read_text()` / `path.exists()`   → `store.read(key)` (`None` = missing)
   - `Catalog` becomes a derived index: `load()` does
     `store.list_items("resources/")`, keeps `*/meta` blobs, sorts by
     `created_at` (§4.1); per-entry writes go to `resources/<id>/meta`.
   - `ResourceFile.load/save/create/exists` map to `resources/<id>/queue`.

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

- **Commit volume:** each `save()` is a commit + push. Phase B saves the `queue`
  **after every task advance** (`agent.py` writes `rf.save()` per upload/artifact
  step — deliberate, so a crash can't lose an NLM `artifact_id` and re-create a
  duplicate). So **run 1** of a resource with `S` sources × `T` tasks costs on
  the order of `S×T` queue commits + a couple of `meta` commits; **steady-state**
  runs collapse to near-zero via the `Store.write` no-op guard. For a 70-chapter
  book this means hundreds of commits/pushes on the first drain — see §13 and the
  batching follow-up in §7's open items.
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
4. Layout: per-file directory `resources/<id>/{meta,queue}` (§4, recommended)
   vs the single-blob or monolithic-`catalog` variants (§5).
5. Ordering: in-memory sort by `created_at` (§4.1, recommended) vs sortable
   keys.
6. **(open)** Batch the `queue` write to once-per-resource-per-run to cut
   first-drain commit volume (§13), weighed against duplicate-artifact crash
   risk. Currently per-task.

Decisions taken in the v0.3.0 implementation: per-file `resources/<id>/{meta,
queue}` layout (§4); `created_at` in-memory ordering (§4.1); `fcntl` lock kept,
relocated to `<kv.repo>/.git/.learning-buddy.lock`; `learning-buddy migrate
--from <dir>` one-shot import; `gitkv` pinned to `v0.4.0`. `kv.repo` is required
config with no default.

---

## 8. Reference implementation sketch

Illustrative, not final code. Pins gitkv `0.4.0`.

### 8.1 `learning_buddy/store.py` (new)

```python
"""Git-backed metadata store (gitkv). One table; keys map verbatim to git paths."""
import gitkv

TABLE = "learning_buddy"
RESOURCE_PREFIX = "resources/"


def meta_key(rid: str) -> str:
    return f"{RESOURCE_PREFIX}{rid}/meta"


def queue_key(rid: str) -> str:
    return f"{RESOURCE_PREFIX}{rid}/queue"


class Store:
    def __init__(self, table):
        self._tbl = table

    @classmethod
    def open(cls, kv_repo: str) -> "Store":
        db = gitkv.open(str(kv_repo))          # arg → GITKV_REPO → config cascade
        if TABLE not in db:
            db.create_table(TABLE)             # idempotent
        return cls(db[TABLE])

    def read(self, key: str) -> str | None:
        return self._tbl.get(key)              # explicit get → None on miss

    def write(self, key: str, text: str) -> None:
        if self._tbl.get(key) == text:         # idempotent: skip no-op commits
            return
        self._tbl[key] = text                  # one commit on the log branch (+ push)

    def delete(self, key: str) -> None:
        try:
            del self._tbl[key]
        except KeyError:
            pass

    def list_meta(self) -> list[str]:
        # blobs only; tree-cheap key scan then read each
        return [k for k in self._tbl.list_keys(RESOURCE_PREFIX) if k.endswith("/meta")]

    def iter_meta(self):
        for k, v in self._tbl.list_items(RESOURCE_PREFIX):
            if k.endswith("/meta"):
                yield v
```

### 8.2 `learning_buddy/catalog.py` (reworked)

The dataclasses and **every query/mutation method keep their signatures**; only
load/save/persistence change. `Catalog` becomes a *derived* index (no stored
`catalog` blob): mutations write the affected `meta` blob immediately, so the
end-of-run `catalog.save()` disappears.

```python
@dataclass
class Catalog:
    store: Store
    _by_id: dict[str, dict]          # rid -> meta dict (in-memory)

    @classmethod
    def load(cls, store: Store) -> "Catalog":
        by_id = {}
        for text in store.iter_meta():
            m = json.loads(text)
            by_id[m["id"]] = m
        return cls(store=store, _by_id=by_id)

    @property
    def resources(self) -> list[dict]:
        return sorted(self._by_id.values(), key=lambda m: m["created_at"])  # §4.1

    # queries (find_by_sha / find_by_id / list_unfinished / categories_in_use /
    # existing_summary / counts) are UNCHANGED — they iterate self.resources.

    def add_resource(self, **fields) -> dict:
        entry = {... , "id": new_resource_id(), "overall_status": NEW,
                 "created_at": now, "updated_at": now}
        self._by_id[entry["id"]] = entry
        self.store.write(meta_key(entry["id"]), _dump_json(entry))   # commit now
        return entry

    def set_overall_status(self, entry: dict, status: str) -> None:
        entry["overall_status"] = status
        entry["updated_at"] = utc_now_iso()
        self.store.write(meta_key(entry["id"]), _dump_json(entry))   # commit now


@dataclass
class ResourceFile:
    store: Store
    resource_id: str
    data: dict

    @classmethod
    def exists(cls, store, rid) -> bool:
        return store.read(queue_key(rid)) is not None

    @classmethod
    def load(cls, store, rid) -> "ResourceFile":
        text = store.read(queue_key(rid))
        if text is None:
            raise FileNotFoundError(rid)
        return cls(store, rid, json.loads(text))

    @classmethod
    def create(cls, store, rid, *, notebook_id, sources) -> "ResourceFile":
        rf = cls(store, rid, {"resource_id": rid, "notebook_id": notebook_id,
                              "created_at": utc_now_iso(), "updated_at": utc_now_iso(),
                              "sources": sources})
        rf.save()
        return rf

    def save(self) -> None:                       # called once per resource per run
        self.data["updated_at"] = utc_now_iso()
        self.store.write(queue_key(self.resource_id), _dump_json(self.data))
```

### 8.3 `agent.py` / `cli.py` deltas

- `cli.py`: `store = Store.open(cfg.kv.repo)` once per `run`, under the existing
  `fcntl` lock; pass `store` into `Catalog.load(store)`.
- `agent.py`: drop the final `catalog.save()` (mutations self-persist).
  `ResourceFile.save()` is still called once after a resource's tasks are
  advanced — unchanged cadence, just a gitkv write instead of a file write.

---

## 9. Worked example — one book, commit by commit

Book "Deep Learning", id `f_a1b2c3d4ef`, split into 3 chapters × 6 tasks.

**Run 1 — Phase A (intake)** writes one blob:

`resources/f_a1b2c3d4ef/meta`
```json
{
  "id": "f_a1b2c3d4ef", "sha256": "9c1f…",
  "title": "Deep Learning", "authors": ["Goodfellow","Bengio","Courville"],
  "kind": "book", "category": ["CS","AI","DL","books"],
  "library_path": "CS/AI/DL/books/Deep Learning.pdf",
  "toc": ["Ch 1 Introduction","Ch 2 Linear Algebra", "…"],
  "page_count": 802, "overall_status": "NEW",
  "created_at": "2026-05-31T10:00:00Z", "updated_at": "2026-05-31T10:00:00Z"
}
```
→ commit `Set key: resources/f_a1b2c3d4ef/meta`

**Run 1 — Phase B (drain)** creates the queue, flips status, advances tasks:

`resources/f_a1b2c3d4ef/queue` (abridged — 3 sources shown as 1)
```json
{
  "resource_id": "f_a1b2c3d4ef", "notebook_id": "nb_deeplearning",
  "created_at": "2026-05-31T10:00:05Z", "updated_at": "2026-05-31T10:00:09Z",
  "sources": [
    { "idx": 1, "title": "Chapter 1 Introduction",
      "library_path": "CS/AI/DL/books/Deep Learning/01_introduction.pdf",
      "page_range": [1, 24], "nlm_source_id": "src_001",
      "tasks": [
        {"type":"upload",    "state":"DONE","retry_count":0,"last_error":null},
        {"type":"note",      "state":"PROCESSING","retry_count":0,"last_error":null,"nlm_artifact_id":"art_n1"},
        {"type":"slide_deck","state":"PROCESSING","retry_count":0,"last_error":null,"nlm_artifact_id":"art_s1"},
        {"type":"video",     "state":"PROCESSING","retry_count":0,"last_error":null,"nlm_artifact_id":"art_v1"},
        {"type":"audio",     "state":"PROCESSING","retry_count":0,"last_error":null,"nlm_artifact_id":"art_a1"},
        {"type":"mind_map",  "state":"PROCESSING","retry_count":0,"last_error":null,"nlm_artifact_id":"art_m1"}
      ] }
  ]
}
```
→ commits: `meta` (status NEW → IN_PROGRESS), then a `queue` write **after each
task advance** (upload + 5 artifact kick-offs). For this 1-source × 6-task book,
run 1 lands ~6 `queue` commits + 2 `meta` commits on top of the intake `meta`.
Verified end-to-end: a real run produced a 19-commit chain on `origin`
(`Set key: …/meta` / `…/queue`), confirming the audit trail.

**Run 2..N (cron):** Phase A no-op; Phase B re-verifies PROCESSING tasks. A
still-rendering video → no state change → the no-op guard skips the write → **0
commits** that run.

**Run F (final):** the remaining artifacts flip to DONE → a `queue` write per
verified task + one `meta` IN_PROGRESS → DONE. Subsequent runs skip the entry
entirely (`overall_status == DONE` not in `UNFINISHED`).

> Note: the local clone is a **partial clone** — `git log` there shows only
> grafted tips; the full chain lives on `origin` (`git -C origin.git log <log
> branch>`).

The book's entire history is `git log -- resources/f_a1b2c3d4ef/` on the active
log branch.

---

## 10. Config — before / after

**Today** (`~/.config/learning-buddy/config.json`):

```json
{ "inbox": "…", "library": "…", "metadata": "/Users/qudi/knowledge_metadata",
  "split": {"min_pages_to_split":35,"max_pages_per_chunk":25},
  "bucket_capacity": 25, "artifacts": ["note","slide_deck","video","audio","mind_map"],
  "llm": {"model":"gpt-5-mini"}, "nlm": {"verify_interval_seconds":30,"max_retries":5} }
```

**After** — replace `metadata` with a `kv` block:

```json
{ "inbox": "…", "library": "…",
  "kv": {
    "repo": "~/learning_buddy_kv",   // local clone; must have `origin` configured
    "table": "learning_buddy",        // gitkv table prefix (^[a-z0-9_]{1,63}$)
    "auto_push": true                 // gitkv pushes every write to origin
  },
  "split": {"min_pages_to_split":35,"max_pages_per_chunk":25},
  "bucket_capacity": 25, "artifacts": ["note","slide_deck","video","audio","mind_map"],
  "llm": {"model":"gpt-5-mini"}, "nlm": {"verify_interval_seconds":30,"max_retries":5} }
```

`config.py` loads `kv.repo` (required), `kv.table` (default `learning_buddy`).
A legacy `metadata` field is tolerated (warn + ignore) so old configs don't
crash. The clone is a user/setup concern: `git clone <kv-remote> ~/learning_buddy_kv`.

---

## 11. One-time migration — `learning-buddy migrate`

Imports the existing on-disk store into gitkv. Idempotent (the `Store.write`
no-op guard means re-running adds no commits if content is unchanged).

```
learning-buddy migrate --from <old_metadata_dir>

  store = Store.open(cfg.kv.repo)
  old   = Path(--from or cfg.legacy_metadata)

  # 1. catalog.json rows → per-resource meta blobs
  for entry in json.load(old/"catalog.json")["resources"]:
      store.write(meta_key(entry["id"]), dumps(entry))

  # 2. resources/<id>.json → per-resource queue blobs
  for f in (old/"resources").glob("*.json"):
      store.write(queue_key(f.stem), f.read_text())

  print summary: N meta, M queue written
```

Verification after migrate: `Catalog.load(store).counts()` should match the old
`catalog.json` counts; `store.list_meta()` length == number of catalog rows.

---

## 12. Concurrency, CAS & multi-machine

Each write commits to the active log branch and fast-forward-pushes to `origin`
(compare-and-swap, up to `DEFAULT_MAX_CAS_ATTEMPTS = 20` retries).

- **Same machine, two runs:** prevented by the existing `fcntl` lock at
  `<state>/.learning-buddy.lock`. Keep it — it's free and avoids needless CAS
  churn.
- **Different machines, different resources:** machine B's push may be rejected
  non-fast-forward; gitkv fetches, replays B's commit on the new tip, re-pushes.
  Since the two writes touch different blob paths (`resources/<idA>/…` vs
  `resources/<idB>/…`) there is no content conflict — this is exactly why the
  per-file layout (§4) matters. Converges within the CAS retry budget.
- **Different machines, same resource, same instant:** last-writer-wins at the
  blob level after CAS replay — one machine's update to that blob can be lost.
  This is the one genuine race. For the intended usage (single user, cron on one
  machine, occasional manual run elsewhere) it is acceptable; if it ever matters,
  a per-resource advisory lock blob (`resources/<id>/.lock`) could gate it.

No data corruption is possible — git guarantees each commit is a consistent
tree; the worst case is a lost *update* to one blob, never a torn write.

---

## 13. Cost per run (git ops)

Let `A` = new inbox files this run, `R` = active (unfinished) resources,
`Tc` = task advances that changed state this run, `Rc` = resources whose status
changed.

| operation | reads (fetch) | writes (commit + push) |
|---|---|---|
| load index | 1 × `list_items("resources/")` | — |
| Phase A intake | — | `A` (one `meta` each) |
| Phase B load queues | `R` (one `read` each) | — |
| Phase B advance | live NLM calls (unchanged) | `Tc` queue + `Rc` meta |
| **total** | `1 + R` | `A + Tc + Rc` |

`Tc` is the catch: on a resource's **first** drain every task changes, so a book
split into `S` chapters costs ~`S × len(artifacts+1)` queue commits that run.
In **steady state** (nothing new, artifacts still rendering) the `Store.write`
no-op guard drops unchanged writes to **0 commits**. gitkv's `list_keys` is
tree-only (no blob fetch); `list_items` fetches blobs on the active branch and is
**partial-clone compatible**, so a large history doesn't bloat the working read.
Log rotation only kicks in at `DEFAULT_ROTATION_THRESHOLD = 10000` commits per
branch — years away at cron cadence.

**Follow-up (not done):** to cut first-drain commit volume, batch the `queue`
write to once per resource per run instead of per task. The tradeoff is crash
recovery — a per-task save guarantees a created NLM `artifact_id` is persisted
before the next kick-off, avoiding duplicate artifacts; batching widens that
window. Left as a conscious follow-up.

---

## 14. Key & store constraints (from gitkv `_store.py`)

- **Table prefix** must match `^[a-z0-9_]{1,63}$` → `learning_buddy` ✓.
- **Key rules** (`_validate_key`): non-empty, **relative** (no leading `/`),
  segments split on `/`; no empty segment, `.`, `..`, or `.git`. Our keys
  `resources/f_<hex>/meta` and `…/queue` satisfy all of these.
- **id format**: `f_` + 10 hex chars (`new_resource_id`) — safe as a path
  segment; no escaping needed.
- **Value type**: strings only → we `json.dumps`/`json.loads` at the boundary.
- **Commit messages** are `Set key: <key>` / `Delete key: <key>` (+ a
  `Commit-Number` trailer), so `git log` on the KV repo is a readable audit log
  keyed by exactly which blob changed.
- **Miss semantics**: `table.get(key)` → `None`; `table[key]` raises `KeyError`.
  `Store.read` uses `.get` so "missing" is `None`, matching today's
  `path.exists()` checks.
- **Pin** `gitkv==0.4.0` (or `git+…@<tag>`) in `pyproject.toml` so the on-disk
  format and API stay stable across machines sharing one KV repo.
