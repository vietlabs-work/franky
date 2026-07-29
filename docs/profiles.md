# Profiles: injecting your agentic-coding setup and MCP config

## Problem

The operator has invested in custom skills, CLAUDE.md-style instruction files, commands, and
agent definitions for their local Claude / Codex / pi setup.  None of it reaches the agent
running inside Franky's container, so the in-container agent performs below the operator's
local agent on the same task - and the PRs it opens look nothing like the ones the operator
writes.

## Design

### What is declared

Two ways, and the first is the one to reach for:

- **`[setups]`** - a whole agentic-coding setup, by directory: `claude = "~/.claude"`. Franky
  sweeps it through a per-kind manifest and injects the capability surface.
- **`[profile]`** - explicit file lists (`skills` / `instructions` / `knowledge` / `mcp_configs`)
  for anything that lives outside a setup dir, or when you want exact control.

### Why a setup directory is swept, not tarred

These directories are enormous, and the bulk of them is exactly what must never enter a
container that carries live credentials. Measured on a real machine:

| Path | Size | Injected? |
|------|------|-----------|
| `~/.claude/projects`, `~/.codex/sessions` | 893 MB | Never - conversation transcripts |
| `~/.claude/plugins`, `~/.codex/plugins` | 107 MB | Never - binaries and vendor trees |
| `~/.codex/auth.json` | 8 KB | Never - live credentials |
| `history.jsonl`, `*.sqlite*`, `settings*.json` | 4 MB | Never - local state / secrets |
| `CLAUDE.md`/`AGENTS.md`, `skills/`, `commands/`, `prompts/`, `rules/`, `agents/` | ~1.1 MB | **Yes** |

So the sweep is an **allowlist** of the capability-bearing subtrees (per kind, in
`franky/setups.py`), with a **deny gate** on top for anything credential- or state-shaped that
could appear inside one of them. Three properties are load-bearing:

- Deny is matched on the **symlink-resolved realpath** too, so a symlink named `notes.md`
  pointing at `auth.json` cannot smuggle a credential in.
- Only **UTF-8-decodable** files ship. The secret scan can only read text, so shipping bytes it
  cannot inspect would be an unscanned hole; binaries are skipped and counted.
- Denied directories are **pruned, not filtered** - `os.walk` never descends into `projects/`,
  so sweeping a 1 GB dir is fast.

Whatever the declaration, every file lands in the SAME fail-closed secret scan and the same
HOME-relative packing. A directory declaration is a shorthand for a file list, never a laxer
path.

### Injection mechanism

On the host, Franky:

1. Expands `[setups]` through the manifests and resolves the explicit `[profile]` lists.
2. Runs a **secret scan** (fail-closed): one credential pattern in any file refuses the entire
   run before a container is started.
3. Packs the clean files into a gzip tar with paths relative to HOME.

Then, around container start:

4. The task container starts with `-e FRANKY_PROFILE_WAIT=1` and blocks in its entrypoint.
5. The host pipes the tar into it over `docker exec -i` (`tar -xzf - -C $HOME`, as the image's
   own uid) and touches a ready marker **last**, only after a clean extract.
6. The entrypoint sees the marker and execs the engine, which finds the files exactly where it
   would locally (e.g. `~/.claude/CLAUDE.md`). No marker within 120s -> it exits nonzero rather
   than silently running a build without the operator's setup.

The bundle never touches the argv or the environment. That is not cosmetic: a swept setup runs
to ~400 KB gzipped, and Linux caps a single argv string at 128 KB - the earlier by-value
`FRANKY_PROFILE_BUNDLE` env var could not carry it. Streaming also keeps the content out of
`ps`, and it is the only mechanism that works at all here, since `docker cp` INTO a
`--read-only` container is refused by the daemon (the same reason `job resume` streams its
workspace).

### Why this preserves the load-bearing invariants

| Invariant | Status after this change |
|-----------|--------------------------|
| No bind mounts, no host FS access | **Unchanged** - no mount is added; content arrives over an exec's stdin |
| Secrets pass by name, never by value | **Unchanged** - the bundle is prose plus validated credential NAMES, and it rides neither argv nor env |
| Fail-closed | **Maintained** - sweep guards, config validation, and the secret scan all abort before any container starts; a failed inject refuses the run |
| Container hardening flags (`_HARDENING`) | **Unchanged** - no flag added, removed, or relaxed |
| Egress stays default-deny | **Unchanged** - a sweep never enables MCP, so it never opens a host |

