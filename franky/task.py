"""Parse a raw task input into a TaskSpec. Three shapes: a GitHub issue URL, a JIRA key,
or prose.

WHY the repo-allowlist check lives at parse time: it is the earliest point we know the
target repo, and refusing here means no off-list repo ever reaches prompt-building or the
container. JIRA keys are now supported; the actual fetch happens host-side in the CLI
(the container has no JIRA creds and no JIRA egress).
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

# LOAD-BEARING path-injection control: the key is interpolated directly into the JIRA
# REST API URL path (`/rest/api/3/issue/<key>`). Relaxing this regex would allow path
# traversal or query smuggling (e.g. "FOO-1/../admin" or "FOO-1?expand=names").
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# A whole stripped input is a JIRA task iff it is the keyword "jira" followed by exactly
# ONE non-space token. Multi-word remainders (e.g. "jira is flaky, fix it") fall through
# to the prose branch intentionally.
JIRA_TASK_RE = re.compile(r"^jira\s+(\S+)\s*$", re.IGNORECASE)


@dataclass
class TaskSpec:
    repo: str
    text: str
    source: str  # "issue" | "prose" | "jira"


def parse_task(raw_input: str, repo_flag: str | None, allowed: list[str]) -> TaskSpec:
    """Issue URL -> repo from the URL (the agent fetches the issue via gh); JIRA key ->
    repo must come from --repo (the CLI fetches the issue host-side); prose -> repo must
    come from --repo. A --repo that contradicts the URL is a hard error (ambiguous intent).
    The resolved repo must be on the allowlist or we refuse."""
    text_in = (raw_input or "").strip()

    jira_match = JIRA_TASK_RE.match(text_in)
    if jira_match:
        token = jira_match.group(1)
        if not JIRA_KEY_RE.match(token):
            raise ValueError(f"invalid JIRA key '{token}'")
        if not repo_flag:
            raise ValueError("jira task needs --repo owner/repo (the JIRA key carries no repo)")
        spec = TaskSpec(repo=repo_flag, text=token, source="jira")
    else:
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
