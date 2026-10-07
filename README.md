# Franky

Franky runs a coding agent inside a fresh, hardened Docker container. It clones a trusted repository, makes a change, and opens a pull request.

```bash
franky build https://github.com/you/repo/issues/42
franky build jira FOO-123 --repo you/repo
franky build "add JSON output" --repo you/repo
franky iterate https://github.com/you/repo/pull/42
```

Franky supports `pi`, `claude`, `codex`, and `opencode`. The default engine is `pi`.

## Install

Install the `franky-agent` package. The installed command is `franky`.

```bash
uv tool install franky-agent
# Or:
pipx install franky-agent
# Or:
pip install franky-agent
```

Docker must be available. Franky pulls public, version-pinned images from GHCR on the first run.

Native AppArmor hosts also need the packaged task profile. Install it after each Franky update.

```bash
(
  set -e
  profile="$(mktemp)"
  trap 'rm -f "$profile"' EXIT
  franky apparmor-profile > "$profile"
  sudo /usr/sbin/apparmor_parser -K -r "$profile"
  sudo /usr/bin/install -m 0644 "$profile" /etc/apparmor.d/franky-task
)
```

Franky detects AppArmor through Docker. Invalid daemon security data stops the run before Franky creates resources.

## Configure

Run the setup wizard:

```bash
franky config init
```

It writes `~/.franky/config` with mode `0600`. Process environment values override file values.

Every build needs:

- `FRANKY_ALLOWED_REPOS`: Comma-separated `owner/repo` patterns. An empty value denies every repository.
- `GH_TOKEN`: A narrow token with content and pull-request access for the allowed repositories.
- Credentials for the selected engine.

JIRA inputs also need `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`; these stay on the host for the ticket fetch.

To give every task on a private repository read-only JIRA and Confluence tools inside the container, run `franky connect jira`. It opens a browser login (OAuth with PKCE and a loopback redirect; no Atlassian admin step). On a headless host run `franky connect jira --no-browser`, open the printed URL elsewhere, and paste the redirect URL back. `--status` shows the connection and `--disconnect` revokes it. The login stays in `~/.franky/atlassian-jira.json` (0600) on the host. For the Claude and Codex engines Franky then adds the Atlassian MCP server (`https://mcp.atlassian.com/v2/mcp`): the host refreshes a short-lived access token and only that token enters the container, as `FRANKY_ATLASSIAN_MCP_HEADER`. Franky requests read and search scopes only, and the write tools are denied in the engine config (Claude deny patterns, a Codex read-tool allowlist). If Atlassian grants broader scopes, read-only is not enforced by Atlassian; `--status` shows the granted scopes. A missing, expired or revoked connection turns the tools off with one hint line, and each run with the tools on prints when its token expires. A very long run can outlive that token; the agent then falls back to the ticket text. The egress allowlist gains `mcp.atlassian.com` only when the tools are on. Pi and OpenCode get no Atlassian tools.

```bash
franky config set FRANKY_ALLOWED_REPOS
franky config set GH_TOKEN
franky config list
franky config path
```

See [`config.example.toml`](config.example.toml) for common settings.

## Engines

Select an engine with `--engine`, then `FRANKY_ENGINE`, or use the default `pi`.