### What is NOT supported

- `~/.claude.json`, `auth.json`, or any credential-bearing file (never).
- Tarring a setup directory wholesale - only the manifest's subtrees are swept.
- Hooks and `settings.json`: hooks are host shell scripts pointing at host-only tooling, and
  `settings.local.json` can carry secrets.
- Auto-enabling MCP from a swept config (see [MCP configuration](#mcp-configuration)).

### The prompt side

Injecting files is only half of it - a slash command is never auto-invoked by an engine, and an
instruction file written for an interactive session can actively derail an autonomous run. So
when `[setups]` is declared, the prompt gains a short block that:

- names where the setup was unpacked, and **which file is the PR-description spec** (found by
  convention: a `pr` command / prompt / skill), telling the agent to follow it for the PR title
  and body, overriding Franky's built-in shape;
- states three precedence rules: Franky's conventions win on conflict; **never wait for
  approval** or treat a plan/review gate in those files as blocking (there is nobody to answer,
  and the run would end with no PR); ignore anything naming a tool, path, or service that does
  not exist in the container.

Everything else self-neutralizes - the agent looks for a host-only tool, finds nothing, moves on.

## Profile file format

Create `~/.franky/profile.toml`. The usual profile is just the `[setups]` table:

```toml
# ~/.franky/profile.toml
[setups]
claude = "~/.claude"
codex = "~/.codex"
```

Supported kinds and their default roots: `claude` (`~/.claude`), `codex` (`~/.codex`),
`opencode` (`~/.config/opencode`), `pi` (`~/.pi`). An unknown kind is refused (so a typo'd
`cluade` fails loudly instead of silently injecting nothing), as is a root that does not exist
or sits outside HOME - members are packed HOME-relative, so a root elsewhere has no
in-container location.

Add explicit lists only for what a sweep does not cover:

```toml
[profile]
# Skills that live outside a setup dir
skills = ["~/some/standalone-skill.md"]

# Extra CLAUDE.md-style instruction files
instructions = ["~/team/conventions.md"]

# Reference / knowledge docs
knowledge = ["~/docs/architecture.md"]
```

Paths support `~` and glob patterns. A file that is both explicitly listed and swept from a
setup is injected once.

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

A `[setups]` sweep NEVER enables MCP, even when the setup declares servers. `~/.codex/config.toml`
and `~/.claude/settings.json` are excluded outright, and enabling a server would add a host to
the default-deny egress proxy and forward a credential - which must be a conscious act, not a
side effect of pointing at a directory. `franky profile check` REPORTS what it noticed
("declares MCP servers - NOT injected") so the choice is visible but never automatic.

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
franky profile init     # interactive wizard: offers the setups it finds on this machine
                        #   (one confirm, no typing), then any explicit extras;
                        #   merges into an existing file, never clobbers it
franky profile check    # dry-run: files + MCP config/creds/domains + secret scan;
                        #   prints what WOULD inject; nonzero exit + offending file on a hit
franky profile show     # print the profile.toml + the glob-expanded file list
franky profile path     # print the resolved profile path
```

`franky profile check` is the high-value one: it runs the **same** `load_profile` +
MCP validation and secret-scan path the build runs, so it catches missing files or process
credentials, malformed config, undeclared hosts, and literal secrets before a build. It
names offending files and variables but never prints credential values or file contents.

For a declared setup it prints one summary line per kind rather than a wall of per-file lines
(a real sweep is ~100 files), plus what was skipped, which PR spec was found, and any MCP
declaration it noticed:

```
  setup  claude: /Users/you/.claude -> 32 file(s), 385 KB
  setup  codex: /Users/you/.codex -> 104 file(s), 767 KB
           skipped 6 non-text file(s) (unscannable, never injected)
           /Users/you/.codex/config.toml declares MCP servers - NOT injected. ...
  pr spec: /Users/you/.claude/commands/pr.md
OK: 136 file(s), 1180332 bytes would be injected
```

Problem files (a secret hit, an unreadable file) are ALWAYS named individually, whichever
declaration they came from.

`franky config init` also offers to set up a profile at the end of the wizard, so the
feature is discoverable during onboarding.

`make smoke-profile` is the real-Docker gate for the injection path itself (the pytest suite
mocks docker entirely, so it cannot catch a daemon-level refusal). Run it before merging any
change to `container.deliver_profile`, the profile-wait branch of the entrypoint, or the task
hardening profile.

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
