# FIBSEM Project Digest

Fetches discussion threads from the FIB-SEM reconstruction tracking board (a private GitHub Projects v2 board) into local JSON snapshots, asks `claude -p` for a short narrative digest, and renders a single self-contained HTML status page for the internal biweekly meeting.

Pulls are incremental: re-running only fetches what changed, and edits to previously seen comments are tracked in the snapshot.

## Setup

### 1. Install the environment

Requires [`uv`](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```sh
uv sync
```

### 2. Obtain a GitHub Personal Access Token

The project board is private, so you need a token with read access to issues and to Projects v2.

1. Go to <https://github.com/settings/tokens> → **Tokens (classic)** → **Generate new token (classic)**.
2. Name it e.g. `fibsem-digest` and pick an expiration.
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

The digest shells out to the Claude Code CLI. Verify:

```sh
claude --version
```

No separate Anthropic API key is handled by this tool — `claude -p` uses your existing Claude Code authentication.

## Usage

### One-command full pipeline

```sh
uv run -m fibsem_digest
```

This runs `fetch` → `digest` → `render`, writing (all gitignored):

- `data/<repo>__<num>.json` — raw snapshots (one per dataset, updated in place).
- `out/<YYYY-MM-DD_HH-MM-SS>/digest.json` — Claude's narrative for the cycle (attention items, per-dataset progress/blockers, post-mortems).
- `out/<YYYY-MM-DD_HH-MM-SS>/digest.html` — the report. Open it in a browser or attach it; styles and scripts are inlined.

A cycle covers the datasets touched by the most recent fetch, and the reporting window is everything since the fetch before that. A dataset that reaches **Done** is fetched once more (for the cycle it finished in), gets a post-mortem, and then drops out.

Before calling Claude the snapshots are cleaned: issue-body boilerplate, HTML, images, quoted e-mail replies and code blocks are stripped, link URLs are dropped, and comments before the window are truncated (except for Done datasets, whose whole thread feeds the post-mortem). Claude only writes the bullets; column, timeline, owner, collaborator, assignee, activity and preview links are computed from the snapshots.

### Stage-by-stage

```sh
uv run -m fibsem_digest fetch                        # pull/update JSON snapshots only
uv run -m fibsem_digest digest                       # claude -p → out/<ts>/digest.json + digest.html
uv run -m fibsem_digest render out/<ts>              # re-render digest.html from an existing digest.json
```

`render` is handy when tweaking the layout: it reuses the saved narrative and does not call Claude.

### Self-check

```sh
uv run tests/test_digest.py
```

## What gets fetched


The tool walks every item on the board and keeps any issue whose column is one of **Imaging**, **Assembly**, **Review**, or **Advanced Processing** (exploratory R&D outside the normal pipeline — reported like the others, but not flagged as needing attention just for being quiet). It additionally keeps issues that just transitioned into or out of **Done** since the previous pull (so those moves show up in the biweekly report). Issues in **Cleaned Up** are ignored.

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
    { "from": null, "to": "Imaging", "changed_at": "...", "detected_at": "..." },
    { "from": "Imaging", "to": "Assembly", "changed_at": "...", "detected_at": "..." }
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

## Tuning the prompt

The prompt is the `PROMPT` constant at the top of `src/fibsem_digest/digest.py`; the JSON it must return is the `Digest` model right below it. Layout and styling live in `src/fibsem_digest/render.py`.

`changed_at` in `status_history` is when the Status field was changed on the board (from GitHub); `detected_at` is when the fetch noticed. Timelines use `changed_at` and fall back to `detected_at` for older snapshots.
