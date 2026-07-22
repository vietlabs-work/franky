# Franky

A lean personal coding agent. Hand it a GitHub issue or a sentence, and it runs a
coding agent inside a fresh, hardened Docker container that clones the repo,
implements the change, and opens a pull request for you to review.

```
franky build https://github.com/you/repo/issues/42
franky build jira FOO-123 --repo you/repo
franky build "add a --json flag to the export command" --repo you/repo
franky build "fix the flaky retry test" --repo you/repo --engine claude

# Got review comments or red CI on a Franky PR? Iterate on it with follow-up commits.
franky iterate https://github.com/you/repo/pull/42
```

The agent is autonomous inside the container. The safety gate is four layers:
a hardened container, a default-deny egress allowlist (the container reaches only
your provider + GitHub + registries, via a creds-blind proxy), a fail-closed
trusted-repo allowlist, and the fact that Franky opens a PR rather than merging -
a human still reviews every change.

## Why

Most coding-agent wrappers either lock you into one vendor or run the agent
straight on your machine with your real credentials and shell. Franky does
neither: the engine is pluggable, and the agent only ever runs inside a
throwaway container with a narrowly scoped token.

## Engines

Franky is vendor-neutral. The engine that runs inside the container is pluggable;
all ship in the one image.

| Engine | CLI | Auth | Notes |
|--------|-----|------|-------|
| `pi` (default) | `@earendil-works/pi-coding-agent` | BYOK provider key | MIT, 15+ providers (OpenRouter, Anthropic, OpenAI, Ollama, ...) |
| `claude` | `@anthropic-ai/claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` | Most capable; uses your Claude subscription |
| `codex` | `@openai/codex` | ChatGPT subscription or `CODEX_API_KEY` | OpenAI Codex headless (`codex exec`) |
| `opencode` | `opencode-ai` | `MOONSHOT_API_KEY` or `OPENROUTER_API_KEY` | Provider selected by `FRANKY_MODEL` |

Select with `--engine pi|claude|codex|opencode`, or set `FRANKY_ENGINE`. Resolution order:
`--engine` flag > `FRANKY_ENGINE` > default `pi`.

OpenCode requires an explicit provider/model. Direct Moonshot:

```
franky config set FRANKY_ENGINE opencode
franky config set FRANKY_MODEL moonshotai/kimi-k3
franky config set MOONSHOT_API_KEY
franky build "fix the flaky retry test" --repo you/repo --engine opencode
```

OpenRouter fallback, including nested model IDs:

```
franky config set FRANKY_MODEL openrouter/anthropic/claude-sonnet-4
franky config set OPENROUTER_API_KEY
franky build "fix the flaky retry test" --repo you/repo --engine opencode
```

For a ChatGPT subscription, log in once without a browser in Docker. Open the displayed
URL and code on any other device; later Codex runs reuse and refresh the credential:

```
franky auth login codex
franky auth status codex
franky auth logout codex
```

The credential file stays in the fixed `franky-codex-auth` Docker named volume, never in
the host's `~/.codex`. Franky reads its bounded token strings into memory only so terminal
and transcript output can be redacted. If `CODEX_API_KEY` is also set, the API key takes
precedence.

## Install

Franky is published to PyPI as `franky-agent` (the installed command is `franky`):

```
uv tool install franky-agent
# or pipx:
pipx install franky-agent
# or:
pip install franky-agent
```

On first run the CLI pulls the version-pinned, public GHCR images
(`ghcr.io/vietlabs-work/franky:X.Y.Z` and `ghcr.io/vietlabs-work/franky-proxy:X.Y.Z`),
so all you need is Docker - no registry login. (Point `FRANKY_GHCR_REPO` at a different
namespace if you host the images elsewhere.)

To move to a newer release later, run `franky update` - it detects how you
installed (uv tool / pipx / pip) and reinstalls the latest version from PyPI via the same
manager. `franky update --force` reinstalls even when already current. (A dev
checkout updates via `git`; `franky update` is a no-op there.)

**For local development**, skip GHCR and point at local builds:

```
docker build -t franky .
docker build -t franky-proxy proxy/
export FRANKY_IMAGE=franky
export FRANKY_PROXY_IMAGE=franky-proxy
```

Franky is agent-agnostic to develop, not just to run: `AGENTS.md` is the canonical
agent guide (build/test commands, architecture, the load-bearing invariants, how to
add an engine), so Codex, Cursor, pi, or Claude Code all start with the same context.
`CLAUDE.md` is a symlink to it.

