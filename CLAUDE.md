# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Franky is a lean personal coding agent. Given a GitHub issue URL or a prose sentence, it runs a pluggable coding agent (`pi` default, `claude` alt) inside a fresh, hardened Docker container that clones the target repo, makes the change, and opens a PR. The agent is autonomous inside the container; safety comes from OS-level container isolation + a default-deny egress allowlist + a fail-closed repo allowlist + opening a PR (never merging).

## Commands

```bash
# Install (editable) into a venv
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Build BOTH images (required once before any real run)
docker build -t franky .
docker build -t franky-proxy proxy/

# Run the full test suite
.venv/bin/python -m pytest -q

# Run one file / one test
.venv/bin/python -m pytest tests/test_container.py -q
.venv/bin/python -m pytest tests/test_container.py::test_build_docker_argv_hardening_flags -q

# Invoke the CLI
franky build <gh-issue-url | "prose"> [--repo owner/repo] [--engine pi|claude]
franky version
```

Tests use no real Docker, no network, and no live creds: every side effect (subprocess runner, env mapping, sleeper) is dependency-injected, so the suite runs in well under a second. Keep it that way - never reach for real `docker`/`gh` in a test. The egress block/allow *behavior* is verified manually against real Docker (see the PR for issue #1), not in CI.

## Architecture

A pure-logic core is wired together by a thin CLI; only one module touches the outside world.

```
cli.build  ->  load_config(engine, env)      config.py   resolve engine, fail-closed passthrough env
           ->  parse_task(input, repo, allow) task.py     issue-URL vs prose, allowlist gate
           ->  build_prompt(spec)             prompt.py   persona + task + literal conventions
           ->  engine.inner_argv(prompt)      engine.py   per-engine headless argv
           ->  ensure_image(franky/-proxy)    container.py
           ->  run_in_container(cfg, argv)    container.py  the ONLY side-effect layer:
           ->  engine.parse_pr_url(out, repo)               net + proxy + task lifecycle
```

`run_in_container` orchestrates an `--internal` Docker network + a Squid proxy sidecar around the task container (allowlist policy from `egress.py`); the task's only egress path is the proxy. See "Egress control" below.

Module responsibilities:

| Module | Role |
|--------|------|
| `franky/cli.py` | Click entrypoint. Wires the pipeline; config/task errors become `ClickException` (operator errors, no traceback). Requires both the `franky` and `franky-proxy` images. Writes a redacted log to `tasks/<timestamp>.log`. |
| `franky/config.py` | `Config` dataclass + `load_config` (fail-closed) + `redact`. Owns the secret list and optional `FRANKY_EXTRA_ALLOWED_DOMAINS`. |
| `franky/task.py` | `parse_task` -> `TaskSpec`. Earliest point the target repo is known, so the allowlist gate lives here. |
| `franky/engine.py` | `Engine` base + `PiEngine`/`ClaudeEngine`. Each engine owns: headless argv, PR-URL parsing, required creds, and `provider_hosts` (which network host(s) feed the egress allowlist). `resolve_engine` order: `--engine` flag > `FRANKY_ENGINE` > default `pi`. |
| `franky/egress.py` | Pure allowlist POLICY: `build_allowlist` = engine provider host(s) + GitHub + npm/PyPI + operator extras. No docker, no I/O. |
| `franky/prompt.py` | `build_prompt` = `persona.md` + task block + literal conventions (branch `franky/<slug>`, tests-green-before-PR, conventional commits, 3-section PR body, never merge). |
| `franky/container.py` | All docker MECHANICS (pure argv builders, testable without Docker): task/proxy/network argv + `run_in_container` (injectable `runner`/`sleeper`) + `ensure_image`. |
| `franky/update_check.py` | `force_update` (behind `franky update [--force]`): fresh latest-release fetch (`gh` then REST w/ `GH_TOKEN` fallback), `X.Y.Z` compare, reinstall via the `_install.py`-detected manager (uv tool/pipx/pip). `maybe_auto_update` (top of `franky build`): hint-only best-effort sibling - tight ~1s fetch, tiered `~/.franky/update_check.json` cache, prints a stderr hint and proceeds; never blocks or re-execs. Stdlib-only. |
| `franky/_install.py` | Install-provenance detection shared by `franky version` and `franky update`: `detect_install` -> `Install(kind, path)` from interpreter path + editable metadata. |
| `proxy/` | The `franky-proxy` image: Squid + an entrypoint that renders a default-deny, HTTPS-only allowlist config from `FRANKY_ALLOWED_DOMAINS`. |
| `franky/persona.md` | The agent's working persona. Packaged via `package-data`; loaded at runtime by `prompt.py`. |

### Load-bearing invariants (do not regress these)

- **Secrets pass by name, never by value.** `build_docker_argv` emits `-e KEY` (name only) for the task; the value is inherited from Franky's own env. A secret value must never land on an argv (visible in `ps`), in the terminal, or in a log file. (The proxy gets only `FRANKY_ALLOWED_DOMAINS` by value - it is policy, not a secret, and the proxy receives NO creds.)
- **Everything printed or logged is redacted first.** `redact()` masks every secret *value* (longest-first). All output returned from `run_in_container` is already scrubbed. Any new print/log path must route through `redact`.
- **Fail-closed.** `load_config` refuses if the repo allowlist is unset/empty, if `GH_TOKEN` is missing, or if the engine has no creds. `parse_task` refuses any repo not in `FRANKY_ALLOWED_REPOS`. `run_in_container` refuses to start the task unless the proxy is confirmed healthy; the proxy refuses an empty or malformed allowlist.
- **Container hardening is a safety boundary**, not tool prompts (the agent runs autonomously - `claude --dangerously-skip-permissions`, pi default tools). The `_HARDENING`/`_PROXY_HARDENING` flags in `container.py` (`--cap-drop=ALL`, `--read-only`, `--security-opt=no-new-privileges`, pids/memory caps, non-root uid, tmpfs-only writes, **no bind mounts, no docker socket**) are load-bearing. The repo is cloned *inside* the container; the agent never touches the host FS.
- **Egress is the other safety boundary.** The task runs on an `--internal` network (no internet route) whose only peer is the Squid proxy enforcing a default-deny `dstdomain` allowlist; egress is HTTPS-only (blind CONNECT, so the proxy never sees creds) and in-container DNS is killed (`--dns 127.0.0.1`) to block DNS exfil. Residual risk: the agent can still reach the allowlisted high-trust hosts (GitHub, provider, registries), so treat those as trusted, not inert. Squid `dstdomain .github.com` matches the apex AND subdomains - do NOT also list the bare apex (Squid 6 FATALs on the overlap).
- **PR-URL detection is repo-scoped.** `parse_pr_url(output, repo=spec.repo)` anchors to the task's own repo so a hostile issue body cannot make Franky report an attacker's PR URL.

### Adding an engine

Subclass `Engine` in `engine.py`; implement `inner_argv`, `parse_pr_url` (usually delegate to `_scan_jsonl_for_pr_url`), `required_env`, and `provider_hosts` (the host(s) its creds talk to - feeds the egress allowlist); register it in `ENGINES`. Both current engines bake into the one `franky` image (`Dockerfile` npm-installs both CLIs), so a new node-based engine just needs adding to that `npm install -g` line. If the engine uses a new provider env var, add it to `PI_PROVIDER_VARS` AND `PI_PROVIDER_HOSTS` (a test guards against drift).
