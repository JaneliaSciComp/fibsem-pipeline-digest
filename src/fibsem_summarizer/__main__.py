"""CLI entry point.

Default (no subcommand) runs the full pipeline: fetch → summarize.
Subcommands are available for running individual stages or converting markdown to pptx.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from .fetch import GitHubError, fetch_all
from .slides import markdown_to_pptx
from .summarize import summarize_biweekly, summarize_dataset


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
    """Run claude -p to produce per-dataset and aggregated markdown summaries."""
    snapshots = sorted(data_dir.glob("*.json"))
    if not snapshots:
        click.echo(f"No snapshots in {data_dir}/. Run `fetch` first.", err=True)
        sys.exit(1)

    cumulative_dir = out_dir / "cumulative"
    click.echo(f"Summarizing {len(snapshots)} dataset(s) via claude -p...")
    for snap in snapshots:
        click.echo(f"  {snap.name} -> cumulative")
        summarize_dataset(snap, cumulative_dir)

    biweekly_path = out_dir / "biweekly.md"
    click.echo("  aggregating biweekly report")
    summarize_biweekly(snapshots, biweekly_path)
    click.echo(f"Wrote summaries under {out_dir}/.")


@cli.command()
@click.argument("markdown", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output .pptx path. Defaults to <markdown>.pptx next to the input.",
)
def slides(markdown: Path, output: Path | None) -> None:
    """Convert a markdown file (e.g. out/biweekly.md) to a .pptx deck."""
    out_path = output if output is not None else markdown.with_suffix(".pptx")
    markdown_to_pptx(markdown, out_path)
    click.echo(f"Wrote {out_path}")


if __name__ == "__main__":
    cli()
