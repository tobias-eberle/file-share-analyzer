"""Click-based CLI: scan, rescan, report, info."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import click

from share_analyzer import __version__
from share_analyzer.config import DEFAULT_EXCLUDES, load_config
from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.rescan import latest_completed_run
from share_analyzer.crawl.retry import DEFAULT_BACKOFF
from share_analyzer.dry_run import dry_run, format_summary
from share_analyzer.index.queries import (
    changed_files_summary, latest_run_id, run_summary,
)
from share_analyzer.index.schema import init_db
from share_analyzer.logging import ProgressPrinter, configure_logging
from share_analyzer.reports import REPORTS, run_all, run_report

_REPORT_NAMES = sorted(REPORTS.keys())


def _resolve_excludes(cfg: dict, cli_excludes: Sequence[str],
                      no_defaults: bool) -> tuple[str, ...]:
    """Compose the effective exclude list from defaults + TOML + CLI."""
    use_defaults = not no_defaults and cfg.get("default_excludes", True)
    defaults = DEFAULT_EXCLUDES if use_defaults else ()
    return tuple(defaults) + tuple(cfg.get("exclude", [])) + tuple(cli_excludes)


def _resolve_backoff(retry_attempts: int) -> tuple[float, ...]:
    """Translate --retry-attempts into a backoff sequence.

    `attempts` counts the *retries* after the first try, so 0 disables
    retries entirely. We take the first N entries from DEFAULT_BACKOFF
    and pad with the last value when the user asks for more than the
    default length — keeps the curve sensible without a separate flag.
    """
    n = max(0, retry_attempts)
    if n == 0:
        return ()
    if n <= len(DEFAULT_BACKOFF):
        return DEFAULT_BACKOFF[:n]
    return DEFAULT_BACKOFF + (DEFAULT_BACKOFF[-1],) * (n - len(DEFAULT_BACKOFF))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="share-analyzer")
@click.option("--config", type=click.Path(path_type=Path, exists=True, dir_okay=False),
              default=None, help="Path to share-analyzer.toml.")
@click.pass_context
def main(ctx: click.Context, config: Path | None) -> None:
    """Crawl a network share and produce RAG-prep reports."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config)


@main.command()
@click.argument("path", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--db", "db_path", type=click.Path(path_type=Path), required=False,
              default=None, help="SQLite index file to write. "
                                  "Optional with --dry-run.")
@click.option("--workers", type=int, default=8, show_default=True,
              help="Parallel fingerprint workers.")
@click.option("--dir-workers", type=int, default=4, show_default=True,
              help="Parallel directory-enumeration workers. "
                   "1 = sequential (lower memory). Higher = better on "
                   "high-latency SMB shares.")
@click.option("--hash-cap-mb", type=int, default=100, show_default=True,
              help="Skip SHA-256 for files larger than this (in MB).")
@click.option("--exclude", "exclude_globs", multiple=True,
              help="Glob to exclude (matched against name and full path). Repeatable.")
@click.option("--no-default-excludes", is_flag=True, default=False,
              help="Don't apply the curated nuisance-file exclude list "
                   "(~$*, Thumbs.db, desktop.ini, *.tmp, $RECYCLE.BIN, …).")
@click.option("--retry-attempts", type=int, default=len(DEFAULT_BACKOFF),
              show_default=True,
              help="Retry transient I/O errors this many times "
                   "(0 = no retries). Backoff is 0.2s, 1s, 5s.")
@click.option("--checkpoint-every", type=int, default=10_000, show_default=True,
              help="Persist a checkpoint every N files.")
