"""Run `claude -p` over JSON snapshots to produce markdown summaries.

Two outputs:
- per-dataset `out/cumulative/<slug>.md`: a cumulative methods + retrospective write-up
  suitable for drafting methods sections and internal process review.
- aggregated `out/biweekly.md`: a single status report for the internal meeting covering
  activity since the previous pull across all active datasets.

This module is intentionally thin: it shells out to `claude -p` and writes files. The
prompts live as module-level string constants below so they're easy to tweak in one place.
"""
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

CUMULATIVE_PROMPT = """\
You are analyzing a single FIB-SEM dataset reconstruction thread that was tracked as a \
GitHub issue. The issue body, every comment (in chronological order), and metadata \
(labels, assignees, column/status transitions, comment edits) are provided as JSON on \
stdin.

Write a Markdown document with exactly two top-level sections and no preamble.

## Reconstruction Steps

Produce a chronological bullet list of the concrete reconstruction steps that were \
performed on this dataset. This list is meant to be suitable for lifting directly into \
the methods section of a paper, so it should cover things like:
- imaging parameters (voxel size, FOV, beam conditions, if mentioned),
- alignment/stitching approach and any parameters,
- QC and review steps and their outcomes.

Use concise, factual bullets. Prefer specific numbers, tool names, and commit/branch \
references when they appear in the thread. Omit interpersonal chatter. If a step is \
mentioned only vaguely, keep the bullet but mark the uncertainty (e.g. "exact parameters \
not recorded in thread").

**Standard steps to check for explicitly.** The following are operations the team \
sometimes performs on a dataset; scan the thread for each and, if it was done, list it \
under Reconstruction Steps with the relevant parameters / scope:
- **Layer patching** — replacement of damaged, missing, charging, or otherwise unusable \
  layers (note which layers and for what reason).
- **Matching / registration parameter tuning** — changes to the resolution at which \
  matches are computed, regularization weights, error tolerances or similar knobs \
  (note the old → new value when possible).
- **Streak correction** - application of a manually inferred filter to reduce streak artifacts \
  in the raw data (note who did it).
- **Shading correction** - application of a manually inferred filter to reduce shading artifacts \
  in the raw data (note who did it).

If a standard step was clearly *not* performed (e.g. no layer patching was needed), do \
not list it — the absence is not informative. Only call out an item if the thread \
mentions it being done or explicitly considered and skipped.

## Process Retrospective

An internal-facing retrospective aimed at improving the reconstruction workflow. \
Organize as three short subsections:
- **What went well** — parts of the process that were smooth, fast, or well-handed-off.
- **Friction points** — places where the thread shows back-and-forth, unclear \
  ownership, delays, or reworks. Assembly ↔ Review oscillation is a normal QC loop; \
  describe it only if it looped several times without or only minor progress.
- **Suggested improvements** — concrete, low-cost changes for future datasets, grounded \
  in what you actually saw in the thread.

Frame everything as process improvement, not blame. Do not name individuals when \
describing friction; describe the step or handoff instead.

Output only the Markdown. No JSON, no commentary outside the sections.
"""

