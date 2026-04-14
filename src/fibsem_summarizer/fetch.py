"""Fetch GitHub issues from a Projects v2 board into per-issue JSON snapshots.

Incremental: on repeated runs we diff against the previous snapshot so that:
- new comments are appended,
- edited comments are replaced in place (tracked in an `edits` log),
- issue body revisions are recorded in `body_history`,
- status (column) transitions are recorded in `status_history`.

All GitHub calls go through the GraphQL v4 API because Projects v2 has no REST endpoint.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

GITHUB_GRAPHQL = "https://api.github.com/graphql"

# Columns we care about for reporting. Items in other columns are skipped unless
# they transitioned recently (see CollectedItem.is_recent_done).
ACTIVE_STATUSES: set[str] = {"Imaging", "Assembly", "Review"}


class GitHubError(RuntimeError):
    """Raised for non-recoverable GitHub API errors."""


# --------------------------------------------------------------------------- #
# GraphQL queries
# --------------------------------------------------------------------------- #

_PROJECT_ITEMS_QUERY = """
query ($org: String!, $project: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $project) {
      id
      title
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          updatedAt
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              id
              number
              title
              url
              state
              createdAt
              updatedAt
              author { login }
              repository { nameWithOwner }
              body
              labels(first: 20) { nodes { name } }
              assignees(first: 10) { nodes { login } }
            }
          }
        }
      }
    }
  }
}
"""

_ISSUE_COMMENTS_QUERY = """
query ($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          author { login }
          createdAt
          updatedAt
          body
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- #
# GraphQL client
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GitHubClient:
    token: str
    timeout: float = 30.0

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                GITHUB_GRAPHQL,
                headers=headers,
                json={"query": query, "variables": variables},
            )
        if resp.status_code != 200:
            raise GitHubError(f"GitHub returned {resp.status_code}: {resp.text[:400]}")
        payload = resp.json()
        if "errors" in payload:
            raise GitHubError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]

    def project_items(self, org: str, project_number: int) -> Iterable[dict[str, Any]]:
        """Yield every item on the project board (paginated)."""
        after: str | None = None
        while True:
            data = self._post(
                _PROJECT_ITEMS_QUERY,
                {"org": org, "project": project_number, "after": after},
            )
            proj = data["organization"]["projectV2"]
            if proj is None:
                raise GitHubError(
                    f"Project {org}/projects/{project_number} not found "
                    "(check token scopes: read:project, read:org)."
                )
            items = proj["items"]
            for node in items["nodes"]:
                yield node
            if not items["pageInfo"]["hasNextPage"]:
                return
            after = items["pageInfo"]["endCursor"]

    def issue_comments(
        self, owner: str, name: str, number: int
    ) -> list[dict[str, Any]]:
        """Return all comments for an issue, oldest first."""
        comments: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self._post(
                _ISSUE_COMMENTS_QUERY,
                {"owner": owner, "name": name, "number": number, "after": after},
            )
            conn = data["repository"]["issue"]["comments"]
            comments.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                return comments
            after = conn["pageInfo"]["endCursor"]


# --------------------------------------------------------------------------- #
# Snapshot merge logic
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _status_from_item(item: dict[str, Any]) -> str | None:
    for fv in item.get("fieldValues", {}).get("nodes", []):
        if fv and fv.get("field", {}).get("name") == "Status":
            return fv.get("name")
    return None


def _slug(issue: dict[str, Any]) -> str:
    """File-safe identifier for snapshot filenames: <org>__<repo>__<num>.json."""
    repo = issue["repository"]["nameWithOwner"]  # e.g. Janelia/foo
    return f"{repo.replace('/', '__')}__{issue['number']}"


