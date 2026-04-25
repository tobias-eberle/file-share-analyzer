# CLAUDE.md

Operational notes for working on this repo with Claude Code. Keep this file
updated as the architecture evolves — it is the first thing future sessions
read.

## What this project is

A read-only crawler + analyzer for enterprise network shares, scoped to
**Phase 1** of the PRD (see commit history; the PRD itself is not in the
repo). It indexes a mounted path into SQLite and produces six reports plus
a `rag_candidates.jsonl` hand-off for downstream RAG ingestion.

Phase 1 is deliberately narrow. Everything that's out of scope (write-back,
incremental rescans, SMB-direct, ACL/PII, Postgres backend) lives in the
"phase 2 candidates" section of the PRD and **must not** leak into phase 1
code paths.

## Layout

```
share_analyzer/
  cli.py                  # Click entry point: scan / report / info
  config.py               # share-analyzer.toml loader
  logging.py              # JSON sidecar logger + ProgressPrinter
  crawl/
    walker.py             # Walker protocol + LocalScandirWalker
    fingerprint.py        # Fingerprinter + StreamingFingerprinter
    sink.py               # Sink + SqliteSink (batched, WAL)
    orchestrator.py       # threading + queues + abort handling
  index/
    schema.py             # versioned migrations, connect()
    mime.py               # MIME categories + libmagic detector
    queries.py            # aggregation queries (read-only)
  reports/
    base.py               # registry, runners, helpers
    <report>.py           # one module per report
    templates/*.html.j2   # jinja2 templates (Plotly inlined)
tests/
  conftest.py             # fixture_share + crawled_db fixtures
  test_*.py               # see "Testing" below
```

## Architecture invariants

These are the rules that keep phase 2 from requiring rewrites. Don't
break them without a deliberate decision.

1. **Three layers, one direction.** crawl → index → reports. Reports
   never touch the filesystem; the index never imports from reports;
   crawl never imports from reports.
2. **Walker / Fingerprinter / Sink are interfaces.** New crawl backends
   (SMB-direct, incremental) plug in by implementing the protocol — they
   must not require changes in the orchestrator or the sink callers.
3. **Schema is versioned.** Every change to tables/views goes through a
   new entry in `_MIGRATIONS` in `index/schema.py`. Never edit an existing
   migration; add the next one.
4. **Reports read from `folders` and `files` only.** If a report needs
   data not yet aggregated, add it to `materialize_folders` or to a new
   index helper — don't re-aggregate inside the report.
5. **HTML reports are self-contained.** Plotly is inlined via
   `include_plotlyjs="inline"`. No CDN `<script src=>`. The
   `test_html_reports_are_self_contained` test enforces this.

## Concurrency model (orchestrator)

The orchestrator is the only place threads run.

- Main thread: walker. Files → `in_q`, walker errors → `out_q`.
- Worker pool: drains `in_q`, fingerprints, pushes `(entry, fp)` → `out_q`.
- Single writer thread: drains `out_q`, is the **only** caller of sink
  write methods during the run.
- `abort: threading.Event` + `_put(q, item, abort)` make every blocking
  put responsive so a writer failure doesn't deadlock the producers.
- Writer exceptions are captured in `writer_exc[]` and re-raised on the
  main thread after threads join. The run is marked `status='failed'`.

If you add a new producer or consumer, route it through `_put` — never
call `q.put(item)` without the abort guard. Otherwise a sink failure
will hang the crawl.

## materialize_folders

This is the trickiest function in the codebase. It must guarantee:

1. Every ancestor of a file-bearing folder exists in `folders`, even
   when the ancestor has no direct files.
2. `total_size` and `file_count` are recursive (direct + descendants).
3. `max_depth_below` is correct for every folder, propagated bottom-up.

`dominant_mime_category` stays direct-files-only on purpose — a recursive
rollup of categories is more confusing than useful in the topology view.

When you change this function, run `tests/test_folders.py` — it pins all
three guarantees against a fixture share where the only files live three
levels deep.

## Testing

```bash
poetry install --with dev
poetry run pytest
```

Conventions:
- `tests/conftest.py::fixture_share` builds a small share covering
  unicode names, deep paths, a duplicate cluster, varied MIME categories,
  and varied mtimes. Use it whenever you need a realistic crawl.
- `tests/conftest.py::crawled_db` runs a full crawl and yields
  `(db_path, run_id)`. Reuse it instead of re-crawling.
- `test_permission_denied_recorded` is **skipped under root** because
  POSIX 0o000 is bypassed by uid 0. If you run as root, expect 1 skip.

### Pytest assertion gotcha

`assert <small> not in <huge>` is a trap. Pytest's failure renderer runs
`difflib` on the operands; on a 4 MB Plotly-inlined HTML body it appears
to hang (it's actually quadratic in the body length). Always check for a
narrow, non-spurious substring (e.g. `'src="https://cdn.'`), not a token
that might legitimately appear inside inlined JS.

## Performance notes (known follow-ups)

These came up while building phase 1. They aren't required to ship but
matter for the 1M-file SLA:

- WAL grows unbounded on long crawls — checkpoint with
  `PRAGMA wal_checkpoint(TRUNCATE)` periodically.
- `staleness_buckets` pulls every row into Python; convert to a pure-SQL
  aggregate (`CASE WHEN mtime >= ?`).
- `top_duplicates` is N+1 on sample paths; replace with a
  `ROW_NUMBER() OVER (PARTITION BY sha256)` join.
- `rag_candidates` deduplicates in Python; same fix applies.
- Composite indexes `(run_id, parent_path)`, `(run_id, mime_category)`,
  `(run_id, sha256)` — every query starts with `run_id`.

## CLI ergonomics

- `share-analyzer report all --db ... --out ...` runs every report into
  one directory.
- `--run-id` is optional; the latest run is used by default.
- A `share-analyzer.toml` in the working directory or any parent supplies
  defaults for `[scan]`. CLI flags override.

## Things to avoid

- Don't add Pandas. Plotly Express pulls it in; the report code uses
  `plotly.graph_objects` directly to keep the dependency footprint tight.
- Don't introduce `pywin32` for owner extraction — the PRD explicitly
  defers it.
- Don't make reports re-crawl. Every report should be derivable from the
  index alone.
- Don't rename CLI commands. `scan/report/info` is part of the contract;
  phase 2 adds `rescan/tag/move` without breaking these.
