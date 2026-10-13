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

Supported setup kinds are `claude`, `codex`, `opencode`, `pi`, and `team` (see [Team setup](#team-setup)). Unknown kinds and missing roots fail validation.

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

## Team setup

`team` is an engine-neutral setup that a team keeps in one place and every engine can use. Declare its directory under HOME:

```toml
# ~/.franky/profile.toml
[setups]
team = "~/.franky/team"
```

The directory has a fixed layout. Franky reads nothing else from it.

| Path | Use |
|------|-----|
| `AGENTS.md` | Team instructions, always on. |
| `rules/*.md` | Team rules, always on. |
| `skills/<name>/SKILL.md` and text references | Skills. |
| `knowledge/**.md` | Reference notes, read on demand. |
| `commands/pr.md` | The PR-description spec. It wins over the operator's own. |
| `MANIFEST.json` | `{"schema": 1, "source", "commit", "plugins": [], "skills": [{"name", "plugin", "path"}]}`. Optional. |

A team setup ships Markdown and plain-text documents only (`.md`, `.markdown`, `.txt`, `.rst`), plus `MANIFEST.json`. Scripts (with or without an execute bit), YAML, JSON, and MCP config (including a nested `.mcp.json`), hooks, settings files, credentials, and binary files never ship. `franky profile check` counts the refused files. Every shipped file passes the same secret scan and limits as the rest of the profile. A broken `MANIFEST.json` fails validation. `franky profile check` prints `team: <source>@<commit>, N plugin(s), M skill(s)`.

Franky maps the team setup onto the selected engine when it packs the bundle. The files also stay at their HOME-relative path (`~/<dir>`), which the prompt names as a pointer.

| Engine | Skills | Instructions | Status |
|--------|--------|--------------|--------|
| `claude` | `~/.claude/skills/<name>/` | `~/.claude/CLAUDE.md` | Verified. |
| `codex` | `$CODEX_HOME/skills/<name>/` | `$CODEX_HOME/AGENTS.md` | Verified: with `CODEX_HOME` set, `codex debug prompt-input` shows its `AGENTS.md` as instructions and lists its `skills/`. |
| `opencode` | `~/.config/opencode/skills/<name>/` | `~/.config/opencode/AGENTS.md` | Unverified. The prompt also points at the team directory. |
| `pi` | `~/.pi/skills/<name>/` | `~/.pi/AGENTS.md` | Unverified. The prompt also points at the team directory. |

The instruction file is one member. It holds the operator's own text for that file first, then the team `AGENTS.md` and `rules/` under `## Team rules (always on)`. A team skill replaces an operator skill of the same name. A profile without `team` packs exactly as before.

Codex runs use a per-run `CODEX_HOME` (`~/.codex` in the task's own volume). The persistent subscription volume mounts beside it, and only `auth.json` is linked in, so a token refresh still persists but no skill or session file reaches the shared volume.

## Repository setup

When the profile declares `team`, Franky also loads the repository's own agent setup before the engine starts. It runs in the same profile-wait phase, through `franky-repo-setup` in the image, as the task user, with the run's GitHub token and proxy.

| Run | Read at |
|-----|---------|
| Every run (`review-pr`, `iterate`, `build`, `plan`, `build --plan-first`) | The default-branch head. |
| `job resume` | The base commit the run recorded, else the default-branch head. |

`job replay` loads no profile.

Because the commit is the default-branch head, never the PR head or the PR base (an author can pick a base branch they made), a PR cannot add skills or rules that a review or iteration then trusts. The working directory stays `/work`, so the repository's own `.claude/settings.json` hooks never load.

The mapper fetches only these paths: `AGENTS.md`, `CLAUDE.md`, `.claude/skills`, `.claude/rules`, `.claude/agents`, `.agents/skills`, `.codex/skills`, and `agent/rules`. It skips symlinks, executables, submodules, settings, hooks, MCP and env files, keys, non-UTF-8 files, and everything else. It stops the run when the setup passes 500 files or 4 MiB, and skips a file over 256 KiB.

It copies the text into the engine's native locations. Skill and agent files keep only an allow-list of header keys (`name`, `description`; agents also `tools`, `model`): `hooks`, `mcpServers`, `permissionMode` and every other key are dropped, and a file whose header cannot be read is skipped and listed in the index. Team skills get the same treatment. A repository skill replaces a team skill of the same name. Repository instruction text, rules included, goes after the team text under `## Repository rules`. `.claude/agents` is copied for `claude` only.

The prompt carries an index of what loaded (skill names, descriptions, paths, rules, instruction files). A repository with no setup is normal: the index says none was found.

A fetch or mapping error fails the run before the engine starts. It ends as `agent_error` with a one-line retryable message that never carries a credential, and the run log shows a `repository setup loaded` line on success.

Precedence when setups disagree: the prompt, then the repository setup, then the team setup, then the operator setup. None of them relaxes a read-only pass.

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

After `franky connect jira`, and when the task repository is private, Franky also adds a built-in MCP server named `atlassian` (the Atlassian MCP server) for Claude and Codex, beside your profile servers. A profile server named `atlassian` is overridden by it. Pi and OpenCode get none. See [Atlassian tools](configuration.md#atlassian-tools) for scopes and tokens.

MCP paths must be explicit files under HOME. Globs and other formats are not supported.

Franky does not install MCP servers. Use binaries in the image or a package runner such as `npx`.

## Runtime behavior

Before the agent starts, Franky:

1. Resolves setup and explicit files.
2. Validates limits, secrets, MCP credentials, and MCP hosts.
3. Packs HOME-relative files into a gzip archive.
4. Starts the task in profile-wait mode.
5. Streams the archive through `docker exec -i`.
6. Loads the repository setup when the profile declares `team`.
7. Marks the profile ready only after both succeed.

The archive never appears in process arguments or environment values. No ready marker within 120 seconds stops the task (220 seconds when a repository setup is loaded, since the mapper has its own 90-second deadline).

The prompt of `build`, `iterate`, `review-pr`, and `plan` (and the planning pass of `build --plan-first`) tells the agent where the setup exists. `build` and `iterate` also identify a discovered PR specification. The read-only passes (`review-pr`, `plan`, the planning pass) omit it, finish by reporting as their own prompt asks, and the setup files never relax their read-only rules.

Franky's autonomous rules take precedence. The agent must not wait for approval or use tools and paths absent from the container.

## Validation

`franky profile check` uses the same loader, limits, MCP validation, and secret scan as a real build.

Run it after each profile change. It reports setup counts, skipped files, MCP policy, and the selected PR specification.

`make smoke-profile` also checks that `claude` and `codex` list a team skill and a repository skill at session start (it needs Docker and engine credentials).

Run `make smoke-profile` after changes to profile delivery, profile-wait startup, or task hardening.
