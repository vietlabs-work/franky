# Profiles: injecting operator skills, instructions, and MCP config

## Problem

The operator has invested in custom skills, CLAUDE.md-style instruction files, and
accumulated knowledge docs for their local Claude/pi setup.  None of it reaches the
agent running inside Franky's container, so the in-container agent performs below the
operator's local agent on the same task.

## Design

### Injection mechanism: Option A — tmpfs-seed via entrypoint

The operator declares an explicit allowlist of files in `~/.franky/profile.toml`.
On the host, Franky:

1. Resolves and reads each listed file.
2. Runs a **secret scan** (fail-closed): if any file contains a credential pattern
   (private key, API token, known secret env-var assignment), the entire run is
   refused before any container is started.
3. Packs the clean files into a gzip-compressed tar archive with paths relative
   to HOME, then base64-encodes the archive.
4. Passes the encoded bundle as the env var `FRANKY_PROFILE_BUNDLE` **by value**
   (like the proxy URL — it is not a secret, so inline `-e KEY=VALUE` is correct
   and does not weaken the secret-by-name discipline).

Inside the container, the DinD entrypoint (`franky-dind-entrypoint.sh`) decodes and
extracts the bundle into `$HOME` before exec-ing the engine, so the agent finds the
files exactly where it would locally (e.g. `~/.claude/CLAUDE.md`).

### Why Option A preserves the load-bearing invariants

| Invariant | Status after this change |
|-----------|--------------------------|
| No bind mounts, no host FS access | **Unchanged** — no new mount is added; content arrives via an env var |
| Secrets pass by name, never by value | **Unchanged** - the bundle contains prose or validated credential names, never values |
| Fail-closed | **Maintained** - config validation and secret scan abort before any container starts |
| Container hardening flags (`_HARDENING`) | **Unchanged** — no flags are added, removed, or relaxed |
| Egress stays default-deny | **Unchanged** — no new egress path is opened |

### What is NOT supported

- `~/.claude.json` or any credential-bearing file (never).
- Auto-discovery of "all my skills" without an explicit allowlist.

## Profile file format

Create `~/.franky/profile.toml`:

```toml
# ~/.franky/profile.toml
# Curated files to inject into the Franky container.
# Paths support ~ and glob patterns. Each file is secret-scanned before injection.

[profile]
# Skills (agent instruction files, typically ~/.claude/skills/*.md)
skills = [
    "~/.claude/skills/coding-style.md",
    "~/.claude/skills/testing-conventions.md",
]

# CLAUDE.md-style global instruction files
instructions = [
    "~/.claude/CLAUDE.md",
]

# Reference / knowledge docs
knowledge = [
    "~/docs/architecture.md",
]

# Your PR-description spec (at most ONE file) - see "PR-description template" below
pr_template = ["~/.claude/commands/pr.md"]
```

## PR-description template

Franky's built-in PR convention is deliberately minimal: a conventional-commit title and a
what / why / test-plan body. If you have your own house style for a PR description, declare it
as `pr_template` and the in-container agent follows it instead.

```toml
[profile]
pr_template = ["~/.claude/commands/pr.md"]
```

Unlike every other category - which lands at its HOME-relative path - this file always unpacks
to the fixed path `~/.franky/pr-template.md` inside the container, so `prompt.py` can name it
literally. That is why the category takes **at most one file** and rejects globs: two entries
would collide on the one destination.

Every PR-opening prompt (`build`, `job resume`, `job replay --open-pr`) then instructs the agent
to read that path if it exists and follow it for the PR title and body, **overriding** the
built-in shape wherever the two disagree. With no `pr_template` the file is absent and the
built-in shape stands, so nothing changes for a profile-less run.

Two things to know when you point this at a spec you wrote for an interactive tool (a Claude
Code slash command, a Codex prompt):

- The agent is told to take only the **title/body spec** and ignore workflow steps, tool names,
  and approval gates in it. A "wait for approval before `gh pr create`" step would otherwise
  stall an autonomous run into a `no_pr` failure.
- A required closing keyword (`Closes #42`, so the issue auto-closes on merge) survives the
  override - the template shapes the body, it does not opt out of that contract.

