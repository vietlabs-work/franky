# Franky

A lean personal coding agent. Hand it a GitHub issue or a sentence, and it runs a
coding agent inside a fresh, hardened Docker container that clones the repo,
implements the change, and opens a pull request for you to review.

```
franky build https://github.com/you/repo/issues/42
franky build jira FOO-123 --repo you/repo
franky build "add a --json flag to the export command" --repo you/repo
franky build "fix the flaky retry test" --repo you/repo --engine claude

# Got review comments or red CI on a Franky PR? Iterate on it with follow-up commits.
franky iterate https://github.com/you/repo/pull/42
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
all ship in the one image.

| Engine | CLI | Auth | Notes |
|--------|-----|------|-------|
| `pi` (default) | `@earendil-works/pi-coding-agent` | BYOK provider key | MIT, 15+ providers (OpenRouter, Anthropic, OpenAI, Ollama, ...) |
| `claude` | `@anthropic-ai/claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` | Most capable; uses your Claude subscription |
| `codex` | `@openai/codex` | `CODEX_API_KEY` or `OPENAI_API_KEY` | OpenAI Codex headless (`codex exec`); API-key auth only |

Select with `--engine pi|claude|codex`, or set `FRANKY_ENGINE`. Resolution order:
`--engine` flag > `FRANKY_ENGINE` > default `pi`.

## Install

Franky is published to PyPI as `franky-agent` (the installed command is `franky`):

```
uv tool install franky-agent
# or pipx:
pipx install franky-agent
# or:
pip install franky-agent
```

On first run the CLI pulls the version-pinned, public GHCR images
(`ghcr.io/franky-agent/franky:X.Y.Z` and `ghcr.io/franky-agent/franky-proxy:X.Y.Z`),
so all you need is Docker - no registry login. (Point `FRANKY_GHCR_REPO` at a different
namespace if you host the images elsewhere.)

To move to a newer release later, run `franky update` - it detects how you
installed (uv tool / pipx / pip) and reinstalls the latest version from PyPI via the same
manager. `franky update --force` reinstalls even when already current. (A dev
checkout updates via `git`; `franky update` is a no-op there.)

**For local development**, skip GHCR and point at local builds:

```
docker build -t franky .
docker build -t franky-proxy proxy/
export FRANKY_IMAGE=franky
export FRANKY_PROXY_IMAGE=franky-proxy
```

Franky is agent-agnostic to develop, not just to run: `AGENTS.md` is the canonical
agent guide (build/test commands, architecture, the load-bearing invariants, how to
add an engine), so Codex, Cursor, pi, or Claude Code all start with the same context.
`CLAUDE.md` is a symlink to it.

## Quickstart

1. Install Docker. Images are pulled automatically from GHCR on first run (see
   Install above). For local dev only, build them manually (see Install above).
2. Install Franky:
   ```
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
3. Configure credentials with the interactive wizard:
   ```
   franky config init
   ```
   This writes `~/.franky/config` (mode 0600) and walks you through engine selection,
   `FRANKY_ALLOWED_REPOS`, `GH_TOKEN`, and engine creds. You can also set individual
   keys later:
   ```
   franky config set FRANKY_ALLOWED_REPOS
   franky config set GH_TOKEN          # secret - entered at a hidden prompt
   franky config list                  # view the file (secrets masked)
   franky config path                  # show where the file lives
   ```
   At minimum you need:
   - `FRANKY_ALLOWED_REPOS` - the trusted-repo allowlist (see below).
   - `GH_TOKEN` - scoped to contents + pull_requests on those repos.
   - the selected engine's creds (a provider key for `pi`,
     `CLAUDE_CODE_OAUTH_TOKEN` for `claude`, or `CODEX_API_KEY` / `OPENAI_API_KEY`
     for `codex`).
   - for JIRA tasks: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (host-side only,
     never forwarded into the container).
4. Run:
   ```
   franky build <gh-issue-url | jira KEY | "prose"> [--repo owner/repo] [--engine pi|claude|codex] [--plan-first]
   ```

Each run writes a redacted log to `tasks/<timestamp>.log` and prints the PR URL.

