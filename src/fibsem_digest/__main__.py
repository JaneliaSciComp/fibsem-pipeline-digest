"""CLI entry point.

Default (no subcommand) runs the full pipeline: fetch → digest → render.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from .digest import Digest, load_cycle, make_digest
from .fetch import GitHubError, fetch_all
from .render import render

# Repo-root-relative locations. The CLI is invoked from the project directory
# (that's where `uv run` lives), so this keeps paths predictable.
DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUT_DIR = Path("out")

_data_dir = click.option(
    "--data-dir", type=click.Path(path_type=Path), default=DEFAULT_DATA_DIR, show_default=True
)


def _load_config() -> tuple[str, str, int]:
    """Load .env and return (token, org, project_number). Exits on missing token."""
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        click.echo("ERROR: GITHUB_TOKEN is not set. Copy .env.example to .env and fill it in.", err=True)
        sys.exit(2)
    org = os.environ.get("FIBSEM_ORG", "JaneliaSciComp").strip()
    return token, org, int(os.environ.get("FIBSEM_PROJECT_NUMBER", "9"))


def _cycle(data_dir: Path) -> list[dict]:
    cycle = load_cycle(data_dir)
    if not cycle:
        click.echo(f"No snapshots in {data_dir}/. Run `fetch` first.", err=True)
        sys.exit(1)
    return cycle


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """FIB-SEM reconstruction digest: fetch the board, digest it with Claude, render HTML."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(fetch)
        ctx.invoke(digest)


@cli.command()
@_data_dir
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
@_data_dir
@click.option("--out-dir", type=click.Path(path_type=Path), default=DEFAULT_OUT_DIR, show_default=True)
def digest(data_dir: Path, out_dir: Path) -> None:
    """Ask claude -p for the narrative digest of the latest fetch, then render it.

    Writes out/<timestamp>/digest.json (Claude's output) and digest.html next to it.
    """
    cycle = _cycle(data_dir)
    run_dir = out_dir / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    click.echo(f"Digesting {len(cycle)} dataset(s) via claude -p...")
    d = make_digest(cycle)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "digest.json").write_text(d.model_dump_json(indent=2), encoding="utf-8")
    out = _render(cycle, d, run_dir)
    click.echo(f"Wrote {out}")


@cli.command("render")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_data_dir
def render_cmd(run_dir: Path, data_dir: Path) -> None:
    """Re-render digest.html in RUN_DIR from its digest.json and the current snapshots."""
    d = Digest.model_validate_json((run_dir / "digest.json").read_text(encoding="utf-8"))
    click.echo(f"Wrote {_render(_cycle(data_dir), d, run_dir)}")


def _render(cycle: list[dict], d: Digest, run_dir: Path) -> Path:
    out = run_dir / "digest.html"
    out.write_text(render(cycle, d), encoding="utf-8")
    return out


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard: report, don't traceback
        click.echo(f"ERROR: {type(e).__name__}: {e}", err=True)
        sys.exit(1)
