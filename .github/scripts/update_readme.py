#!/usr/bin/env python3
"""Refresh generated profile README blocks from GitHub.

The hand-authored intro above the generated markers is never rewritten.
Daily runs only replace the marked recent-work block.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README_PATH = ROOT / "README.md"
API = "https://api.github.com/graphql"
GENERATED_BLOCKS = ("recent-work",)
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")
OPEN_SOURCE_PAGE_CAP = 40
OPEN_SOURCE_LIMIT = 50
MIN_UPSTREAM_STARS = 100
CONVENTIONAL_PREFIX = re.compile(
    r"^[A-Za-z][A-Za-z0-9+-]*(?:\([^)]+\))?\s*:\s*"
)
TRAILING_PR_REF = re.compile(r"\s*\(#\d+\)\s*$")

# Spam / noise repos that should never appear in the recent-work table.
SKIP_UPSTREAM_REPOS = frozenset()

OPEN_SOURCE_PRS_QUERY = """
query ($prsQuery: String!, $cursor: String) {
  search(query: $prsQuery, type: ISSUE, first: 25, after: $cursor) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        title
        url
        mergedAt
        repository {
          nameWithOwner
          url
          stargazerCount
          isFork
          owner {
            login
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


def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "official-burak-readme",
        },
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
    if not data:
        fail("GitHub GraphQL returned no data")
    return data


def require_int(value: object, label: str) -> int:
    if not isinstance(value, int):
        fail(f"Missing numeric field: {label}")
    return value


def require_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"Missing string field: {label}")
    return value


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


def page_info(container: object, label: str) -> tuple[bool, str | None]:
    if not isinstance(container, dict):
        fail(f"Missing pageInfo parent: {label}")
    info = container.get("pageInfo")
    if not isinstance(info, dict):
        fail(f"Missing pageInfo: {label}")
    has_next = info.get("hasNextPage")
    if not isinstance(has_next, bool):
        fail(f"Missing hasNextPage: {label}")
    cursor = info.get("endCursor")
    if has_next:
        return True, require_str(cursor, f"{label} endCursor")
    return False, cursor if isinstance(cursor, str) else None


def shipped_line(title: str) -> str:
    text = clean_text(title).strip()
    text = TRAILING_PR_REF.sub("", text).strip()
    stripped = CONVENTIONAL_PREFIX.sub("", text).strip()
    if stripped:
        text = stripped
    if not text:
        fail("Merged PR title was empty after cleanup")
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def open_source_search_query(login: str, merged_since: str | None) -> str:
    query = f"author:{login} is:pr is:merged is:public -user:{login}"
    if merged_since:
        query += f" merged:>={merged_since}"
    return query


def parse_open_source_node(node: object, login: str) -> dict | None:
    if node is None:
        fail("Open source PR search returned a null node")
    if not isinstance(node, dict):
        fail("Open source PR search returned a non-object node")
    if "mergedAt" not in node:
        fail("Open source PR search returned a non-pull-request node")
    url = require_str(node.get("url"), "open source PR url")
    title = require_str(node.get("title"), f"{url} title")
    merged_at = parse_utc(node.get("mergedAt"), f"{url} mergedAt")
    repo = node.get("repository")
    if not isinstance(repo, dict):
        fail(f"Open source PR repository missing on {url}")
    name = require_str(repo.get("nameWithOwner"), f"{url} repository")
    repo_url = require_str(repo.get("url"), f"{url} repository url")
    stars = require_int(repo.get("stargazerCount"), f"{name} stargazerCount")
    owner = repo.get("owner")
    if not isinstance(owner, dict):
        fail(f"Open source PR owner missing on {url}")
    owner_login = require_str(owner.get("login"), f"{url} owner")
    if owner_login.lower() == login.lower():
        return None
    if name.lower() == f"{login}/official-burak".lower():
        return None
    if name.lower() in {item.lower() for item in SKIP_UPSTREAM_REPOS}:
        return None
    if repo.get("isFork") is True:
        return None
    if stars < MIN_UPSTREAM_STARS:
        return None
    project = name.split("/")[-1]
    if not project:
        fail(f"Open source PR repository name missing on {url}")
    return {
        "project": project,
        "name_with_owner": name,
        "repo_url": repo_url,
        "url": url,
        "title": title,
        "impact": shipped_line(title),
        "merged_at": merged_at,
        "stars": stars,
    }


def fetch_merged_upstream_prs(
    token: str, login: str, merged_since: str | None
) -> list[dict]:
    cursor = None
    pages = 0
    expected: int | None = None
    seen = 0
    items: list[dict] = []
    query = open_source_search_query(login, merged_since)
    label = "open source PR search" if merged_since else "open source PR backfill"

    while True:
        pages += 1
        if pages > OPEN_SOURCE_PAGE_CAP:
            fail(f"Too many {label} pages; refusing a partial table")
        data = graphql(
            token,
            OPEN_SOURCE_PRS_QUERY,
            {"prsQuery": query, "cursor": cursor},
        )
        search = data.get("search")
        if not isinstance(search, dict):
            fail(f"{label} returned no data")
        count = require_int(search.get("issueCount"), f"{label} count")
        if expected is None:
            expected = count
        elif count != expected:
            fail(f"{label} count changed while paging")
        nodes = search.get("nodes")
        if not isinstance(nodes, list):
            fail(f"{label} nodes missing")
        for node in nodes:
            parsed = parse_open_source_node(node, login)
            seen += 1
            if parsed:
                items.append(parsed)
        has_next, next_cursor = page_info(search, label)
        if not has_next:
            break
        if not next_cursor:
            fail(f"{label} missing endCursor")
        cursor = next_cursor

    if expected is None:
        fail(f"{label} returned no page")
    if seen != expected:
        fail(f"{label} incomplete: got {seen} nodes, expected {expected}")
    return items


def unique_by_repo(items: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for item in items:
        key = item["name_with_owner"].lower()
        current = best.get(key)
        if current is None or item["merged_at"] > current["merged_at"]:
            best[key] = item
    return list(best.values())


def collect_open_source(token: str, login: str, created_since: str) -> list[dict]:
    year_items = unique_by_repo(
        fetch_merged_upstream_prs(token, login, created_since)
    )
    year_items.sort(key=lambda item: item["merged_at"], reverse=True)
    picked = year_items[:OPEN_SOURCE_LIMIT]
    if len(picked) < OPEN_SOURCE_LIMIT:
        seen = {item["name_with_owner"].lower() for item in picked}
        older = unique_by_repo(fetch_merged_upstream_prs(token, login, None))
        older.sort(key=lambda item: item["merged_at"], reverse=True)
        for item in older:
            if item["name_with_owner"].lower() in seen:
                continue
            picked.append(item)
            seen.add(item["name_with_owner"].lower())
            if len(picked) >= OPEN_SOURCE_LIMIT:
                break
    picked.sort(key=lambda item: item["merged_at"], reverse=True)
    return picked[:OPEN_SOURCE_LIMIT]


def clean_text(value: str) -> str:
    return value.replace("\u2014", "-").replace("\u2013", "-")


def h(value: str) -> str:
    return html.escape(clean_text(value), quote=True)


def first_generated_index(readme: str) -> int:
    indexes = []
    for name in GENERATED_BLOCKS:
        marker = f"<!-- {name}:start -->"
        found = readme.find(marker)
        if found != -1:
            indexes.append(found)
    # Also respect legacy markers so hand-authored prefix stays locked
    # while migrate strips obsolete generated blocks.
    for name in ("github-stats", "selected-work"):
        marker = f"<!-- {name}:start -->"
        found = readme.find(marker)
        if found != -1:
            indexes.append(found)
    if not indexes:
        fail("README is missing generated section markers")
    return min(indexes)


def hand_authored_prefix(readme: str) -> str:
    """Everything above the generated markers. Daily runs must not change this."""
    return readme[: first_generated_index(readme)]


def replace_block(readme: str, name: str, body: str) -> str:
    if name not in GENERATED_BLOCKS:
        fail(f"Refusing to replace unmarked block: {name}")
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    if start not in readme or end not in readme:
        fail(f"README is missing {start} / {end} markers")
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    replacement = start + "\n" + body.rstrip() + "\n" + end
    prefix = hand_authored_prefix(readme)
    updated, count = pattern.subn(replacement, readme, count=1)
    if count != 1:
        fail(f"Could not replace {name} block")
    if not updated.startswith(prefix):
        fail("Refusing to rewrite the hand-authored README intro")
    return updated


def recent_work_markdown(items: list[dict]) -> str:
    rows = [
        "## Recent work",
        "",
        "<table>",
        "<thead>",
        "<tr>",
        '<th align="left">Project</th>',
        '<th align="left">What shipped</th>',
        '<th align="left">Status</th>',
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    if not items:
        rows.extend(
            [
                "<tr>",
                "<td colspan=\"3\">No merged public upstream work to list yet.</td>",
                "</tr>",
            ]
        )
    else:
        for item in items:
            rows.extend(
                [
                    "<tr>",
                    f'<td><a href="{h(item["repo_url"])}">{h(item["project"])}</a></td>',
                    f"<td>{h(item['impact'])}</td>",
                    f'<td><a href="{h(item["url"])}">Merged</a></td>',
                    "</tr>",
                ]
            )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def assert_clean(text: str, label: str = "README") -> None:
    if "\u2014" in text or "\u2013" in text:
        fail(f"Generated {label} contains em or en dashes")
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    brand = "cur" + "sor"
    if (
        brand + "agent" in compact
        or "madewith" + brand in compact
        or "madeby" + brand in compact
    ):
        fail(f"Generated {label} contains a forbidden string")
    if re.search(r"^## Focus\b", text, re.MULTILINE):
        fail(f"Generated {label} still contains a Focus heading")
    if "Recent upstreams" in text:
        fail(f"Generated {label} still contains a Recent upstreams section")
    if "recently-updated:start" in text or "recently-updated:end" in text:
        fail(f"Generated {label} still contains recently-updated markers")
    if "github-stats:start" in text or "github-stats:end" in text:
        fail(f"Generated {label} still contains github-stats markers")
    if "Last 12 months:" in text:
        fail(f"Generated {label} still contains Last 12 months stats")


def strip_marked_block(readme: str, name: str) -> str:
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    if start not in readme and end not in readme:
        return readme
    if start not in readme or end not in readme:
        fail(f"README has a partial {name} marker pair")
    pattern = re.compile(
        r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n*",
        re.DOTALL,
    )
    updated, count = pattern.subn("\n\n", readme, count=1)
    if count != 1:
        fail(f"Could not remove {name} block")
    return updated


def migrate_readme(readme: str) -> str:
    readme = readme.replace("<!-- selected-work:start -->", "<!-- recent-work:start -->")
    readme = readme.replace("<!-- selected-work:end -->", "<!-- recent-work:end -->")
    for name in ("focus", "tech-stack", "recently-updated", "github-stats"):
        readme = strip_marked_block(readme, name)
    return readme


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        fail("GITHUB_TOKEN is required")

    login = os.environ.get("GITHUB_REPOSITORY_OWNER") or "official-burak"
    if not LOGIN_RE.fullmatch(login):
        fail("Invalid GitHub login")

    if not README_PATH.is_file():
        fail(f"README not found: {README_PATH}")

    now = datetime.now(timezone.utc)
    window_from_dt = now - timedelta(days=365)
    created_since = window_from_dt.strftime("%Y-%m-%d")

    open_source_items = collect_open_source(token, login, created_since)

    readme = README_PATH.read_text(encoding="utf-8")
    prefix = hand_authored_prefix(readme)
    rest = migrate_readme(readme[len(prefix) :])
    readme = prefix + rest
    readme = replace_block(readme, "recent-work", recent_work_markdown(open_source_items))
    if hand_authored_prefix(readme) != prefix:
        fail("Refusing to rewrite the hand-authored README intro")
    assert_clean(readme)

    README_PATH.write_text(readme, encoding="utf-8")


if __name__ == "__main__":
    main()
