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

JIRA inputs also need `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`.

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
| `claude` | `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code subscription token |
| `codex` | `franky auth login codex` or `CODEX_API_KEY` | ChatGPT subscription or API key |
| `opencode` | `FRANKY_MODEL` plus its matching key | Moonshot or OpenRouter |

Configure OpenCode for one provider:

```bash
franky config set FRANKY_ENGINE opencode
franky config set FRANKY_MODEL moonshotai/kimi-k3
franky config set MOONSHOT_API_KEY
```

OpenRouter uses `FRANKY_MODEL=openrouter/<model-id>` with `OPENROUTER_API_KEY`.

Codex subscription authentication stays in a Docker named volume:

```bash
franky auth login codex
franky auth status codex
franky auth logout codex
```

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
| `franky build TASK` | Implement a GitHub issue, JIRA task, prose task, or stdin task. Open one PR. |
| `franky iterate PR_URL` | Address review or CI feedback with additive commits on Franky's existing PR. |
| `franky review-pr PR_URL [INSTRUCTIONS]` | Review a PR. Publish findings unless `--no-publish` is set. |
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
| `franky job status JOB_ID` | Show the record, live state, and runtime diagnostics. |
| `franky job logs JOB_ID` | Print the redacted transcript. |
| `franky job kill JOB_ID` | Stop a run and remove its task, proxy, network, and volumes. |
| `franky job export JOB_ID` | Export the record and redacted transcript as a portable archive. |
| `franky job diagnose JOB_ID` | Request a read-only failure analysis. |
| `franky job replay JOB_ID` | Reproduce a build from saved inputs in a fresh container. |
| `franky job resume JOB_ID` | Continue a stopped run from its saved workspace. |
| `franky job attach JOB_ID` | Send one correction to a running steerable engine. |

`replay` starts from saved inputs. `resume` restores `/work`. Use `replay --open-pr` only when the reproduced run should open a PR.

`attach` supports `pi`, `claude`, and `codex`. OpenCode does not support steering.

### Authentication, configuration, and profile commands

| Command | Purpose |
|---------|---------|
| `franky auth login codex` | Create persistent Codex subscription authentication. |
| `franky auth status codex` | Check the stored Codex authentication. |
| `franky auth logout codex` | Delete the Codex authentication volume. |
| `franky config init` | Create or update the user configuration. |
| `franky config set KEY [VALUE]` | Set one configuration value. Secret values use a hidden prompt. |
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

Scope `GH_TOKEN` permissions because they are the enforced GitHub boundary for the autonomous container.

Use `--expected-head-sha` to reject a changed PR head. Use `--no-publish` for a read-only GitHub run.

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

v0.2.0. Live end-to-end runs require operator-supplied credentials.