@click.option("--follow-symlinks", is_flag=True, default=False,
              help="Follow symbolic links during traversal.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Walk PATH without fingerprinting or writing to the "
                   "DB; print a summary (file/folder counts, total size, "
                   "top extensions, sample paths).")
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def scan(ctx: click.Context, path: Path, db_path: Path | None,
         workers: int, dir_workers: int, hash_cap_mb: int,
         exclude_globs: tuple[str, ...],
         no_default_excludes: bool, retry_attempts: int,
         checkpoint_every: int, follow_symlinks: bool,
         dry_run: bool, verbose: bool) -> None:
    """Crawl PATH and build the SQLite index."""
    cfg = ctx.obj["config"].get("scan", {}) if ctx.obj else {}
    workers = cfg.get("workers", workers)
    dir_workers = cfg.get("dir_workers", dir_workers)
    hash_cap_mb = cfg.get("hash_cap_mb", hash_cap_mb)
    effective_excludes = _resolve_excludes(cfg, exclude_globs, no_default_excludes)

    if dry_run:
        click.echo(f"share-analyzer {__version__}: dry-run on {path}")
        from share_analyzer.dry_run import dry_run as _dry_run
        summary = _dry_run(
            path,
            exclude_globs=effective_excludes,
            follow_symlinks=follow_symlinks,
            retry_backoff=_resolve_backoff(retry_attempts),
            dir_workers=dir_workers,
        )
        click.echo(format_summary(summary))
        return

    if db_path is None:
        raise click.UsageError("--db is required unless --dry-run is set")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    configure_logging(db_path, verbose=verbose)
    progress = ProgressPrinter()

    def on_progress(files: int, errors: int, _bytes: int) -> None:
        # invoked inside the writer thread; safe — ProgressPrinter is locked
        progress._count = files  # noqa: SLF001 — direct because we already counted
        progress._errors = errors  # noqa: SLF001
        progress.update(force=True)

    options = CrawlOptions(
        workers=workers,
        dir_workers=dir_workers,
        hash_cap_bytes=hash_cap_mb * 1024 * 1024,
        exclude_globs=effective_excludes,
        checkpoint_every=checkpoint_every,
        follow_symlinks=follow_symlinks,
        retry_backoff=_resolve_backoff(retry_attempts),
    )

    click.echo(f"share-analyzer {__version__}: scanning {path} → {db_path}")
    result = run_crawl(path, db_path, options, progress=on_progress)
    progress.finish()
    if result.status == "disconnected":
        click.echo(
            f"interrupted — run #{result.run_id} marked 'disconnected': "
            f"{result.file_count:,} files indexed, "
            f"{result.error_count:,} errors before the share went away"
        )
        if result.advisory:
            click.echo(f"  advisory: {result.advisory}")
        sys.exit(2)
    click.echo(
        f"done — run #{result.run_id}: "
        f"{result.file_count:,} files, {result.error_count:,} errors"
    )
    if result.advisory:
        click.echo(f"  advisory: {result.advisory}")


@main.command()
@click.argument("path", type=click.Path(path_type=Path, exists=True, file_okay=False),
                required=False)
@click.option("--db", "db_path", type=click.Path(path_type=Path, exists=True),
              required=True, help="Existing SQLite index from a previous scan.")
@click.option("--from-run", type=int, default=None,
              help="Run id to diff against (defaults to most recent completed).")
@click.option("--workers", type=int, default=8, show_default=True)
@click.option("--dir-workers", type=int, default=4, show_default=True)
@click.option("--hash-cap-mb", type=int, default=100, show_default=True)
@click.option("--exclude", "exclude_globs", multiple=True)
@click.option("--no-default-excludes", is_flag=True, default=False)
@click.option("--retry-attempts", type=int, default=len(DEFAULT_BACKOFF),
              show_default=True)
@click.option("--checkpoint-every", type=int, default=10_000, show_default=True)
@click.option("--follow-symlinks", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def rescan(ctx: click.Context, path: Path | None, db_path: Path, from_run: int | None,
           workers: int, dir_workers: int, hash_cap_mb: int,
           exclude_globs: tuple[str, ...],
           no_default_excludes: bool, retry_attempts: int,
           checkpoint_every: int, follow_symlinks: bool, verbose: bool) -> None:
    """Crawl PATH again and record only the delta against a prior run.

    Files whose (size, mtime) match the prior snapshot reuse the
    previous SHA-256 and MIME — no rehash. New, modified, and deleted
    files are recorded so reports can show the churn.
    """
    conn = init_db(db_path)
    if from_run is None:
        prev = latest_completed_run(conn)
        if prev is None:
            raise click.ClickException(
                "no completed runs in this database — use `scan` first"
            )
        from_run = prev["id"]
        prev_root = prev["root_path"]
    else:
        s = run_summary(conn, from_run)
        if not s:
            raise click.ClickException(f"run {from_run} not found")
        if s["status"] != "completed":
            raise click.ClickException(
                f"run {from_run} status is {s['status']}, refusing to diff"
            )
        prev_root = s["root_path"]
    conn.close()

    if path is None:
        path = Path(prev_root)
    if str(path) != prev_root:
        raise click.ClickException(
            f"rescan path '{path}' differs from prior run root '{prev_root}'; "
            "use `scan` to start a fresh index for a different root"
        )

    configure_logging(db_path, verbose=verbose)
    progress = ProgressPrinter()

    def on_progress(files: int, errors: int, _bytes: int) -> None:
        progress._count = files     # noqa: SLF001
        progress._errors = errors   # noqa: SLF001
        progress.update(force=True)

    cfg = ctx.obj["config"].get("scan", {}) if ctx.obj else {}
    effective_excludes = _resolve_excludes(cfg, exclude_globs, no_default_excludes)
    options = CrawlOptions(
        workers=workers,
        dir_workers=dir_workers,
        hash_cap_bytes=hash_cap_mb * 1024 * 1024,
        exclude_globs=effective_excludes,
        checkpoint_every=checkpoint_every,
        follow_symlinks=follow_symlinks,
        previous_run_id=from_run,
        retry_backoff=_resolve_backoff(retry_attempts),
    )

    click.echo(
        f"share-analyzer {__version__}: rescanning {path} against run #{from_run}"
    )
    result = run_crawl(path, db_path, options, progress=on_progress)
    progress.finish()
    if result.status == "disconnected":
        click.echo(
            f"interrupted — run #{result.run_id} marked 'disconnected' "
            f"({result.error_count:,} errors before the share went away)"
        )
        if result.advisory:
            click.echo(f"  advisory: {result.advisory}")
        sys.exit(2)

    sc = result.state_counts or {}
    click.echo(
        f"done — run #{result.run_id} (vs #{result.previous_run_id}): "
        f"+{sc.get('added', 0):,} added "
        f"~{sc.get('modified', 0):,} modified "
        f"={sc.get('unchanged', 0):,} unchanged "
        f"-{sc.get('deleted', 0):,} deleted "
        f"({result.error_count:,} errors)"
    )
    if result.advisory:
        click.echo(f"  advisory: {result.advisory}")


@main.command()
@click.argument("name", type=click.Choice(["all", *_REPORT_NAMES], case_sensitive=False))
@click.option("--db", "db_path", type=click.Path(path_type=Path, exists=True),
              required=True)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), required=True,
              help="Output directory for report files.")