| Engine | Authentication | Notes |
|--------|----------------|-------|
| `pi` | One supported provider variable | Vendor-neutral BYOK client |
| `claude` | `franky auth login claude` or `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code subscription token |
| `codex` | `franky auth login codex` or `CODEX_API_KEY` | ChatGPT subscription or API key |
| `opencode` | `FRANKY_MODEL` plus its matching key | Moonshot or OpenRouter |

Configure OpenCode for one provider:

```bash
franky config set FRANKY_ENGINE opencode
franky config set FRANKY_MODEL moonshotai/kimi-k3
franky config set MOONSHOT_API_KEY
```

OpenRouter uses `FRANKY_MODEL=openrouter/<model-id>` with `OPENROUTER_API_KEY`.

`FRANKY_MODEL` is optional for `claude` and `codex`: unset, the engine uses its own default. When it is set, a run refuses a name of the wrong shape before it starts a container. `claude` takes an alias (`best`, `fable`, `opus`, `sonnet`, `haiku`, `opusplan`, each with an optional `[1m]`) or a `claude-*` id, for example `claude-opus-5-5`. `codex` takes a `gpt-*`, `o<N>` or `codex-*` id. The check is on the shape only, so a well-formed id that does not exist still fails inside the engine.

Codex subscription authentication stays in a Docker named volume:

```bash
franky auth login codex
franky auth status codex
franky auth logout codex
```

`franky auth login claude` runs `claude setup-token` when Claude Code is on the PATH, then saves the token you paste as `CLAUDE_CODE_OAUTH_TOKEN` in the config file. `auth status claude` and `auth logout claude` check and remove it.

Set `FRANKY_CODEX_AUTH_VOLUME` to isolate logins between Franky instances. `CODEX_API_KEY` takes precedence when both methods exist.

Release runs use an engine-specific image. The unsuffixed image contains all engines for cross-engine tasks.

| Setting | Purpose |
|---------|---------|
| `FRANKY_IMAGE` | Override the task image. |
| `FRANKY_PROXY_IMAGE` | Override the proxy image. |
| `FRANKY_GHCR_REPO` | Override the GHCR namespace. |

## Command catalog

Run `franky COMMAND --help` for flags and examples. Run `franky schema` for the complete machine-readable contract.

### Main commands

| Command | Purpose |
|---------|---------|
| `franky build TASK` | Implement a GitHub issue, JIRA task, prose task, or stdin task. Open one PR. Add `--thread` to hand the engine session to the PR's author thread. |
| `franky iterate PR_URL` | Address review or CI feedback with additive commits on Franky's existing PR. Add `--thread` to continue the PR's author session. |
| `franky review-pr PR_URL [INSTRUCTIONS]` | Review a PR. Publish findings unless `--no-publish` is set. Add `--thread` to continue one stored review session per PR. |
| `franky plan TASK` | Request a read-only scope assessment and return PR-sized tasks. |
| `franky jobs` | List runs. Add `--stats` for success, hang, duration, and cost data. |
| `franky gh ARGS...` | Run the host `gh` CLI with Franky's token. |
| `franky schema` | Print all commands, arguments, flags, result shapes, and exit codes as JSON. |
| `franky version` | Print version, install source, engine, and selected image. |
| `franky update` | Install the latest release through the detected installer. |
| `franky apparmor-profile` | Print the native Linux task profile. |

### Job commands

| Command | Purpose |
|---------|---------|
| `franky job status JOB_ID` | Show the state, the one next step, the record, and runtime diagnostics. |
| `franky job logs JOB_ID` | Print the redacted transcript. |
| `franky job kill JOB_ID` | Stop a run and remove its task, proxy, network, and volumes. A `--thread` build also keeps its engine session. |
| `franky job export JOB_ID` | Export the record and redacted transcript as a portable archive. |
| `franky job diagnose JOB_ID` | Request a read-only failure analysis. |
| `franky job replay JOB_ID` | Reproduce a build from saved inputs in a fresh container. |
| `franky job resume JOB_ID` | Continue a stopped run from its saved workspace, and for a `--thread` build also its engine session. |
| `franky job attach JOB_ID` | Send one correction to a running steerable engine. |

`replay` starts from saved inputs. `resume` restores `/work`, plus the engine session when it can (see [Author threads](#author-threads)). Use `replay --open-pr` only when the reproduced run should open a PR.

`attach` supports `pi`, `claude`, and `codex`. OpenCode does not support steering.

### Track a run

With `--json`, every run prints one `{"event":"started","job_id":...,"status_command":...}` line on stderr when it starts, even with `--quiet`. It is always the first stderr line; profile notes follow it. It adds `"atlassian"` (`on`, `not_connected`, `expired`, `network`, `busy`, `not_private`) when the Atlassian gate ran, and `"atlassian_warning": "broad_scope"` when the grant includes write scopes. Under `--json` or `--quiet` Franky prints no Atlassian prose; without them the notes follow the first line. Stdout stays one result object. Poll with `franky job status JOB_ID --json`.

If the engine's own login is missing, expired, revoked, or refused, the command ends with the `auth_error` envelope (exit 5) and a `message` that starts with `no creds present for engine '<e>'`, `engine '<e>' requires `, or `engine '<e>' login was refused`. The last form appears only for `build`, `iterate`, `review-pr`, `plan`, and `job replay`, and only when no attempt of the run made an engine tool call, so rerunning the same request with another engine is safe. It proves no engine tool call, not that operator hooks or profile MCP servers did nothing. `job resume` and the build pass of `build --plan-first` keep `agent_error`, and so does a `build --retry` attempt after the first. The job record stays `agent_error`.

`state` is `active` (output in the last 120 s), `quiet` (alive, silent), `orphaned` (the owning process is gone), `unknown` (liveness cannot be confirmed), or `finished`. `next` gives one `action` (`wait`, `check`, `kill`, `resume`, `rerun`, `inspect`, `done`), its `command`, `retry_safe`, and `check_after`. Do not rerun unless `retry_safe` is true. `unknown` is never proof of death.

A run also keeps `<job_id>.progress.json` beside its record: phase, attempt, last tool name (never arguments), and last output time. It is host-only, never exported, and pruned with the record.

### Thread commands

| Command | Purpose |
|---------|---------|
| `franky threads list` | List stored review and author threads with engine, last head, and session size. |
| `franky threads prune` | Remove orphaned, idle (`--older-than`, default 30d), and optionally closed-PR (`--closed`) threads. Cap stored sessions (`--max-bytes`, default 2G). |
| `franky threads purge OWNER/REPO#N` | Delete one PR's threads. `--role reviewer` or `--role author` limits it to one role. `--all` deletes every thread. |

