"""CLI entry point.

Default (no subcommand) runs the full pipeline: fetch → summarize.
Subcommands are available for running individual stages or converting markdown to pptx.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from .fetch import GitHubError, fetch_all
from .slides import markdown_to_pdf
from .summarize import (
    INTERNAL_FILES,
    changed_since_last_summary,
    clear_pending_run,
    load_pending_run,
    output_path_for,
    record_summarized,
    save_pending_run,
    summarize_biweekly,
    summarize_dataset,
)


# Repo-root-relative output locations. The CLI is invoked from the project
# directory (that's where `uv run` lives), so this keeps paths predictable.
DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUT_DIR = Path("out")


def _load_config() -> tuple[str, str, int]:
    """Load .env and return (token, org, project_number). Exits on missing token."""
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        click.echo(
            "ERROR: GITHUB_TOKEN is not set. Copy .env.example to .env and fill it in.",
            err=True,
        )
        sys.exit(2)
    org = os.environ.get("FIBSEM_ORG", "JaneliaSciComp").strip()
    project_number = int(os.environ.get("FIBSEM_PROJECT_NUMBER", "9"))
    return token, org, project_number


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """FIBSEM project summarizer.

    Run with no subcommand to execute the full pipeline (fetch then summarize).
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(fetch)
        ctx.invoke(summarize)


@cli.command()
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DATA_DIR,
    show_default=True,
    help="Directory to write JSON snapshots into.",
)
def fetch(data_dir: Path) -> None:
    """Pull GitHub issues from the project board into JSON snapshots."""
    token, org, project_number = _load_config()
    click.echo(f"Fetching items from {org}/projects/{project_number}...")
    try:
        written = fetch_all(data_dir, org, project_number, token)
    except GitHubError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    click.echo(f"Wrote/updated {len(written)} snapshot(s) in {data_dir}/.")


@cli.command()
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_DATA_DIR,
    show_default=True,
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_OUT_DIR,
    show_default=True,
)
def summarize(data_dir: Path, out_dir: Path) -> None:
    """Run claude -p to produce per-dataset and aggregated markdown summaries.

    Each reporting cycle writes into a fresh timestamped subdirectory of `out/` so prior
    cycles are preserved untouched. All markdown files (per-dataset cumulative reports
    plus the aggregated biweekly report) sit flat in that directory.

    If a previous run left datasets unsummarized, re-running resumes into that same
    directory: only the missing per-dataset summaries are generated, and the biweekly
    report is rewritten to cover the whole cycle, not just the retried datasets.
    """
    snapshots = sorted(
        p for p in data_dir.glob("*.json") if p.name not in INTERNAL_FILES
    )
    if not snapshots:
        click.echo(f"No snapshots in {data_dir}/. Run `fetch` first.", err=True)
        sys.exit(1)

    to_process = changed_since_last_summary(snapshots, data_dir)
    skipped = len(snapshots) - len(to_process)
    if skipped:
        click.echo(f"Skipping {skipped} dataset(s) untouched since the last summary.")

    pending = load_pending_run(data_dir)
    if pending is not None:
        # Resume the unfinished cycle, folding in anything that changed since.
        run_dir, cycle = pending
        known = {p.name for p in cycle}
        cycle = cycle + [p for p in to_process if p.name not in known]
        click.echo(f"Resuming unfinished run in {run_dir}/ ({len(cycle)} in cycle).")
    else:
        if not to_process:
            click.echo("Nothing new to summarize.")
            return
        run_dir = out_dir / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        cycle = to_process

    run_dir.mkdir(parents=True, exist_ok=True)
    save_pending_run(data_dir, run_dir, cycle)

    # An existing output file is the done-marker, so a resumed run redoes only the gaps
    # (and regenerates anything whose .md was deleted by hand).
    todo = [p for p in cycle if not output_path_for(p, run_dir).exists()]
    failed: list[Path] = []
    if todo:
        click.echo(f"Summarizing {len(todo)} dataset(s) via claude -p into {run_dir}/")
    for snap in todo:
        try:
            written = summarize_dataset(snap, run_dir)
        except RuntimeError as e:
            click.echo(f"  {snap.name} FAILED: {e}", err=True)
            failed.append(snap)
            continue
        record_summarized([snap], data_dir)
        click.echo(f"  {snap.name} -> {written.name}")

    available = [p for p in cycle if output_path_for(p, run_dir).exists()]
    if not available:
        click.echo("No datasets summarized; no biweekly report written.", err=True)
        sys.exit(1)

    biweekly_path = run_dir / "biweekly.md"
    click.echo(f"  aggregating biweekly report over {len(available)} dataset(s)")
    try:
        summarize_biweekly(available, biweekly_path)
    except RuntimeError as e:
        click.echo(f"  biweekly report FAILED: {e}", err=True)
        click.echo("Re-run to rebuild it; per-dataset summaries are kept.", err=True)
        sys.exit(1)

    click.echo(f"Wrote summaries under {run_dir}/.")
    if failed:
        click.echo(
            f"{len(failed)} dataset(s) still failing; re-run to retry just those and "
            "refresh the biweekly report over the full cycle.",
            err=True,
        )
        sys.exit(1)

    clear_pending_run(data_dir)


@cli.command()
@click.argument("markdown", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output .pdf path. Defaults to <markdown>.pdf next to the input.",
)
def pdf(markdown: Path, output: Path | None) -> None:
    """Render a markdown file (e.g. out/biweekly.md) to a styled PDF."""
    out_path = output if output is not None else markdown.with_suffix(".pdf")
    markdown_to_pdf(markdown, out_path)
    click.echo(f"Wrote {out_path}")


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Re-run to resume from where this left off.", err=True)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard: report, don't traceback
        click.echo(f"ERROR: {type(e).__name__}: {e}", err=True)
        sys.exit(1)
