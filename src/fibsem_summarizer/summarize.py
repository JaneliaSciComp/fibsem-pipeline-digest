"""Run `claude -p` over JSON snapshots to produce markdown summaries.

Two outputs:
- per-dataset `out/cumulative/<slug>.md`: a cumulative methods + retrospective write-up
  suitable for drafting methods sections and internal process review.
- aggregated `out/biweekly.md`: a single status report for the internal meeting covering
  activity since the previous pull across all active datasets.

This module is intentionally thin: it shells out to `claude -p` and writes files. The
prompts live as module-level string constants below so they're easy to tweak in one place.
"""
import json
import subprocess
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
- `current_status` (which column the dataset is in: Imaging / Assembly / Review / Done),
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

Output only the Markdown. No JSON, no commentary outside the sections.
"""


# --------------------------------------------------------------------------- #
# Subprocess wrapper
# --------------------------------------------------------------------------- #


def _run_claude(prompt: str, stdin_payload: str) -> str:
    """Invoke `claude -p <prompt>` with the JSON payload on stdin. Return stdout."""
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
        raise RuntimeError(
            f"claude -p failed (exit {e.returncode}): {e.stderr[:500]}"
        ) from e
    return result.stdout


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def summarize_dataset(snapshot_path: Path, out_dir: Path) -> Path:
    """Produce the per-dataset cumulative summary for one snapshot JSON file."""
    snapshot = snapshot_path.read_text(encoding="utf-8")
    md = _run_claude(CUMULATIVE_PROMPT, snapshot)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (snapshot_path.stem + ".md")
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
