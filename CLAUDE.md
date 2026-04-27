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
  cli.py                  # Click entry point: scan / rescan / report / info
  config.py               # share-analyzer.toml loader + DEFAULT_EXCLUDES
  logging.py              # JSON sidecar logger + ProgressPrinter
  dry_run.py              # --dry-run summary (no DB, no fingerprinting)
  tags.py                 # extract_tags: folder-path → list[str]
  crawl/
    walker.py             # Walker protocol + LocalScandirWalker (seq + parallel)
    fingerprint.py        # Fingerprinter + StreamingFingerprinter
    sink.py               # Sink + SqliteSink (batched, WAL)
    rescan.py             # RescanContext: prior-run delta classifier
    retry.py              # is_transient + with_retry for SMB I/O
    health.py             # HealthMonitor: disconnect detection + advisory
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
- Worker pool: drains `in_q`, fingerprints, pushes `(entry, fp, state)` → `out_q`.
- Single writer thread: drains `out_q`, is the **only** caller of sink
  write methods during the run.
- `abort: threading.Event` + `_put(q, item, abort)` make every blocking
  put responsive so a writer failure doesn't deadlock the producers.
- Writer exceptions are captured in `writer_exc[]` and re-raised on the
  main thread after threads join. The run is marked `status='failed'`.

If you add a new producer or consumer, route it through `_put` — never
call `q.put(item)` without the abort guard. Otherwise a sink failure
will hang the crawl. **One exception:** workers always forward their
in_q sentinel to out_q via plain `out_q.put(_SENTINEL)`, even on abort.
The writer counts N sentinels to exit; if abort blocks the put the
writer hangs forever waiting for them. Sentinels are control flow, not
data — they never carry partial work.

### Parallel walker (`dir_workers > 1`)

`LocalScandirWalker.walk()` switches between `_walk_sequential` (a
plain stack iteration) and `_walk_parallel` (a thread-pool pulling from
`dir_q` and pushing results to a bounded `result_q`). Both share
`_process_directory`, so the SMB quirks live in one place. Termination
of the parallel mode rides on `queue.Queue.join()` — every directory
push increments unfinished_tasks, every `task_done` decrements; a
watcher thread sees the count hit zero and pushes a single sentinel
onto `result_q`. The `visited` symlink-loop set is guarded by a lock
because multiple workers may want to claim the same target at once.

### Progress rendering

`ProgressPrinter` (`logging.py`) takes **absolute** counts plus a
`last_path` from the orchestrator, not deltas. Callers can hammer
`update()` thousands of times per second; the printer rate-limits
internally to one `\r`-redraw per `every` seconds (default 0.5 s).
Three deliberately small ideas:

1. **Hot-path callers don't worry about throttling.** The orchestrator
   calls progress on every batch flush *and* every walker-error
   ingest. That's ~1000 calls/sec at the high end; one render/sec
   reaches stderr.
2. **Path truncation from the left.** A 250-character SMB path becomes
   `…/path/that/goes/well/past/twenty/chars/file.txt` — the tail is
   the informative part. Set `path_width` to taste.
3. **Pad-over for variable-length lines.** Each render remembers its
   length so the next, possibly shorter, render pads with spaces and
   doesn't leave stale characters from a previous longer line.

Non-tty streams (CI logs, redirected stdout) skip the rendering
entirely — `\r` floods are noise in log files. The CLI still prints
the final summary line; that's enough for non-interactive runs.

### Disconnect detection (`HealthMonitor`)

The main thread calls `health.record_error(reason)` for every
`WalkError` and the writer does the same for `fp.error` from the
fingerprint workers. Recording on the main thread for walker errors is
critical — `is_disconnected()` is checked immediately after, and we
need it to see the error that was just yielded, not whatever the
writer thread has caught up to. The check is a cheap count test;
crossing the threshold triggers a single `os.stat(root)` (rate-limited
to once per `recheck_interval_s`) to confirm the share is actually
gone. On confirmation: `disconnected.set()` + `abort.set()`, end the
run with `status='disconnected'`, **skip `materialize_folders`** —
folder aggregates over a partial snapshot would lie in subsequent
reports.

