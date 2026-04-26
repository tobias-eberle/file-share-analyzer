# Share Analyzer (Phase 1)

Read-only crawler and analyzer for enterprise network shares. Builds a SQLite
index of every file's metadata and content fingerprint, then produces
interactive HTML and machine-readable exports suitable for scoping a RAG
ingestion project.

Phase 1 is intentionally narrow: index a mounted share, generate six reports,
hand off a `rag_candidates.jsonl` to the downstream pipeline.

## Quick start

```bash
# install (Python 3.12+, Poetry 2.x)
poetry install

# crawl a share into a SQLite index
poetry run share-analyzer scan /mnt/share --db share.sqlite --workers 8

# generate every report into ./out
poetry run share-analyzer report all --db share.sqlite --out ./out

# inspect the latest run
poetry run share-analyzer info --db share.sqlite
```

The HTML reports are self-contained single files — Plotly is inlined, no CDN —
so they can be emailed or dropped on a SharePoint without hosting.

## Reports

| Name                | Output           | What it answers                                         |
|---------------------|------------------|---------------------------------------------------------|
| `topology`          | HTML treemap, CSV | Folder tree to depth 4, sized by bytes, coloured by MIME |
| `size_hotspots`     | HTML, CSV         | Top 50 largest folders at depths 1–4                     |
| `staleness`         | HTML, CSV         | Files bucketed by latest mtime (<1y, 1–3y, 3–5y, 5y+)    |
| `duplication`       | HTML, CSV         | Top duplicate clusters by wasted space                   |
| `type_distribution` | HTML, CSV         | Counts/sizes per MIME category and per top-20 extension  |
| `rag_candidates`    | JSONL             | Files matching the default ingestion filters             |

The default RAG filter is: `mime_category = text-extractable`,
`1 KB ≤ size ≤ 50 MB`, modified within the last 5 years, one canonical copy
per duplicate cluster.

## CLI

```
share-analyzer scan <path> [--db <file.db>]
                  [--workers 8] [--dir-workers 4] [--hash-cap-mb 100]
                  [--exclude <glob>]... [--no-default-excludes]
                  [--retry-attempts 3] [--checkpoint-every 10000]
                  [--follow-symlinks] [--dry-run] [-v]

share-analyzer rescan [<path>] --db <file.db>
                  [--from-run <id>] [--workers 8] [--dir-workers 4]
                  [--hash-cap-mb 100]
                  [--exclude <glob>]... [--no-default-excludes]
                  [--retry-attempts 3] [--checkpoint-every 10000]
                  [--follow-symlinks] [-v]

share-analyzer report <name|all> --db <file.db> --out <dir>
                  [--format html|csv|jsonl|all] [--run-id <id>]

share-analyzer info --db <file.db> [--run-id <id>]
```

**Pre-flight `--dry-run`.** `share-analyzer scan PATH --dry-run` walks
the share without fingerprinting and without writing a database. It
prints file/folder counts, total size, top extensions, and a few
sample paths — useful as a sanity check before committing to a
multi-hour scan.

**Parallel directory enumeration.** `--dir-workers N` parallelises
`os.scandir` across N threads. Default 4. On a high-latency SMB share
the walk is usually the bottleneck rather than fingerprinting, so
this typically pays off. Set to 1 for sequential debugging.

**Default excludes.** `scan` and `rescan` apply a curated list that
filters the usual Windows nuisance files: Office lock files (`~$*`),
`Thumbs.db`, `desktop.ini`, `*.tmp`, `$RECYCLE.BIN`, `.DS_Store`, and a
handful of system files. Disable with `--no-default-excludes` or
`[scan].default_excludes = false` in `share-analyzer.toml`.

**Transient I/O retries.** Network blips, locked files, Windows
`ERROR_NETNAME_DELETED`, … are retried with exponential backoff
(0.2 s / 1 s / 5 s by default). Tune with `--retry-attempts 0..N`;
`0` disables retries. Permanent errors (`ENOENT`, `EACCES`, …) are not
retried.

**Disconnect detection.** If the share unmounts mid-crawl, the run
ends with `status='disconnected'` (CLI exit code 2) instead of
masquerading as a successful scan with thousands of bogus errors.
Folder aggregates aren't materialised on a partial snapshot — re-run
`scan` once the mount is restored. Elevated-but-not-disconnected
error rates surface as a one-line advisory: `"high error rate: N
network-class errors in the last 10s — consider lowering --workers"`.

**UNC paths** work without `net use`: pass `\\server\share\path`
directly on Windows.

**`rescan`** re-walks the share, reuses prior SHA-256 + MIME for files
whose `(size, mtime)` are unchanged (within a 2 s tolerance to absorb
SMB1/FAT/NTFS rounding differences), fingerprints only the delta, and
records added / modified / deleted rows so reports can show churn.
Defaults to diffing against the most recent completed run.

**Path-derived tags.** Every indexed file gets a `tags` column —
folder names from its path, lowercased and deduped, with drive
letters / Windows long-path prefixes / hidden+system folders /
common organisational chrome (`shared`, `backup`, `final`, …)
stripped out. Surfaced in the RAG candidate JSONL so downstream
ingestion can filter by tag without re-parsing paths:

```json
{
  "path": "Z:\\maschinen\\12345\\anleitungen\\gasmesser\\xyz.pdf",
  "tags": ["maschinen", "12345", "anleitungen", "gasmesser"],
  "mime_type": "application/pdf",
  "size": 5242880,
  "...": "..."
}
```

A `share-analyzer.toml` in the working directory (or any parent) supplies
persistent defaults; CLI flags override.

```toml
[scan]
workers = 16
hash_cap_mb = 100
exclude = ["*.tmp", "Thumbs.db", "~$*"]
```

## Architecture

Three layers, each behind an interface so phase 2 can swap pieces without
touching the reports:

```
crawl/      Walker → Fingerprinter → Sink (orchestrated, bounded queues)
index/      schema + migrations + aggregation queries
reports/    Jinja2 templates + Plotly figures, file-system free
```

Phase 1 ships `LocalScandirWalker`, `StreamingFingerprinter`, `SqliteSink`.
Phase 2 plugs in `SmbDirectWalker`, `IncrementalSink`, `PostgresSink`, and a
`Mutator` write-back layer without rewrites.

## Data model

Single SQLite database, WAL mode, schema-versioned via the `schema_version`
table. Core tables:

- `crawl_runs`     — one row per scan
- `files`          — full metadata + sha256 + MIME per file
- `folders`        — materialised aggregates per folder (file count, size,
                      max depth below, mtime range, dominant MIME category)
- `crawl_errors`   — every skipped item with a reason
- `crawl_checkpoint` — resume seed for phase 2 incremental scans
- `mime_categories` — text-extractable / ocr-needed / media / archive /
                      executable / other
- `duplicates`     — view grouping files by sha256 with count ≥ 2

## Development

```bash
poetry install --with dev
poetry run pytest
```

The test suite covers crawler robustness (permission denied, unicode,
long paths, symlink loops, hash cap, exclude globs), schema migrations,
and report generation against a fixture share.

## Distribution

A single-binary Windows build is produced via PyInstaller:

```bash
poetry run pyinstaller --onefile --name share-analyzer share_analyzer/__main__.py
```

## Phase 1 scope

In: read-only crawl, SQLite index, six reports, JSONL hand-off.
Out: ACL/PII scanning, write-back, incremental rescan, SMB-direct, multi-share
federation, full-text indexing, web UI.
