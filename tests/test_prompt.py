from franky.prompt import (
    DIAGNOSE_TRANSCRIPT_TAIL_CHARS,
    build_decompose_prompt,
    build_diagnose_prompt,
    build_iterate_prompt,
    build_plan_prompt,
    build_prompt,
    load_persona,
    task_slug,
)
from franky.task import TaskSpec


def test_load_persona_has_professional_guard():
    persona = load_persona()
    assert persona
    # the load-bearing guard: persona must NOT bleed into commits/PRs
    assert "professional" in persona.lower()


def test_build_prompt_prose_includes_task_and_conventions():
    spec = TaskSpec(repo="me/repo", text="add a --json flag", source="prose")
    p = build_prompt(spec)
    assert "me/repo" in p
    assert "add a --json flag" in p
    # literal convention substrings the runtime depends on
    assert "franky/" in p
    assert "conventional" in p.lower()
    assert "gh pr create" in p
    assert "what" in p.lower()
    assert "why" in p.lower()
    assert "test" in p.lower()


def test_build_prompt_issue_mentions_fetch():
    url = "https://github.com/octocat/hello/issues/42"
    spec = TaskSpec(repo="octocat/hello", text=url, source="issue")
    p = build_prompt(spec)
    assert url in p
    assert "octocat/hello" in p
    assert "gh issue view" in p
    assert "gh pr create" in p
    assert "franky/" in p


def test_build_prompt_issue_includes_closing_keyword():
    # The PR body must carry a closing keyword so the issue auto-closes on merge.
    spec = TaskSpec(
        repo="octocat/hello", text="https://github.com/octocat/hello/issues/42", source="issue"
    )
    p = build_prompt(spec)
    assert "Closes #42" in p


def test_build_prompt_prose_has_no_closing_keyword():
    # Prose tasks have no issue to close.
    spec = TaskSpec(repo="me/repo", text="add a --json flag", source="prose")
    p = build_prompt(spec)
    assert "Closes #" not in p


def test_build_prompt_issue_without_parseable_url_degrades_gracefully():
    # parse_task guarantees a real issue URL, but a directly-built spec with unparseable text
    # must degrade to no closing keyword rather than crash.
    spec = TaskSpec(repo="octocat/hello", text="not-a-url", source="issue")
    p = build_prompt(spec)
    assert "Closes #" not in p


def test_issue_branch_slug_uses_issue_number():
    # The branch slug is now deterministic and task-distinguishing: an issue keys on its
    # number (`issue-42`) so a retry resolves to the same branch for the idempotency check.
    spec = TaskSpec(
        repo="octocat/hello", text="https://github.com/octocat/hello/issues/42", source="issue"
    )
    p = build_prompt(spec)
    assert "franky/issue-42" in p


def test_issue_slug_without_parseable_number_falls_back_to_repo_tokens():
    # A directly-built issue spec whose text has no issue number degrades to the old
    # repo-token tokenization rather than crashing.
    spec = TaskSpec(repo="octocat/hello", text="not-a-url", source="issue")
    assert task_slug(spec) == "octocat-hello"


def test_build_prompt_pins_explicit_branch_and_drops_slug_latitude():
    # When the CLI passes a branch, the prompt pins EXACTLY it (host-predicted == prompt-pinned
    # for the idempotency contract) and no longer hands the agent slug latitude.
    spec = TaskSpec(repo="me/repo", text="add a --json flag", source="prose")
    p = build_prompt(spec, branch="franky/x")
    assert "named exactly `franky/x`" in p
    assert "pick a short descriptive slug" not in p


# ---------------------------------------------------------------------------
# build_plan_prompt (the --plan-first read-only planning pass)
# ---------------------------------------------------------------------------


def test_plan_prompt_asks_for_a_plan_and_forbids_writing():
    spec = TaskSpec(repo="me/repo", text="add a --json flag", source="prose")
    p = build_plan_prompt(spec)
    assert "me/repo" in p
    assert "add a --json flag" in p
    assert "plan" in p.lower()
    # The planning pass must not instruct the agent to open a PR.
    assert "gh pr create" not in p
    # ...and must explicitly forbid mutating actions.
    assert "Do NOT" in p