BIWEEKLY_PROMPT = """\
You are preparing a status report for an internal biweekly meeting about FIB-SEM dataset \
reconstruction. You are given, on stdin, a JSON array where each element is one dataset's \
snapshot. Each snapshot contains:
- `issue` (title, repository, URL, labels, assignees, current body),
- `current_status` (which column the dataset is in: Imaging / Assembly / Review / \
Advanced Processing / Done),
- `status_history` (all column transitions observed),
- `comments` (every comment, with author, createdAt, updatedAt, body),
- `edits` (comments that were edited since we last pulled),
- `last_pull_at` (when this fetch ran — now),
- `previous_last_pull_at` (when the prior fetch ran, or null if this is the first pull).

For each dataset, the "window of interest" is everything that happened after \
`previous_last_pull_at`. If `previous_last_pull_at` is null, the window is the entire \
history of the dataset.

Write a single Markdown document with this structure and no preamble:

## Since <previous_last_pull_at or "project start">

A one- or two-sentence overall framing of the period.

## Needs attention this week

A bullet list distilled across all datasets of items that genuinely need action before \
the next meeting. Include the dataset title next to each bullet. Keep this list \
short — only things that are actually blocked, stuck, or waiting on a specific person. \
If nothing needs attention, write "- Nothing blocking." and move on.

## Per-dataset updates

For each dataset (heading `### <repo>#<number> — <title>` with the issue URL as a link), \
write:
- **Owner:** formatted as `<owner> (<collaborator>)`. The owner is parsed from an \
  `owner: ...` line in the initial issue body. The collaborator is whichever of \
  `Cellmap`, `FuncEWOrm`, or `eFIB-SEM SR` appears in the issue labels. If either is \
  missing, write `unknown` in its place.
- **Status:** current column. If a transition happened in the window, show it like \
  `Assembly → Review (on 2026-03-20)`.
- **Progress:** 2-5 bullets of what actually advanced in the window — new imaging data, \
  alignment completed, model retrained, review comments addressed, etc. Cite specific \
  commenters only when the attribution matters (e.g. "QC signed off by <login>").
- **Open questions / blockers:** bullets of unresolved questions or things waiting on \
  someone. Omit the subsection if there are none.

**Transition handling rules** — important:
- Assembly ↔ Review bouncing is *normal QC iteration*. Describe it factually, do not \
  frame a single round-trip as regression or a problem.
- Only flag a dataset in "Needs attention this week" on transition grounds if either \
  (a) it has bounced between Assembly and Review **three or more times** in the window \
  without visible progress on the review comments, or \
  (b) it has been sitting in Review with an unanswered question for more than a week.
- A single Review → Assembly move with the assignee actively working through comments \
  is healthy; note it under Progress, not Needs attention.
- **Advanced Processing** is exploratory R&D that sits outside the normal reconstruction \
  pipeline. Cover those datasets under "Per-dataset updates" exactly like Assembly/Review \
  ones, but treat them as lower urgency: only list one in "Needs attention this week" if \
  it is explicitly waiting on a decision or a specific person, never merely for being \
  slow or quiet.

Output only the Markdown. No JSON, no commentary outside the sections.
"""


# --------------------------------------------------------------------------- #
# Subprocess wrapper
# --------------------------------------------------------------------------- #


MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 10


