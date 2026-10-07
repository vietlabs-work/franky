# Automation

`franky schema` is the discovery entry point for an AI agent. It emits one JSON object with:

- Every live command and subcommand.
- Ordered positional arguments, including requiredness, arity, type, and choices.
- Flags, types, defaults, and help.
- Per-command JSON output mappings and all result shapes.
- Stable exit-code meanings.

Commands that offer `--json` emit one JSON value on stdout. `jobs` emits an array unless `--stats` is set.

Interactive commands fail with exit `2` in a non-TTY. They do not wait forever. Stdin tasks need piped input.

`--max-duration SECONDS` limits agent runs. Most runs default to 1800 seconds. `job diagnose` defaults to 300 seconds.

| Exit | Meaning |
|-----:|---------|
| `0` | Success, including an existing open PR. |
| `2` | Usage error or required interactive input. |
| `3` | Invalid configuration. |
| `4` | Rejected task or repository, or a `--no-publish` export refused because a run secret is in the commits. |
| `5` | Missing or rejected authentication. |
| `6` | Docker, image, or required host tool unavailable. |
| `7` | Agent or result failure, including a stale review head, or a `--no-publish` run with no new commits or a failed export. |
| `8` | JIRA or network failure, including review publication. |
| `9` | Run timeout. |

## Build `--no-publish`

`franky build TASK --repo OWNER/REPO --no-publish --json` commits on a branch and exports it as a git bundle. It pushes nothing and opens no PR. See [Build without publishing](review.md#build-without-publishing).

Result fields, absent when they do not apply and never null (except `pr_url`):

| Field | Value |
|-------|-------|
| `status` | Always present. See the table below. |
| `pr_url` | Always `null`. |
| `branch` | The local ref in the bundle, `franky/<slug>`. |
| `base_sha` | The 40-hex default-branch tip the branch starts from. Present in every `--no-publish` result. |
| `head_sha` | The 40-hex tip of `branch`. Only with `branch_ready`. |
| `bundle_path` | Absolute path of the bundle (0600). Only with `branch_ready`. |

| Status | Exit | Meaning |
|--------|-----:|---------|
| `branch_ready` | `0` | The bundle holds `base_sha..branch` as `refs/heads/<branch>`. |
| `no_changes` | `7` | The branch has no commit beyond `base_sha`. A `--retry` may retry it. |
| `export_failed` | `7` | The helper or Docker failed, the branch is missing or not a descendant of `base_sha`, the output passed 256 MiB, the export ran out of its 45 s budget, or the bundle header failed verification. |
| `export_refused` | `4` | A secret value of the run is in the new commits. Franky deleted the bundle. |
| `timeout` | `9` | The run exceeded `--max-duration`. |
| `agent_error` | `7` | The engine exited non-zero. |
| `auth_error` | `5` | The engine login was refused, as for other builds. |

The result always reports `status`, so branch on it, not on the exit code alone.

Every run stores a small JSON record and a redacted transcript under `FRANKY_RUNS_DIR`. The default is `~/.franky/runs`.

`franky gh` is different from the autonomous container path. It exposes the full host `gh` surface and ignores `FRANKY_ALLOWED_REPOS`.

Scope `GH_TOKEN` to the exact repositories and permissions that callers may use. `FRANKY_GH_TIMEOUT` limits each host command.

## Track a run

With `--json`, every run prints one `{"event":"started","job_id":...,"status_command":...}` line on stderr when it starts, even with `--quiet`. It is always the first stderr line; profile notes follow it. It adds `"atlassian"` (`on`, `not_connected`, `expired`, `network`, `busy`, `not_private`) when the Atlassian gate ran, and `"atlassian_warning": "broad_scope"` when the grant includes write scopes. Under `--json` or `--quiet` Franky prints no Atlassian prose; without them the notes follow the first line. Stdout stays one result object. Poll with `franky job status JOB_ID --json`.

If the engine's own login is missing, expired, revoked, or refused, the command ends with the `auth_error` envelope (exit 5) and a `message` that starts with `no creds present for engine '<e>'`, `engine '<e>' requires `, or `engine '<e>' login was refused`. The last form appears only for `build`, `iterate`, `review-pr`, `plan`, and `job replay`, and only when no attempt of the run made an engine tool call, so rerunning the same request with another engine is safe. It proves no engine tool call, not that operator hooks or profile MCP servers did nothing. `job resume` and the build pass of `build --plan-first` keep `agent_error`, and so does a `build --retry` attempt after the first. The job record stays `agent_error`.

`state` is `active` (output in the last 120 s), `quiet` (alive, silent), `orphaned` (the owning process is gone), `unknown` (liveness cannot be confirmed), or `finished`. `next` gives one `action` (`wait`, `check`, `kill`, `resume`, `rerun`, `inspect`, `done`), its `command`, `retry_safe`, and `check_after`. Do not rerun unless `retry_safe` is true. `unknown` is never proof of death.

A run also keeps `<job_id>.progress.json` beside its record: phase, attempt, last tool name (never arguments), and last output time. It is host-only, never exported, and pruned with the record.
