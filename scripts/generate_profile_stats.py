#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
README_START = "<!-- profile-stats:start -->"
README_END = "<!-- profile-stats:end -->"
API_BASE = "https://api.github.com"
USER_AGENT = "geopolitis-profile-stats"


def github_request(path: str) -> object:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(f"{API_BASE}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed for {path}: {exc.code} {detail}") from exc


def format_number(value: int | None) -> str:
    return f"{value or 0:,}"


def github_years(created_at: str | None) -> int:
    if not created_at:
        return 0
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return max(0, math.floor((now - created).total_seconds() / (365.25 * 24 * 60 * 60)))


def estimate_lifetime_commits(login: str, repos: list[dict]) -> tuple[int | None, int]:
    owned = [
        repo
        for repo in repos
        if repo.get("owner", {}).get("login") == login and not repo.get("fork")
    ]
    owned.sort(key=lambda repo: repo.get("pushed_at") or "", reverse=True)
    owned = owned[:8]
    if not owned:
        return None, 0

    total = 0
    sampled = 0
    for repo in owned:
        name = urllib.parse.quote(repo["name"])
        contributors = github_request(
            f"/repos/{urllib.parse.quote(login)}/{name}/contributors?per_page=100"
        )
        if not isinstance(contributors, list):
            continue
        me = next((row for row in contributors if row.get("login") == login), None)
        if me is None:
            continue
        sampled += 1
        total += int(me.get("contributions") or 0)

    return (total if sampled > 0 else None), sampled


def estimate_event_window_days(events: list[dict]) -> int:
    if not events:
        return 0
    newest = datetime.fromisoformat(events[0]["created_at"].replace("Z", "+00:00"))
    oldest = datetime.fromisoformat(events[-1]["created_at"].replace("Z", "+00:00"))
    return max(1, round((newest - oldest).total_seconds() / (24 * 60 * 60)))


def external_contribution_repo_count(login: str, events: list[dict]) -> int:
    repos = {
        event.get("repo", {}).get("name")
        for event in events
        if event.get("repo", {}).get("name")
        and not event["repo"]["name"].lower().startswith(f"{login.lower()}/")
    }
    return len(repos)


def register_stack_signals(stack_counts: dict[str, int], root_names: set[str], has_ci: bool) -> None:
    checks = [
        ("TypeScript", "tsconfig.json" in root_names),
        ("Node.js", "package.json" in root_names),
        (
            "Python",
            "requirements.txt" in root_names
            or "pyproject.toml" in root_names
            or "pipfile" in root_names,
        ),
        ("Go", "go.mod" in root_names),
        ("Rust", "cargo.toml" in root_names),
        (
            "Java",
            "pom.xml" in root_names
            or "build.gradle" in root_names
            or "build.gradle.kts" in root_names,
        ),
        ("Docker", "dockerfile" in root_names or "docker-compose.yml" in root_names),
        ("Terraform", any(name.endswith(".tf") for name in root_names)),
        ("Next.js", any(name.startswith("next.config.") for name in root_names)),
        ("Vite", any(name.startswith("vite.config.") for name in root_names)),
        ("GitHub Actions", has_ci),
    ]
    for label, hit in checks:
        if hit:
            stack_counts[label] = stack_counts.get(label, 0) + 1