def test_plan_prompt_issue_still_fetches_but_does_not_close():
    url = "https://github.com/octocat/hello/issues/42"
    spec = TaskSpec(repo="octocat/hello", text=url, source="issue")
    p = build_plan_prompt(spec)
    assert url in p
    assert "gh issue view" in p
    # No PR is opened in a planning pass, so no closing keyword either.
    assert "Closes #" not in p


# ---------------------------------------------------------------------------
# build_decompose_prompt (the `franky plan` read-only decomposition pass)
# ---------------------------------------------------------------------------


def test_decompose_prompt_is_read_only_and_names_repo():
    spec = TaskSpec(repo="me/repo", text="add a big feature", source="prose")
    p = build_decompose_prompt(spec, "abc123")
    assert "me/repo" in p
    assert "add a big feature" in p
    # Read-only: must forbid mutating actions and must not instruct opening a PR.
    assert "do NOT" in p or "Do NOT" in p
    assert "gh pr create" not in p


def test_decompose_prompt_has_nonce_fenced_sentinel_and_schema():
    spec = TaskSpec(repo="me/repo", text="add a big feature", source="prose")
    p = build_decompose_prompt(spec, "abc123")
    assert "FRANKY_PLAN_abc123_BEGIN" in p
    assert "FRANKY_PLAN_abc123_END" in p
    # The decomposition schema keys must appear so the agent emits the right shape.
    assert "fits_one_pr" in p
    assert "subtasks" in p
    assert "rationale" in p
    assert "suggested_repo" in p
    # Must instruct: no text after the closing sentinel.
    assert "no text after" in p.lower()


# ---------------------------------------------------------------------------
# JIRA task prompts
# ---------------------------------------------------------------------------


def test_build_prompt_jira_includes_text_and_no_closing_keyword():
    spec = TaskSpec(repo="me/repo", text="[FOO-123] do a thing", source="jira")
    p = build_prompt(spec)
    assert "[FOO-123] do a thing" in p
    assert "me/repo" in p
    assert "Closes #" not in p
    assert "gh pr create" in p


def test_build_plan_prompt_jira_mentions_plan_not_pr():
    spec = TaskSpec(repo="me/repo", text="[FOO-123] do a thing", source="jira")
    p = build_plan_prompt(spec)
    assert "[FOO-123] do a thing" in p
    assert "plan" in p.lower()
    assert "gh pr create" not in p


def test_build_prompt_jira_label_says_from_jira():
    spec = TaskSpec(repo="me/repo", text="[FOO-123] do a thing", source="jira")
    p = build_prompt(spec)
    assert "JIRA" in p


def test_build_prompt_jira_slug_uses_bare_key():
    # At parse_task return time a jira spec.text IS the bare key (e.g. "FOO-123"); the slug is
    # that key lowercased, so the predicted branch is deterministic for the idempotency check.
    spec = TaskSpec(repo="me/repo", text="FOO-123", source="jira")
    assert task_slug(spec) == "foo-123"
    p = build_prompt(spec)
    assert "franky/foo-123" in p


# ---------------------------------------------------------------------------
# build_iterate_prompt (the `iterate` follow-up pass)
# ---------------------------------------------------------------------------


def _pr_spec():
    return TaskSpec(repo="me/repo", text="https://github.com/me/repo/pull/42", source="pr")


def test_iterate_prompt_checks_out_existing_branch_and_gathers_feedback():
    p = build_iterate_prompt(_pr_spec())
    assert "me/repo" in p
    assert "https://github.com/me/repo/pull/42" in p
    # It checks out the EXISTING branch and reads the feedback in-container via gh.
    assert "gh pr checkout" in p
    assert "gh pr view" in p
    assert "gh pr checks" in p
    assert "gh pr diff" in p
    assert "Do NOT create a new branch" in p


def test_iterate_prompt_forbids_force_push_new_pr_and_merge():
    p = build_iterate_prompt(_pr_spec())
    # Additive only: never force-push (incl. the "safe" force) and never rewrite history.
    # Assert the two prohibitions distinctly - "--force" alone is a substring of
    # "--force-with-lease", so it would pass even if the plain-force ban were deleted.
    assert "`git push --force`" in p
    assert "`git push --force-with-lease`" in p
    # Never open a new PR and never merge - a human reviews every change.
    assert "gh pr create" in p  # appears only inside the prohibition ("no gh pr create")
    assert p.count("gh pr create") == 1
    assert "open a new PR" in p
    assert "gh pr merge" in p
    assert "merge the PR" in p


