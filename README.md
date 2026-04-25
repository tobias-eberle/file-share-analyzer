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
share-analyzer scan <path> --db <file.db>
                  [--workers 8] [--hash-cap-mb 100]
                  [--exclude <glob>]... [--checkpoint-every 10000]
                  [--follow-symlinks] [-v]

share-analyzer rescan [<path>] --db <file.db>
                  [--from-run <id>] [--workers 8] [--hash-cap-mb 100]
                  [--exclude <glob>]... [--checkpoint-every 10000]
                  [--follow-symlinks] [-v]

share-analyzer report <name|all> --db <file.db> --out <dir>
                  [--format html|csv|jsonl|all] [--run-id <id>]

share-analyzer info --db <file.db> [--run-id <id>]
```

`rescan` re-walks the share, reuses prior SHA-256 + MIME for files
whose `(size, mtime)` are unchanged, fingerprints only the delta, and
records added / modified / deleted rows so reports can show churn.
Defaults to diffing against the most recent completed run.

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
