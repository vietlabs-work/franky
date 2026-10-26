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

`build --no-publish` writes nothing to GitHub. The agent clones the repository into `/work/<repo-name>`, cuts `franky/<slug>` from the default-branch tip that Franky pins at start (`base_sha`), commits its work there, and does not push or open a PR. A host caller that holds the write credential takes the commits from the result. Franky never pushes them.

- Supply a read-only `GH_TOKEN`. Franky cannot enforce this: the token is the only thing that stops the agent from pushing.
- A start that cannot resolve the default-branch tip exits `8` before any container runs. `--thread` is refused with exit `2`. The open-PR check is skipped, and a retry never looks for a PR.
- After a clean exit, two networkless, read-only helpers read the task's `/work` volume during the entrypoint's session hold. They mount only that volume, read-only, never HOME, `/tmp`, or the Codex login volume. Git runs with replace refs and every global or system config off. The first helper prints the branch tip and then the raw objects that the bundle will carry (commits with all their headers, trees, blobs). The host scans them for the run's credential values: tokens, keys, and the Basic and Atlassian forms, not the JIRA email. The second helper writes `base_sha..branch` as one git bundle to `<FRANKY_RUNS_DIR>/<job_id>.bundle` (0600), and refuses if the branch moved since the scan. The host reads only the bundle header, which must name that same tip. It never runs git on the container's checkout.
- The scan guards against accidental leaks of exact values only. An encoded or split value passes, so the caller that pushes the bundle must scan for credential patterns.
- Both helpers, their start-up, and their removal share 30 s of the 60 s hold. The bundle step is skipped, and the result is `export_failed`, when less than 10 s remain.
- Status `branch_ready` (exit `0`) returns `branch`, `base_sha`, `head_sha`, and `bundle_path`. The bundle holds exactly `refs/heads/<branch>`. `no_changes`, `export_failed`, `timeout`, and `agent_error` exit `7` or `9`. Helper output never reaches the result; it goes to the run log only. `export_refused` exits `4`. [Automation](automation.md#build---no-publish) lists every field and status.
- Franky deletes the bundle on every outcome except `branch_ready`. Run-record pruning deletes it with its record, so a caller should take it promptly.
- `job resume` and `job replay` refuse these runs, and `job status` never offers a resume.

`iterate` only targets an allowlisted PR from a same-repository `franky/*` branch. Its prompt forbids force pushes, new PRs, and merges.

The `review-pr` prompt tells the agent to inspect only. The host publishes comments or change requests after it rechecks the PR head.

Three flags let the host do more, and all are off by default. All need publishing, so `--no-publish` refuses them.

- `--allow-approve` lets the review be an `APPROVE`. The host picks it from the full finding list: no blocking, Major, or question finding is left open (resolved ones and verified accepted risks do not count, and a prior finding that a threaded re-review leaves out counts as open), no finding was dropped as malformed, and no check failed. Open nits do not block it, and the body never lists a prior nit. Otherwise the review is `COMMENT` or `REQUEST_CHANGES`. An accepted risk is a prior finding that a threaded re-review reports as `accepted` with `accepted_by` (a GitHub login). The host keeps it only when that person is not the PR author or a bot, the finding matches exactly one prior handoff finding, and the person's latest review (`APPROVED`, `CHANGES_REQUESTED` or `DISMISSED`) is an `APPROVED` on the reviewed head, not by Franky, with `author_association` `OWNER`, `MEMBER` or `COLLABORATOR`. An App token sees a private org member as `CONTRIBUTOR` or `NONE`: for any such association the host reads `repos/R/collaborators/LOGIN/permission` (once per login) and accepts only `admin`, `maintain` or `write`; a failed read fails closed. That review must be submitted after the review that posted the finding (compared at full precision, strictly after). An unreadable `submitted_at` on any of the person's reviews, or a same-time tie that review ids cannot break, fails closed. If any check fails, or the review list cannot be read, the finding stays `open`. A verified accepted finding above nit adds one body line: `Accepted risk: N finding(s) above nit accepted by @login (approval on this commit).` A review that would be an `APPROVE` is posted as a `COMMENT` that says `code-clean, human sign-off required` when the PR changes a path matched by the reviewed repo's `.github/human-signoff-paths` (read at the PR base commit), changes that file itself, or when that file or the PR file list cannot be read. A missing file means no floor. Globs are gitignore-style: `*`, `?`, `**`, a leading `/` anchors, a trailing `/` marks a directory, `!` negates (the last match wins) and `#` starts a comment, with no `[...]` classes. The caller must enforce branch protection, and that protection must dismiss stale approvals on push: a push can land between Franky's last head check and the POST.
- If an `APPROVE` gets HTTP 422 with inline comments, the host retries once as a body-only `APPROVE`. If an `APPROVE` is refused (a wrapper line starting `GitHub wrapper refused:` or `(HTTP 422)`), it posts a `COMMENT` instead. If the outcome is uncertain (timeout, other failure, unreadable reply), it first lists the PR's reviews (`gh api repos/OWNER/REPO/pulls/N/reviews?per_page=100 --paginate --slurp`, read twice a short time apart) for a review on the same commit with the body marker `<!-- bot-review:ID -->`, which only an `APPROVE` body carries. Found: it reports that review. Absent on both reads: it posts one `COMMENT`. If a read failed, or the head moved while an `APPROVE` may be on the PR, it posts nothing and ends with status `publish_uncertain` (non-zero exit, no `review_event`), so the caller must look at the PR before acting. The result `review_event` is the event actually posted.
- `--resolve-fixed` (needs `--thread`) resolves the review threads of findings the re-review marked `resolved`, after the review posts and only if the PR head did not move. A thread matches only if it is open, its first comment is by the publishing bot (the login in the POST response, without `[bot]`), and it starts with a `**Label: title**` heading Franky wrote (any severity label). A finding picks the matching threads on its own file first, then falls back to the bot's open threads anywhere by title, because the model can report a prior finding under another file. Zero or several matches skip the finding, and so does a new or open finding with the same file and title (same title in the fallback). A thread with any open finding is never resolved. If the publishing login is unknown, or more than 5 pages of threads exist, nothing is resolved. A failure is logged and never fails the run. The result `threads_resolved` counts the threads resolved.
- `--reply-prior` (needs `--thread`) posts one reply `Still open at SHA7: evidence` (evidence capped at 600 characters) on each matched thread whose finding is `open`, using the same match and the same single thread read as `--resolve-fixed`. It replies only when the thread has no reply yet or a human posted last, never after a bot's reply (ours or another), and never when the thread data is malformed. A failure is logged and never fails the run. The result `threads_replied` counts the replies.

Review layout. Each finding has `evidence` (file:line and a concrete trigger), `impact` and `fix`; an inline comment shows them as **Evidence**, **Why it matters** and **Suggestion** under the `**Label: title**` heading. The review body shows only a headline (`Review of SHA7: N findings (B blocking, M major, Q question).` plus the summary; a zero major or question count is left out). Everything else is collapsed: findings with no line, "Since the last review" (`Fixed` / `Still open` / `Accepted risk (by @login)`), "What I verified" and failed checks. A threaded re-review adds `Follows [the previous review](URL).`, using the review URL stored in the thread handoff. The optional `verified` list (up to 8 `{claim, evidence, status}`) records checked PR-description claims; a contradicted claim must also be a finding. A finding with only the legacy `body` still renders as before. The body ends with `<sub>Automated review by Franky</sub>`. Set `FRANKY_REVIEW_FOOTER` to post your own line instead, up to 500 characters and posted as is, or set it empty to post no footer. A longer value stops the run at start with a config error (exit 3).

A nit is posted inline or not at all: it never goes in the review body, and it never takes an inline slot from a Major or Blocking finding. Findings outside the diff or without a line still go to the body.

If `JIRA_CLI` or all of `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` are set on the host (see [JIRA tickets](configuration.md#jira-tickets)), `review-pr` also fetches up to 3 JIRA tickets linked in the PR title, branch, or body and gives their text to the reviewer as untrusted data. The credentials never enter the container. Only private repositories are fetched, and tickets with a security level are skipped. The `--json` result lists each ticket in `context_sources` (`ref`, `status`, and a `reason` when it was not included) without its text. Each included ticket also carries its 5 newest comments (800 characters each; comments restricted to a group or role are skipped), and its Scope, Out of Scope and Acceptance criteria sections move to the top before the 4,000-character cut. The list is empty when no key was found, and in frozen `--at-sha` mode, which never fetches tickets.

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

## Skill mode

`review-pr --skill NAME --skill-policy FILE` runs a team skill as the review method instead of Franky's own. `NAME` is a skill directory of the profile's `team` setup (`skills/NAME/SKILL.md`). The container holds a read token, so the skill cannot post. It puts the exact payloads in the sentinel; the host validates them against the policy and posts through the same wrapper shapes as any review. Franky knows no skill by name: everything skill-specific comes from the policy file.

```bash
franky review-pr --skill NAME --skill-policy ~/policy.json --thread --expected-head-sha SHA --json -- PR_URL "focus text"
```

- **Caller duty:** in skill mode the container `GH_TOKEN` MUST be read-only. Every write goes through the caller's wrapper; Franky cannot enforce this, the token is the only thing that stops the agent from posting.
- Needs a profile with a `team` setup that holds the skill as `skills/NAME/SKILL.md` with a readable header (else exit `3`). The policy file must not sit inside a profile, setup or bundle input (a setup root or a listed profile file), else exit `3`. `--skill` and `--skill-policy` come together, `--skill` is refused with `--allow-approve` and with `--at-sha`, and the event is always `COMMENT`. `--no-publish` validates and posts nothing. `--thread` resumes the engine session; the prompt carries no thread handoff block, because the skill reads its own state from the PR.
- The policy file is read on the host and never mounted. It must be a regular file owned by the caller, not writable by others, at most 16 KiB. A refusal names the key, never a value.
- Both `claude` and `codex` see the team setup. The skill is installed natively (`~/.claude/skills/NAME` or `~/.codex/skills/NAME`), and the prompt names the engine-neutral copy `<team dir>/skills/NAME/SKILL.md`. The team directory is HOME-relative like every setup file ([profiles](profiles.md#team-setup)). `opencode` and `pi` get the pointer only.
- The prompt has one short fixed block: the skill and its `SKILL.md` path, `SKILL_DIR`, `CLAUDE_PLUGIN_ROOT` for plugin `P` (`<team dir>/tool/P`), "scripts are data: run them with `bash` or `python3`", "GitHub writes fail here: put the exact payloads in the sentinel", the bot login, and the payload shapes. The container gets `GH_LOGIN` with the login from the policy (not redacted, it is not a secret).

### Policy file

A JSON object. Unknown, missing or mistyped keys refuse the run (exit `2`).

| Key | Type | Meaning |
|-----|------|---------|
| `ledger_schema` | string | The ledger object's `schema` must equal it. |
| `ledger_summary_marker` | string | The single `<summary>` of the ledger `<details>` block must contain it. |
| `max_body_chars` | int, 1000 to 65000 | Cap on the final review body. |
| `max_comments` | int, 0 to 50 | Cap on inline comments. |
| `max_comment_chars` | int, 100 to 65000 | Cap on each final inline comment. |
| `bot_login` | string, must end in `[bot]` | The posting identity, the full `slug[bot]` login. The skill uses it instead of `gh api user`. The host matches REST authors (`user.type` is `Bot` and the login equals it, case-insensitive, nothing stripped) and GraphQL thread authors (`__typename` is `Bot` and the login equals it without `[bot]`, which is how GraphQL reports it). |
| `allow_ledger_update` | bool, default `false` | Allow the `ledger_update` shape below. |
| `blocking_field` | string, default `sev` | The finding field that marks a blocking finding. |
| `blocking_values` | 1 to 10 strings, default `["blocking"]` | The values of that field that count as blocking. |
| `open_statuses` | 1 to 10 strings, default `["open"]` | Finding statuses that count as open. A missing `status` is open. |
| `cleared_statuses` | 1 to 10 strings, default `["fixed","resolved","wontfix"]` | Finding statuses that count as cleared. Must not overlap `open_statuses`. |

The ledger convention Franky reads is small: an object with `schema`, `reviewed_sha`, `state` (`clean` or `findings`), an optional integer `round` (at least 1), and `findings[]` of objects with a string `id`, a `loc` (`path:line`) and an optional `status`. A missing `status` means open. A status in neither `open_statuses` nor `cleared_statuses` refuses the ledger (`skill_ledger_findings`). Status comparison is exact, so case matters. Finding `id` must be a non-empty unique string and `loc` must match `^[^\s`<>]+:[1-9][0-9]*$`, else `skill_ledger_findings`.

### Sentinel payloads

The agent ends its output with one `FRANKY_REVIEW_<nonce>_BEGIN{...}FRANKY_REVIEW_<nonce>_END` block, as for any review. One of two shapes:

```json
{"kind": "review", "decision": "first-pass|delta|second-opinion",
 "ledger": {"schema": "...", "round": 1, "reviewed_sha": "<head>", "state": "clean|findings", "findings": []},
 "review": {"body": "...", "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "..."}]},
 "resolve": ["<thread node id>"],
 "replies": [{"comment_id": 123, "body": "..."}],
 "ledger_update": null}
```

```json
{"kind": "terminal", "decision": "done|skip|halt|author-mode|not-reviewable",
 "reason": "short text", "prior_ledger_review_id": null}
```

A `terminal` payload posts nothing and ends `review_complete` (exit `0`). A `review` payload may carry `review: null` when the decision has nothing new; with `ledger_update` it then edits one earlier review (a quiet round), and without it nothing is posted. `side` defaults to `RIGHT`. `review` and `ledger_update` together are refused.

### Validation

The host refuses the whole payload and posts nothing on any miss. The result is status `skill_payload_invalid` (exit `7`) and the reason holds one fixed code, never payload text:

| Code | Refused because |
|------|-----------------|
| `skill_payload_missing`, `skill_payload_shape` | No sentinel, or a wrong type, decision, comment shape or id. A comment `path` must be a repo-relative name (letters, digits and `._@+=~,/-`, no leading `/`, no `..` segment) that redaction leaves unchanged. |
| `skill_ledger_schema`, `skill_ledger_sha`, `skill_ledger_round`, `skill_ledger_findings` | `schema` is not `ledger_schema`, `reviewed_sha` is not the pinned head, `round` is not an int of 1 or more, or `findings` is not a list of objects with unique ids, a valid `loc` and a known status. |
| `skill_ledger_state` | `state` is not `clean` or `findings`, or disagrees with the findings (clean means zero open findings, findings means at least one). |
| `skill_body_block` | The body does not hold exactly one `<details>` block whose `<summary>` contains `ledger_summary_marker` and whose json fence parses to an object equal to `ledger`. The check runs on the agent's text and again on the final text. |
| `skill_body_cap`, `skill_comment_cap` | A cap is exceeded. Caps are measured on the FINAL text, after redaction, `<!--` neutralization and the footer. |
| `skill_update_refused`, `skill_update_with_review`, `skill_update_target` | `ledger_update` is not allowed by the policy, is combined with a review, or does not name one of the bot's own ledger reviews on this PR in state `COMMENTED` with none of the approve markers in its body. |

Every `<!--` in a body, comment, reply or update becomes `&lt;!--`, so agent text cannot forge the marker that `--allow-approve` reconciliation trusts. Text is redacted with the run's secrets.

### What the host posts

All reads use the read token. Every write goes through the caller's `gh` wrapper, so the caller allowlists these shapes byte for byte (`<repo>`, `<N>`, `<id>`, `<cid>`, `<body>` are values):

| Step | argv | stdin |
|------|------|-------|
| New review | `gh api -X POST repos/<repo>/pulls/<N>/reviews --input -` | `{"event": "COMMENT", "body": ..., "commit_id": "<head>", "comments": [...]}` |
| Ledger update (new, only with `allow_ledger_update`) | `gh api -X PUT repos/<repo>/pulls/<N>/reviews/<id> --input -` | `{"body": ...}` |
| Resolve (`--resolve-fixed`) | `gh api graphql -f query=<RESOLVE_MUTATION> -F id=<node id>` | none |
| Reply (`--reply-prior`) | `gh api -X POST repos/<repo>/pulls/<N>/comments/<cid>/replies -f body=<body>` | none |

Reads: `repos/<repo>/pulls/<N>/reviews?per_page=100 --paginate --slurp` (existing), and the review-thread GraphQL read with the extra thread fields `line originalLine` (`SKILL_THREADS_QUERY` in `reviewpr.py`, the existing `THREADS_QUERY` with those two fields added; allowlist it too). A reply body is at most 2,000 characters.

- The head is re-checked immediately before each review write (the POST, the 422 retry, the PUT), once before the thread batch, and every 10 thread writes. A moved head posts nothing more (`publish_blocked_stale_head`).
- After the POST the response `user` must be the bot (type `Bot`, exact login); after the PUT the response `user`, if present, must be. Otherwise resolve and reply are skipped with note `identity_mismatch`. Resolve and reply also need a successful POST or PUT in the same run, else note `resolve_no_ledger`.
- If GitHub rejects the inline anchors (HTTP 422), the host re-checks the head and posts once more with the comments folded into the body, if the body still fits its cap (`skill_notes` gets `anchors_folded:1`).
- `ledger_update` replaces the body of the one review named by `review_id`. That review must be by `bot_login`, on this PR, and already hold a ledger block of `ledger_schema`; the new body must pass the same block check and carry the same ledger.
- `--resolve-fixed` and `--reply-prior` (both need `--thread`) act on the payload's own lists, and only when set. A thread is resolved only when it is open, its first comment is by the bot, and its anchor (`path` and `line`, or `originalLine` once outdated) equals the `loc` of exactly one open finding of the PRIOR ledger (the newest ledger review by the bot, read before the write) and that finding is present in the new ledger with a cleared status. No match or ambiguity drops the id. A reply is posted only on the bot's own open thread, and only when Franky's existing reply rule holds: nobody replied yet, or a human posted last. One reply per thread.
- An id or reply that fails a check is dropped, not posted, and counted in the result's `skill_notes` (`resolve_not_ours`, `resolve_not_cleared`, `resolve_no_ledger`, `identity_mismatch`, `reply_not_due`, `reply_refused`, `threads_unreadable`).

### Result

The normal review fields appear (`status`, `reviewed_sha`, `review_url`, `review_id`, `review_event`, `threads_resolved`, `threads_replied`). `review_event` is `COMMENT` or absent. A result adds:

```json
"ledger": {"kind": "review|terminal", "decision": "delta", "state": "clean|findings|null",
           "round": 2, "reviewed_sha": "<head>", "open": 3, "blocking": 1, "review_url": "..."}
```

`open` counts open findings and `blocking` counts open findings whose `blocking_field` is in `blocking_values`. A terminal `prior_ledger_review_id` is kept only when it names one of the bot's ledger reviews on the PR. A terminal round reports `state`, `round`, `open` and `blocking` as `null`, plus `reason`; its `review_url` is the prior ledger review it named, if any. `skill_notes` lists `code:count` strings when something was dropped. `franky schema` advertises `--skill` and `--skill-policy` on `review-pr`, so a caller can probe for support.
