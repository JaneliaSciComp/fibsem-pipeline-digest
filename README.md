# FIBSEM Project Digest

Fetches the FIB-SEM reconstruction tracking board (a private GitHub Projects v2 board) with each dataset's full discussion thread and column history, asks `claude -p` for a short narrative digest, and renders a single self-contained HTML status page for the internal biweekly meeting.

Every run is self-contained: it pulls everything fresh from GitHub and writes one run directory. There is no local state between runs.

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

This runs `fetch` → `digest` → `render`, writing into `out/<YYYY-MM-DD_HH-MM-SS>/` (gitignored):

- `board.json` — everything fetched: issue, comments, column moves with their timestamps.
- `digest.json` — Claude's narrative (attention items, per-dataset progress/blockers, post-mortems).
- `digest.html` — the report. Open it in a browser or attach it; styles and scripts are inlined.

The reporting window starts at the previous run's fetch time (the newest `out/*/board.json`), or 14 days ago if there is none; override with `--since YYYY-MM-DD`. A run covers every dataset in an active column plus datasets moved to **Done** inside the window, which get a post-mortem and then drop out.

Before calling Claude the threads are cleaned: issue-body boilerplate, HTML, images, quoted e-mail replies and code blocks are stripped, link URLs are dropped, and comments before the window are truncated (except for Done datasets, whose whole thread feeds the post-mortem). Claude only writes the bullets; column, timeline, owner, collaborator, assignee, activity and preview links are computed from `board.json`.

The envelope icon next to a dataset title copies pieces of the card to the clipboard for pasting into an e-mail. Click copies the summary as plain text (issue and preview links, progress, blockers, post-mortem), shift-click copies the collapsed card (title, timeline, owner line) as a PNG rendered in the browser, alt-click copies the raw GitHub activity as plain text. Three pastes, since a clipboard holds one item and Outlook drops pasted HTML.

### Stage-by-stage

```sh
uv run -m fibsem_digest fetch [--since 2026-09-16]   # GitHub → out/<ts>/board.json
uv run -m fibsem_digest digest out/<ts>              # claude -p → digest.json + digest.html
uv run -m fibsem_digest render out/<ts>              # re-render digest.html, no Claude call
```

`render` is handy when tweaking the layout; `digest` on an old run re-asks Claude about the same data (useful when tuning the prompt).

### Self-check

```sh
uv run tests/test_digest.py
```

## What gets fetched

The tool walks every item on the board and keeps any issue whose column is one of **Imaging**, **Assembly**, **Review**, or **Advanced Processing** (exploratory R&D outside the normal pipeline — reported like the others, but not flagged as needing attention just for being quiet), plus issues moved to **Done** inside the window. Issues in **Cleaned Up** are ignored.

Column moves come from the issue timeline (`ProjectV2ItemStatusChangedEvent`), which records the column name in use at the time; `render.py` maps retired names (`Alignment`, `R&D`) onto today's. Timeline events can lag a few minutes behind the board, so the Status field's own `updatedAt` fills in the current column when its event is missing.

`board.json` looks roughly like:

```jsonc
{
  "fetched_at": "2026-09-28T20:59:29+00:00",
  "since": "2026-09-16T00:00:00+00:00",
  "board": "JaneliaSciComp/projects/9",
  "datasets": [
    {
      "issue": { "number": 42, "title": "...", "repository": "Janelia/foo", "labels": [], "assignees": [], "body": "..." },
      "status": "Review",
      "status_changed_at": "2026-09-08T10:00:00Z",
      "transitions": [
        { "from": null, "to": "Imaging", "at": "..." },
        { "from": "Imaging", "to": "Assembly", "at": "..." }
      ],
      "comments": [{ "author": { "login": "..." }, "createdAt": "...", "body": "..." }]
    }
  ]
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
