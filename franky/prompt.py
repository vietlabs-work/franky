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
    task hints from the repo name rather than collapsing to the empty default.

    issue -> slug from repo name (owner/repo has no useful text).
    prose/jira -> slug from text (jira text begins "[FOO-123] ..." -> readable slug).
    """
    basis = spec.repo if spec.source == "issue" else spec.text
    words = [w for w in re.split(r"[^a-z0-9]+", basis.lower()) if w][:5]
    return "-".join(words) or "task"


def _task_block(spec: TaskSpec, *, plan: bool) -> tuple[str, str]:
    """Build the task block and (build-mode-only) closing-keyword line.

    `plan=True` frames it as a read-only planning pass ("plan it"); `plan=False` as the
    real build ("build it") and adds the closing-keyword line for issue tasks. The shared
    bits (repo line, `gh issue view` fetch step) stay identical across modes so a plan and
    its execution describe the same target.

    close_line tells the agent to link the issue with a GitHub closing keyword so the issue
    auto-closes when the PR merges. Only issue tasks (and only in build mode) have a PR to
    close; the regex always matches a parse_task-produced issue spec, but we guard so a
    directly-built spec degrades to no keyword rather than crashing.
    """
    close_line = ""
    if spec.source == "issue":
        verb = "plan the implementation of" if plan else "implement"
        action = "plan it" if plan else "build it"
        task_block = (
            f"Repo: {spec.repo}\n"
            f"Task: {verb} the GitHub issue at this URL. Fetch it first with "
            f"`gh issue view {spec.text}` to read the full issue body and comments, then {action}.\n"
        )
        if not plan:
            m = GH_ISSUE_RE.search(spec.text)
            if m:
                close_line = (
                    f"- The PR body must include `Closes #{m.group('number')}` (a GitHub closing "
                    "keyword) so the issue auto-closes when the PR is merged.\n"
                )
    elif spec.source == "jira":
        label = "Task to plan (from JIRA)" if plan else "Task (from JIRA)"
        task_block = f"Repo: {spec.repo}\n{label}: {spec.text}\n"
    else:
        label = "Task to plan (prose)" if plan else "Task (prose)"
        task_block = f"Repo: {spec.repo}\n{label}: {spec.text}\n"
    return task_block, close_line


def build_prompt(spec: TaskSpec) -> str:
    persona = load_persona()

    task_block, close_line = _task_block(spec, plan=False)

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


def build_plan_prompt(spec: TaskSpec) -> str:
    """Prompt for the `--plan-first` planning pass: produce a plan, change NOTHING.

    Used before the real build when the operator wants to inspect the approach first. The
    hard guarantee against an early write/PR is the operator approval gate in the CLI (the
    container is autonomous and still carries creds); these instructions keep the agent from
    wasting the planning pass on edits it cannot persist (the container is ephemeral) and
    from opening a PR before approval.
    """
    persona = load_persona()
    task_block, _ = _task_block(spec, plan=True)

    plan_conventions = (
        "PLAN-FIRST MODE - this is a read-only planning pass, NOT execution:\n"
        "- Inspect the issue and the repo as needed to understand the change.\n"
        "- Output a concise, numbered implementation plan: the files you intend to change, "
        "the approach, and how you will verify it (which tests).\n"
        "- Do NOT modify any files, commit, push, create a branch, or open a PR. "
        "Produce the plan and stop.\n"
    )

    return f"{persona}\n\n{task_block}\n{plan_conventions}"