`prune --repo OWNER/REPO` applies every pass to one repository, so a token scoped to that repository covers `--closed`. It skips the global disk cap and reports `disk_skipped`.

### Authentication, configuration, and profile commands

| Command | Purpose |
|---------|---------|
| `franky auth login codex` | Create persistent Codex subscription authentication. |
| `franky auth status codex` | Check the stored Codex authentication. |
| `franky auth logout codex` | Delete the Codex authentication volume. |
| `franky auth login claude` | Save a Claude Code subscription token to the config file. |
| `franky auth status claude` | Check that a Claude token is set. |
| `franky auth logout claude` | Remove the Claude token from the config file. |
| `franky config init` | Create or update the user configuration. |
| `franky config set KEY [VALUE]` | Set one configuration value. Secret values use a hidden prompt, or `--stdin` to read one piped line. |
| `franky config list` | List configuration values with secrets masked. |
| `franky config path` | Print the configuration path. |
| `franky profile init` | Discover setups and merge profile selections. |
| `franky profile check` | Validate files, MCP policy, limits, and secrets. |
| `franky profile show` | Print the profile and expanded file list. |
| `franky profile path` | Print the resolved profile path. |

## Build and review behavior

`TASK` accepts a GitHub issue URL, `jira KEY`, prose, or `-` for stdin.

```bash
printf '%s\n' 'fix the flaky retry test' | franky build - --repo you/repo
franky build "split this parser" --repo you/repo --plan-first
franky build "fix the flaky retry test" --repo you/repo --retry 2
```

`--plan-first` requests a planning pass before the build. Use `--yes` for non-interactive approval.

Before each build attempt, Franky checks for an open PR on the predicted branch. `--force` skips this idempotency check.

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

### Review threads

`review-pr --thread` keeps one review session per PR and role under `~/.franky/threads` (override: `FRANKY_THREADS_DIR`). The next run continues it:

- With `claude`, Franky resumes the same session (`--resume`). Its id is saved before the container starts, so a crash leaves a known id.
- Other engines, and a session that is stale, changed, or rejected, start a new session seeded with the stored findings. A thread never blocks the review.
- If a resumed session fails, Franky drops it and retries once in the same run with a seeded session. The result reports `session: seeded` with `session_reason: resume_failed`.
- A new session is stored only after a clean run with parsed findings. A failed run keeps the previous session and findings.
- The prompt tells the agent to verify each prior finding (`open` or `resolved`), review the delta since the last reviewed head, and report new findings on unchanged code only at blocking severity.
- A second run for the same PR while one is active exits 4 (`thread_busy`).
- Thread ids are lowercase, so `Me/Repo` and `me/repo` share one thread.
- Without `--thread`, Franky ignores any finding `status` the agent reports.
- Re-reviews should report each prior finding under its exact prior title; `--resolve-fixed` matches threads by it.
- `--rubric-version` pins a rubric label. Changing it, the engine, or the model starts a new session.

