"""Compose the prompt handed to the inner engine: persona + task + the hard conventions.

WHY the conventions are spelled out literally: the engine is autonomous inside the
container, so the prompt is the only place we can pin branch naming, the test-before-PR
rule, commit style, and PR shape. Tests assert these literal substrings survive.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import container, profile
from .task import GH_ISSUE_RE, TaskSpec

_PERSONA_PATH = Path(__file__).parent / "persona.md"

# Mid-run steering (issue #72, `franky job attach`): a prompt-level, best-effort channel - the
# hard guarantee is only that the host successfully writes the steer file; whether the agent
# ACTUALLY incorporates it depends on its loop re-reading the path at its next major step, the
# same trust register as build_iterate_prompt's own-PR guard. The path is sourced from
# container.STEER_FILE (a plain import - container.py does not import prompt.py, so there is no
# cycle) so the literal the agent is told to poll can NEVER drift from the one the host writes.
# Appended as the LAST convention line on every build-shaped prompt (a fresh build, iterate,
# replay, and resume all represent a live run an operator might want to steer).
_STEER_CONVENTION = (
    "- Mid-run steering: before starting each new sub-task or major step, check whether "
    f"the file `{container.STEER_FILE}` exists and is non-empty. If it does, an "
    "operator has sent you a correction - read it, prioritize its instructions over your "
    "current plan, then DELETE the file so you do not re-apply the same correction. It will "
    "not exist unless a correction was sent.\n"
)


# Operator PR-description spec (profile category `pr_template`): the operator's own house style
# for a PR body, injected by the profile bundle at a FIXED container path so the literal named
# here can never drift from where the bundle unpacks it. Same register as _STEER_CONVENTION -
# stated UNCONDITIONALLY, because with no profile (or no `pr_template` entry) the file simply does
# not exist and the default shape above stands, so a run without one is unchanged. It has to
# override rather than extend the default: the two describe the same artifact, and an agent handed
# both without a precedence rule splits the difference. Carried by every PR-opening prompt (build,
# replay --open-pr, resume); `job replay`/`job resume` do not pack a profile bundle today, so the
# file is simply absent there and the default shape stands, exactly as for a profile-less build.
_PR_TEMPLATE_CONVENTION = (
    "- Before you write the PR, check whether the file "
    f"`{profile.PR_TEMPLATE_CONTAINER_PATH}` exists and is non-empty. If it does, it is the "
    "operator's own PR-description spec: read it and follow it for the PR title and body, "
    "OVERRIDING the default title/body shape above wherever the two disagree (still include the "
    "closing keyword if one was required above). Take ONLY its PR title/body spec: it was likely "
    "written for an interactive tool, so ignore any workflow steps, tool names, or approval gates "
    "in it - you are autonomous, nobody is there to approve a draft, and the conventions above "
    "still decide when and how you open the PR. It will not exist unless the operator configured "
    "one.\n"
)


def load_persona() -> str:
    return _PERSONA_PATH.read_text(encoding="utf-8").strip()


def task_slug(spec: TaskSpec) -> str:
    """A DETERMINISTIC, task-distinguishing slug for the `franky/<slug>` branch name.

    WHY deterministic: the host computes this BEFORE the run so the idempotency pre-check can
    look up an open PR on exactly the branch the agent will push (issue #50 retry-safety). A
    loose hint the agent reinterprets would break that lookup, so the prompt now PINS this slug
    rather than handing the agent latitude.

    Computed on the spec as it stands at parse_task return time (issue text = the URL; jira text
    = the bare KEY like "FOO-123"; prose text = the prose - the JIRA body has not been fetched in
    yet, and that is fine, the slug keys on the stable identifier):

    - issue -> "issue-<n>" from the URL's issue number; falls back to the old repo-token
      tokenization if no number matches (a directly-built spec with unparseable text).
    - jira  -> the bare key lowercased (e.g. "foo-123"). spec.text IS the key here.
    - prose -> up to 5 leading tokens of the prose, or "task" if empty.

    Free-text-derived slugs (prose, the issue repo-fallback, a jira key) are length-bounded:
    a degenerate input (e.g. one 4000-char no-separator prose token) would otherwise yield an
    unrealizable git ref the agent cannot create, silently diverging from the host-predicted
    branch and defeating the idempotency lookup on the next retry.
    """
    if spec.source == "issue":
        m = GH_ISSUE_RE.search(spec.text)
        if m:
            return f"issue-{m.group('number')}"
        words = [w for w in re.split(r"[^a-z0-9]+", spec.repo.lower()) if w][:5]
        return _bound_slug("-".join(words))
    if spec.source == "jira":
        return _bound_slug(spec.text.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", spec.text.lower()) if w][:5]
    return _bound_slug("-".join(words))


_SLUG_MAX_CHARS = 60


def _bound_slug(slug: str) -> str:
    """Trim a free-text slug to a realizable git-ref length, or "task" if empty."""
    return slug[:_SLUG_MAX_CHARS].rstrip("-") or "task"


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


def _prior_failures_block(prior_failures: tuple[str, ...] | list[str]) -> str:
    """Render the 'earlier attempts failed, avoid repeating' block for a retry (issue #64 #5).

    Empty -> "" so a non-retry build prompt is byte-identical to before (guarded by tests). Each
    hint comes from a prior attempt's diagnosis `retry_hint`; the numbered list gives the fresh
    attempt a concrete learning signal instead of a blind restart.
    """
    if not prior_failures:
        return ""
    lines = "\n".join(f"  {i}. {hint}" for i, hint in enumerate(prior_failures, 1))
    return (
        "IMPORTANT - earlier automated attempts at THIS task already failed. Learn from them "
        "and do NOT repeat these mistakes:\n"
        f"{lines}\n\n"
    )


def build_prompt(
    spec: TaskSpec,
    *,
    branch: str | None = None,
    prior_failures: tuple[str, ...] | list[str] = (),
) -> str:
    """Compose the build prompt, pinning the branch the agent must use.

    `branch` is the host-computed branch name (`franky/<slug>`). The CLI passes it so the
    branch the agent pushes matches the one the idempotency pre-check looked up (issue #50);
    when omitted (standalone callers / tests) it is computed here from `task_slug`. Either way
    the prompt pins EXACTLY this branch - the agent is given no slug latitude.

    `prior_failures` (issue #64 #5) is the list of diagnosis `retry_hint`s from earlier failed
    attempts; when non-empty a learning-signal block is injected. Empty (the default) yields a
    byte-identical prompt to the pre-retry build.
    """
    persona = load_persona()

    task_block, close_line = _task_block(spec, plan=False)
    branch = branch or f"franky/{task_slug(spec)}"

    conventions = (
        "Conventions (follow exactly):\n"
        f"- Clone {spec.repo} and work on a new branch named exactly `{branch}`.\n"
        "- Run the repo's tests and make them pass BEFORE opening the PR. Do not open a PR on red tests.\n"
        "- Use conventional-commit messages: `<type>: <summary>` (e.g. `feat:`, `fix:`, `chore:`).\n"
        "- PR title uses the same conventional format: `<type>: <summary>`.\n"
        "- The PR body must contain three sections: what (the change), why (the motivation), "
        "and a test-plan (how you verified it).\n"
        f"{close_line}"
        f"{_PR_TEMPLATE_CONVENTION}"
        "- Open the PR with `gh pr create`. Do NOT merge it - a human reviews every change.\n"
        "- Keep commit messages and PR text professional; no persona flavor in the deliverables.\n"
        f"{_STEER_CONVENTION}"
    )

    return f"{persona}\n\n{task_block}\n{_prior_failures_block(prior_failures)}{conventions}"


def build_iterate_prompt(spec: TaskSpec) -> str:
    """Prompt for the `iterate` command: a FOLLOW-UP pass on an existing Franky PR.

    Standalone on purpose - it does NOT reuse `_task_block`/`_slug_hint` (which key on
    "issue"/"jira"/"prose"), so the new `source="pr"` never falls through to build-mode
    behaviour and the build conventions (create a `franky/` branch, `gh pr create`) are not
    inherited. The agent checks out the EXISTING branch and pushes ADDITIVE commits.

    `spec.text` is the canonical PR URL (reconstructed in parse_pr_task). The own-PR guard
    (head branch `franky/*` AND not cross-repository) is prompt-level - the same trust
    register as build's "do not merge": the engine is autonomous and carries creds, so the
    hard bounds are the repo allowlist + the egress cage + a human reviewing the PR. It
    keeps `iterate` from acting on a fork PR or a non-Franky branch (issue #24 non-goal).
    """
    persona = load_persona()
    url = spec.text
    owner = spec.repo.split("/", 1)[0]

    task_block = (
        f"Repo: {spec.repo}\n"
        f"Task: this is a FOLLOW-UP pass on a pull request you (Franky) already opened: {url}. "
        "Address its open review feedback and any failing CI with additive follow-up commits.\n"
    )

    conventions = (
        "Conventions (follow exactly):\n"
        f"- FIRST confirm this is your own PR before changing anything: run "
        f"`gh pr view {url} --json headRefName,isCrossRepository,headRepositoryOwner` and verify "
        f"ALL of: the head branch name (`headRefName`) starts with `franky/`; the PR is NOT "
        f"cross-repository (`isCrossRepository` is false); and the head repository owner "
        f"(`headRepositoryOwner.login`) is `{owner}`. If ANY check fails, STOP - make no commits "
        f"and no push.\n"
        f"- Clone {spec.repo} and check out the PR's existing branch with `gh pr checkout {url}`. "
        "Do NOT create a new branch.\n"
        f"- Gather the feedback to address: read the review comments and requested changes with "
        f"`gh pr view {url} --comments`, inspect failing checks with `gh pr checks {url}`, and "
        f"review the current diff with `gh pr diff {url}`.\n"
        "- Address that feedback with ADDITIVE follow-up commits on the same branch. Do NOT amend, "
        "squash, rebase, or otherwise rewrite history.\n"
        "- Run the repo's tests and make them pass BEFORE pushing. Do not push on red tests.\n"
        "- Use conventional-commit messages: `<type>: <summary>` (e.g. `fix:`, `chore:`).\n"
        "- Push the follow-up commits to the SAME branch with a normal `git push`. NEVER use "
        "`git push --force` or `git push --force-with-lease`.\n"
        "- Do NOT open a new PR (no `gh pr create`) and do NOT merge the PR (no `gh pr merge`) - "
        "a human reviews every change.\n"
        "- Keep commit messages and PR text professional; no persona flavor in the deliverables.\n"
        f"{_STEER_CONVENTION}"
    )

    return f"{persona}\n\n{task_block}\n{conventions}"


def build_replay_prompt(
    spec: TaskSpec,
    *,
    branch: str,
    base_sha: str,
    open_pr: bool,
) -> str:
    """Prompt for `franky job replay`: re-run a recorded task from its saved inputs (issue #70).

    WHY this exists: replay reproduces the INPUTS (the original task text + the exact base
    commit it started from), NOT bit-identical output - the underlying LLM is not
    deterministic, so two runs over the same inputs can still diverge. The value is a
    controlled, repeatable starting point for debugging a failure, not a guarantee of the same
    transcript.

    Standalone like `build_iterate_prompt` - it reuses `_task_block` (for the repo line +
    task/issue framing, identical to a normal build) but owns its own conventions block so the
    base-commit pin and the reproduce-only/open-pr split live in exactly one place.

    `base_sha` is ALWAYS pinned: immediately after cloning, the agent must check out that exact
    commit before doing anything else, so the run starts from the SAME state the original run
    did (a repo that has moved on since must not silently change what is being reproduced).

    `open_pr=False` (the DEFAULT) is reproduce-only and side-effect-free on the remote: the
    agent works entirely inside the container to reproduce the outcome, and is told NOT to
    create a branch, push, or open a PR - so a replay can always be run without risking a
    duplicate or unwanted PR. `open_pr=True` opts into the normal build conventions (branch,
    tests-green-before-PR, conventional commits, 3-section PR body, `gh pr create`, never
    merge) so a confirmed-fixed replay can still land a real PR.
    """
    persona = load_persona()
    task_block, close_line = _task_block(spec, plan=False)

    base_pin = (
        "This is a REPLAY of an earlier run. After cloning "
        f"{spec.repo}, immediately check out the exact base commit `{base_sha}` (e.g. "
        f"`git fetch origin {base_sha} && git checkout {base_sha}`, or `git checkout "
        f"{base_sha}`) and start ALL work from that commit, so this reproduces the original "
        "run's starting state.\n"
    )

    if open_pr:
        conventions = (
            "Conventions (follow exactly):\n"
            f"{base_pin}"
            f"- Work on a new branch named exactly `{branch}`.\n"
            "- Run the repo's tests and make them pass BEFORE opening the PR. Do not open a PR "
            "on red tests.\n"
            "- Use conventional-commit messages: `<type>: <summary>` (e.g. `feat:`, `fix:`, "
            "`chore:`).\n"
            "- PR title uses the same conventional format: `<type>: <summary>`.\n"
            "- The PR body must contain three sections: what (the change), why (the "
            "motivation), and a test-plan (how you verified it).\n"
            f"{close_line}"
            f"{_PR_TEMPLATE_CONVENTION}"
            "- Open the PR with `gh pr create`. Do NOT merge it - a human reviews every "
            "change.\n"
            "- Keep commit messages and PR text professional; no persona flavor in the "
            "deliverables.\n"
            f"{_STEER_CONVENTION}"
        )
    else:
        conventions = (
            "Conventions (follow exactly):\n"
            f"{base_pin}"
            "- REPRODUCE-ONLY MODE: work entirely inside the container to reproduce the "
            "outcome - make the change, run the repo's tests, and report what happened.\n"
            "- Do NOT create or push any branch, and do NOT open a pull request "
            "(`gh pr create`) - this pass only reproduces, it changes nothing on the remote.\n"
            "- Keep your report professional; no persona flavor in the deliverables.\n"
            f"{_STEER_CONVENTION}"
        )

    return f"{persona}\n\n{task_block}\n{conventions}"


def build_resume_prompt(spec: TaskSpec, *, branch: str) -> str:
    """Prompt for `franky job resume`: continue a hung/timed-out/killed run's restored workspace.

    WHY this differs from a fresh build: resume restores the FILESYSTEM (the prior run's `/work`
    clone + its branch state), NOT the agent's LLM/session state. A fresh engine therefore
    re-orients from the branch state on disk and carries the original task forward from where it
    left off, rather than re-cloning and starting over.

    Standalone like `build_replay_prompt` - it reuses `_task_block` (repo line + task/issue
    framing, identical to a normal build) but owns its own conventions block so the
    do-not-re-clone / restored-under-/work framing lives in exactly one place. `branch` is the
    original run's branch (recorded, or a freshly predicted slug); the agent stays on it so the
    continued work lands on the same head the idempotency check knows about.
    """
    persona = load_persona()
    task_block, close_line = _task_block(spec, plan=False)

    conventions = (
        "Conventions (follow exactly):\n"
        f"- RESUME MODE: your prior workspace - the clone of {spec.repo} and its branch state - "
        "has been RESTORED under `/work`. `cd` into the existing clone there and do NOT re-clone "
        "the repo.\n"
        "- Continue the original task from where it left off, building on the work already "
        "present in the restored workspace.\n"
        f"- Stay on the existing branch `{branch}`; do NOT create a new branch.\n"
        "- Run the repo's tests and make them pass BEFORE opening the PR. Do not open a PR on "
        "red tests.\n"
        "- Use conventional-commit messages: `<type>: <summary>` (e.g. `feat:`, `fix:`, "
        "`chore:`).\n"
        "- The PR body must contain three sections: what (the change), why (the motivation), "
        "and a test-plan (how you verified it).\n"
        f"{close_line}"
        f"{_PR_TEMPLATE_CONVENTION}"
        "- Open the PR with `gh pr create`. Do NOT merge it - a human reviews every change.\n"
        "- Keep commit messages and PR text professional; no persona flavor in the deliverables.\n"
        f"{_STEER_CONVENTION}"
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


def build_decompose_prompt(spec: TaskSpec, nonce: str) -> str:
    """Prompt for the `franky plan` command: read-only scope-assessment + decomposition.

    The agent inspects the repo/issue (read-only), decides whether the task fits ONE focused
    PR or needs splitting into PR-sized sub-tasks, and ends its response with EXACTLY ONE
    machine-readable sentinel block carrying the decomposition JSON.

    `nonce` is a per-run token (the CLI generates `secrets.token_hex`) fenced into the
    sentinel so a hostile issue body / repo file cannot plant a fixed sentinel to hijack the
    payload Franky reports - the same trust register as the repo-scoped PR-URL guard. The
    parser keys on this exact token, so the literal must survive into the prompt. Same
    read-only register as build_plan_prompt: the container is autonomous, but plan builds and
    opens NOTHING, so these instructions keep the agent from wasting the pass on edits it
    cannot persist (the container is ephemeral) and from creating a branch or PR.
    """
    persona = load_persona()
    task_block, _ = _task_block(spec, plan=True)

    begin = f"FRANKY_PLAN_{nonce}_BEGIN"
    end = f"FRANKY_PLAN_{nonce}_END"

    decompose_conventions = (
        "PLAN MODE - this is a read-only scope-assessment and decomposition pass, NOT "
        "execution:\n"
        "- Inspect the issue and the repo as needed (read-only) to understand the work.\n"
        "- Assess whether the task fits ONE focused pull request or should be split into "
        "several PR-sized sub-tasks. Each sub-task must be sized to roughly one PR.\n"
        "- Do NOT modify any files, commit, push, create a branch, or open a pull request. "
        "Assess, decompose, and stop.\n"
        "- END your response with EXACTLY ONE machine-readable block and NO text after it, "
        "in this exact form (a single line, compact JSON, no surrounding code fence):\n"
        # The middle segment carries LITERAL braces, so it cannot be an f-string; the explicit
        # `+` concatenation around it is intentional (not a typo).
        f"  {begin}" + "{<compact ONE-LINE JSON>}" + f"{end}\n"
        "  where the JSON is exactly this shape:\n"
        '  {"fits_one_pr": <bool>, "subtasks": [{"title": "...", "summary": "...", '
        '"suggested_repo": "owner/repo"}], "rationale": "..."}\n'
        "- If the task fits one PR, set `fits_one_pr` true; `subtasks` may then be a single "
        "entry or empty, and `rationale` explains why it fits. Otherwise set it false and "
        "list one entry per PR-sized sub-task.\n"
        f"- Output ONLY the sentinel block as the FINAL content of your response; add no text "
        f"after `{end}`.\n"
    )

    return f"{persona}\n\n{task_block}\n{decompose_conventions}"


# Cap the transcript injected into a diagnose prompt. The failure signal is almost always at the
# END of a run, so we keep the tail; an uncapped transcript could blow the engine's context.
DIAGNOSE_TRANSCRIPT_TAIL_CHARS = 20000


def build_diagnose_prompt(record: dict, transcript: str, nonce: str) -> str:
    """Prompt for `franky job diagnose` / the `build --retry` interstitial pass (issue #64).

    Read-only failure analysis: the agent is handed a FAILED run's metadata + (tail-capped)
    transcript and must emit EXACTLY ONE `FRANKY_DIAG_<nonce>` sentinel block with a structured
    root-cause + proposed fix + a `retryable`/`retry_hint` learning signal. It clones nothing,
    edits nothing, and opens no PR - like `build_decompose_prompt`, the read-only register is
    enforced here (the container is autonomous but this pass persists nothing anyway).

    `transcript` is ALREADY redacted (run_in_container scrubs its output; the standalone command
    reads the redacted tasks/*.log), so injecting it adds no secret surface. `nonce` is the
    anti-injection token the parser keys on, so the literal must survive into the prompt.

    When `record["diagnostics"]` is present (issue #69: best-effort runtime signals captured
    host-side just before container teardown), a "Runtime diagnostics" block is rendered right
    after the metadata, before the transcript. WHY: these are HARD facts (an actual exit code,
    an actual OOM flag) rather than something the agent has to infer from prose, so they give
    the diagnose pass a firmer footing than the transcript alone. Absent/empty diagnostics ->
    no block at all, so a record with no diagnostics produces the exact same prompt as before.
    """
    persona = load_persona()

    if len(transcript) > DIAGNOSE_TRANSCRIPT_TAIL_CHARS:
        tail = transcript[-DIAGNOSE_TRANSCRIPT_TAIL_CHARS:]
        note = f" (last {DIAGNOSE_TRANSCRIPT_TAIL_CHARS} chars; earlier output omitted)"
    else:
        tail = transcript
        note = ""

    diag = record.get("diagnostics")
    diagnostics_block = ""
    if diag:
        lines = ["Runtime diagnostics (captured host-side before container teardown):"]
        for key in ("task_exit_code", "oom_killed", "task_state", "dind_ready", "tmpfs_full"):
            if key in diag:
                lines.append(f"- {key}: {diag[key]}")
        if "egress_denied" in diag:
            denied = diag.get("egress_denied") or []
            rendered = (
                ", ".join(f"{e.get('host')} (x{e.get('count')})" for e in denied)
                if denied
                else "none"
            )
            lines.append(f"- egress_denied: {rendered}")
        diagnostics_block = "\n".join(lines) + "\n\n"

    failure_block = (
        "A previous Franky run FAILED. Diagnose why.\n\n"
        "Run metadata:\n"
        f"- command: {record.get('command')}\n"
        f"- repo: {record.get('repo')}\n"
        f"- engine: {record.get('engine')}\n"
        f"- status: {record.get('status')}\n"
        f"- exit_code: {record.get('exit_code')}\n"
        f"- task: {record.get('task')}\n\n"
        f"{diagnostics_block}"
        f"Run transcript{note}:\n"
        "-----BEGIN TRANSCRIPT-----\n"
        f"{tail}\n"
        "-----END TRANSCRIPT-----\n"
    )

    begin = f"FRANKY_DIAG_{nonce}_BEGIN"
    end = f"FRANKY_DIAG_{nonce}_END"

    diagnose_conventions = (
        "DIAGNOSE MODE - this is a read-only failure analysis, NOT execution:\n"
        "- Analyze the transcript + metadata above and determine WHY the run failed.\n"
        "- Do NOT clone, modify files, commit, push, or open a pull request. Analyze and stop.\n"
        "- END your response with EXACTLY ONE machine-readable block and NO text after it, in "
        "this exact form (a single line, compact JSON, no surrounding code fence):\n"
        # The middle segment carries LITERAL braces, so it cannot be an f-string; the explicit
        # `+` concatenation around it is intentional (not a typo).
        f"  {begin}" + "{<compact ONE-LINE JSON>}" + f"{end}\n"
        "  where the JSON is exactly this shape:\n"
        '  {"root_cause": "...", "category": "<one of: dind_daemon|egress_denied|test_failure|'
        'build_error|timeout|auth|no_pr|agent_confusion|rate_limit|unknown>", '
        '"evidence": ["a quoted line or concrete fact from the transcript", "..."], '
        '"proposed_fix": "...", "retryable": <bool>, '
        '"retry_hint": "one concise instruction a fresh attempt should follow to avoid this '
        'failure", "confidence": "<low|medium|high>"}\n'
        "- Set `retryable` true ONLY if a fresh attempt following your `retry_hint` could "
        "plausibly succeed; set it false for a deterministic failure (missing creds, an "
        "impossible task, a repo that cannot build regardless of approach).\n"
        f"- Output ONLY the sentinel block as the FINAL content of your response; add no text "
        f"after `{end}`.\n"
    )

    return f"{persona}\n\n{failure_block}\n{diagnose_conventions}"
