#!/usr/bin/env python3
"""Delete stale personal forks that do not head an open pull request."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com/graphql"
REST = "https://api.github.com"
STALE_AFTER = timedelta(days=90)
PAGE_CAP = 50
PROTECTED = frozenset({"official-burak/official-burak"})
USER_AGENT = "official-burak-fork-cleanup"

FORKS_QUERY = """
query ($cursor: String) {
  viewer {
    login
    repositories(
      first: 100
      after: $cursor
      isFork: true
      ownerAffiliations: OWNER
      orderBy: {field: NAME, direction: ASC}
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isFork
        parent {
          nameWithOwner
        }
      }
    }
  }
}
"""

SEARCH_PRS_QUERY = """
query ($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        url
        state
        createdAt
        updatedAt
        headRepository {
          nameWithOwner
        }
      }
    }
  }
}
"""

OPEN_REFS_QUERY = """
query ($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        associatedPullRequests(states: OPEN, first: 10) {
          nodes {
            url
            headRepository {
              nameWithOwner
            }
          }
        }
      }
    }
  }
}
"""


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        method="POST",
        headers=headers(token),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        fail(f"GitHub GraphQL HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"GitHub GraphQL request failed: {exc.reason}")

    errors = payload.get("errors")
    if errors:
        fail(f"GitHub GraphQL errors: {json.dumps(errors)}")
    data = payload.get("data")
    if not isinstance(data, dict):
        fail("GitHub GraphQL returned no data")
    return data


def rest_delete(token: str, repo: str) -> None:
    request = urllib.request.Request(
        f"{REST}/repos/{repo}",
        method="DELETE",
        headers=headers(token),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        fail(f"Delete {repo} failed HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"Delete {repo} failed: {exc.reason}")
    if status not in {204, 200}:
        fail(f"Delete {repo} returned HTTP {status}")


def require_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"Missing string field: {label}")
    return value


def require_dict(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        fail(f"Missing object: {label}")
    return value


def page_info(container: object, label: str) -> tuple[bool, str | None]:
    info = require_dict(require_dict(container, label).get("pageInfo"), f"{label} pageInfo")
    has_next = info.get("hasNextPage")
    if not isinstance(has_next, bool):
        fail(f"Missing hasNextPage: {label}")
    cursor = info.get("endCursor")
    if has_next:
        return True, require_str(cursor, f"{label} endCursor")
    return False, cursor if isinstance(cursor, str) else None


def parse_utc(value: object, label: str) -> datetime:
    text = require_str(value, label)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        fail(f"Invalid timestamp: {label}")
    if parsed.tzinfo is None:
        fail(f"Timestamp missing timezone: {label}")
    return parsed.astimezone(timezone.utc)


def dry_run_enabled() -> bool:
    raw = os.environ.get("DRY_RUN", "true").strip().lower()
    return raw not in {"0", "false", "no"}


def is_protected(name: str, login: str) -> bool:
    lowered = name.lower()
    if lowered in {item.lower() for item in PROTECTED}:
        return True
    return lowered == f"{login.lower()}/{login.lower()}"


def list_forks(token: str) -> tuple[str, list[dict]]:
    login = ""
    forks: list[dict] = []
    cursor = None
    for page in range(1, PAGE_CAP + 1):
        data = graphql(token, FORKS_QUERY, {"cursor": cursor})
        viewer = require_dict(data.get("viewer"), "viewer")
        login = require_str(viewer.get("login"), "viewer.login")
        repos = require_dict(viewer.get("repositories"), "viewer.repositories")
        nodes = repos.get("nodes")
        if not isinstance(nodes, list):
            fail("viewer.repositories.nodes missing")
        for node in nodes:
            forks.append(require_dict(node, "fork"))
        has_next, cursor = page_info(repos, "viewer.repositories")
        if not has_next:
            return login, forks
    fail(f"Fork listing exceeded {PAGE_CAP} pages")


def search_prs(token: str, query: str):
    cursor = None
    for page in range(1, PAGE_CAP + 1):
        data = graphql(token, SEARCH_PRS_QUERY, {"q": query, "cursor": cursor})
        search = require_dict(data.get("search"), "search")
        nodes = search.get("nodes")
        if not isinstance(nodes, list):
            fail(f"search.nodes missing for: {query}")
        for node in nodes:
            if isinstance(node, dict) and node:
                yield node
        has_next, cursor = page_info(search, f"search {query}")
        if not has_next:
            return
    fail(f"Search pagination exceeded {PAGE_CAP} pages: {query}")


def head_name(pr: dict) -> str | None:
    head = pr.get("headRepository")
    if head is None:
        return None
    if not isinstance(head, dict):
        fail("headRepository is not an object")
    name = head.get("nameWithOwner")
    if name is None:
        return None
    return require_str(name, "headRepository.nameWithOwner")


def open_prs_by_head(token: str, login: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for pr in search_prs(token, f"is:pr is:open author:{login}"):
        repo = head_name(pr)
        if repo and repo not in found:
            found[repo] = require_str(pr.get("url"), "open PR url")
    return found


def open_pr_from_fork_refs(token: str, repo: str) -> str | None:
    owner, name = repo.split("/", 1)
    cursor = None
    for _page in range(1, PAGE_CAP + 1):
        data = graphql(
            token,
            OPEN_REFS_QUERY,
            {"owner": owner, "name": name, "cursor": cursor},
        )
        repository = data.get("repository")
        if not isinstance(repository, dict):
            fail(f"Repository not found: {repo}")
        refs = require_dict(repository.get("refs"), f"{repo} refs")
        nodes = refs.get("nodes")
        if not isinstance(nodes, list):
            fail(f"{repo} refs.nodes missing")
        for ref in nodes:
            node = require_dict(ref, f"{repo} ref")
            prs = require_dict(
                node.get("associatedPullRequests"),
                f"{repo} associatedPullRequests",
            )
            pr_nodes = prs.get("nodes")
            if not isinstance(pr_nodes, list):
                fail(f"{repo} associatedPullRequests.nodes missing")
            for pr in pr_nodes:
                item = require_dict(pr, f"{repo} associated PR")
                if head_name(item) == repo:
                    return require_str(item.get("url"), f"{repo} associated PR url")
        has_next, cursor = page_info(refs, f"{repo} refs")
        if not has_next:
            return None
    fail(f"Ref pagination exceeded {PAGE_CAP} pages: {repo}")


def open_pr_on_parent(token: str, login: str, fork: str, parent: str) -> str | None:
    query = f"is:pr is:open repo:{parent} author:{login}"
    for pr in search_prs(token, query):
        if head_name(pr) == fork:
            return require_str(pr.get("url"), f"{fork} parent open PR url")
    return None


def latest_pr_activity(
    token: str, login: str, fork: str, parent: str
) -> tuple[datetime, str] | None:
    query = f"is:pr repo:{parent} author:{login} sort:updated-desc"
    latest: tuple[datetime, str] | None = None
    for pr in search_prs(token, query):
        if head_name(pr) != fork:
            continue
        created = parse_utc(pr.get("createdAt"), f"{fork} createdAt")
        updated = parse_utc(pr.get("updatedAt"), f"{fork} updatedAt")
        activity = updated if updated >= created else created
        url = require_str(pr.get("url"), f"{fork} PR url")
        if latest is None or activity > latest[0]:
            latest = (activity, url)
        # Results are newest-first; the first matching PR is enough.
        return latest
    return latest


def decide(
    token: str,
    login: str,
    fork: dict,
    open_heads: dict[str, str],
    cutoff: datetime,
) -> tuple[str, str]:
    name = require_str(fork.get("nameWithOwner"), "nameWithOwner")
    if is_protected(name, login):
        return "keep", "protected profile repository"
    if fork.get("isFork") is not True:
        return "keep", "not a fork"
    owner = name.split("/", 1)[0]
    if owner.lower() != login.lower():
        return "keep", "not owned by the authenticated user"

    open_url = open_heads.get(name)
    if open_url:
        return "keep", f"heads open pull request {open_url}"

    parent = fork.get("parent")
    if not isinstance(parent, dict):
        ref_url = open_pr_from_fork_refs(token, name)
        if ref_url:
            return "keep", f"heads open pull request {ref_url}"
        return "keep", "parent repository is missing; not deleting"
    parent_name = require_str(parent.get("nameWithOwner"), f"{name} parent")

    activity = latest_pr_activity(token, login, name, parent_name)
    if activity is not None:
        when, url = activity
        days = int((cutoff + STALE_AFTER - when).total_seconds() // 86400)
        if when >= cutoff:
            return "keep", f"pull request activity {days} days ago ({url})"

    parent_open = open_pr_on_parent(token, login, name, parent_name)
    if parent_open:
        return "keep", f"heads open pull request {parent_open}"
    ref_url = open_pr_from_fork_refs(token, name)
    if ref_url:
        return "keep", f"heads open pull request {ref_url}"
    if activity is None:
        return "delete", "no pull requests from this fork"
    when, url = activity
    days = int((cutoff + STALE_AFTER - when).total_seconds() // 86400)
    return "delete", f"last pull request activity {days} days ago ({url})"


def main() -> None:
    token = os.environ.get("FORK_CLEANUP_TOKEN", "").strip()
    if not token:
        fail("FORK_CLEANUP_TOKEN is required")

    dry_run = dry_run_enabled()
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_AFTER
    login, forks = list_forks(token)
    open_heads = open_prs_by_head(token, login)

    kept = 0
    deleted = 0
    would_delete = 0

    print(f"Authenticated as {login}")
    print(f"Forks listed: {len(forks)}")
    print(f"Open pull requests headed by those forks: {len(open_heads)}")
    print(f"Stale after: {STALE_AFTER.days} days")
    print(f"Mode: {'dry-run' if dry_run else 'delete'}")

    for fork in forks:
        name = require_str(fork.get("nameWithOwner"), "nameWithOwner")
        action, reason = decide(token, login, fork, open_heads, cutoff)
        if action == "keep":
            kept += 1
            print(f"KEEP   {name}  {reason}")
            continue
        if dry_run:
            would_delete += 1
            print(f"DRY    {name}  would delete: {reason}")
            continue
        rest_delete(token, name)
        deleted += 1
        print(f"DELETE {name}  {reason}")

    print(
        f"Summary: kept={kept} deleted={deleted} would_delete={would_delete} dry_run={dry_run}"
    )


if __name__ == "__main__":
    main()
