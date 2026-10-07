# Build and review

`TASK` accepts a GitHub issue URL, `jira KEY`, prose, or `-` for stdin.

```bash
printf '%s\n' 'fix the flaky retry test' | franky build - --repo you/repo
franky build "split this parser" --repo you/repo --plan-first
franky build "fix the flaky retry test" --repo you/repo --retry 2
```

`--plan-first` requests a planning pass before the build. Use `--yes` for non-interactive approval.

Before each build attempt, Franky checks for an open PR on the predicted branch. `--force` skips this idempotency check.

## Build without publishing

```bash
franky build "fix the flaky retry test" --repo you/repo --no-publish --json
```

`build --no-publish` writes nothing to GitHub. The agent cuts `franky/<slug>` from the default-branch tip that Franky pins at start (`base_sha`), commits its work there, and does not push or open a PR. A host caller that holds the write credential takes the commits from the result. Franky never pushes them.

- Supply a read-only `GH_TOKEN`. Franky cannot enforce this: the token is the only thing that stops the agent from pushing.
- A start that cannot resolve the default-branch tip exits `8` before any container runs. `--thread` is refused with exit `2`. The open-PR check is skipped, and a retry never looks for a PR.
- After a clean exit, two networkless, read-only helpers read the stopped task's volumes during the entrypoint's session hold. The first reads the new commits as patch text, and the host scans them for the run's secret values. The second writes `base_sha..branch` as one git bundle to `<FRANKY_RUNS_DIR>/<job_id>.bundle` (0600). The host reads only the bundle header, and never runs git on the container's checkout.
- Status `branch_ready` (exit `0`) returns `branch`, `base_sha`, `head_sha`, and `bundle_path`. The bundle holds exactly `refs/heads/<branch>`. `no_changes`, `export_failed`, `timeout`, and `agent_error` exit `7` or `9`. `export_refused` exits `4`. [Automation](automation.md#build---no-publish) lists every field and status.
- Franky deletes the bundle on every outcome except `branch_ready`. Run-record pruning deletes it with its record, so a caller should take it promptly.
- `job resume` refuses these runs, and `job status` never offers a resume.

`iterate` only targets an allowlisted PR from a same-repository `franky/*` branch. Its prompt forbids force pushes, new PRs, and merges.

The `review-pr` prompt tells the agent to inspect only. The host publishes comments or change requests after it rechecks the PR head.

Two flags let the host do more, and both are off by default. Both need publishing, so `--no-publish` refuses them.

- `--allow-approve` lets the review be an `APPROVE`. The host picks it from the full finding list: no blocking, Major, or question finding is left open (resolved ones do not count, and a prior finding that a threaded re-review leaves out counts as open), no finding was dropped as malformed, and no check failed. Open nits do not block it, and the body never lists a prior nit. Otherwise the review is `COMMENT` or `REQUEST_CHANGES`. The caller must enforce branch protection, and that protection must dismiss stale approvals on push: a push can land between Franky's last head check and the POST.
- If an `APPROVE` gets HTTP 422 with inline comments, the host retries once as a body-only `APPROVE`. If an `APPROVE` is refused (a wrapper line starting `GitHub wrapper refused:` or `(HTTP 422)`), it posts a `COMMENT` instead. If the outcome is uncertain (timeout, other failure, unreadable reply), it first lists the PR's reviews (`gh api repos/OWNER/REPO/pulls/N/reviews?per_page=100 --paginate --slurp`, read twice a short time apart) for a review on the same commit with the body marker `<!-- franky-review:ID -->`, which only an `APPROVE` body carries. Found: it reports that review. Absent on both reads: it posts one `COMMENT`. If a read failed, or the head moved while an `APPROVE` may be on the PR, it posts nothing and ends with status `publish_uncertain` (non-zero exit, no `review_event`), so the caller must look at the PR before acting. The result `review_event` is the event actually posted.
- `--resolve-fixed` (needs `--thread`) resolves the review threads of findings the re-review marked `resolved`, after the review posts and only if the PR head did not move. A thread matches only if it is open, its first comment is by the publishing bot (the login in the POST response, without `[bot]`) on the finding's file, and it starts with a `**Label: title**` heading Franky wrote (any severity label). Zero or several matches skip the finding, and so does a new or open finding with the same file and title. If the publishing login is unknown, or more than 5 pages of threads exist, nothing is resolved. A failure is logged and never fails the run. The result `threads_resolved` counts the threads resolved.

Review layout. Each finding has `evidence` (file:line and a concrete trigger), `impact` and `fix`; an inline comment shows them as **Evidence**, **Why it matters** and **Suggestion** under the `**Label: title**` heading. The review body shows only a headline (`Review of SHA7: N findings (B blocking).` plus the summary). Everything else is collapsed: findings with no line, "Since the last review" (`Fixed` / `Still open`), "What I verified" and failed checks. A threaded re-review adds `Follows [the previous review](URL).`, using the review URL stored in the thread handoff. The optional `verified` list (up to 8 `{claim, evidence, status}`) records checked PR-description claims; a contradicted claim must also be a finding. A finding with only the legacy `body` still renders as before.

A nit is posted inline or not at all: it never goes in the review body, and it never takes an inline slot from a Major or Blocking finding. Findings outside the diff or without a line still go to the body.

If `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` are set on the host, `review-pr` also fetches up to 3 JIRA tickets linked in the PR title, branch, or body and gives their text to the reviewer as untrusted data. The credentials never enter the container. Only private repositories are fetched, and tickets with a security level are skipped. The `--json` result lists each ticket in `context_sources` (`ref`, `status`, and a `reason` when it was not included) without its text. The list is empty when no key was found, and in frozen `--at-sha` mode, which never fetches tickets.

Scope `GH_TOKEN` permissions because they are the enforced GitHub boundary for the autonomous container.

Use `--expected-head-sha` to reject a changed PR head. Use `--no-publish` for a read-only GitHub run.
`--no-publish --json` returns a `review_body` with the complete rendered findings.
Franky refuses an unpublished body above 8,000 characters or more than 10 findings.
The caller must protect and remove Franky's run files when the review uses private context.

Use `--instructions-file PATH` to keep review instructions out of command arguments.
This mode requires `--no-publish --json` and refuses threads and verbose output.
The file must be a regular UTF-8 file owned by the caller with mode `0600`.
It can contain at most 4,000 characters. The caller must remove it after the run.
Do not combine the file with inline instructions.