`--plan-first` adds an opt-in approval gate for sensitive targets: Franky runs a
read-only planning pass, prints the plan, and waits for explicit confirmation
before it builds or opens a PR. Decline (or run non-interactively) and nothing is
written. The default stays autonomous - the sandbox plus PR review is the gate.

`franky build` also does a quick (~1s, cached) check for a newer release and
prints a one-line hint if one exists - it never blocks the build. Silence it with
`FRANKY_NO_UPDATE_CHECK=1`, or set `FRANKY_AUTO_UPDATE=1` to auto-install the new
release for your next run. (Both are host-CLI only; neither reaches the container.)

## Iterating on a PR

Franky is no longer one-shot. When a PR it opened gets review comments or a red CI
check, point it back at the PR and it responds with **additive follow-up commits** on
the same branch:

```
franky iterate https://github.com/you/repo/pull/42 [--engine pi|claude|codex]
```

It runs the **same hardened, egress-controlled container** as `franky build`, but instead
of starting fresh it checks out the PR's existing branch, reads the review comments and
failing checks with `gh` (in-container, already allowlisted), addresses them, runs the
tests green, and pushes. It **never force-pushes, never rewrites history, never opens a new
PR, and never merges** - a human still reviews every change. The PR URL carries the repo, so
there is no `--repo` flag, and the repo allowlist gates it exactly like `build`.

Unlike `build` (which prints the new PR URL to stdout), `iterate` opens no new PR - on a
clean run it writes only an economics summary and a labeled completion line to stderr, and
nothing to stdout. Review the existing PR for the new commits. The redacted transcript still
lands in `tasks/<timestamp>.log`.

`iterate` is intended for Franky's **own** PRs. As a guardrail it is instructed to confirm
the PR's head branch is a `franky/*` branch in the same repo (not a fork) before touching
anything, and to stop otherwise. This is a prompt-level guard in the same register as the
"never merge" rule (the agent is autonomous); the hard bounds remain the repo allowlist, the
egress cage, and PR-not-merge. See the Security section.

## Security

Read this before pointing Franky at anything.

**Container hardening is load-bearing.** Because the agent runs autonomously
(claude with `--dangerously-skip-permissions`, codex with
`--dangerously-bypass-approvals-and-sandbox`, pi with its default tools), the
OS-level isolation is what bounds it, not tool-permission prompts. Franky runs the
container with:

- `--cap-drop=ALL`, then adds back only `CAP_SETUID`/`CAP_SETGID` (needed by the
  rootless Docker daemon - see "Docker-in-Docker" below)