Sessions move by stream-in (tar over `docker exec`) and copy-out (a `docker cp ... -` tar stream, capped at 64 MiB) only. No volume or mount is added. Only the session file and its side directory move. Claude's project memory never moves between runs. Stored sessions contain regular files only, are scrubbed of known secret values and token patterns, and are refused on any finding. They stay host-only (0700/0600) and are never exported. The stored findings are redacted the same way. `threads prune` and `threads purge` delete them.

A resumed session replays earlier content from the same PR to the read-only reviewer. It never crosses PRs or roles. Resume stops after 14 days without a successful review, or above 64 MiB of session files.

### Author threads

`build --thread` and `iterate --thread` keep one author session per PR (role `author`). The reviewer and author roles never share a session.

- `build --thread` with `claude` saves a session id in the job record before the container starts. When the PR exists, the session becomes the PR's author thread (`thread.id`). The result reports `session_reason: new_thread`.
- Before the bind, the host confirms that the open PR on the build's branch is the PR the agent reported, and records the session sidecar and PR URL in the job record. If either step fails, the bind stays pending (`bind_pending`).
- The bind waits at most 60 seconds for a busy author thread. After that it stays pending; the job record keeps the session sidecar and the PR URL. It never overwrites a stored author session (`thread_exists`). A session that is not stored is deleted unless `job resume` can still use it (a workspace snapshot exists).
- `iterate --thread` resumes the author session with `claude`. Other engines, and a session that is stale, changed, or too large, start a new session seeded with the stored PR head. The prompt fences that context as untrusted data and says that its own conventions override the earlier conversation.
- If `iterate --thread` finds no author thread, or one whose bind never finished, it first binds the newest unbound session sidecar of a `build --thread` for that PR whose branch the host confirms. A sidecar that fails to extract or verify is skipped for the next one. This recovers a build that stopped between its record write and the end of its bind.
- An author run retries only when the engine refuses the stored session at startup (`resume_failed`, one seeded retry). Any other failure never re-runs the pass, because it can already have pushed.
- An author session resumes at most 10 times in a row. The next run starts a seeded session (`resume_cap`) and resets the count.
- A second author run for the same PR while one is active exits 4 (`thread_busy`).

A timed-out `build --thread` copies its session out with its workspace. `job kill` copies it out before it removes the container, then scrubs it with the loaded configuration's secrets, the profile's MCP credentials, and every secret key in the environment. If the configuration or the profile cannot load, `job kill` captures no session. Each session goes to a `<job_id>.session.tar.gz` sidecar beside the run record: regular files only, scrubbed, fail-closed verified, host-only (0600), and never exported. Run-record pruning deletes it with its record.

`job resume` of such a run restores `/work` and the session in one container, and resumes the session (V2). Before it streams the session in, it verifies it again, with the profile's MCP credentials too. It falls back to a workspace-only resume (V1) with `thread.session: fresh` plus the reason: `session_missing`, `session_corrupt`, `engine_changed`, `model_changed`, `no_native_resume`, or `verify_failed` (also when the profile cannot load). The fallback also prints one stderr line, except under `--json` or `--quiet`, where the JSON reason carries it. If the engine refuses the restored session at startup, Franky retries once as V1 in a new container with a new session id (`resume_failed`). A resumed run that opens the PR binds its session like `build --thread`.

`threads purge OWNER/REPO#N --role author` deletes only the author thread.

## Agent and script interface

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
| `4` | Rejected task or repository. |
| `5` | Missing or rejected authentication. |
| `6` | Docker, image, or required host tool unavailable. |
| `7` | Agent or result failure, including a stale review head. |
| `8` | JIRA or network failure, including review publication. |
| `9` | Run timeout. |

Every run stores a small JSON record and a redacted transcript under `FRANKY_RUNS_DIR`. The default is `~/.franky/runs`.

`franky gh` is different from the autonomous container path. It exposes the full host `gh` surface and ignores `FRANKY_ALLOWED_REPOS`.

