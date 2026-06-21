# Franky

A lean personal coding agent. Hand it a GitHub issue or a sentence, and it runs a
coding agent inside a fresh, hardened Docker container that clones the repo,
implements the change, and opens a pull request for you to review.

```
franky build https://github.com/you/repo/issues/42
franky build "add a --json flag to the export command" --repo you/repo
franky build "fix the flaky retry test" --repo you/repo --engine claude
```

The agent is autonomous inside the container. The safety gate is three layers:
a hardened, network-isolated-from-the-host container, a fail-closed trusted-repo
allowlist, and the fact that Franky opens a PR rather than merging - a human still
reviews every change.

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

## Quickstart

1. Install Docker and build the image once before first use:
   ```
   docker build -t franky .
   ```
   Franky checks the image exists before each run and tells you to build it if not.
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

**Open egress is the v0 residual risk.** The container can still reach the network
(it has to, to clone and push). That means a prompt-injected agent - fed a
malicious issue or a poisoned repo - could exfiltrate whatever credentials are in
the container env: your Claude subscription token, or your BYOK API key. The
disposable container does NOT bound this. Only egress control does, and that is
deferred to v0.2 (an egress-proxy sidecar).

v0 mitigations, none of which fully close the above:

1. **Fail-closed trusted-repo allowlist.** Franky refuses any repo not in
   `FRANKY_ALLOWED_REPOS`, and refuses everything if that var is unset. This
   limits injection to content you already trust.
2. **Scope your tokens narrowly.** Give `GH_TOKEN` only contents + pull_requests
   on the target repos. Prefer a low-spend or separate API key for `pi`.
3. **PR, not merge.** Franky only opens PRs. You review before anything lands.

**Do not point Franky at issues or repos whose content you do not trust until
egress filtering lands (v0.2).**

**GitHub Actions warning.** Opening a PR can trigger workflows. A PR built from an
attacker-influenced issue could run attacker-influenced workflow code with your
repo's Actions secrets. Review workflow changes in the PR diff, and consider
requiring approval for workflow runs on PRs.

## Status

v0. Real end-to-end runs need live engine credentials, supplied out-of-band by the
operator. The pieces under test here are the container hardening, the secret
redaction, the allowlist, and the engine abstraction.
