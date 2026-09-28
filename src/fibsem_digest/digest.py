"""Turn a fetched board into a short narrative digest via `claude -p`.

Everything that can be computed from the board (column, timeline, owner, activity) is
left to `render`; Claude only supplies what needs judgement: what needs attention,
per-dataset progress and blockers, and a post-mortem for datasets that just reached
Done. The result is validated JSON (see `Digest`).
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

PROMPT = """\
You are preparing the narrative part of a status digest for an internal biweekly meeting \
about FIB-SEM dataset reconstruction. On stdin you get a JSON array, one element per \
dataset, with: number, title, status (board column: Imaging / Assembly / Review / \
Advanced Processing / Done), labels, assignees, description, window_start, and comments \
(author, date, text, in_window). The reporting window is everything with in_window=true \
(everything after window_start). Older comments are context only and may be truncated.

Return ONLY a JSON object, no prose, no code fence, with exactly these keys:

{
  "attention": [{"number": <int>, "text": "<one sentence: what is stuck and who/what it waits on>"}],
  "datasets": {"<number>": {"progress": ["<bullet>", ...], "blockers": ["<bullet>", ...]}},
  "postmortem": {"<number>": {"well": ["<bullet>", ...], "bad": ["<bullet>", ...], "actions": ["<bullet>", ...]}}
}

Rules:
- `datasets` must have an entry for EVERY dataset in the input. `progress`: 1-4 short \
factual bullets of what advanced in the window (empty list if nothing happened). \
`blockers`: unresolved questions or things waiting on someone (usually empty).
- `attention` is a short list of things that genuinely need action before the next \
meeting: blocked, stuck, or waiting on a specific person. Assembly <-> Review bouncing is \
normal QC iteration; only flag it after three or more round-trips in the window without \
visible progress, or when a question in Review has sat unanswered for over a week. \
Advanced Processing is exploratory R&D: flag it only if it explicitly waits on a decision \
or a person, never for being quiet. Empty list if nothing is blocking.
- `postmortem` has an entry for each dataset whose status is Done, and no others. Look at \
the whole thread: `well` = what was smooth or well handed off, `bad` = friction, rework, \
unclear ownership or delays (describe the step, not the person), `actions` = concrete, \
low-cost improvements for future datasets grounded in this thread. 1-4 bullets each.
- Bullets are plain text; inline markdown (**bold**, `code`, [links](url)) is fine. Cite \
people only when it matters who did it (e.g. "QC signed off by <login>").
"""


class Attention(BaseModel):
    number: int
    text: str


class Update(BaseModel):
    progress: list[str] = []
    blockers: list[str] = []


class PostMortem(BaseModel):
    well: list[str] = []
    bad: list[str] = []
    actions: list[str] = []


class Digest(BaseModel):
    attention: list[Attention] = []
    datasets: dict[int, Update] = Field(default_factory=dict)
    postmortem: dict[int, PostMortem] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


_JUNK = [
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"```.*?```", re.S), "[code]"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), "[image]"),
    (re.compile(r"<img[^>]*>"), "[image]"),
    (re.compile(r"<[^>]+>"), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # links: keep the text, drop the URL
    (re.compile(r"^On .{0,120} wrote:\s*$.*", re.S | re.M), ""),  # quoted e-mail reply
    (re.compile(r"^>.*$", re.M), ""),
    (re.compile(r"[ \t]+$", re.M), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
]


def clean_text(text: str) -> str:
    for pat, repl in _JUNK:
        text = pat.sub(repl, text)
    return text.strip()


def clean(snap: dict[str, Any], start: datetime, older_chars: int = 400) -> dict[str, Any]:
    """The subset of a dataset worth sending to Claude."""
    issue = snap["issue"]
    done = snap["status"] == "Done"
    comments = []
    for c in snap["comments"]:
        text = clean_text(c["body"] or "")
        in_window = dt(c["createdAt"]) > start
        if not in_window and not done and len(text) > older_chars:
            text = text[:older_chars] + " …"
        comments.append(
            {
                "author": (c.get("author") or {}).get("login"),
                "date": c["createdAt"][:10],
                "in_window": in_window,
                "text": text,
            }
        )
    return {
        "number": issue["number"],
        "title": issue["title"],
        "status": snap["status"],
        "labels": issue["labels"],
        "assignees": issue["assignees"],
        "description": clean_text(issue["body"] or ""),
        "window_start": start.isoformat(),
        "comments": comments,
    }


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 10


def run_claude(prompt: str, stdin_payload: str) -> str:
    """Invoke `claude -p <prompt>` with the payload on stdin. Return stdout.

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


def parse_digest(text: str) -> Digest:
    """Validate Claude's reply, tolerating prose or a code fence around the JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"claude -p returned no JSON object: {text[:300]!r}")
    return Digest.model_validate_json(text[start : end + 1])


def make_digest(board: dict[str, Any]) -> Digest:
    start = dt(board["since"])
    payload = json.dumps([clean(s, start) for s in board["datasets"]], indent=1, ensure_ascii=False)
    return parse_digest(run_claude(PROMPT, payload))