@click.option("--format", "fmt",
              type=click.Choice(["all", "html", "csv", "jsonl"]),
              default="all", show_default=True)
@click.option("--run-id", type=int, default=None,
              help="Crawl run id (defaults to most recent).")
def report(name: str, db_path: Path, out_dir: Path, fmt: str,
           run_id: int | None) -> None:
    """Generate report NAME from the index."""
    conn = init_db(db_path)
    rid = run_id or latest_run_id(conn)
    if rid is None:
        raise click.ClickException("no crawl runs found in the index")

    out_dir.mkdir(parents=True, exist_ok=True)
    if name == "all":
        artifacts = run_all(conn, rid, out_dir)
    else:
        artifacts = [run_report(name, conn, rid, out_dir, fmt)]

    for a in artifacts:
        for p in a.paths:
            click.echo(f"  {a.name}: {p}")
    conn.close()


@main.command()
@click.option("--db", "db_path", type=click.Path(path_type=Path, exists=True),
              required=True)
@click.option("--run-id", type=int, default=None)
def info(db_path: Path, run_id: int | None) -> None:
    """Print a summary of the latest (or given) crawl run."""
    conn = init_db(db_path)
    rid = run_id or latest_run_id(conn)
    if rid is None:
        click.echo("no crawl runs found")
        sys.exit(1)
    s = run_summary(conn, rid)
    folder_count = conn.execute(
        "SELECT COUNT(*) FROM folders WHERE run_id = ?", (rid,)
    ).fetchone()[0]
    err_sample = conn.execute(
        "SELECT path, reason FROM crawl_errors WHERE run_id = ? LIMIT 5", (rid,)
    ).fetchall()

    prev = conn.execute(
        "SELECT previous_run_id FROM crawl_runs WHERE id = ?", (rid,)
    ).fetchone()
    previous_run_id = prev[0] if prev else None

    click.echo(f"Run #{s['id']}")
    click.echo(f"  root        : {s['root_path']}")
    click.echo(f"  status      : {s['status']}")
    click.echo(f"  started     : {s['started_at']}")
    click.echo(f"  completed   : {s['completed_at']}")
    click.echo(f"  files       : {s['file_count']:,}")
    click.echo(f"  errors      : {s['error_count']:,}")
    click.echo(f"  folders     : {folder_count:,}")
    click.echo(f"  workers     : {s['workers']}")
    click.echo(f"  hash cap    : {s['hash_cap_bytes']} bytes")
    if previous_run_id is not None:
        sc = changed_files_summary(conn, rid)
        click.echo(f"  rescan of   : run #{previous_run_id}")
        click.echo(
            f"  delta       : "
            f"+{sc['added']:,} added "
            f"~{sc['modified']:,} modified "
            f"={sc['unchanged']:,} unchanged "
            f"-{sc['deleted']:,} deleted"
        )
    if err_sample:
        click.echo("  error sample:")
        for r in err_sample:
            click.echo(f"    - {r['path']}: {r['reason']}")
    conn.close()


if __name__ == "__main__":
    main()