def build_insight_bundle(login: str, repos: list[dict], events: list[dict], authored_prs: int | None, merged_prs: int | None) -> dict:
    owned = [
        repo
        for repo in repos
        if repo.get("owner", {}).get("login") == login and not repo.get("fork")
    ]
    owned.sort(key=lambda repo: repo.get("pushed_at") or "", reverse=True)
    owned = owned[:8]

    language_bytes: dict[str, int] = {}
    stack_counts: dict[str, int] = {}
    with_ci = 0
    with_tests = 0
    with_readme = 0
    mature_repos = 0

    for repo in owned:
        owner = urllib.parse.quote(login)
        name = urllib.parse.quote(repo["name"])
        repo_path = f"/repos/{owner}/{name}"
        languages = github_request(f"{repo_path}/languages")
        root = github_request(f"{repo_path}/contents")

        try:
            workflows = github_request(f"{repo_path}/contents/.github/workflows")
            workflows_list = workflows if isinstance(workflows, list) else []
        except RuntimeError as exc:
            if "404" in str(exc):
                workflows_list = []
            else:
                raise

        if isinstance(languages, dict):
            for lang, byte_count in languages.items():
                language_bytes[lang] = language_bytes.get(lang, 0) + int(byte_count or 0)

        root_items = root if isinstance(root, list) else []
        root_names = {str(item.get("name", "")).lower() for item in root_items}
        has_readme = any(name.startswith("readme") for name in root_names)
        has_license = repo.get("license") is not None
        has_tests = any(name in {"test", "tests", "__tests__", "spec", "specs"} for name in root_names)
        has_ci = len(workflows_list) > 0
        pushed_at = repo.get("pushed_at")
        recent_push = False
        if pushed_at:
            pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            recent_push = (datetime.now(timezone.utc) - pushed).days <= 180

        score = sum(1 for value in (has_readme, has_license, has_tests, has_ci, recent_push) if value) * 20
        if score >= 60:
            mature_repos += 1
        if has_ci:
            with_ci += 1
        if has_tests:
            with_tests += 1
        if has_readme:
            with_readme += 1

        register_stack_signals(stack_counts, root_names, has_ci)

    language_bytes_rows = sorted(language_bytes.items(), key=lambda item: item[1], reverse=True)[:8]
    stack_rows = [
        f"{name}: detected in {count} repo{'s' if count > 1 else ''}"
        for name, count in sorted(stack_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    ]
    merge_rate = "Unavailable"
    if authored_prs and authored_prs > 0 and merged_prs is not None:
        merge_rate = f"{round((merged_prs / authored_prs) * 100)}%"
    recent_commit_count = sum(
        len((event.get("payload") or {}).get("commits") or [])
        for event in events
        if event.get("type") == "PushEvent"
    )

    return {
        "owned_sampled": len(owned),
        "language_bytes_rows": language_bytes_rows,
        "stack_rows": stack_rows,
        "insight_rows": [
            f"PR merge rate (public): {merge_rate}",
            f"External collaboration breadth: {external_contribution_repo_count(login, events)} repos in recent public events",
            f"Commit velocity: {recent_commit_count} commits over ~{estimate_event_window_days(events)} days of visible events",
            (
                f"Mature repo coverage: {mature_repos}/{len(owned)} sampled owned repos score 60+"
                if owned
                else "Mature repo coverage: unavailable"
            ),
            (
                f"Quality signals in sample: README {with_readme}/{len(owned)}, Tests {with_tests}/{len(owned)}, CI {with_ci}/{len(owned)}"
                if owned
                else "Quality signals in sample: unavailable"
            ),
        ],
    }


def build_stats_block() -> str:
    user = github_request("/users/geopolitis")
    repos = github_request("/users/geopolitis/repos?per_page=100")
    orgs = github_request("/users/geopolitis/orgs?per_page=100")
    events = github_request("/users/geopolitis/events/public?per_page=100")
    authored_prs = github_request("/search/issues?q=author:geopolitis+type:pr&per_page=1").get("total_count")
    merged_prs = github_request("/search/issues?q=author:geopolitis+type:pr+is:merged&per_page=1").get("total_count")

    if not isinstance(user, dict) or not isinstance(repos, list) or not isinstance(orgs, list) or not isinstance(events, list):
        raise RuntimeError("Unexpected GitHub API response shape.")

    total_stars = sum(int(repo.get("stargazers_count") or 0) for repo in repos)
    lifetime_commits, sampled_repos = estimate_lifetime_commits("geopolitis", repos)
    insight_bundle = build_insight_bundle("geopolitis", repos, events, authored_prs, merged_prs)

    top_repos = sorted(repos, key=lambda repo: int(repo.get("stargazers_count") or 0), reverse=True)[:5]

    language_counts: dict[str, int] = {}
    for repo in repos:
        language = repo.get("language")
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1
    top_languages = sorted(language_counts.items(), key=lambda item: (-item[1], item[0]))[:6]

    language_bytes_rows = insight_bundle["language_bytes_rows"][:6]
    language_bytes_total = sum(bytes_count for _, bytes_count in language_bytes_rows)

    summary_table = "\n".join(
        [
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Years on GitHub | {github_years(user.get('created_at'))} |",
            f"| Public repos | {format_number(len(repos))} |",
            f"| Followers | {format_number(user.get('followers'))} |",
            f"| Following | {format_number(user.get('following'))} |",
            f"| Public orgs | {format_number(len(orgs))} |",
            f"| Total stars earned | {format_number(total_stars)} |",
            f"| PRs authored (public) | {format_number(authored_prs)} |",
            f"| PRs merged (public) | {format_number(merged_prs)} |",
            (
                f"| Estimated lifetime commits (owned repos sample) | {format_number(lifetime_commits)} across {sampled_repos} repos |"
                if lifetime_commits is not None
                else "| Estimated lifetime commits (owned repos sample) | Unavailable |"
            ),
        ]
    )

    top_repo_lines = "\n".join(
        f"- [{repo['name']}]({repo['html_url']}): {repo.get('stargazers_count', 0)} star"
        + ("" if int(repo.get("stargazers_count") or 0) == 1 else "s")
        + (f" - {repo['description']}" if repo.get("description") else "")
        for repo in top_repos
    )

    top_language_lines = "\n".join(
        f"- {language}: primary language in {count} repo{'s' if count > 1 else ''}"
        for language, count in top_languages
    )

    language_footprint_lines = "\n".join(
        f"- {language}: {((bytes_count / language_bytes_total) * 100):.1f}% ({format_number(bytes_count)} bytes)"
        for language, bytes_count in language_bytes_rows
    )

    stack_lines = "\n".join(f"- {row}" for row in insight_bundle["stack_rows"][:6]) or "- No framework/tool signals detected."
    insight_lines = "\n".join(f"- {row}" for row in insight_bundle["insight_rows"])

    return f"""GitHub since **2012**. This section is generated from the same repo- and event-level logic used in `my-github-cv/app.js`, using public GitHub data plus the workflow token.

{summary_table}

**Top starred repositories**
{top_repo_lines}

**Primary languages by repo count**
Same logic as `app.js`: this counts each public repository by its single GitHub-detected primary language.
{top_language_lines}

**Language footprint by code bytes**
Same logic as `app.js`: this samples the 8 most recently pushed owned public repositories, then sums GitHub language-byte totals.
{language_footprint_lines}

**Stack fingerprint**
{stack_lines}

**Quality signals**
{insight_lines}
"""


def update_readme(stats_block: str) -> None:
    readme = README_PATH.read_text()
    start = readme.find(README_START)
    end = readme.find(README_END)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("README stats markers not found.")

    replacement = f"{README_START}\n{stats_block.rstrip()}\n{README_END}"
    updated = readme[:start] + replacement + readme[end + len(README_END) :]
    README_PATH.write_text(updated)


def main() -> int:
    stats_block = build_stats_block()
    update_readme(stats_block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
