# Configuration

## Settings file

`franky config init` writes `~/.franky/config` with mode `0600`. Process environment values override file values.

```bash
franky config set FRANKY_ALLOWED_REPOS
franky config set GH_TOKEN
franky config list
franky config path
```

See [`config.example.toml`](../config.example.toml) for common settings.

## AppArmor

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

## Engine credentials

**Claude.** `franky auth login claude` runs `claude setup-token` when Claude Code is on the PATH. It saves the token you paste as `CLAUDE_CODE_OAUTH_TOKEN` in the config file. `auth status claude` and `auth logout claude` check and remove it.

**Codex.** Subscription authentication stays in a Docker named volume.

```bash
franky auth login codex
franky auth status codex
franky auth logout codex
```

Set `FRANKY_CODEX_AUTH_VOLUME` to isolate logins between Franky instances. `CODEX_API_KEY` takes precedence when both methods exist.

**OpenCode.** Configure one provider:

```bash
franky config set FRANKY_ENGINE opencode
franky config set FRANKY_MODEL moonshotai/kimi-k3
franky config set MOONSHOT_API_KEY
```

OpenRouter uses `FRANKY_MODEL=openrouter/<model-id>` with `OPENROUTER_API_KEY`.

## Model names

`FRANKY_MODEL` is optional for `claude` and `codex`. If it is unset, the engine uses its own default. Franky checks only the shape of a set name, before it starts a container:

| Engine | Accepted shape |
|--------|----------------|
| `claude` | An alias (`best`, `fable`, `opus`, `sonnet`, `haiku`, `opusplan`, each with an optional `[1m]`) or a `claude-*` id |
| `codex` | A `gpt-*`, `o<N>`, or `codex-*` id |

A well-formed id that does not exist still fails inside the engine.

## JIRA tickets

`franky build jira KEY` and `review-pr` read tickets on the host, in one of two ways:

| Mode | Settings | Notes |
|------|----------|-------|
| `acli` | `JIRA_CLI` (absolute path to `acli`) | Uses the `acli` login already on the host. Wins when both modes are set. |
| REST | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | The token stays on the host. |

`JIRA_CLI` is not a secret: `franky config set JIRA_CLI /path/to/acli`. `acli` runs as the host user with only `HOME`, `PATH`, `LANG`, `TMPDIR` and `USER`, never inside a container. It reads every ticket that login can read, so use a login scoped to what Franky may see. Needs `acli jira workitem comment list`; an `acli` that lacks it only loses ticket comments.

## Atlassian tools

`franky connect jira` gives every task on a private repository read-only JIRA and Confluence tools inside the container.

- Login: a browser OAuth flow with PKCE and a loopback redirect. No Atlassian admin step.
- Headless host: run `franky connect jira --no-browser`, open the printed URL elsewhere, and paste the redirect URL back.
- `--status` shows the connection and the granted scopes. `--disconnect` revokes it.
- The login stays on the host in `~/.franky/atlassian-jira.json` (0600).
- Only `claude` and `codex` get the tools, through the Atlassian MCP server (`https://mcp.atlassian.com/v2/mcp`).
- Only a short-lived access token enters the container, as `FRANKY_ATLASSIAN_MCP_HEADER`. Each run prints when that token expires.
- Franky requests read and search scopes, and the engine config denies the write tools. If Atlassian grants broader scopes, Atlassian does not enforce read-only.
- A missing, expired, or revoked connection turns the tools off with one hint line. A run that outlives its token falls back to the ticket text.
- The egress allowlist gains `mcp.atlassian.com` only when the tools are on.

## Images

Release runs use an engine-specific image. The unsuffixed image contains all engines for cross-engine tasks.

| Setting | Purpose |
|---------|---------|
| `FRANKY_IMAGE` | Override the task image. |
| `FRANKY_PROXY_IMAGE` | Override the proxy image. |
| `FRANKY_GHCR_REPO` | Override the GHCR namespace. |
| `FRANKY_PULL_ENGINES` | Comma-separated engines that `franky update` also pre-pulls. |

`franky update` pulls the task image of the configured engine, of every engine with a local released image, and of each engine in `FRANKY_PULL_ENGINES`, then the proxy image. A failed pull does not skip the others, and the command exits nonzero.

When a run finds its versioned image missing, it uses the newest older patch of the same minor version and engine that is local, and pulls the missing image in the background (log: `~/.franky/image-pull.log`). It never crosses a minor version. With no such image, or with `FRANKY_IMAGE` or `FRANKY_PROXY_IMAGE` set, it pulls and waits as before.