def merge_snapshot(
    existing: dict[str, Any] | None,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    status: str | None,
) -> dict[str, Any]:
    """Merge newly fetched issue data into the previous snapshot.

    Returns the new snapshot dict. `existing` may be None on a first pull.
    """
    detected = _now()
    previous_last_pull = existing.get("last_pull_at") if existing else None

    # Comments: keyed by node ID. Preserve previous list order when possible,
    # append any new IDs at the end (they should already be chronological).
    prev_comments: list[dict[str, Any]] = (
        list(existing["comments"]) if existing else []
    )
    prev_by_id = {c["id"]: c for c in prev_comments}
    new_by_id = {c["id"]: c for c in comments}

    edits: list[dict[str, Any]] = list(existing.get("edits", [])) if existing else []
    merged_comments: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Walk the fresh list so we capture the current order from GitHub.
    for c in comments:
        seen.add(c["id"])
        prev = prev_by_id.get(c["id"])
        if prev is not None and prev.get("body") != c.get("body"):
            edits.append(
                {
                    "comment_id": c["id"],
                    "old_body": prev["body"],
                    "new_body": c["body"],
                    "detected_at": detected,
                }
            )
        merged_comments.append(c)

    # Any previous comments that vanished (deleted on GitHub) are dropped from
    # `comments` but recorded for traceability.
    deleted = [c for c in prev_comments if c["id"] not in new_by_id]
    deletions: list[dict[str, Any]] = (
        list(existing.get("deletions", [])) if existing else []
    )
    for c in deleted:
        deletions.append({"comment": c, "detected_at": detected})

    # Issue body history.
    body_history: list[dict[str, Any]] = (
        list(existing.get("body_history", [])) if existing else []
    )
    prev_body = existing.get("issue", {}).get("body") if existing else None
    if prev_body is None:
        # First sighting — seed history with current body.
        body_history.append({"body": issue["body"], "recorded_at": detected})
    elif _hash(prev_body) != _hash(issue["body"] or ""):
        body_history.append({"body": issue["body"], "recorded_at": detected})

    # Status history.
    status_history: list[dict[str, Any]] = (
        list(existing.get("status_history", [])) if existing else []
    )
    prev_status = status_history[-1]["to"] if status_history else None
    if status != prev_status:
        status_history.append(
            {"from": prev_status, "to": status, "detected_at": detected}
        )

    return {
        "schema_version": 1,
        "last_pull_at": detected,
        "previous_last_pull_at": previous_last_pull,
        "issue": {
            "id": issue["id"],
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["url"],
            "state": issue["state"],
            "author": (issue.get("author") or {}).get("login"),
            "created_at": issue["createdAt"],
            "updated_at": issue["updatedAt"],
            "repository": issue["repository"]["nameWithOwner"],
            "labels": [n["name"] for n in issue["labels"]["nodes"]],
            "assignees": [n["login"] for n in issue["assignees"]["nodes"]],
            "body": issue["body"],
        },
        "current_status": status,
        "status_history": status_history,
        "body_history": body_history,
        "comments": merged_comments,
        "edits": edits,
        "deletions": deletions,
    }


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def fetch_all(
    data_dir: Path,
    org: str,
    project_number: int,
    token: str,
    include_statuses: set[str] | None = None,
) -> list[Path]:
    """Fetch every active dataset's issue and save/merge a snapshot.

    Returns the list of snapshot paths that were written.
    """
    include = include_statuses if include_statuses is not None else ACTIVE_STATUSES
    client = GitHubClient(token=token)
    written: list[Path] = []

    for item in client.project_items(org, project_number):
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue  # drafts / PRs are skipped
        status = _status_from_item(item)

        # Include issues in active columns, plus issues whose most recent
        # transition carried them into (or out of) Done since our last pull —
        # those matter for the biweekly report. We recognize "out of Done" by
        # status_history in the existing snapshot.
        snapshot_path = data_dir / f"{_slug(content)}.json"
        existing: dict[str, Any] | None = None
        if snapshot_path.exists():
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))

        prev_status = (
            existing.get("current_status") if existing else None
        )
        is_active = status in include
        is_recent_done_transition = (
            status == "Done" and prev_status in include
        ) or (
            prev_status == "Done" and status in include
        )

        if not (is_active or is_recent_done_transition):
            continue

        owner, name = content["repository"]["nameWithOwner"].split("/", 1)
        comments = client.issue_comments(owner, name, content["number"])
        snapshot = merge_snapshot(existing, content, comments, status)
        _write_atomic(snapshot_path, snapshot)
        written.append(snapshot_path)

    return written