def test_iterate_prompt_own_pr_guard_and_test_before_push():
    p = build_iterate_prompt(_pr_spec())
    # Own-PR guard inspects all three gh fields: head branch franky/*, not cross-repository,
    # and head-repo owner == the task owner. A typo in any field name silently weakens it.
    assert "headRefName" in p
    assert "isCrossRepository" in p
    assert "headRepositoryOwner" in p
    assert "franky/" in p
    # Tests green before pushing; professional deliverables; no issue-close keyword (no issue).
    assert "BEFORE pushing" in p
    assert "professional" in p.lower()
    assert "Closes #" not in p


# ---------------------------------------------------------------------------
# build_prompt prior_failures (issue #64 #5) + build_diagnose_prompt (#4)
# ---------------------------------------------------------------------------


def test_build_prompt_no_prior_failures_is_byte_identical():
    # The retry learning-signal must not perturb a normal (non-retry) build prompt at all.
    spec = TaskSpec(source="prose", text="add a flag", repo="me/repo")
    assert build_prompt(spec, prior_failures=()) == build_prompt(spec)


def test_build_prompt_injects_prior_failures():
    spec = TaskSpec(source="prose", text="add a flag", repo="me/repo")
    p = build_prompt(spec, prior_failures=["avoid the DinD race", "do not touch the proxy"])
    assert "earlier automated attempts" in p.lower()
    assert "1. avoid the DinD race" in p
    assert "2. do not touch the proxy" in p
    # The conventions still follow the injected block.
    assert "Conventions (follow exactly)" in p


def _diag_record(**over):
    rec = {
        "command": "build",
        "repo": "me/repo",
        "engine": "pi",
        "status": "no_pr",
        "exit_code": 7,
        "task": "add a flag",
    }
    rec.update(over)
    return rec


def test_diagnose_prompt_has_nonce_block_and_is_read_only():
    p = build_diagnose_prompt(_diag_record(), "some transcript text", "d1ag")
    assert "FRANKY_DIAG_d1ag_BEGIN" in p and "FRANKY_DIAG_d1ag_END" in p
    assert "DIAGNOSE MODE" in p
    assert "do NOT clone" in p.lower() or "do not clone" in p.lower()
    # The record metadata + transcript are injected.
    assert "me/repo" in p and "no_pr" in p and "some transcript text" in p
    # The required JSON keys are named for the agent.
    for key in ("root_cause", "category", "retryable", "retry_hint", "confidence"):
        assert key in p


def test_diagnose_prompt_tail_caps_a_long_transcript():
    marker_head = "HEAD_UNIQUE_MARKER"
    long = marker_head + ("x" * (DIAGNOSE_TRANSCRIPT_TAIL_CHARS + 5000)) + "TAIL_UNIQUE_MARKER"
    p = build_diagnose_prompt(_diag_record(), long, "d2")
    assert "TAIL_UNIQUE_MARKER" in p  # the tail (where failures live) is kept
    assert marker_head not in p  # the head is dropped
    assert "earlier output omitted" in p


def test_diagnose_prompt_short_transcript_not_truncated():
    p = build_diagnose_prompt(_diag_record(), "short", "d3")
    assert "earlier output omitted" not in p


def test_diagnose_prompt_includes_runtime_diagnostics_block():
    # issue #69: hard runtime signals, captured host-side just before teardown, are rendered as
    # their own block ahead of the transcript so the diagnose pass can reason over facts.
    record = _diag_record(
        diagnostics={
            "task_exit_code": 137,
            "oom_killed": True,
            "task_state": "exited",
            "dind_ready": False,
            "tmpfs_full": True,
            "egress_denied": [{"host": "evil.example.com", "count": 2}],
            "proxy_denied_count": 2,
        }
    )
    p = build_diagnose_prompt(record, "some transcript text", "d4")
    assert "Runtime diagnostics" in p
    assert "task_exit_code: 137" in p
    assert "oom_killed: True" in p
    assert "dind_ready: False" in p
    assert "tmpfs_full: True" in p
    assert "evil.example.com (x2)" in p


def test_diagnose_prompt_omits_block_when_no_diagnostics():
    p = build_diagnose_prompt(_diag_record(), "some transcript text", "d5")
    assert "Runtime diagnostics" not in p
