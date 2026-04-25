"""Click-based CLI: scan, report, info."""
from __future__ import annotations

import sys
from pathlib import Path

import click

from share_analyzer import __version__
from share_analyzer.config import load_config
from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.rescan import latest_completed_run
from share_analyzer.index.queries import (
    changed_files_summary, latest_run_id, run_summary,
)
from share_analyzer.index.schema import init_db
from share_analyzer.logging import ProgressPrinter, configure_logging
from share_analyzer.reports import REPORTS, run_all, run_report

_REPORT_NAMES = sorted(REPORTS.keys())


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
@click.option("--db", "db_path", type=click.Path(path_type=Path), required=True,
              help="SQLite index file to write.")
@click.option("--workers", type=int, default=8, show_default=True,
              help="Parallel fingerprint workers.")
@click.option("--hash-cap-mb", type=int, default=100, show_default=True,
              help="Skip SHA-256 for files larger than this (in MB).")
@click.option("--exclude", "exclude_globs", multiple=True,
              help="Glob to exclude (matched against name and full path). Repeatable.")
@click.option("--checkpoint-every", type=int, default=10_000, show_default=True,
              help="Persist a checkpoint every N files.")
@click.option("--follow-symlinks", is_flag=True, default=False,
              help="Follow symbolic links during traversal.")
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def scan(ctx: click.Context, path: Path, db_path: Path,
         workers: int, hash_cap_mb: int, exclude_globs: tuple[str, ...],
         checkpoint_every: int, follow_symlinks: bool, verbose: bool) -> None:
    """Crawl PATH and build the SQLite index."""
    cfg = ctx.obj["config"].get("scan", {}) if ctx.obj else {}
    workers = cfg.get("workers", workers)
    hash_cap_mb = cfg.get("hash_cap_mb", hash_cap_mb)
    exclude_globs = tuple(cfg.get("exclude", [])) + exclude_globs

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
        hash_cap_bytes=hash_cap_mb * 1024 * 1024,
        exclude_globs=exclude_globs,
        checkpoint_every=checkpoint_every,
        follow_symlinks=follow_symlinks,
    )

    click.echo(f"share-analyzer {__version__}: scanning {path} → {db_path}")
    result = run_crawl(path, db_path, options, progress=on_progress)
    progress.finish()
    click.echo(
        f"done — run #{result.run_id}: "
        f"{result.file_count:,} files, {result.error_count:,} errors"
    )


@main.command()
@click.argument("path", type=click.Path(path_type=Path, exists=True, file_okay=False),
                required=False)
@click.option("--db", "db_path", type=click.Path(path_type=Path, exists=True),
              required=True, help="Existing SQLite index from a previous scan.")
@click.option("--from-run", type=int, default=None,
              help="Run id to diff against (defaults to most recent completed).")
@click.option("--workers", type=int, default=8, show_default=True)
@click.option("--hash-cap-mb", type=int, default=100, show_default=True)
@click.option("--exclude", "exclude_globs", multiple=True)
@click.option("--checkpoint-every", type=int, default=10_000, show_default=True)
@click.option("--follow-symlinks", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False)
def rescan(path: Path | None, db_path: Path, from_run: int | None,
           workers: int, hash_cap_mb: int, exclude_globs: tuple[str, ...],
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

    options = CrawlOptions(
        workers=workers,
        hash_cap_bytes=hash_cap_mb * 1024 * 1024,
        exclude_globs=exclude_globs,
        checkpoint_every=checkpoint_every,
        follow_symlinks=follow_symlinks,
        previous_run_id=from_run,
    )

    click.echo(
        f"share-analyzer {__version__}: rescanning {path} against run #{from_run}"
    )
    result = run_crawl(path, db_path, options, progress=on_progress)
    progress.finish()

    sc = result.state_counts or {}
    click.echo(
        f"done — run #{result.run_id} (vs #{result.previous_run_id}): "
        f"+{sc.get('added', 0):,} added "
        f"~{sc.get('modified', 0):,} modified "
        f"={sc.get('unchanged', 0):,} unchanged "
        f"-{sc.get('deleted', 0):,} deleted "
        f"({result.error_count:,} errors)"
    )


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
