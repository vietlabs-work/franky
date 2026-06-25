# Profiles: injecting operator skills and instructions

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
| Secrets pass by name, never by value | **Unchanged** — the bundle is curated prose, not a credential |
| Fail-closed | **Maintained** — secret scan aborts the run before any container starts |
| Container hardening flags (`_HARDENING`) | **Unchanged** — no flags are added, removed, or relaxed |
| Egress stays default-deny | **Unchanged** — no new egress path is opened |

### What is NOT supported (Tier-2, future)

- MCP server configs (require their own egress + credentials — design separately).
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
```

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