Scope `GH_TOKEN` to the exact repositories and permissions that callers may use. `FRANKY_GH_TIMEOUT` limits each host command.

## Operator profiles and MCP

Profiles inject selected instructions, skills, prompts, rules, agent definitions, and knowledge files into the task container.

```toml
# ~/.franky/profile.toml
[setups]
claude = "~/.claude"
codex = "~/.codex"
```

Franky uses setup allowlists. It excludes credentials, sessions, plugins, caches, hooks, settings, databases, and binary files.

Each included file passes a fail-closed secret scan. Swept setup configuration never enables MCP automatically.

Explicit MCP configuration can declare validated JSON or TOML files, credential names, and allowed hosts.

```bash
franky profile init
franky profile check
franky profile show
franky profile path
```

See [`docs/profiles.md`](docs/profiles.md) for formats, limits, MCP rules, and security behavior.

## Resource use

Franky stores `/work`, `/home/franky`, and `/tmp` in disposable disk volumes. Only small runtime paths use tmpfs.

| Resource | Default | Configuration |
|----------|---------|---------------|
| Task tree, including nested containers | 2048 MiB RAM, no swap | `FRANKY_MEMORY_MB`, 256 through 8192 |
| Proxy | 128 MiB RAM, no swap | Fixed per task |
| Task disk data | 8192 MiB soft budget | `FRANKY_DISK_MB`, 1024 through 32768 |
| Disk helper | 64 MiB RAM | Short-lived and networkless |

For two jobs on a 16 GiB Mac, allocate at least 6 GiB to Docker Desktop. Start with one active job per caller.

Limits are ceilings, not reservations. The disk watchdog samples approximately every five seconds, so it is not a filesystem quota.

Docker shares common image layers across Franky instances. Engine-specific images omit the other engine clients.

The fixed Ubuntu 24.04 footprint gate validates native AppArmor, security, images, and two-job and four-job runtime loads.

```bash
make footprint
make smoke-memory
make smoke-memory ARGS="--jobs 4"
```

Synthetic checks do not prove that an arbitrary repository or live model workload fits. Test your largest repository before increasing concurrency.

## Security model

The autonomous engine disables its own approval and sandbox prompts. The container is the safety boundary.

Franky applies these controls:

- A non-root user, read-only root, process and memory limits, and no swap.
- No host bind mounts, host filesystem access, host Docker socket, or privileged container.
- A packaged seccomp policy and the named `franky-task` AppArmor profile on native hosts.
- A fail-closed repository allowlist.
- Name-only credential forwarding and redaction of autonomous output and stored transcripts.
- An internal Docker network with no direct internet route or task DNS.
- A default-deny, HTTPS-only Squid proxy with blind TLS tunnels.
- Prompts forbid autonomous merges. Token permissions enforce the available GitHub actions.

Each task runs its own rootless Docker daemon. Agents can build images, run Compose, and use testcontainers without reaching the host daemon.

```text
task and nested Docker -> internal network -> Squid proxy -> allowed HTTPS hosts
```

The default egress list includes the selected provider, GitHub, package registries, and container registries. Add required hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS`.

Allowlisted hosts remain trusted destinations, not inert endpoints. A hostile task can use available credentials against those hosts.

The agent can also pass its credentials to nested containers. The outer limits and egress policy still apply to the full task tree.

Opening a PR can start GitHub Actions. Review workflow changes before you allow a run to use repository secrets.

Codex subscription authentication is the only persistent task volume. Franky scrubs it to `auth.json` before each autonomous run.

Read [`AGENTS.md`](AGENTS.md) before changing security, container, egress, profile, or resource code.

## Development and evaluation

[`AGENTS.md`](AGENTS.md) is the canonical maintainer guide. `CLAUDE.md` links to it for Claude Code.

The opt-in eval harness measures pass rate with real Docker, credentials, and a throwaway repository:

```bash
make eval ARGS="-n 3 --engine pi --compare-engine codex"
```

See [`evals/README.md`](evals/README.md) for task formats. See [`docs/releasing.md`](docs/releasing.md) for releases.

## Status

v0.3.15. Live end-to-end runs require operator-supplied credentials.
