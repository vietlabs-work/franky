# Franky

Franky runs a coding agent in a fresh, hardened Docker container. It clones an allowlisted repository, makes a change, and opens a pull request.

```bash
franky build https://github.com/you/repo/issues/42
franky build jira FOO-123 --repo you/repo
franky build "add JSON output" --repo you/repo
franky iterate https://github.com/you/repo/pull/42
franky review-pr https://github.com/you/repo/pull/42
```

## Install

```bash
uv tool install franky-agent   # or: pipx install / pip install franky-agent
```

Docker must be available. Franky pulls version-pinned images from GHCR on the first run. Native AppArmor hosts also need the [task profile](docs/configuration.md#apparmor).

## Configure

```bash
franky config init
```

Every build needs:

- `FRANKY_ALLOWED_REPOS`: comma-separated `owner/repo` patterns. An empty value denies every repository.
- `GH_TOKEN`: a narrow token with content and pull-request access for the allowed repositories.
- Credentials for the selected engine.

## Engines

Select an engine with `--engine`, then `FRANKY_ENGINE`. The default is `pi`.

| Engine | Authentication | Notes |
|--------|----------------|-------|
| `pi` | One supported provider variable | Vendor-neutral BYOK client |
| `claude` | `franky auth login claude` or `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code subscription token |
| `codex` | `franky auth login codex` or `CODEX_API_KEY` | ChatGPT subscription or API key |
| `opencode` | `FRANKY_MODEL` plus its matching key | Moonshot or OpenRouter |

## Commands

| Command | Purpose |
|---------|---------|
| `franky build TASK` | Implement an issue, JIRA key, prose, or stdin task. Open one PR. |
| `franky iterate PR_URL` | Address review or CI feedback on Franky's PR with additive commits. |
| `franky review-pr PR_URL` | Review a PR and publish findings. |
| `franky plan TASK` | Split a task into PR-sized tasks, read-only. |
| `franky jobs` | List runs. `--stats` adds success, hang, duration, and cost data. |
| `franky gh ARGS...` | Run the host `gh` CLI with Franky's token. |
| `franky schema` | Print the full machine-readable CLI contract. |
| `franky version` | Print version, install source, engine, and image. |
| `franky update` | Install the latest release through the detected installer. |
| `franky apparmor-profile` | Print the native Linux task profile. |

`build`, `iterate`, and `review-pr` take `--thread` to keep the engine session per PR ([threads](docs/threads.md)). Run `franky COMMAND --help` for flags.

## Docs

| Topic | File |
|-------|------|
| Settings, AppArmor, engine logins, Atlassian tools | [docs/configuration.md](docs/configuration.md) |
| Job, thread, auth, config, and profile commands | [docs/commands.md](docs/commands.md) |
| Build, iterate, and review-pr behavior | [docs/review.md](docs/review.md) |
| Review and author threads | [docs/threads.md](docs/threads.md) |
| JSON output, run tracking, exit codes | [docs/automation.md](docs/automation.md) |
| Operator profiles and MCP | [docs/profiles.md](docs/profiles.md) |
| Memory, disk, and concurrency | [docs/resources.md](docs/resources.md) |
| Security model | [docs/security.md](docs/security.md) |
| Releases | [docs/releasing.md](docs/releasing.md) |
| Evals | [evals/README.md](evals/README.md) |
| Contributing (maintainer guide) | [AGENTS.md](AGENTS.md) |

## Status

v0.3.15. Live end-to-end runs require operator-supplied credentials.
