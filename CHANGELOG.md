# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Third engine `codex` (OpenAI Codex, `@openai/codex`): `franky build --engine codex` runs
  `codex exec --json --dangerously-bypass-approvals-and-sandbox` headless in the container.
  API-key auth (`CODEX_API_KEY` or `OPENAI_API_KEY`); `api.openai.com` is allowlisted only
  when a codex key is present. OAuth/file-based auth is out of scope. The `--engine` CLI
  choice is now derived from the engine registry so it can never drift. Completes #29.
- Per-run economics summary (tokens, est. cost, duration) printed at the end of
  `franky build` and appended to `tasks/<ts>.log`, redacted. Best-effort: unparseable
  usage degrades to "unknown" and never fails the run; cost is always labeled an
  estimate (#26).
- Always-on rootless Docker-in-Docker: every task container runs its own rootless Docker
  daemon, so the agent can `docker build`, `docker compose up` test infra, and run
  testcontainers inside the sandbox - building/testing repos whose suites need local infra.
  No host socket, no `--privileged`; the nested daemon's pulls + builds go through the egress
  proxy and the container registries are added to the default allowlist (#12).
- Best-effort auto-update hint on `franky build`: a tight (~1s), cached check prints a
  one-line stderr hint when a newer release exists, then proceeds - never blocks, never
  re-execs. `FRANKY_NO_UPDATE_CHECK=1` silences it; `FRANKY_AUTO_UPDATE=1` opts in to
  installing for the next run. Dev checkout / offline -> silent (#11).
- `franky update [--force]`: on-demand self-update to the latest published release via the
  detected installer (uv tool / pipx / pip). Dev checkout -> git hint; undetectable installer
  -> manual hint, nonzero exit. Latest-tag fetch via `gh` then REST (`GH_TOKEN` fallback) (#10).
- Release pipeline: `make release VERSION=x.y.z` bumps version, commits, tags, pushes; CI
  publishes wheel + both GHCR images (franky, franky-proxy) + a GitHub Release (#8).
- Default-deny egress proxy (Squid) over a Docker `--internal` network; task container
  reaches only allowlisted hosts via a creds-blind CONNECT proxy (#1).
- CI workflow: pytest across Python 3.10-3.13 + ruff check/format on every PR and push
  to main (#3).

### Changed
- Container hardening profile relaxed (minimally) to support rootless DinD, applied to every
  task: `--security-opt=no-new-privileges` dropped (incompatible with the rootless uid-map
  helpers), `+systempaths=unconfined`, `CAP_SETUID`/`CAP_SETGID` added back on `--cap-drop=ALL`,
  `+/dev/net/tun`; `--memory` 4g->8g (`--memory-swap` pinned, no swap), `--pids-limit`
  512->2048, rootless data root on a size-capped tmpfs. No host socket / no host bind mount
  still hold. See the README security section for the rationale and residual risk (#12).
- Engine abstraction decoupled from pi-specifics: each `Engine` now owns its own missing-creds
  hint (`cred_hint`), so the fail-closed refusal names the resolved engine's vars and shared
  `config.py` no longer imports `PI_PROVIDER_VARS`. Documented the per-engine guardrail-bypass
  invariant (each engine disables its own approval/sandbox because the container is the
  boundary). Part of #29 (the CodexEngine that completes it is in Added above).
