"""Refresh the generated statistics block in the profile README.

Uses only the Python standard library. A GH_STATS_TOKEN with read:user scope
can include anonymized private contribution counts when the account opts in.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = "<!-- github-stats:start -->"
END = "<!-- github-stats:end -->"

QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    followers { totalCount }
    repositories(first: 100, after: $cursor, privacy: PUBLIC,
                 ownerAffiliations: OWNER) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        isFork
        stargazerCount
        languages(first: 100) {
          edges { size node { name } }
        }
      }
    }
    contributionsCollection {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalRepositoryContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
  }
}
"""


def graphql(token: str, login: str, cursor: str | None) -> dict:
    payload = json.dumps({"query": QUERY, "variables": {"login": login, "cursor": cursor}}).encode()
    request = Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-generator",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"GitHub API returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"GitHub API request failed: {error.reason}") from error

    if result.get("errors"):
        messages = "; ".join(error["message"] for error in result["errors"])
        raise RuntimeError(f"GitHub GraphQL error: {messages}")
    user = result.get("data", {}).get("user")
    if user is None:
        raise RuntimeError(f"GitHub user {login!r} was not found")
    return user


def fetch_stats(token: str, login: str) -> dict:
    cursor = None
    languages: Counter[str] = Counter()
    stars = 0
    first_user = None

    while True:
        user = graphql(token, login, cursor)
        if first_user is None:
            first_user = user
        repositories = user["repositories"]
        for repo in repositories["nodes"]:
            stars += repo["stargazerCount"]
            if repo["isFork"]:
                continue
            for edge in repo["languages"]["edges"]:
                languages[edge["node"]["name"]] += edge["size"]

        page = repositories["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
        if not cursor:
            raise RuntimeError("GitHub returned an incomplete repository page")

    assert first_user is not None
    collection = first_user["contributionsCollection"]
    days = sorted(
        (
            (date.fromisoformat(day["date"]), day["contributionCount"])
            for week in collection["contributionCalendar"]["weeks"]
            for day in week["contributionDays"]
        ),
        key=lambda item: item[0],
    )
    return {
        "followers": first_user["followers"]["totalCount"],
        "repositories": first_user["repositories"]["totalCount"],
        "stars": stars,
        "languages": languages,
        "collection": collection,
        "days": days,
    }


def streaks(days: list[tuple[date, int]]) -> tuple[int, int]:
    if not days:
        return 0, 0

    longest = run = 0
    for _, count in days:
        run = run + 1 if count else 0
        longest = max(longest, run)

    # An unfinished day does not end a streak that was active yesterday.
    today = date.today()
    recent = [(day, count) for day, count in days if day <= today]
    if recent and recent[-1][0] == today and recent[-1][1] == 0:
        recent.pop()
    if not recent or recent[-1][0] < today - timedelta(days=1):
        return 0, longest
    current = 0
    expected = recent[-1][0] if recent else today
    for day, count in reversed(recent):
        if day != expected or count == 0:
            break
        current += 1
        expected -= timedelta(days=1)
    return current, longest


def render(stats: dict) -> str:
    collection = stats["collection"]
    current, longest = streaks(stats["days"])
    total_language_bytes = sum(stats["languages"].values())
    top_languages = stats["languages"].most_common(5)
    if top_languages:
        shares = [(name, size / total_language_bytes) for name, size in top_languages]
        language_text = ", ".join(
            f"{name} {share:.0%}" if share >= 0.01 else f"{name} <1%"
            for name, share in shares
        )
    else:
        language_text = "пока нет данных"

    private = collection["restrictedContributionsCount"]
    private_note = f" (в том числе {private} скрытых)" if private else ""
    return "\n".join(
        [
            "| Показатель | Значение |",
            "|:--|--:|",
            f"| Вклады за последние 12 месяцев | **{collection['contributionCalendar']['totalContributions']}**{private_note} |",
            f"| Серия: сейчас / максимум за период | **{current} / {longest}** дней |",
            f"| Коммиты / pull requests | **{collection['totalCommitContributions']} / {collection['totalPullRequestContributions']}** |",
            f"| Issues / code reviews | **{collection['totalIssueContributions']} / {collection['totalPullRequestReviewContributions']}** |",
            f"| Создано репозиториев | **{collection['totalRepositoryContributions']}** |",
            f"| Публичные репозитории / звёзды | **{stats['repositories']} / {stats['stars']}** |",
            f"| Подписчики | **{stats['followers']}** |",
            "",
            f"**Языки публичных репозиториев:** {language_text}.",
            "",
            "<sub>Вклады, серии, коммиты, PR, issues, reviews и созданные репозитории — за последние 12 месяцев; остальные числа — текущие. Данные GitHub API. Проценты языков рассчитаны по объёму кода в собственных публичных репозиториях без форков.</sub>",
        ]
    )


def update_readme(block: str) -> bool:
    original = README.read_text(encoding="utf-8")
    if original.count(START) != 1 or original.count(END) != 1:
        raise RuntimeError("README must contain exactly one pair of statistics markers")
    before, rest = original.split(START, 1)
    _, after = rest.split(END, 1)
    updated = before + START + "\n" + block + "\n" + END + after
    if updated == original:
        return False
    README.write_text(updated, encoding="utf-8", newline="\n")
    return True


def main() -> None:
    token = os.environ.get("GH_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GH_STATS_TOKEN or GITHUB_TOKEN")
    login = os.environ.get("GITHUB_REPOSITORY_OWNER", "ichirou-hue")
    changed = update_readme(render(fetch_stats(token, login)))
    print("README statistics updated" if changed else "README statistics already current")


if __name__ == "__main__":
    main()