`job resume` and `job replay` do not pack a profile bundle today, so a resumed or replayed run
falls back to the built-in shape.

## MCP configuration

Profiles can also carry engine-native JSON or TOML MCP configuration. Franky validates
the config before packing it, passes credentials by environment-variable name, and adds
only the declared hosts to the existing proxy allowlist.

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

Supply each credential in the process environment that starts Franky:

```bash
export LINEAR_API_KEY=...
franky profile check
franky build https://github.com/you/repo/issues/42
```

MCP credentials are intentionally not loaded from `~/.franky/config`. Each name must
match `[A-Z_][A-Z0-9_]*`, have a non-empty process value, and be referenced by a config.
Config strings may reference a declared name as exact `NAME` or `${NAME}`. A field whose
key is the credential name must use exactly `${NAME}`; a literal value is refused.
Franky's own `FRANKY_*`, proxy, HOME, PATH, Docker, and engine-home variables are reserved.
Credential-like config fields also refuse literal values and must name a declaration.

`mcp_domains` entries are hostnames only. Every explicit HTTP(S) URL in a config must use
a declared hostname. Schemes, ports, paths, and wildcards are rejected in the domain list.
The proxy remains default-deny at runtime.

Codex keeps `--ignore-user-config`. Its MCP file must be exactly
`~/.codex/franky-mcp.config.toml` and contain only the top-level `mcp_servers` table.
Franky parses that table and passes each server as an explicit `-c` override, so mutable
Codex user config is never enabled.

Claude's reserved file is `~/.claude/franky-mcp.json`. It may contain only the top-level
`mcpServers` object; Franky starts Claude with that exact container path through
`--mcp-config ... --strict-mcp-config`. `~/.claude.json` remains unsupported.

Pi has no built-in MCP config loader. Inject a Pi MCP extension at its normal HOME-relative
path through an existing profile file list, and let that extension read a bundled JSON/TOML
config. `mcp_credentials` and `mcp_domains` still provide its named credentials and runtime
egress policy. Franky does not claim an arbitrary Pi config will auto-load.

MCP config paths are explicit files under HOME; globs and formats other than JSON/TOML are
refused. Franky does not install servers. Use binaries already in the image or `npx`.

## Usage

```bash
# Auto-discovers ~/.franky/profile.toml when present:
franky build https://github.com/you/repo/issues/42

# Explicit profile path:
franky build https://github.com/you/repo/issues/42 --profile ~/my-profiles/python.toml

# Override via env var (useful in CI / config file):
FRANKY_PROFILE_PATH=~/my-profiles/python.toml franky build ...

# Set in ~/.franky/config:
franky config set FRANKY_PROFILE_PATH
```

## CLI commands

Instead of hand-writing the TOML, use the `franky profile` command group (mirrors
`franky config`). Every command honors `FRANKY_PROFILE_PATH`.

```bash
franky profile init     # interactive wizard: scaffold ~/.franky/profile.toml
                        #   (merges into an existing file, never clobbers it)
franky profile check    # dry-run: files + MCP config/creds/domains + secret scan;
                        #   prints what WOULD inject; nonzero exit + offending file on a hit
franky profile show     # print the profile.toml + the glob-expanded file list
franky profile path     # print the resolved profile path
```

`franky profile check` is the high-value one: it runs the **same** `load_profile` +
MCP validation and secret-scan path the build runs, so it catches missing files or process
credentials, malformed config, undeclared hosts, and literal secrets before a build. It
names offending files and variables but never prints credential values or file contents.

`franky config init` also offers to set up a profile at the end of the wizard, so the
feature is discoverable during onboarding.

## Secret scan patterns

The following patterns trigger a fail-closed refusal:

- PEM private key blocks (`-----BEGIN ... PRIVATE KEY-----`)
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` prefixes)
- Anthropic API keys (`sk-ant-...`)
- OpenRouter API keys (`sk-or-v1-...`)
- OpenAI-style API keys (`sk-` followed by 48+ characters)
- Env-var assignments of known secret variables (`GH_TOKEN=...`, `ANTHROPIC_API_KEY=...`, etc.)

If a file is flagged, remove the credential from the file before adding it to the profile.
The scan is conservative — it targets high-confidence patterns to avoid false positives on
legitimate technical prose.