## Path-derived tags

`tags.py::extract_tags(path)` turns `Z:\maschinen\12345\anleitungen\gasmesser\xyz.pdf`
into `['maschinen', '12345', 'anleitungen', 'gasmesser']`. The sink
calls it once per file at insert time and stores the JSON-encoded
list in `files.tags`. The `rag_candidates.jsonl` export decodes and
emits the list so downstream RAG can filter without re-parsing
paths.

The function is deliberately small (~30 LOC) but hardened for the
messy paths that real shares throw at it:

- Strips the Windows long-path prefix (`\\?\` and `\\?\UNC\`)
  *before* splitting, so `unc` and `?` never leak as tags.
- Drops drive letters (`C:`, `Z:`) — they're location, not content.
- Skips `_`-, `.`-, and `$`-prefixed folders (private / hidden /
  system) before lowercasing.
- Lowercases for matching, then dedupes — `Foo/foo/bar` emits one
  `foo`.
- Drops blocklisted organisational chrome (`shared`, `backup`,
  `temp`, `final`, …); see `BLOCKLIST` in `tags.py`.
- Caps tag length at `MAX_TAG_LEN = 64` and tag count at
  `MAX_TAGS = 16`. Pathologically deep paths get clipped — `tags`
  is a tag set, not a path index.

When you change the rules: edit `tags.py` and rerun
`tests/test_tags.py`. The `realistic_german_smb_path` test is the
canonical "this is what real users hit" check.

The `tags` column is added in migration v3. v2 databases upgrade
cleanly with the column NULLed on legacy rows; new inserts
populate it. Deleted-row carryover (`copy_deleted_from_previous`)
copies tags forward from the prior run — a deleted file's path
doesn't change, so neither do its tags.

## SMB / Windows quirks

These bit us on real shares; they're worth understanding before changing
the affected code.

- **`st_ino == 0` on remote filesystems.** Windows often returns 0 for
  the inode on SMB-mounted directories because the file-id API isn't
  fully supported. The walker's symlink-loop guard skips the inode check
  when `st_ino == 0` (and only enables the guard at all when
  `follow_symlinks=True`, since loops are structurally impossible without
  symlink traversal). See `walker.py::LocalScandirWalker.walk` and
  `tests/test_crawler.py::test_walker_does_not_falsely_flag_smb_loop`.
- **Timestamp resolution varies by protocol.** SMB1 rounds to 100 ns,
  FAT to 2 s, ext4 to 1 ns. A re-mount under a different protocol can
  round mtimes differently between two scans of the same unmodified
  file. `RescanContext` therefore compares mtimes with a 2 s tolerance
  whenever size is unchanged. See `rescan.py::DEFAULT_MTIME_TOLERANCE_S`.
- **Transient I/O errors.** Network blips, server reconnects, and brief
  lock contention all surface as `OSError` with errnos like `ETIMEDOUT`,
  `ECONNRESET`, or Windows `ERROR_NETNAME_DELETED` (winerror 64). The
  walker's `os.scandir` and `entry.stat`, plus the fingerprinter's
  `open()`, retry up to `len(retry_backoff)` times for the errnos in
  `retry.TRANSIENT_ERRNOS / TRANSIENT_WINERRORS`. **Reads inside an open
  file are NOT retried** — a mid-stream failure invalidates the partial
  hash, so we abandon and record the error rather than re-cost the
  bytes already read. The retry budget is per-call, not global; that
  means the worst case for a fully-degraded share is `attempts × N`
  files of pain rather than instant failure. Worth keeping in mind.
- **Curated default excludes.** `config.py::DEFAULT_EXCLUDES` filters
  the standard nuisance set (`~$*` Office locks, `Thumbs.db`,
  `desktop.ini`, `*.tmp`, `$RECYCLE.BIN`, `.DS_Store`, …) on every scan.
  Disable with `--no-default-excludes` or
  `[scan].default_excludes = false`. Edit the constant — not the
  walker — when adding new patterns.

## File state and incremental rescan

Every `files` row carries a `state` column:

| state       | meaning                                                      |
|-------------|--------------------------------------------------------------|
| `baseline`  | full scan, no prior run                                      |
| `added`     | path didn't exist in the previous run                        |
| `modified`  | size or mtime changed since the previous run                 |
| `unchanged` | (size, mtime) match the previous run; sha256/MIME reused     |
| `deleted`   | path was in the previous run, isn't in this one — row carries the *prior* run's metadata so reports can show what's gone |

**Every report query must filter `state != 'deleted'`** unless it's
specifically reporting on churn. `materialize_folders` already does this,
so anything reading from `folders` is safe; queries that read `files`
directly need an explicit predicate.

`crawl_runs.previous_run_id` links a rescan to its baseline. Phase 2
write-back will key its audit log on the same id pair.

`RescanContext` (`crawl/rescan.py`) loads the prior snapshot into an
in-memory dict (~150 B per file, ~150 MB at 1M files). The orchestrator
calls `classify(entry)` before queuing; `unchanged` rows skip the
fingerprint workers entirely and go straight to `out_q`. After the walk,
`SqliteSink.copy_deleted_from_previous` does a single `INSERT … SELECT`
to materialise deleted rows from the prior run.

Memory ceiling is the obvious follow-up: a streaming merge against a
sorted walker would drop it to O(depth), but Phase 2 v1 trades that
complexity for clarity.

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

## Performance notes

Done in the v2 hardening pass; kept here so the rationale doesn't get
lost:

- `sink.wal_checkpoint()` issues `PRAGMA wal_checkpoint(TRUNCATE)` every
  `checkpoint_every` files (and once at end-of-run). The WAL no longer
  grows unbounded on a 1M-file scan.
- `staleness_buckets`, `top_duplicates`, `rag_candidates` are all
  pure-SQL aggregates now. `top_duplicates` collects sample paths via a
  windowed `GROUP_CONCAT` with a U+001F separator (Unit Separator never
  appears in a path). `rag_candidates` dedups via
  `ROW_NUMBER() OVER (PARTITION BY COALESCE(sha256, 'p:'||path))` so
  files without a hash never collapse against unrelated files.
- Composite indexes `(run_id, parent_path|mime_category|sha256|state|path)`
  match every report's leading filter.

Still on the list:

- Owner extraction (`pywin32`) — deliberately deferred per PRD.
- Streaming merge for rescan against a sorted walker (memory ceiling).
- True move-detection in rescan — currently a renamed file is recorded
  as `deleted` + `added` even when its sha256 is unchanged.

## CLI ergonomics

- `share-analyzer report all --db ... --out ...` runs every report into
  one directory.
- `--run-id` is optional; the latest run is used by default.
- `share-analyzer rescan --db ...` defaults `--from-run` to the most
  recent completed run and reuses its `root_path`. To diff against a
  specific run, pass `--from-run <id>`. A different root_path is
  rejected — start a fresh index with `scan` instead.
- `share-analyzer scan PATH --dry-run` walks the share without
  fingerprinting and without writing to a DB; prints file/folder
  counts, total size, top extensions, sample paths. Useful as a
  pre-flight check before committing to a multi-hour crawl.
- `--dir-workers N` parallelises directory enumeration. Default 4 — on
  high-latency SMB shares, the walk is the bottleneck rather than
  fingerprinting, so spreading scandir across threads typically pays.
  `--dir-workers 1` keeps the original sequential walk for debugging.
- The CLI exits with status 2 when a run ends `disconnected` and
  surfaces a one-line `advisory` from the HealthMonitor when error
  rates are elevated but the share is still up — that's the user's
  cue to lower `--workers` or check the server.
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
