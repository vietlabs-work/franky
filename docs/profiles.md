# Operator profiles

Profiles give the container selected instructions, skills, prompts, rules, agent definitions, knowledge, and MCP configuration.

Use `[setups]` for a known agent directory. Use `[profile]` for explicit files outside those directories.

## Create a profile

The wizard discovers supported setup directories and merges selections into the existing file.

```bash
franky profile init
franky profile check
franky profile show
franky profile path
```

Franky uses `~/.franky/profile.toml` by default. Override it with `--profile` or `FRANKY_PROFILE_PATH`.

```toml
# ~/.franky/profile.toml
[setups]
claude = "~/.claude"
codex = "~/.codex"
opencode = "~/.config/opencode"
pi = "~/.pi"

[profile]
skills = ["~/team/skills/review.md"]
instructions = ["~/team/AGENTS.md"]
knowledge = ["~/team/architecture.md"]
```

Supported setup kinds are `claude`, `codex`, `opencode`, and `pi`. Unknown kinds and missing roots fail validation.

Paths support `~` and glob patterns. Franky includes a file once when multiple declarations match it.

## Setup sweep

Franky does not copy a setup directory wholesale. It uses a per-engine allowlist for capability files and directories.

Typical included paths are instruction files, `skills/`, `commands/`, `prompts/`, `rules/`, and `agents/`.

Franky excludes:

- Credentials, including `auth.json` and `~/.claude.json`.
- Sessions, projects, history, databases, caches, logs, and plugin trees.
- Hooks and mutable settings files.
- Binary or non-UTF-8 files.
- Denied targets reached through symlinks.

Denied directories are pruned before traversal. The sweep never walks large session or plugin trees.

Every included file passes the same fail-closed secret scan. One detected secret stops the complete run before container creation.

The scan detects private keys, common provider tokens, GitHub tokens, and assignments to known credential variables.

Remove the credential from the file, then run `franky profile check` again. The command names the file but never prints its contents.

## Limits

One combined profile has these limits:

| Scope | Limit |
|-------|------:|
| Included explicit, setup, and MCP files | 5,000 files |
| Included file data | 20 MiB |
| Glob and setup entries examined | 5,000 entries |
| Setup directories visited | 5,000 directories |
| Profile TOML file | 20 MiB |

Skipped binary and unreadable setup candidates consume the file and byte budget. Files that grow during a bounded read fail validation.

Explicit files must contain valid UTF-8. Setup sweeps skip unscannable files and report the count.

## MCP configuration

MCP is always explicit. A setup sweep can report MCP declarations, but it never enables them.

Declare the native config file, credential names, and exact hosts:

```toml
# ~/.franky/profile.toml
[profile]
mcp_configs = ["~/.codex/franky-mcp.config.toml"]
mcp_credentials = ["LINEAR_API_KEY"]
mcp_domains = ["mcp.linear.app"]
```

```toml
# ~/.codex/franky-mcp.config.toml
[mcp_servers.linear]
url = "https://mcp.linear.app/mcp"
bearer_token_env_var = "LINEAR_API_KEY"
```

Supply credential values only in the process environment:

```bash
export LINEAR_API_KEY=...
franky profile check
franky build https://github.com/you/repo/issues/42
```

Do not store MCP credentials in `~/.franky/config`. Credential declarations must match `[A-Z_][A-Z0-9_]*` and have non-empty process values.

Config strings can reference a credential as `NAME` or `${NAME}`. Credential-value fields must use `${NAME}`.

Literal secrets fail validation. Franky reserves its own runtime, proxy, HOME, PATH, Docker, and engine-home variables.

`mcp_domains` accepts hostnames only. It rejects schemes, ports, paths, and wildcards.

Every HTTP or HTTPS URL in an MCP config must use a declared host. The runtime proxy remains default-deny.

| Engine | MCP rule |
|--------|----------|
| Codex | Use only `~/.codex/franky-mcp.config.toml` with one top-level `mcp_servers` table. Franky passes explicit overrides with `--ignore-user-config`. |
| Claude | Use only `~/.claude/franky-mcp.json` with one top-level `mcpServers` object. Franky uses `--strict-mcp-config`. |
| Pi | Inject an MCP extension and its JSON or TOML config. Pi has no built-in MCP config loader. |
| OpenCode | Use an explicit validated native JSON or TOML config. |

After `franky connect jira`, and when the task repository is private, Franky also adds a built-in MCP server named `atlassian` (the Atlassian MCP server) for Claude and Codex, beside your profile servers. A profile server named `atlassian` is overridden by it. Pi and OpenCode get none. Franky requests read and search scopes only and denies write tools in the engine config; if Atlassian grants broader scopes, read-only is not enforced by Atlassian (`franky connect jira --status` shows the granted scopes).

MCP paths must be explicit files under HOME. Globs and other formats are not supported.

Franky does not install MCP servers. Use binaries in the image or a package runner such as `npx`.

## Runtime behavior

Before the agent starts, Franky:

1. Resolves setup and explicit files.
2. Validates limits, secrets, MCP credentials, and MCP hosts.
3. Packs HOME-relative files into a gzip archive.
4. Starts the task in profile-wait mode.
5. Streams the archive through `docker exec -i`.
6. Marks the profile ready only after extraction succeeds.

The archive never appears in process arguments or environment values. No ready marker within 120 seconds stops the task.

The prompt tells the agent where the setup exists and identifies a discovered PR specification.

Franky's autonomous rules take precedence. The agent must not wait for approval or use tools and paths absent from the container.

## Validation

`franky profile check` uses the same loader, limits, MCP validation, and secret scan as a real build.

Run it after each profile change. It reports setup counts, skipped files, MCP policy, and the selected PR specification.

Run `make smoke-profile` after changes to profile delivery, profile-wait startup, or task hardening.