def _run_claude(prompt: str, stdin_payload: str) -> str:
    """Invoke `claude -p <prompt>` with the JSON payload on stdin. Return stdout.

    Retries on nonzero exit with linear backoff: `claude -p` fails transiently on API
    overload and rate limits. A missing binary is not transient, so it is not retried.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                input=stdin_payload,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "`claude` CLI not found on PATH. Install Claude Code first."
            ) from e
        except subprocess.CalledProcessError as e:
            # claude -p reports API/auth/rate-limit errors on stdout, not stderr.
            detail = ((e.stderr or "") + (e.stdout or ""))[:500] or "<no output>"
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"claude -p failed after {MAX_ATTEMPTS} attempts "
                    f"(exit {e.returncode}): {detail}"
                ) from e
            delay = BACKOFF_SECONDS * attempt
            print(
                f"    claude -p failed (exit {e.returncode}): {detail}\n"
                f"    retrying in {delay}s ({attempt}/{MAX_ATTEMPTS - 1})",
                file=sys.stderr,
            )
            time.sleep(delay)
        else:
            return result.stdout
    raise AssertionError("unreachable")  # loop either returns or raises


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _filename_for(snapshot: dict[str, Any]) -> str:
    """Derive `<issue_number>_<dataset_title>.md` from a snapshot dict.

    The title is sanitized for filesystem safety: whitespace and any character outside
    `[A-Za-z0-9._-]` collapses to a single `-`.
    """
    issue = snapshot["issue"]
    number = issue["number"]
    title = (issue.get("title") or "untitled").strip()
    safe_title = _UNSAFE_FILENAME_CHARS.sub("-", title).strip("-") or "untitled"
    return f"{number}_{safe_title}.md"


# --------------------------------------------------------------------------- #
# Skip datasets unchanged since the last summarize run
# --------------------------------------------------------------------------- #

STATE_FILENAME = ".summarize_state.json"


def _fingerprint(snapshot: dict[str, Any]) -> str:
    """Hash the substantive parts of a snapshot (everything but the pull timestamps,
    which change on every fetch regardless of whether anything actually happened)."""
    substantive = {
        k: v for k, v in snapshot.items() if k not in ("last_pull_at", "previous_last_pull_at")
    }
    payload = json.dumps(substantive, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_state(data_dir: Path) -> dict[str, str]:
    path = data_dir / STATE_FILENAME
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def changed_since_last_summary(snapshot_paths: list[Path], data_dir: Path) -> list[Path]:
    """Return the subset of snapshot_paths whose content changed since they were last
    summarized (or that have never been summarized before)."""
    state = _load_state(data_dir)
    changed = []
    for path in snapshot_paths:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if state.get(path.stem) != _fingerprint(snapshot):
            changed.append(path)
    return changed


def _write_state(state: dict[str, str], data_dir: Path) -> None:
    path = data_dir / STATE_FILENAME
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def record_summarized(snapshot_paths: list[Path], data_dir: Path) -> None:
    """Record the current fingerprint of each snapshot as "summarized" so a future run
    can skip it if nothing changes."""
    state = _load_state(data_dir)
    for path in snapshot_paths:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        state[path.stem] = _fingerprint(snapshot)
    _write_state(state, data_dir)


# --------------------------------------------------------------------------- #
# Pending-run marker: lets an interrupted run resume into the same output dir
# --------------------------------------------------------------------------- #

PENDING_RUN_FILENAME = ".summarize_run.json"

#: Bookkeeping files that live in data_dir alongside the snapshots.
INTERNAL_FILES = frozenset({STATE_FILENAME, PENDING_RUN_FILENAME})


def load_pending_run(data_dir: Path) -> tuple[Path, list[Path]] | None:
    """Return (run_dir, cycle_snapshots) of an unfinished run, or None if the last
    run completed. Snapshots that have since disappeared are dropped."""
    path = data_dir / PENDING_RUN_FILENAME
    if not path.exists():
        return None
    pending = json.loads(path.read_text(encoding="utf-8"))
    cycle = [data_dir / name for name in pending["cycle"]]
    return Path(pending["run_dir"]), [p for p in cycle if p.exists()]


def save_pending_run(data_dir: Path, run_dir: Path, cycle: list[Path]) -> None:
    """Mark a run as in-progress, remembering every snapshot in its reporting cycle."""
    payload = {"run_dir": str(run_dir), "cycle": sorted(p.name for p in cycle)}
    (data_dir / PENDING_RUN_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def clear_pending_run(data_dir: Path) -> None:
    """Mark the current cycle finished; the next run starts a fresh output dir."""
    (data_dir / PENDING_RUN_FILENAME).unlink(missing_ok=True)


def output_path_for(snapshot_path: Path, out_dir: Path) -> Path:
    """Where `summarize_dataset` writes this snapshot's summary. Its existence is the
    done-marker used to decide what a resumed run still has to do."""
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return out_dir / _filename_for(snapshot)


def summarize_dataset(snapshot_path: Path, out_dir: Path) -> Path:
    """Produce the per-dataset cumulative summary for one snapshot JSON file."""
    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    md = _run_claude(CUMULATIVE_PROMPT, snapshot_text)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _filename_for(json.loads(snapshot_text))
    out_path.write_text(md, encoding="utf-8")
    return out_path


def summarize_biweekly(snapshot_paths: list[Path], out_path: Path) -> Path:
    """Produce the single aggregated biweekly status report."""
    bundle: list[dict[str, Any]] = [
        json.loads(p.read_text(encoding="utf-8")) for p in snapshot_paths
    ]
    payload = json.dumps(bundle, indent=2, ensure_ascii=False)
    md = _run_claude(BIWEEKLY_PROMPT, payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path