- `--read-only` root filesystem; writable paths only via `--tmpfs` (the clone, the
  agent's HOME, and the rootless Docker data root, each owned by the non-root uid)
- `--pids-limit` and `--memory` caps (with `--memory-swap` = `--memory`, no swap)
- a non-root user (uid 1001) baked into the image
- **no Docker socket mount and no host bind mounts** - the repo is cloned inside
  the container and the nested Docker daemon is rootless, so the agent never
  touches your filesystem or your host's Docker daemon
- only the selected engine's required env vars passed in; nothing else

### Docker-in-Docker (always on)

Many repos cannot run their test suite without Docker (compose-based integration
tests, testcontainers, a `docker build` step). So every Franky container runs its
**own rootless Docker daemon** - the agent can `docker build`, `docker compose up`
test infra, and run testcontainers entirely inside the sandbox. Nothing to enable;
it is always available.

This is rootless DinD (a daemon running as the non-root `franky` user inside its
own user namespace), **not** a mounted host Docker socket and **not** `--privileged`.
It needs a few specific, minimal relaxations of the locked profile, applied to every
task and verified on Docker Desktop for Mac:

- `--security-opt=no-new-privileges` is **dropped** (it blocks the setuid uid-map
  helpers rootless Docker needs to start),
- `--security-opt=systempaths=unconfined` (unmasks `/proc` so the nested runtime can
  mount it for inner containers - far narrower than `--privileged`/`seccomp=unconfined`),
- `CAP_SETUID`/`CAP_SETGID` added back on top of `--cap-drop=ALL`, and `/dev/net/tun`
  for the rootless network stack.

The blast radius stays bounded by everything else (rootless user namespace, read-only
root, the egress cage below, no host FS, repo allowlist, PR-not-merge). The nested
daemon's image pulls and `docker build` fetches go **through the same egress proxy**
(it inherits `HTTP(S)_PROXY`), and inner containers have no route to the internet
except that proxy - verified: an off-allowlist `docker build` `FROM` or `RUN` fetch is
refused by the proxy, and a nested container's direct egress has no route out.

### Egress control

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
  `openrouter.ai`, `api.openai.com`), GitHub (clone/push/PR), the npm + PyPI
  registries, and - because
  Docker-in-Docker is always on - a broad set of well-known **container image
  registries** (Docker Hub + CDN, GHCR, GCR/Artifact Registry, `registry.k8s.io`,
  Quay, ECR Public, MCR, GitLab, plus the CDNs they serve layer blobs from). Add
  extra hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS` (comma-separated).

**Residual risk.** The allowlisted hosts are high-trust, but the agent can still
reach GitHub, your model provider, the package registries, and the container
registries above - so a determined injection could still smuggle data to one of
those (e.g. a gist, an issue comment). Treat allowlisted destinations as trusted,
not inert. Two consequences of always-on DinD specifically:

- **Wider reachable set + a relaxed profile on every task** (incl. non-Docker ones):
  the registry allowlist is broad (notably `.cloudfront.net`, a shared CDN), and the
  hardening relaxations above apply universally. This is a deliberate trade for
  "building/testing just works".
- **The agent can move its own creds into nested containers** (e.g. `docker run -e
  GH_TOKEN ...`). The egress allowlist still bounds *where* anything can go and
  PR-not-merge still bounds the damage, but the secret is no longer confined to a
  single process. There is also no per-inner-container resource limit and no
  cross-task concurrency cap - the outer `--memory`/`--pids` cap (~8 GB, tmpfs image
  storage is RAM) bounds one task's whole container tree.

v0 mitigations, still in force:

1. **Fail-closed trusted-repo allowlist.** Franky refuses any repo not in
   `FRANKY_ALLOWED_REPOS`, and refuses everything if that var is unset. This
   limits injection to content you already trust.

   The allowlist supports per-segment glob patterns (case-insensitive):
   - `my-org/my-repo` - exact match
   - `my-org/*` - every repo in `my-org`
   - `my-org/team-*` - repos with a name prefix
   - `*` - every repo the `GH_TOKEN` can reach (its **full scope** - a conscious opt-in,
     not the default; use only if the token is already narrowly scoped)
2. **Scope your tokens narrowly.** Give `GH_TOKEN` only contents + pull_requests
   on the target repos. Prefer a low-spend or separate API key for `pi`.
3. **PR, not merge.** Franky only opens PRs. You review before anything lands.
   `franky iterate` follows the same rule: it only pushes additive commits to an
   existing PR's branch (never force-push, never merge, never a new PR), and the
   "act only on a `franky/*` branch in the same repo" check is prompt-level - so
   point `iterate` only at PRs Franky itself opened, in an allowlisted repo.

**GitHub Actions warning.** Opening a PR can trigger workflows. A PR built from an
attacker-influenced issue could run attacker-influenced workflow code with your
repo's Actions secrets. Review workflow changes in the PR diff, and consider
requiring approval for workflow runs on PRs.

## Evals

Agent quality is probabilistic, so changes to the persona, prompt, model, or profile
should be gated on a measured **pass-rate**, not a hunch. The eval harness runs a golden
task set through the *real* Franky flow N times and reports pass-rate, plus a comparison
mode that reports the delta between two configs (e.g. one engine vs another).

It is **opt-in and out-of-band** (like the manual egress check) - it needs real Docker +
creds + a throwaway sandbox repo, so it is not part of the fast hermetic unit suite. Point
`evals/tasks.json` at your sandbox repo and run:

```
make eval ARGS="-n 3 --engine pi --compare-engine codex"
```

See [`evals/README.md`](evals/README.md) for setup, the task schema, and the success
checkers.

## Status

v0.0.1. Real end-to-end runs need live engine credentials, supplied out-of-band by
the operator. The pieces under test here are the container hardening, the egress
allowlist + proxy orchestration, the secret redaction, the trusted-repo allowlist,
and the engine abstraction.
