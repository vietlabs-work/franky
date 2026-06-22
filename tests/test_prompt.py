from franky.prompt import build_plan_prompt, build_prompt, load_persona
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


def test_issue_branch_hint_uses_repo_name_not_empty_default():
    # Regression: owner/repo has a slash, so a naive isalnum() filter collapsed every
    # issue branch hint to `franky/task`. The hint must reflect the repo.
    spec = TaskSpec(
        repo="octocat/hello", text="https://github.com/octocat/hello/issues/42", source="issue"
    )
    p = build_prompt(spec)
    assert "franky/octocat-hello" in p


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


def test_build_prompt_jira_slug_uses_text():
    # JIRA text starts with "[FOO-123] ..." so the slug should include those tokens.
    spec = TaskSpec(repo="me/repo", text="[FOO-123] fix the thing", source="jira")
    p = build_prompt(spec)
    # The slug is derived from the text, not the repo name.
    assert "franky/foo" in p.lower() or "franky/fix" in p.lower()
