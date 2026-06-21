"""Parse a raw task input into a TaskSpec. Two shapes only: a GitHub issue URL, or prose.

WHY the repo-allowlist check lives at parse time: it is the earliest point we know the
target repo, and refusing here means no off-list repo ever reaches prompt-building or the
container. JIRA is intentionally cut from v0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import repo_allowed

PROSE_MAX_CHARS = 4000

# Capture owner/repo (+ issue number) from a canonical issue URL.
GH_ISSUE_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<number>\d+)"
)


@dataclass
class TaskSpec:
    repo: str
    text: str
    source: str  # "issue" | "prose"


def parse_task(raw_input: str, repo_flag: str | None, allowed: list[str]) -> TaskSpec:
    """Issue URL -> repo from the URL (the agent fetches the issue via gh); prose -> repo
    must come from --repo. A --repo that contradicts the URL is a hard error (ambiguous
    intent). The resolved repo must be on the allowlist or we refuse."""
    text_in = (raw_input or "").strip()

    issue = GH_ISSUE_RE.search(text_in)
    if issue:
        repo = f"{issue.group('owner')}/{issue.group('repo')}"
        if repo_flag and repo_flag != repo:
            raise ValueError(
                f"--repo '{repo_flag}' conflicts with the issue URL repo '{repo}' - "
                "drop --repo or fix the URL"
            )
        spec = TaskSpec(repo=repo, text=text_in, source="issue")
    else:
        if not repo_flag:
            raise ValueError(
                "prose task needs --repo owner/repo (no repo could be inferred from the input)"
            )
        text = text_in[:PROSE_MAX_CHARS].strip()
        spec = TaskSpec(repo=repo_flag, text=text, source="prose")

    if not repo_allowed(spec.repo, allowed):
        raise ValueError(f"repo '{spec.repo}' is not in the allowlist - refusing")
    return spec
