"""CLI entry point.

Default (no subcommand) runs the full pipeline: fetch → digest → render, into one new
run directory out/<timestamp>/ holding board.json, digest.json and digest.html.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

from .digest import Digest, dt, make_digest
from .fetch import GitHubError, fetch_board
from .render import render

DEFAULT_OUT_DIR = Path("out")
DEFAULT_WINDOW = timedelta(days=14)

_out_dir = click.option("--out-dir", type=click.Path(path_type=Path), default=DEFAULT_OUT_DIR, show_default=True)
_since = click.option(
    "--since", type=click.DateTime(), default=None,
    help="Start of the reporting window. Default: the previous run's fetch time, else 14 days ago.",
)
_run_dir = click.argument("run_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))


def _load_config() -> tuple[str, str, int]:
    """Load .env and return (token, org, project_number). Exits on missing token."""
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        click.echo("ERROR: GITHUB_TOKEN is not set. Copy .env.example to .env and fill it in.", err=True)
        sys.exit(2)
    org = os.environ.get("FIBSEM_ORG", "JaneliaSciComp").strip()
    return token, org, int(os.environ.get("FIBSEM_PROJECT_NUMBER", "9"))


def default_since(out_dir: Path, now: datetime) -> datetime:
    """When the previous run fetched, or 14 days ago if there is none."""
    previous = sorted(out_dir.glob("*/board.json"))
    if previous:
        return dt(json.loads(previous[-1].read_text(encoding="utf-8"))["fetched_at"])
    return now - DEFAULT_WINDOW


def _board(run_dir: Path) -> dict:
    return json.loads((run_dir / "board.json").read_text(encoding="utf-8"))


def _write(run_dir: Path, name: str, text: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    path.write_text(text, encoding="utf-8")
    return path


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """FIB-SEM reconstruction digest: fetch the board, digest it with Claude, render HTML."""
    if ctx.invoked_subcommand is None:
        run_dir = ctx.invoke(fetch)
        ctx.invoke(digest, run_dir=run_dir)


@cli.command()
@_out_dir
@_since
def fetch(out_dir: Path, since: datetime | None) -> Path:
    """Pull the board from GitHub into out/<timestamp>/board.json."""
    token, org, project_number = _load_config()
    now = datetime.now(timezone.utc)
    since = since.replace(tzinfo=timezone.utc) if since and since.tzinfo is None else since
    since = since or default_since(out_dir, now)
    click.echo(f"Fetching {org}/projects/{project_number}, window since {since:%Y-%m-%d %H:%M} UTC...")
    try:
        board = fetch_board(org, project_number, token, since)
    except GitHubError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    run_dir = out_dir / now.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    _write(run_dir, "board.json", json.dumps(board, indent=1, ensure_ascii=False))
    click.echo(f"Wrote {len(board['datasets'])} dataset(s) to {run_dir / 'board.json'}")
    return run_dir


@cli.command()
@_run_dir
def digest(run_dir: Path) -> None:
    """Ask claude -p for the narrative digest of RUN_DIR/board.json, then render it."""
    board = _board(run_dir)
    click.echo(f"Digesting {len(board['datasets'])} dataset(s) via claude -p...")
    d = make_digest(board)
    _write(run_dir, "digest.json", d.model_dump_json(indent=2))
    click.echo(f"Wrote {_write(run_dir, 'digest.html', render(board, d))}")


@cli.command("render")
@_run_dir
def render_cmd(run_dir: Path) -> None:
    """Re-render RUN_DIR/digest.html from its board.json and digest.json (no Claude call)."""
    d = Digest.model_validate_json((run_dir / "digest.json").read_text(encoding="utf-8"))
    click.echo(f"Wrote {_write(run_dir, 'digest.html', render(_board(run_dir), d))}")


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard: report, don't traceback
        click.echo(f"ERROR: {type(e).__name__}: {e}", err=True)
        sys.exit(1)
