"""Fetch the datasets on a Projects v2 board, with full comment and column history.

Stateless: every run pulls everything GitHub knows about the relevant issues. Column
moves come from the issues' own timelines (`ProjectV2ItemStatusChangedEvent`), so no
local bookkeeping between runs is needed.

All GitHub calls go through the GraphQL v4 API because Projects v2 has no REST endpoint.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

GITHUB_GRAPHQL = "https://api.github.com/graphql"

# Columns that are always reported. Done items are included only when they got there
# inside the reporting window; Cleaned Up is the archive and never fetched.
ACTIVE_STATUSES: set[str] = {"Imaging", "Assembly", "Review", "Advanced Processing"}


class GitHubError(RuntimeError):
    """Raised for non-recoverable GitHub API errors."""


# --------------------------------------------------------------------------- #
# GraphQL queries
# --------------------------------------------------------------------------- #

_PROJECT_ITEMS_QUERY = """
query ($org: String!, $project: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $project) {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                updatedAt
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              number
              title
              url
              state
              createdAt
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

# Comments are paginated; column moves are few, so 100 is plenty.
# ponytail: no timeline pagination, add if a dataset ever bounces >100 times.
_ISSUE_HISTORY_QUERY = """
query ($owner: String!, $name: String!, $number: Int!, $after: String, $events: Boolean!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } createdAt body }
      }
      timelineItems(first: 100, itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]) @include(if: $events) {
        nodes {
          ... on ProjectV2ItemStatusChangedEvent {
            createdAt
            previousStatus
            status
            project { number }
          }
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
            yield from items["nodes"]
            if not items["pageInfo"]["hasNextPage"]:
                return
            after = items["pageInfo"]["endCursor"]

    def issue_history(
        self, owner: str, name: str, number: int, project_number: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (comments oldest first, column transitions oldest first) for an issue."""
        comments: list[dict[str, Any]] = []
        transitions: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self._post(
                _ISSUE_HISTORY_QUERY,
                {"owner": owner, "name": name, "number": number, "after": after, "events": after is None},
            )
            issue = data["repository"]["issue"]
            comments.extend(issue["comments"]["nodes"])
            for ev in (issue.get("timelineItems") or {}).get("nodes", []):
                if ev and (ev.get("project") or {}).get("number") == project_number:
                    transitions.append(
                        {"from": ev["previousStatus"] or None, "to": ev["status"], "at": ev["createdAt"]}
                    )
            if not issue["comments"]["pageInfo"]["hasNextPage"]:
                return comments, transitions
            after = issue["comments"]["pageInfo"]["endCursor"]


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def _status_from_item(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """(column name, when it was last changed) from the item's Status field."""
    for fv in item.get("fieldValues", {}).get("nodes", []):
        if fv and (fv.get("field") or {}).get("name") == "Status":
            return fv.get("name"), fv.get("updatedAt")
    return None, None


def fetch_board(org: str, project_number: int, token: str, since: datetime) -> dict[str, Any]:
    """Everything the digest needs about the board, in one JSON-serialisable dict.

    Includes every issue in an active column plus issues moved to Done after `since`.
    """
    client = GitHubClient(token=token)
    datasets: list[dict[str, Any]] = []
    for item in client.project_items(org, project_number):
        issue = item.get("content") or {}
        if issue.get("__typename") != "Issue":
            continue  # drafts / PRs are skipped
        status, changed_at = _status_from_item(item)
        recent_done = status == "Done" and changed_at and datetime.fromisoformat(changed_at) > since
        if status not in ACTIVE_STATUSES and not recent_done:
            continue
        owner, name = issue["repository"]["nameWithOwner"].split("/", 1)
        comments, transitions = client.issue_history(owner, name, issue["number"], project_number)
        datasets.append(
            {
                "issue": {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["url"],
                    "state": issue["state"],
                    "author": (issue.get("author") or {}).get("login"),
                    "created_at": issue["createdAt"],
                    "repository": issue["repository"]["nameWithOwner"],
                    "labels": [n["name"] for n in issue["labels"]["nodes"]],
                    "assignees": [n["login"] for n in issue["assignees"]["nodes"]],
                    "body": issue["body"],
                },
                "status": status,
                # When the Status field last changed. Timeline events lag by minutes, so
                # a very recent move may be missing from `transitions`; this fills the gap.
                "status_changed_at": changed_at,
                "transitions": transitions,
                "comments": comments,
            }
        )
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"),
        "board": f"{org}/projects/{project_number}",
        "datasets": datasets,
    }
