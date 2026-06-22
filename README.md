# Franky

A lean personal coding agent. Hand it a GitHub issue or a sentence, and it runs a
coding agent inside a fresh, hardened Docker container that clones the repo,
implements the change, and opens a pull request for you to review.

```
franky build https://github.com/you/repo/issues/42
franky build "add a --json flag to the export command" --repo you/repo
franky build "fix the flaky retry test" --repo you/repo --engine claude
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
both ship in the one image.

| Engine | CLI | Auth | Notes |
|--------|-----|------|-------|
| `pi` (default) | `@earendil-works/pi-coding-agent` | BYOK provider key | MIT, 15+ providers (OpenRouter, Anthropic, OpenAI, Ollama, ...) |
| `claude` | `@anthropic-ai/claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` | Most capable; uses your Claude subscription |

Select with `--engine pi|claude`, or set `FRANKY_ENGINE`. Resolution order:
`--engine` flag > `FRANKY_ENGINE` > default `pi`.

## Install

While the repository is private, install from the git tag:

```
uv tool install git+ssh://git@github.com/vietlabs-work/franky@vX.Y.Z
# or pipx:
pipx install git+ssh://git@github.com/vietlabs-work/franky@vX.Y.Z
# or download the wheel from the GitHub Release and pip install it
```

On first run the CLI pulls the version-pinned GHCR images
(`ghcr.io/vietlabs-work/franky:X.Y.Z` and `ghcr.io/vietlabs-work/franky-proxy:X.Y.Z`),
so you need Docker and, while the packages are private, `docker login ghcr.io`
with a PAT that has `read:packages`.

To move to a newer release later, run `franky update` - it detects how you
installed (uv tool / pipx / pip) and reinstalls the latest tag via the same
manager. `franky update --force` reinstalls even when already current. (A dev
checkout updates via `git`; `franky update` is a no-op there.)

**For local development**, skip GHCR and point at local builds:

```
docker build -t franky .
docker build -t franky-proxy proxy/
export FRANKY_IMAGE=franky
export FRANKY_PROXY_IMAGE=franky-proxy
```

## Quickstart

1. Install Docker. Images are pulled automatically from GHCR on first run (see
   Install above). For local dev only, build them manually (see Install above).
2. Install Franky:
   ```
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
3. Configure credentials. Copy `.env.example` to `.env` and fill it in. At minimum:
   - `FRANKY_ALLOWED_REPOS` - comma-separated `owner/repo` allowlist (required).
   - `GH_TOKEN` - scoped to contents + pull_requests on those repos.
   - the selected engine's creds (a provider key for `pi`, or
     `CLAUDE_CODE_OAUTH_TOKEN` for `claude`).
4. Run:
   ```
   franky build <gh-issue-url | "prose"> [--repo owner/repo] [--engine pi|claude]
   ```

Each run writes a redacted log to `tasks/<timestamp>.log` and prints the PR URL.

## Security

Read this before pointing Franky at anything.

**Container hardening is load-bearing.** Because the agent runs autonomously
(claude with `--dangerously-skip-permissions`, pi with its default tools), the
OS-level isolation is what bounds it, not tool-permission prompts. Franky runs the
container with:

- `--cap-drop=ALL` and `--security-opt=no-new-privileges`
- `--read-only` root filesystem, writable work only via `--tmpfs /work`
- `--pids-limit` and `--memory` caps
- a non-root user baked into the image
- **no Docker socket mount and no host bind mounts** - the repo is cloned inside
  the container, so the agent never touches your filesystem
- only the selected engine's required env vars passed in; nothing else

### Egress control (v0.2)

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
- **The allowlist** covers: your engine's provider host (e.g. `api.anthropic.com`,
  `openrouter.ai`), GitHub (clone/push/PR), and the npm + PyPI registries. Add
  extra hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS` (comma-separated).

**Residual risk.** The allowlisted hosts are high-trust, but the agent can still
reach GitHub, your model provider, and the package registries - so a determined
injection could still smuggle data to one of those (e.g. a gist, an issue
comment). Treat allowlisted destinations as trusted, not inert.

v0 mitigations, still in force:

1. **Fail-closed trusted-repo allowlist.** Franky refuses any repo not in
   `FRANKY_ALLOWED_REPOS`, and refuses everything if that var is unset. This
   limits injection to content you already trust.
2. **Scope your tokens narrowly.** Give `GH_TOKEN` only contents + pull_requests
   on the target repos. Prefer a low-spend or separate API key for `pi`.
3. **PR, not merge.** Franky only opens PRs. You review before anything lands.

**GitHub Actions warning.** Opening a PR can trigger workflows. A PR built from an
attacker-influenced issue could run attacker-influenced workflow code with your
repo's Actions secrets. Review workflow changes in the PR diff, and consider
requiring approval for workflow runs on PRs.

## Status

v0.2. Real end-to-end runs need live engine credentials, supplied out-of-band by
the operator. The pieces under test here are the container hardening, the egress
allowlist + proxy orchestration, the secret redaction, the trusted-repo allowlist,
and the engine abstraction.
