# FIBSEM Project Summarizer

Fetches discussion threads from the FIB-SEM reconstruction tracking board (a private GitHub Projects v2 board) into local JSON snapshots, then asks `claude -p` to turn those snapshots into Markdown summaries:

- a **per-dataset cumulative** document covering reconstruction steps (methods-ready) and an internal process retrospective, and
- a **single aggregated biweekly status report** for the internal meeting, covering activity since the previous pull.

Pulls are incremental: re-running only fetches what changed, and edits to previously seen comments are tracked in the snapshot.

## Setup

### 1. Install the environment

Requires [`uv`](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```sh
uv sync
```

### 2. Obtain a GitHub Personal Access Token

The project board is private, so you need a token with read access to issues and to Projects v2.

1. Go to <https://github.com/settings/tokens> → **Tokens (classic)** → **Generate new token (classic)**.
2. Name it e.g. `fibsem-summarizer` and pick an expiration.
3. Check these scopes:
   - `repo` (full — needed to read issues & comments in private repos)
   - `read:project` (read Projects v2)
   - `read:org` (org-owned project)
4. Click **Generate token** and **copy the token immediately** (GitHub only shows it once).

### 3. Configure the token

```sh
cp .env.example .env
# then paste your token into .env
```

The `.env` file is gitignored. Never commit the token.

### 4. Make sure `claude` is on your PATH

The summarizer shells out to the Claude Code CLI. Verify:

```sh
claude --version
```

No separate Anthropic API key is handled by this tool — `claude -p` uses your existing Claude Code authentication.

## Usage

### One-command full pipeline

```sh
uv run -m fibsem_summarizer
```

This runs `fetch` and then `summarize`, writing (all gitignored):

- `data/<repo>__<num>.json` — raw snapshots (one per dataset, updated in place).
- `out/<YYYY-MM-DD_HH-MM-SS>/<num>_<title>.md` — per-dataset methods + retrospective.
- `out/<YYYY-MM-DD_HH-MM-SS>/biweekly.md` — single aggregated status report.

Each summarize run creates a fresh timestamped directory under `out/`, so prior runs
are preserved untouched. All Markdown files for a run sit flat in that directory.

### Stage-by-stage

```sh
uv run -m fibsem_summarizer fetch         # pull/update JSON snapshots only
uv run -m fibsem_summarizer summarize     # produce Markdown from existing snapshots
```

### Convert a summary to slides

```sh
uv run -m fibsem_summarizer slides out/2026-04-14_15-30-22/biweekly.md
# writes out/2026-04-14_15-30-22/biweekly.pptx next to the input
```

Slide layout: each `##` heading becomes a new slide; bullets and paragraphs under it populate the body. Open the resulting `.pptx` in PowerPoint or Keynote for any cosmetic tweaks.

## What gets fetched

The tool walks every item on the board and keeps any issue whose column is one of **Imaging**, **Assembly**, or **Review**. It additionally keeps issues that just transitioned into or out of **Done** since the previous pull (so those moves show up in the biweekly report). Issues in **Cleaned Up** are ignored.

## Snapshot shape (for reference)

Each `data/*.json` looks roughly like:

```jsonc
{
  "schema_version": 1,
  "last_pull_at": "2026-04-13T14:20:00+00:00",
  "previous_last_pull_at": "2026-03-30T09:11:00+00:00",
  "issue": { "number": 42, "title": "...", "repository": "Janelia/foo", "..." : "..." },
  "current_status": "Review",
  "status_history": [
    { "from": null, "to": "Imaging", "detected_at": "..." },
    { "from": "Imaging", "to": "Assembly", "detected_at": "..." }
  ],
  "body_history": [{ "body": "...", "recorded_at": "..." }],
  "comments": [{ "id": "...", "author": "...", "body": "...", "createdAt": "...", "updatedAt": "..." }],
  "edits": [{ "comment_id": "...", "old_body": "...", "new_body": "...", "detected_at": "..." }],
  "deletions": []
}
```

## Overriding the board

By default the tool targets `JaneliaSciComp/projects/9`. Override with env vars (e.g., in `.env`):

```
FIBSEM_ORG=SomeOtherOrg
FIBSEM_PROJECT_NUMBER=12
```

## Tuning the prompts

Both prompts are module-level constants near the top of `src/fibsem_summarizer/summarize.py` (`CUMULATIVE_PROMPT` and `BIWEEKLY_PROMPT`). Just edit them.
