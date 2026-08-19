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
    STATE_FILENAME,
    changed_since_last_summary,
    forget_summarized,
    record_summarized,
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

    Each run writes into a fresh timestamped subdirectory of `out/` so prior runs are
    preserved untouched. All markdown files (per-dataset cumulative reports plus the
    aggregated biweekly report) sit flat in that directory.
    """
    snapshots = sorted(
        p for p in data_dir.glob("*.json") if p.name != STATE_FILENAME
    )
    if not snapshots:
        click.echo(f"No snapshots in {data_dir}/. Run `fetch` first.", err=True)
        sys.exit(1)

    to_process = changed_since_last_summary(snapshots, data_dir)
    skipped = len(snapshots) - len(to_process)
    if skipped:
        click.echo(f"Skipping {skipped} dataset(s) untouched since the last summary.")
    if not to_process:
        click.echo("Nothing new to summarize.")
        return

    run_dir = out_dir / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"Summarizing {len(to_process)} dataset(s) via claude -p into {run_dir}/")
    done: list[Path] = []
    failed: list[Path] = []
    for snap in to_process:
        try:
            written = summarize_dataset(snap, run_dir)
        except RuntimeError as e:
            click.echo(f"  {snap.name} FAILED: {e}", err=True)
            failed.append(snap)
            continue
        # Record immediately so a re-run after a later failure skips this dataset.
        record_summarized([snap], data_dir)
        done.append(snap)
        click.echo(f"  {snap.name} -> {written.name}")

    if not done:
        click.echo("All datasets failed; no biweekly report written.", err=True)
        sys.exit(1)

    biweekly_path = run_dir / "biweekly.md"
    click.echo("  aggregating biweekly report")
    try:
        summarize_biweekly(done, biweekly_path)
    except RuntimeError as e:
        forget_summarized(done, data_dir)
        click.echo(f"  biweekly report FAILED: {e}", err=True)
        click.echo(
            f"Per-dataset summaries are in {run_dir}/. Re-run to rebuild the "
            "biweekly report.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Wrote summaries under {run_dir}/.")
    if failed:
        click.echo(
            f"{len(failed)} dataset(s) failed and were left unrecorded; re-run to "
            "retry just those. This biweekly report covers only the successful ones.",
            err=True,
        )
        sys.exit(1)


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
