# AGENTS.md

Guidance for any coding agent (Claude Code, Codex, Cursor, pi, ...) working with code in this repository. This is the canonical agent guide; `CLAUDE.md` is a symlink to it, so Claude Code picks up the same content.

## What this is

Franky is a lean personal coding agent. Given a GitHub issue URL, a JIRA key, or a prose sentence, it runs a pluggable coding agent (`pi` default, `claude` alt) inside a fresh, hardened Docker container that clones the target repo, makes the change, and opens a PR. The agent is autonomous inside the container; safety comes from OS-level container isolation + a default-deny egress allowlist + a fail-closed repo allowlist + opening a PR (never merging). The container also runs its own **rootless Docker daemon, always on** (issue #12), so the agent can `docker build` / `docker compose up` test infra / run testcontainers to actually build+verify repos - rootless DinD, never a host socket or `--privileged`, and the nested daemon stays inside the same egress cage.

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
franky build <gh-issue-url | jira KEY | "prose"> [--repo owner/repo] [--engine pi|claude|codex] [--plan-first]
# Follow-up pass on an existing Franky PR: address review/CI feedback with additive commits.
franky iterate <gh-pr-url> [--engine pi|claude|codex]
franky version

# Opt-in, OUT-OF-BAND agent-quality eval (#25) - needs real Docker + creds + a sandbox repo.
# Bare runs evals/tasks.json once; ARGS passes flags. See evals/README.md.
make eval ARGS="-n 3 --engine pi --compare-engine codex"
```

Tests use no real Docker, no network, and no live creds: every side effect (subprocess runner, env mapping, sleeper) is dependency-injected, so the suite runs in well under a second. Keep it that way - never reach for real `docker`/`gh` in a test. The egress block/allow *behavior* is verified manually against real Docker (see the PR for issue #1), not in CI. The **eval harness** (`scripts/eval.py`, #25) is the other out-of-band tool: it drives the real `franky build` flow to measure agent pass-rate, so its actual runs need creds/Docker - but its *logic* is unit-tested with an injected fake runner (`tests/test_eval.py`), staying in the fast suite.

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
| `franky/cli.py` | Click entrypoint. Wires the pipeline; config/task errors become `ClickException` (operator errors, no traceback). Requires both the `franky` and `franky-proxy` images. Writes a redacted log to `tasks/<timestamp>.log`. The `build` and `iterate` commands share `_ensure_images` / `_run_pass` / `_economics_line`; the only difference between them is the prompt. |
| `franky/config.py` | `Config` dataclass + `load_config` (fail-closed) + `redact`. Owns the secret list and optional `FRANKY_EXTRA_ALLOWED_DOMAINS`. |
| `franky/task.py` | `parse_task` -> `TaskSpec` (issue URL / JIRA key / prose, for `build`) and `parse_pr_task` -> `TaskSpec(source="pr")` (a PR URL, for `iterate`). Earliest point the target repo is known, so the allowlist gate lives here. `GH_PR_RE` is anchored and `parse_pr_task` reconstructs the canonical URL from the captured groups so the gate and what `gh pr checkout` acts on can never diverge. |
| `franky/engine.py` | `Engine` base + `PiEngine`/`ClaudeEngine`. Each engine owns: headless argv, PR-URL parsing, required creds, and `provider_hosts` (which network host(s) feed the egress allowlist). `resolve_engine` order: `--engine` flag > `FRANKY_ENGINE` > default `pi`. |
| `franky/egress.py` | Pure allowlist POLICY: `build_allowlist` = engine provider host(s) + GitHub + npm/PyPI + container image registries (`DOCKER_REGISTRY_DOMAINS`, for always-on DinD) + operator extras. No docker, no I/O. |
| `franky/prompt.py` | `build_prompt` = `persona.md` + task block + literal conventions (branch `franky/<slug>`, tests-green-before-PR, conventional commits, 3-section PR body, never merge). `build_iterate_prompt` is the `iterate` variant: check out the existing branch (`gh pr checkout`, no new branch), gather review/CI feedback via `gh`, push ADDITIVE commits, never force-push / new-PR / merge, with a prompt-level own-PR guard (head `franky/*` + not cross-repo). Standalone - it does not reuse `_task_block`, so `source="pr"` never hits build-mode logic. |
| `franky/container.py` | All docker MECHANICS (pure argv builders, testable without Docker): task/proxy/network argv + `run_in_container` (injectable `runner`/`sleeper`) + `ensure_image`. `_HARDENING` is the relaxed-for-DinD profile (see invariants). |
| `franky-dind-entrypoint.sh` | Image ENTRYPOINT (not a Python module): starts the rootless Docker daemon, renders `~/.docker/config.json` proxies so inner containers inherit the cage (proxy URLs only, never creds), waits for the socket (30s cap, proceeds on timeout), then execs the engine argv. |
| `franky/update_check.py` | `force_update` (behind `franky update [--force]`): fresh latest-release fetch (`gh` then REST w/ `GH_TOKEN` fallback), `X.Y.Z` compare, reinstall via the `_install.py`-detected manager (uv tool/pipx/pip). `maybe_auto_update` (top of `franky build`): hint-only best-effort sibling - tight ~1s fetch, tiered `~/.franky/update_check.json` cache, prints a stderr hint and proceeds; never blocks or re-execs. Stdlib-only. |
| `franky/_install.py` | Install-provenance detection shared by `franky version` and `franky update`: `detect_install` -> `Install(kind, path)` from interpreter path + editable metadata. |
| `franky/github.py` | `run_gh(args, env, runner, timeout)` for `franky gh`: a host-side passthrough to the real `gh` CLI with Franky's `GH_TOKEN` (the caller's sandbox usually has neither). Token reaches `gh` via the child ENV, never on the argv; output is CAPTURED (injectable `runner`, so tests never invoke real `gh`) then redacted by the CLI. FULL `gh` surface, bounded only by the token's scopes - no read-only gate, no repo allowlist here (deliberately: `build`/`iterate`'s allowlist gates the autonomous content-driven path; `franky gh` is an operator/agent command, same trust as the host-side `idempotency` check). Non-interactive by design (capture -> `gh` sees a non-TTY and skips prompts, so no hang). |
| `franky/jobs.py` | Run registry for `franky jobs` / `franky job status\|logs\|kill\|export` (issues #63, #64). Filesystem-only (no docker), stdlib-only: per-run JSON record under `~/.franky/runs/<job_id>.json` (override dir via `FRANKY_RUNS_DIR` for tests), atomic write (tempfile+replace, 0600/0700, mirrors userconfig). `new_record`/`write_record`/`read_record`/`update_record`/`list_records`(newest-first)/`prune`(bounded, never prunes a `running` record). Records store NO secret value - only names/paths/status/timings + a redacted task summary. Job id is validated `[0-9a-f]` so a user-supplied `job status <id>` can't traverse out of the runs dir. Read/update never raise (fail-closed -> None/False), so a registry hiccup never breaks a build. The build/iterate path writes a `running` record BEFORE the pass (best-effort) and updates it after; `run_id` threads into `run_in_container` so the container/net/proxy names derive from the job id and `job kill`/`status` can target them. Docker mechanics for status/kill live in container.py (`run_names`/`container_running`/`reap_run`). #64 adds two PURE helpers over the same records/artifacts (no docker): `compute_stats(records)` (cross-run success/hang rate + median duration/cost, broken down by engine/repo; `hangs` = timeout + stale-`running` orphans) for `jobs --stats`, and `export_bundle(record, dest)` (a secret-free `.tar.gz` of `record.json` + the already-redacted `transcript.log`, with host-free tar member metadata) for `job export`. |
| `franky/economics.py` | Best-effort per-run economics: `parse_usage` (walk engine JSONL, take the terminal event's token/cost totals, never sum) + `format_economics` (one-line summary). Pure, no I/O; never raises - degrades to all-unknown so economics can never fail a build. |
| `franky/userconfig.py` | On-disk persistence for `~/.franky/config` (TOML, mode 0600). `read_config_file` / `write_config_file` (atomic, hand-rolled TOML for the constrained single-table schema). `load_config_file(env)` injects file values into `env` via `setdefault` (process env wins). `set_value` read-modify-write. `SECRET_KEYS` frozenset (union of all cred vars from engine.py + jira.py + config.py). `mask_value` for display. Path overridable via `FRANKY_CONFIG_FILE` env var for hermetic tests. |
| `proxy/` | The `franky-proxy` image: Squid + an entrypoint that renders a default-deny, HTTPS-only allowlist config from `FRANKY_ALLOWED_DOMAINS`. |
| `franky/persona.md` | The agent's working persona. Packaged via `package-data`; loaded at runtime by `prompt.py`. |

### Load-bearing invariants (do not regress these)

- **Secrets pass by name, never by value.** `build_docker_argv` emits `-e KEY` (name only) for the task; the value is inherited from Franky's own env. A secret value must never land on an argv (visible in `ps`), in the terminal, or in a log file. (The proxy gets only `FRANKY_ALLOWED_DOMAINS` by value - it is policy, not a secret, and the proxy receives NO creds.)
- **Everything printed or logged is redacted first.** `redact()` masks every secret *value* (longest-first). All output returned from `run_in_container` is already scrubbed. Any new print/log path must route through `redact`.
- **Fail-closed.** `load_config` refuses if the repo allowlist is unset/empty, if `GH_TOKEN` is missing, or if the engine has no creds. `parse_task` refuses any repo not in `FRANKY_ALLOWED_REPOS`. `run_in_container` refuses to start the task unless the proxy is confirmed healthy; the proxy refuses an empty or malformed allowlist.
- **Container hardening is a safety boundary**, not tool prompts (the agent runs autonomously - `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, pi default tools). The `_HARDENING`/`_PROXY_HARDENING` flags in `container.py` are load-bearing: `--read-only`, pids/memory caps (`--memory-swap` = `--memory`, no swap), non-root uid 1001, tmpfs-only writes, and **no host bind mounts, no host docker socket** (the repo is cloned *inside* the container and the Docker daemon is rootless+nested, so the agent never touches the host FS or the host daemon). The task `_HARDENING` is deliberately **relaxed for always-on rootless DinD** (#12): `--cap-drop=ALL` keeps the baseline but `CAP_SETUID`/`CAP_SETGID` are added back (rootless uid-map helpers), `--security-opt=no-new-privileges` is **dropped** (it blocks those setuid helpers - do NOT re-add it), `--security-opt=systempaths=unconfined` and `--device /dev/net/tun` are added. This is NOT `--privileged` and NOT `seccomp=unconfined`; each relaxation is the minimal one proven necessary (see the README security section). Image binaries `newuidmap`/`newgidmap` use file capabilities (`cap_setuid/setgid+ep`), NOT the Debian setuid bit, which fails under `--cap-drop=ALL`. Changes here must be re-verified with `make smoke-dind` (real Docker; the pytest suite never touches Docker).
- **Each engine bypasses its OWN approval/sandbox - on purpose - because the container is the safety boundary, not the engine's in-tool guardrails.** The agent runs autonomously, so a per-engine approval prompt is both redundant with the OS-level isolation + egress cage and would hang a headless run. So every engine disables its own gate: `claude` passes `--dangerously-skip-permissions`, `codex` passes `--dangerously-bypass-approvals-and-sandbox`, `pi` runs its default tools, and a new engine MUST do the equivalent. Some engines additionally **self-sandbox** (Landlock/seccomp - codex does both): nested inside Franky's already-hardened container that self-sandbox is redundant AND can fail to initialize, which is exactly why codex's bypass flag disables the sandbox as well as the approval prompt - we deliberately turn it off and trust the container. The bargain is identical for every engine: the engine's docs warn the flag is "only for an isolated runner," and Franky's container is that runner. An engine author must preserve this - shipping an engine that still prompts for approval (or insists on its own sandbox) will stall the autonomous build.
- **Egress is the other safety boundary.** The task runs on an `--internal` network (no internet route) whose only peer is the Squid proxy enforcing a default-deny `dstdomain` allowlist; egress is HTTPS-only (blind CONNECT, so the proxy never sees creds) and in-container DNS is killed (`--dns 127.0.0.1`) to block DNS exfil. The nested rootless Docker daemon inherits `HTTP(S)_PROXY`, so its image pulls + `docker build` fetches go through the proxy too (verified: off-allowlist `FROM`/`RUN` is `403`'d, and a nested container has no direct route out). The allowlist therefore includes a broad set of container registries (`DOCKER_REGISTRY_DOMAINS`). Residual risk: the agent can reach the allowlisted high-trust hosts (GitHub, provider, language + container registries) AND can move its creds into nested containers (bounded by the allowlist + PR-not-merge), so treat allowlisted destinations as trusted, not inert. Squid `dstdomain .github.com` matches the apex AND subdomains - do NOT also list the bare apex (Squid 6 FATALs on the overlap); the same dot-form rule governs the registry list.
- **PR-URL detection is repo-scoped.** `parse_pr_url(output, repo=spec.repo)` anchors to the task's own repo so a hostile issue body cannot make Franky report an attacker's PR URL.

### Adding an engine

Subclass `Engine` in `engine.py`; implement `inner_argv`, `parse_pr_url` (usually delegate to `_scan_jsonl_for_pr_url`), `required_env`, `provider_hosts` (the host(s) its creds talk to - feeds the egress allowlist), and `cred_hint` (the operator-facing "set these creds" string used by `load_config`'s fail-closed refusal, so the message names YOUR engine's vars - shared config never hardcodes one engine's); register it in `ENGINES` (the `--engine` CLI choice is derived from `ENGINES`, so registering is all it takes to expose the flag). In `inner_argv`, disable the engine's own approval/sandbox (see the guardrail-bypass invariant above - e.g. `codex` passes `--dangerously-bypass-approvals-and-sandbox`, which turns off both its approval prompt and its Landlock/seccomp self-sandbox). All current engines bake into the one `franky` image (`Dockerfile` npm-installs every CLI), so a new node-based engine just needs adding to that `npm install -g` line. For provider env vars: a pi BYOK var goes in `PI_PROVIDER_VARS` AND `PI_PROVIDER_HOSTS` (a test guards against drift); an engine whose creds live entirely in its own class (like `claude`/`codex`) just owns its vars + `provider_hosts` directly.
