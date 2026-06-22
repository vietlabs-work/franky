"""Compose the prompt handed to the inner engine: persona + task + the hard conventions.

WHY the conventions are spelled out literally: the engine is autonomous inside the
container, so the prompt is the only place we can pin branch naming, the test-before-PR
rule, commit style, and PR shape. Tests assert these literal substrings survive.
"""

from __future__ import annotations

import re
from pathlib import Path

from .task import GH_ISSUE_RE, TaskSpec

_PERSONA_PATH = Path(__file__).parent / "persona.md"


def load_persona() -> str:
    return _PERSONA_PATH.read_text(encoding="utf-8").strip()


def _slug_hint(spec: TaskSpec) -> str:
    """A short hint the agent turns into the branch slug. Kept loose on purpose - the agent
    derives the real slug; we only anchor the `franky/` prefix and give it source material.
    Non-alphanumerics (incl. the `/` in owner/repo) split into separate words so an issue
    task hints from the repo name rather than collapsing to the empty default."""
    basis = spec.text if spec.source == "prose" else spec.repo
    words = [w for w in re.split(r"[^a-z0-9]+", basis.lower()) if w][:5]
    return "-".join(words) or "task"


def build_prompt(spec: TaskSpec) -> str:
    persona = load_persona()

    # close_line tells the agent to link the issue with a GitHub closing keyword so the issue
    # auto-closes when the PR merges. Without it, merged PRs leave their issue open. Only issue
    # tasks have an issue to close; the regex always matches a parse_task-produced issue spec,
    # but we guard so a directly-built spec degrades to no keyword rather than crashing.
    close_line = ""
    if spec.source == "issue":
        task_block = (
            f"Repo: {spec.repo}\n"
            f"Task: implement the GitHub issue at this URL. Fetch it first with "
            f"`gh issue view {spec.text}` to read the full issue body and comments, then build it.\n"
        )
        m = GH_ISSUE_RE.search(spec.text)
        if m:
            close_line = (
                f"- The PR body must include `Closes #{m.group('number')}` (a GitHub closing "
                "keyword) so the issue auto-closes when the PR is merged.\n"
            )
    else:
        task_block = f"Repo: {spec.repo}\nTask (prose): {spec.text}\n"

    conventions = (
        "Conventions (follow exactly):\n"
        f"- Clone {spec.repo} and work on a new branch named `franky/{_slug_hint(spec)}` "
        "(the `franky/` prefix is required; pick a short descriptive slug after it).\n"
        "- Run the repo's tests and make them pass BEFORE opening the PR. Do not open a PR on red tests.\n"
        "- Use conventional-commit messages: `<type>: <summary>` (e.g. `feat:`, `fix:`, `chore:`).\n"
        "- PR title uses the same conventional format: `<type>: <summary>`.\n"
        "- The PR body must contain three sections: what (the change), why (the motivation), "
        "and a test-plan (how you verified it).\n"
        f"{close_line}"
        "- Open the PR with `gh pr create`. Do NOT merge it - a human reviews every change.\n"
        "- Keep commit messages and PR text professional; no persona flavor in the deliverables.\n"
    )

    return f"{persona}\n\n{task_block}\n{conventions}"