## Quickstart

1. Install Docker. Images are pulled automatically from GHCR on first run (see
   Install above). For local dev only, build them manually (see Install above).
2. Install Franky:
   ```
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
3. Configure credentials with the interactive wizard:
   ```
   franky config init
   ```
   This writes `~/.franky/config` (mode 0600) and walks you through engine selection,
   `FRANKY_ALLOWED_REPOS`, `GH_TOKEN`, and engine creds. You can also set individual
   keys later:
   ```
   franky config set FRANKY_ALLOWED_REPOS
   franky config set GH_TOKEN          # secret - entered at a hidden prompt
   franky config list                  # view the file (secrets masked)
   franky config path                  # show where the file lives
   ```
   To inject your own skills / instructions / knowledge or MCP config into the container, set up an
   operator profile (see [docs/profiles.md](docs/profiles.md)):
   ```
   franky profile init                 # interactive wizard -> ~/.franky/profile.toml
   franky profile check                # dry-run: files + MCP policy + secret scan
   franky profile show                 # view the profile + expanded file list
   franky profile path                 # show where the profile lives
   ```
   At minimum you need:
   - `FRANKY_ALLOWED_REPOS` - the trusted-repo allowlist (see below).
   - `GH_TOKEN` - scoped to contents + pull_requests on those repos.
   - the selected engine's creds (a provider key for `pi`,
     `CLAUDE_CODE_OAUTH_TOKEN` for `claude`, or `franky auth login codex` / `CODEX_API_KEY`
     for `codex`; OpenCode supports `FRANKY_MODEL=moonshotai/kimi-k3` with
     `MOONSHOT_API_KEY`, or `FRANKY_MODEL=openrouter/<model-id>` with
     `OPENROUTER_API_KEY`).
   - for JIRA tasks: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (host-side only,
     never forwarded into the container).
4. Run:
   ```
   franky build <gh-issue-url | jira KEY | "prose" | -> [--repo owner/repo] [--engine pi|claude|codex|opencode] [--plan-first] [--json] [-q] [-y]
   ```

Each run writes a redacted log to `tasks/<timestamp>.log` and prints the PR URL.
Pass `-` as the task to read the prose task from stdin. For scripting / agent callers,
see [Machine / scripting interface](#machine--scripting-interface) (`--json`, exit codes).

`--plan-first` adds an opt-in approval gate for sensitive targets: Franky runs a
read-only planning pass, prints the plan, and waits for explicit confirmation
before it builds or opens a PR. Decline (or run non-interactively) and nothing is
written. The default stays autonomous - the sandbox plus PR review is the gate.

`franky build` also does a quick (~1s, cached) check for a newer release and
prints a one-line hint if one exists - it never blocks the build. Silence it with
`FRANKY_NO_UPDATE_CHECK=1`, or set `FRANKY_AUTO_UPDATE=1` to auto-install the new
release for your next run. (Both are host-CLI only; neither reaches the container.)

## Iterating on a PR

Franky is no longer one-shot. When a PR it opened gets review comments or a red CI
check, point it back at the PR and it responds with **additive follow-up commits** on
the same branch:

```
franky iterate https://github.com/you/repo/pull/42 [--engine pi|claude|codex|opencode]
```

It runs the **same hardened, egress-controlled container** as `franky build`, but instead
of starting fresh it checks out the PR's existing branch, reads the review comments and
failing checks with `gh` (in-container, already allowlisted), addresses them, runs the
tests green, and pushes. It **never force-pushes, never rewrites history, never opens a new
PR, and never merges** - a human still reviews every change. The PR URL carries the repo, so
there is no `--repo` flag, and the repo allowlist gates it exactly like `build`.

Unlike `build` (which prints the new PR URL to stdout), `iterate` opens no new PR - on a
clean run it writes only an economics summary and a labeled completion line to stderr, and
nothing to stdout. Review the existing PR for the new commits. The redacted transcript still
lands in `tasks/<timestamp>.log`.

`iterate` is intended for Franky's **own** PRs. As a guardrail it is instructed to confirm
the PR's head branch is a `franky/*` branch in the same repo (not a fork) before touching
anything, and to stop otherwise. This is a prompt-level guard in the same register as the
"never merge" rule (the agent is autonomous); the hard bounds remain the repo allowlist, the
egress cage, and PR-not-merge. See the Security section.

## Querying GitHub with Franky's token (`franky gh`)

Franky's caller is usually an agent in a sandbox with **no `gh` CLI and no GitHub token** - so
after it dispatches a build it can't even confirm the PR landed or read CI on its own. Franky
already holds a scoped token. `franky gh` lends it to the real `gh` CLI:

```
franky gh pr list --repo you/repo
franky gh pr checks 42 --repo you/repo
franky gh pr merge 42 --repo you/repo --squash
franky gh api /repos/you/repo/pulls
```

**Full power, bounded only by the token.** It is a straight passthrough to `gh` - read AND
write (comment, label, `merge`, `api`, workflow dispatch, ...), whatever the token's scopes
allow. Franky imposes **no** capability ceiling here: no read-only gate and no repo allowlist on
this surface. If you want to limit what Franky can touch, scope the token (a fine-grained PAT) -
the credential is the control lever. (The `build`/`iterate` repo allowlist is unaffected; it
still gates the autonomous, content-driven build path.)

The one guarantee kept: **the token value never leaks.** It reaches `gh` via the environment,
never on the argv (so it is not visible in `ps`), and `gh`'s output is redacted before printing.
`gh`'s own exit code is passed through; a missing token exits 5, a missing `gh` binary exits 6.

Runs **host-side** (like Franky's own PR-idempotency check), not in the container - a `franky gh`
call is a deterministic operator/agent command, not the autonomous engine. It is **non-interactive
by design**: output is captured then redacted, so pass `gh`'s own flags rather than relying on its
prompts. Long-lived/streaming subcommands (e.g. `gh run watch`) buffer until completion rather than
streaming live. A watchdog caps each call at `FRANKY_GH_TIMEOUT` seconds (default 120, exit 9 on
hit) so a stalled call can't wedge an autonomous caller; set `FRANKY_GH_TIMEOUT=0` for no cap.
Requires the `gh` CLI on the host.

> **Scope the token deliberately.** Because there is no allowlist on this surface, `franky gh` is
> exactly as powerful as the token you give Franky: a prompt-injected agent that can invoke it can
> `merge`, `gh api -X DELETE`, dispatch workflows, or read Actions secrets on any repo the token
> reaches - and `gh` can print GitHub-side secrets (e.g. `gh api .../actions/secrets`) that
> Franky's redaction does not know about. Use a **fine-grained PAT** scoped to the repos and
> permissions you actually want Franky to have. The credential is the limit.

## Tracking runs (`franky jobs` / `franky job`)

Every `build` / `iterate` run is recorded in a small registry (`~/.franky/runs/<job_id>.json`)
so you can see what happened to it afterwards - or observe and stop one that is stuck. Each run
prints its `job_id` at start (and it appears as `job_id` in `--json` output).

```
franky jobs                    # recent runs, newest first: id, command, status, age, repo
franky jobs --stats            # cross-run health: success/hang rate, median dur & cost, by engine/repo
franky job status <job_id>     # one run's record + whether its container is still alive
franky job logs <job_id>       # print the run's redacted transcript
franky job kill <job_id>       # force-remove a stuck run's container + reap its proxy/network
franky job export <job_id>     # bundle a run's record + transcript into a portable .tar.gz
franky job diagnose <job_id>   # dispatch a read-only agent to explain WHY a run failed
franky job replay <job_id>     # re-run a build from its saved inputs to reproduce a failure
franky job resume <job_id>     # continue a hung/timeout/killed run WITH its restored workspace
franky job attach <job_id>     # inject a mid-run correction into a LIVE run's steer-file mailbox
```

This is what turns the half-day silent hang into a 30-second `job status` -> `job kill`. The
records store **no secret values** (only names, paths, status, timings, and a redacted task
summary), the runs dir is bounded (old finished records are pruned; a `running` record is never
pruned), and an unknown/corrupt job id is a clean error (exit 2), never a traceback.

`franky jobs --stats` aggregates over **all** recorded runs (not just the `-n` most recent):
success rate, a **hang count** (timeout runs + stale `running` orphans that never finished),
median duration, total cost, and the same broken down `by engine` / `by repo` - so a rising
hang rate on a repo or engine is visible at a glance. `--stats --json` emits the numbers as one
object.

`franky job export <id>` writes a `.tar.gz` (default `./franky-job-<id>.tar.gz`, override with
`-o`) holding `record.json` + `transcript.log` - hand it to a human or another agent to inspect
a failed run offline. Both members are secret-free by construction (the record carries no secret
value; the transcript was redacted when written), so the bundle exposes nothing new, and its tar
members carry no host uid/username/timestamp.

### Runtime diagnostics

`franky job status <id>` also shows a `diagnostics:` block: the task's exit code, whether it was
OOM-killed, its final container state, whether the nested rootless Docker daemon came up
(`dind_ready`), a tmpfs-full heuristic, and any hosts the egress proxy denied (with counts).
These are captured host-side, best-effort, right before the task/proxy containers are reaped -
for a wedged run, `job kill` captures them too (before it reaps). `--json` includes the same
`diagnostics` object (see `franky schema` -> `job_record_schema`).

### Diagnose and auto-retry a failed run

`franky job diagnose <id>` dispatches a **read-only meta-agent** at a failed run's transcript +
metadata (plus the `diagnostics` block above, when present, as hard evidence alongside the
prose transcript): it clones nothing and changes nothing, and emits a structured root-cause, a
proposed fix, and a `retryable`/`retry_hint` learning signal (`--json` for the machine object;
see `franky schema` -> `diagnosis_result_schema`). It answers "why did job X hang / produce no
PR" with an agent that actually reads the evidence.

`franky build --retry N` closes the loop (bounded, `N` <= 5): on a **retryable** failure
(timeout / agent-error / no-PR) it diagnoses the attempt, then retries with the root-cause fed
back into the prompt ("last attempt failed on X - avoid it"). It stops early when the diagnosis
says the failure is not retryable (never a blind restart), re-checks idempotency before each
retry so it can never open a second PR, and each attempt is its own tracked job. The `--json`
result then carries an `attempts` trail; a plain `build` (no `--retry`) is unchanged.

> **Retry caveat.** A retry is most effective for failures where nothing was pushed (an early
> timeout, agent-error, or no-PR). If an attempt already pushed the branch, a fresh retry may not
> be able to advance it (Franky never force-pushes - safe, but it may not progress); the
> diagnosis will usually flag that case as not retryable.

> **Note on in-flight runs.** `franky build` still **blocks** the shell it runs in, so to
> observe or kill a run *while it is going* you need a second shell/channel to run `franky
> jobs` / `job kill` from (exactly the case where a dispatching agent left the build running).
> `job logs` shows the transcript once the run finishes (it is written at the end); for a live
> view during a run use `franky build -v`. A `--detach` launch mode, `job shell` (exec into a
> running container), and `logs -f` are tracked as follow-ups.

### Replaying a run

`franky job replay <job_id>` re-runs a recorded `build` (or an earlier `replay`) from its
**saved inputs** - the original task text and the exact base commit (the target repo's
default-branch tip at the start of that run's build pass, after any `--plan-first` approval) -
so you get a controlled, repeatable starting
point for debugging a failure. It is **reproduce-only by default**: the container makes the
change and reports what happened, but pushes nothing and opens no PR, so it is always safe to
re-run. Pass `--open-pr` once a fix is confirmed to opt into the normal build conventions
(branch, tests-green-before-PR, `gh pr create`).

> **Nondeterminism caveat.** Replay reproduces the *inputs* (task + base commit), not
> bit-identical output - the underlying LLM is not deterministic, so a replay's transcript can
> still diverge from the original even with identical inputs.

> **Source-fidelity note.** A jira- or prose-sourced replay uses the frozen task text recorded
> at the original run. An issue-sourced replay re-fetches the live issue via `gh issue view`
> inside the container, so it reflects the issue's current content, not necessarily its state
> at the original run.

Only `build`/`replay` runs carry reproducible inputs; a run recorded before replay support was
added, or whose base commit no longer exists (force-pushed or garbage-collected), is refused
up front (exit 2) rather than started.

### Resuming a hung / timed-out / killed run

`franky job resume <job_id>` re-enters a hung/timeout/killed run **with its workspace** so a
fresh engine *continues* it instead of starting over. When a run times out (or you `job kill` a
wedged one), Franky captures a snapshot of the container's `/work` (the repo clone + its branch
state) before teardown; `resume` launches a fresh (still fully hardened, egress-controlled)
container, restores that snapshot into it, and runs the engine on the existing branch.

Only **build/replay** runs are resumable - a fresh engine continues from the restored `/work`
branch state, which iterate/diagnose runs do not have (resuming one reports `not_resumable`, exit
2). And only **timeout/killed** runs produce a snapshot, so a clean or never-captured run reports
`no_snapshot` (exit 2).

> **v1 limitation.** Resume restores the *filesystem*, not the agent's LLM/session state. A fresh
> engine re-orients from the branch state on disk and continues the task; it does not remember the
> prior run's reasoning. (Git push still works: the tokenized remote URL is stripped from the
> snapshot's `.git/config`, but the container re-authenticates from `GH_TOKEN`.)

**Security framing.** The snapshot is scrubbed **fail-closed** before it is stored: known
credential files (`.git-credentials`, `.netrc`, `*.pem`, ssh keys, `hosts.yml`) are removed, git
remote URL userinfo and credential-helper lines are stripped from every `.git/config`, and known
secret values are redacted out of every non-`.git` file. It is then **verified** - every file is
re-scanned for surviving values and fresh-token patterns, and git history is *decompressed* via
`git cat-file` and scanned for known values; **any** hit refuses the snapshot (nothing is
stored). The snapshot is also **contained**: it is host-local, mode `0600`, pruned alongside its
run record (with orphan sweeping), and **never** included in `franky job export` (the exported
bundle stays record + redacted transcript only). This is the same trust boundary as
`~/.franky/config`, which already holds plaintext creds on the host.

### Steering a live run

```
franky job attach <job_id> -m "stop refactoring, just fix the failing test"
```

injects a mid-run correction into a still-running build/iterate/replay/resume pass. It only
works on a job that is currently `running` and whose engine opted into steering (pi, claude, and
codex all do); it delivers the message via a host-side `docker exec` into a small mailbox file
inside the container - no bind mount, no change to the container's hardening.

This is a **best-effort, prompt-level channel**: delivery is guaranteed (the file write either
succeeds or `attach` reports `steer_delivery_failed`), but *incorporation* depends on the
engine's own loop re-reading the mailbox at its next major step - Franky's prompt tells every
engine to check for it before starting a new sub-task, but a run deep inside a single long tool
call may not notice it immediately.

`-m` is the unattended-safe path (never hangs). Omit it for a single interactive prompt, but
only in a TTY - a non-interactive shell fails fast (exit 2) instead of hanging.

> **Never pass a secret via `-m`.** Chat history, shell history, and process argv are all unsafe
> places for a credential. The message is redacted for any known Franky secret *value* before
> delivery, but that is a safety net, not a reason to rely on it.

Live output streaming (watching the run react to the correction in real time) is **not** part of
this v1 - check back with `franky job status` / `job logs` once the run finishes.

## Planning a big task

**One franky run = one focused PR.** A run is meant to produce a single, reviewable pull
request, not a sprawling multi-concern changeset. If a task is too big for one PR, split it
first with `franky plan`:

```
franky plan "rework auth + add SSO + migrate the user table" --repo you/repo [--json]
franky plan https://github.com/you/repo/issues/42 --json
franky plan - --repo you/repo          (read the prose task from stdin)
```

`plan` runs **one read-only container pass** that inspects the repo/issue, decides whether the
task fits one PR or needs splitting, and emits a decomposition. It **builds nothing** - no
branch, no commits, no PR. It accepts the same task forms as `build` (issue URL / JIRA key /
prose / `-` stdin), gates on the same repo allowlist, and threads `--engine`, `--profile`, and
`--max-duration` the same way. The caller orchestrates what to do with the sub-tasks (e.g. run
`franky build` per sub-task). `build --help` carries a static pointer to this command.

Under `--json`, `plan` emits a **distinct** envelope (NOT the build/iterate `result_schema`):

```json
{ "fits_one_pr": false,
  "subtasks": [
    {"title": "split out SSO", "summary": "add the SSO provider hooks", "suggested_repo": "you/repo"},
    {"title": "migrate user table", "summary": "the schema change + backfill", "suggested_repo": "you/repo"}
  ],
  "rationale": "three independent concerns; each is its own reviewable PR",
  "engine": "pi", "repo": "you/repo", "exit_code": 0 }
```

Errors share the same `{"error":{...}}` envelope and exit-code taxonomy as `build`/`iterate`
(a parseable plan that the agent never produced is exit `7`, `kind: no_plan`).

## Machine / scripting interface

`franky build` / `iterate` / `plan` are built to be driven by a script or an LLM/agent without
parsing prose. Three guarantees:

**1. `--json` - one machine-readable object on stdout.**

Success / agent result:

```json
{ "status": "pr_opened|already_open|no_pr|agent_error|timeout|iterate_complete",
  "pr_url": "https://github.com/you/repo/pull/42",
  "branch": "franky/issue-42",
  "reason": "PR opened",
  "exit_code": 0,
  "economics": {"tokens_in": 1200, "tokens_out": 340, "cost_usd": 0.0123, "duration_s": 47.5},
  "log_path": "tasks/20260625-101500.log",
  "engine": "pi",
  "repo": "you/repo" }
```

Failure:

```json
{ "error": {"code": 5, "kind": "auth_error", "message": "...", "hint": "..."} }
```

`--json` implies `--quiet`, so stdout carries **exactly one** JSON object and nothing else
(progress + the update hint are suppressed). The object is fully redacted - a secret value
never appears, even nested in a field. `branch` is the **predicted** branch name
(`franky/<slug>`) the host computed before the run; the agent may deviate, so treat it as a
hint, not a guarantee (`iterate` reports `null`).

**2. Exit-code taxonomy (a SemVer contract).** The process exit code always equals the
failure's `code`:

| code | meaning |
|---|---|
| 0 | success (PR opened / iterate pass complete) |
| 2 | usage/flag error; interactive input required in a non-TTY |
| 3 | config error (bad config file, allowlist unset/empty/malformed, bad engine) |
| 4 | allowlist / task rejection |
| 5 | auth/creds missing (`GH_TOKEN`, engine creds, JIRA creds, JIRA 401/403) |
| 6 | docker / image unavailable, or a required host tool (e.g. `gh`) is missing |
| 7 | agent ran but exited nonzero or produced no PR |
| 8 | network/timeout (JIRA reach/HTTP/parse) |
| 9 | run exceeded `--max-duration` (the container was aborted) |

Note `build` exits **7** (not 0) when the agent finishes cleanly but opens no PR, so success
is distinguishable from a no-PR outcome by exit code alone. The taxonomy is append-only -
new codes may be added, but existing values never change meaning.

**3. Never-hang.** Every interactive prompt fails fast with exit `2` in a non-TTY instead of
blocking forever:

- `--plan-first` without a TTY needs `--yes` to auto-approve, else it exits 2 **before**
  running the planning pass.
- `franky build -` (and `franky plan -`) reads the task from stdin; on an interactive TTY (no
  piped input) it exits 2 rather than block waiting for a human to type the task - pipe the
  task in instead.
- `franky config set <KEY>` (no value) and `franky config init` exit 2 without a TTY rather
  than waiting on a prompt.

Other flags: `-q/--quiet` suppresses progress and the update hint (stdout stays exactly the
bare PR URL); `-y/--yes` auto-approves `--plan-first`. Without `--json`, errors print a single
`franky: <message>` line to stderr and use the same exit code.

**4. Budget guardrail.** `--max-duration SECONDS` (on `build`, `iterate`, and `plan`) aborts a
runaway run; the container is killed and the result is `status: timeout` / exit `9` (`plan`
raises the timeout error). The default budget is 1800s. (A token/cost cap is out of scope -
token usage is only known after the run.)

**5. Idempotency (retry-safety).** Before launching, `build` computes a deterministic branch
(`franky/issue-42`, `franky/<jira-key>`, or `franky/<prose-slug>`) and asks GitHub whether an
open Franky PR already uses it. If so it reports `status: already_open` with the existing
`pr_url` at exit `0` and does **not** open a duplicate - so an agent that retries the same task
converges instead of stacking PRs. The check is best-effort (any error just proceeds with the
build) and `--force` skips it.

**6. `franky schema`.** Prints one JSON object describing every command + its flags, the
result/error object shapes (including the distinct `plan_result_schema`), and the exit-code
table - machine introspection so an agent can discover the contract instead of parsing
`--help`.

## Security

Read this before pointing Franky at anything.

**Container hardening is load-bearing.** Because the agent runs autonomously
(claude with `--dangerously-skip-permissions`, codex with
`--dangerously-bypass-approvals-and-sandbox`, OpenCode with `--auto --pure`, and pi with its
default tools), the
OS-level isolation is what bounds it, not tool-permission prompts. Franky runs the
container with:

- `--cap-drop=ALL`, then adds back only `CAP_SETUID`/`CAP_SETGID` (needed by the
  rootless Docker daemon - see "Docker-in-Docker" below)
- `--read-only` root filesystem; writable paths only via `--tmpfs` (the clone, the
  agent's HOME, and the rootless Docker data root, each owned by the non-root uid)
- `--pids-limit` and `--memory` caps (with `--memory-swap` = `--memory`, no swap)
- a non-root user (uid 1001) baked into the image
- **no Docker socket mount and no host bind mounts** - the repo is cloned inside
  the container and the nested Docker daemon is rootless, so the agent never
  touches your filesystem or your host's Docker daemon
- only the selected engine's required env vars passed in; nothing else

MCP profiles can add explicitly named process-environment credentials and hostname-only
egress destinations. Credential values reach Docker only through name-only `-e NAME` flags
and join the redactor; native JSON/TOML config reaches HOME through the existing profile
bundle. Franky recognizes only the reserved Codex and Claude MCP files; Pi requires a
profile-injected MCP extension. See [Profiles](docs/profiles.md#mcp-configuration).

Codex subscription auth is the narrow persistence exception: only the fixed Docker named
volume `franky-codex-auth` is mounted at `/home/franky/.codex`, and only for subscription
runs. Before every autonomous run a trusted networkless helper deletes every entry except
`auth.json`, rejects malformed or unsafe-shaped state, and loads token values only into the
in-memory redactor; Codex also runs with `--ignore-user-config`. A validated
`~/.codex/franky-mcp.config.toml` is parsed on the host and supplied only as explicit `-c`
MCP server overrides, never loaded as user config. Subscription runs can reach
only `chatgpt.com` and `auth.openai.com` through the normal proxy cage. The volume is writable
so Codex can rotate OAuth tokens; like every engine credential available to an autonomous
agent, a hostile task can invalidate it. `franky auth logout codex` removes the whole volume.

### Docker-in-Docker (always on)

Many repos cannot run their test suite without Docker (compose-based integration
tests, testcontainers, a `docker build` step). So every Franky container runs its
**own rootless Docker daemon** - the agent can `docker build`, `docker compose up`
test infra, and run testcontainers entirely inside the sandbox. Nothing to enable;
it is always available.

This is rootless DinD (a daemon running as the non-root `franky` user inside its
own user namespace), **not** a mounted host Docker socket and **not** `--privileged`.
It needs a few specific, minimal relaxations of the locked profile, applied to every
task and verified on Docker Desktop for Mac:

- `--security-opt=no-new-privileges` is **dropped** (it blocks the setuid uid-map
  helpers rootless Docker needs to start),
- `--security-opt=systempaths=unconfined` (unmasks `/proc` so the nested runtime can
  mount it for inner containers - far narrower than `--privileged`/`seccomp=unconfined`),
- `CAP_SETUID`/`CAP_SETGID` added back on top of `--cap-drop=ALL`, and `/dev/net/tun`
  for the rootless network stack.

The blast radius stays bounded by everything else (rootless user namespace, read-only
root, the egress cage below, no host FS, repo allowlist, PR-not-merge). The nested
daemon's image pulls and `docker build` fetches go **through the same egress proxy**
(it inherits `HTTP(S)_PROXY`), and inner containers have no route to the internet
except that proxy - verified: an off-allowlist `docker build` `FROM` or `RUN` fetch is
refused by the proxy, and a nested container's direct egress has no route out.

### Egress control

The big v0 hole - a prompt-injected agent exfiltrating the creds it carries -
is now closed by a default-deny egress allowlist. The task container runs on a
Docker `--internal` network with NO route to the internet; its only peer is a
Squid proxy enforcing a domain allowlist.

```
                  Docker --internal network (no internet route)
   +-----------------------------------------------------------------+
   |                                                                 |
   |   [ task container ] --HTTP(S)_PROXY--> [ franky-proxy (Squid) ]-+--> allowlisted
   |    --dns 127.0.0.1                       default-deny allowlist  |    hosts only
   |    (no creds on argv)                    (sees NO creds)         |
   +-----------------------------------------------------------------+
```

- **Blind CONNECT, no creds at the proxy.** Egress is HTTPS-only (port 443):
  Squid tunnels it with a blind CONNECT (no TLS termination), so it never sees the
  bytes - your Claude token or BYOK key tunnel through encrypted and are never
  visible to the proxy. Plain HTTP (port 80) is denied outright, so there is no
  cleartext, proxy-visible path even to an allowlisted host.
- **DNS is killed in the task container** (`--dns 127.0.0.1`), so a hostile agent
  cannot resolve or reach an off-allowlist host directly; only the proxy resolves.
- **Fail-closed.** Franky refuses to start the task unless the proxy is confirmed
  healthy, and the proxy refuses to start with an empty or malformed allowlist.
- **The allowlist** covers: your engine's selected provider host (e.g.
  `api.moonshot.ai`, `openrouter.ai`, `api.anthropic.com`, `api.openai.com`), GitHub
  (clone/push/PR), the npm + PyPI
  registries, and - because
  Docker-in-Docker is always on - a broad set of well-known **container image
  registries** (Docker Hub + CDN, GHCR, GCR/Artifact Registry, `registry.k8s.io`,
  Quay, ECR Public, MCR, GitLab, plus the CDNs they serve layer blobs from). Add
  extra hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS` (comma-separated).

**Residual risk.** The allowlisted hosts are high-trust, but the agent can still
reach GitHub, your model provider, the package registries, and the container
registries above - so a determined injection could still smuggle data to one of
those (e.g. a gist, an issue comment). Treat allowlisted destinations as trusted,
not inert. Two consequences of always-on DinD specifically:

- **Wider reachable set + a relaxed profile on every task** (incl. non-Docker ones):
  the registry allowlist is broad (notably `.cloudfront.net`, a shared CDN), and the
  hardening relaxations above apply universally. This is a deliberate trade for
  "building/testing just works".
- **The agent can move its own creds into nested containers** (e.g. `docker run -e
  GH_TOKEN ...`). The egress allowlist still bounds *where* anything can go and
  PR-not-merge still bounds the damage, but the secret is no longer confined to a
  single process. There is also no per-inner-container resource limit and no
  cross-task concurrency cap - the outer `--memory`/`--pids` cap (~8 GB, tmpfs image
  storage is RAM) bounds one task's whole container tree.

v0 mitigations, still in force:

1. **Fail-closed trusted-repo allowlist.** Franky refuses any repo not in
   `FRANKY_ALLOWED_REPOS`, and refuses everything if that var is unset. This
   limits injection to content you already trust.

   The allowlist supports per-segment glob patterns (case-insensitive):
   - `my-org/my-repo` - exact match
   - `my-org/*` - every repo in `my-org`
   - `my-org/team-*` - repos with a name prefix
   - `*` - every repo the `GH_TOKEN` can reach (its **full scope** - a conscious opt-in,
     not the default; use only if the token is already narrowly scoped)
2. **Scope your tokens narrowly.** Give `GH_TOKEN` only contents + pull_requests
   on the target repos. Prefer a low-spend or separate API key for `pi`.
3. **PR, not merge.** Franky only opens PRs. You review before anything lands.
   `franky iterate` follows the same rule: it only pushes additive commits to an
   existing PR's branch (never force-push, never merge, never a new PR), and the
   "act only on a `franky/*` branch in the same repo" check is prompt-level - so
   point `iterate` only at PRs Franky itself opened, in an allowlisted repo.

**GitHub Actions warning.** Opening a PR can trigger workflows. A PR built from an
attacker-influenced issue could run attacker-influenced workflow code with your
repo's Actions secrets. Review workflow changes in the PR diff, and consider
requiring approval for workflow runs on PRs.

## Evals

Agent quality is probabilistic, so changes to the persona, prompt, model, or profile
should be gated on a measured **pass-rate**, not a hunch. The eval harness runs a golden
task set through the *real* Franky flow N times and reports pass-rate, plus a comparison
mode that reports the delta between two configs (e.g. one engine vs another).

It is **opt-in and out-of-band** (like the manual egress check) - it needs real Docker +
creds + a throwaway sandbox repo, so it is not part of the fast hermetic unit suite. Point
`evals/tasks.json` at your sandbox repo and run:

```
make eval ARGS="-n 3 --engine pi --compare-engine codex"
```

See [`evals/README.md`](evals/README.md) for setup, the task schema, and the success
checkers.

## Status

v0.1.1. Real end-to-end runs need live engine credentials, supplied out-of-band by
the operator. The pieces under test here are the container hardening, the egress
allowlist + proxy orchestration, the secret redaction, the trusted-repo allowlist,
and the engine abstraction.
