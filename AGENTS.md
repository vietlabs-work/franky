# AGENTS.md

This file is the canonical guide for coding agents in this repository. `CLAUDE.md` is a symlink to this file.

## Product

Franky runs a pluggable coding agent in a fresh Docker container. It accepts an issue, JIRA key, PR, or prose task.

The task container clones an allowlisted repository, makes or reviews changes, and can open a PR. Prompts forbid autonomous merges.

Each task also runs a rootless Docker daemon. The agent can use Docker, Compose, and testcontainers without access to the host daemon.

## Development commands

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

docker build -t franky .
docker build -t franky-proxy proxy/

.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_container.py -q
.venv/bin/python -m pytest tests/test_container.py::test_build_docker_argv_hardening_flags -q

ruff check .
ruff format --check .

make footprint
make smoke-security
make smoke-dind
make smoke-resume
make smoke-profile
make smoke-thread
make smoke-memory
make smoke-memory ARGS="--jobs 4"

make eval ARGS="-n 3 --engine pi --compare-engine codex"
```

Unit tests must not use real Docker, networks, GitHub, or credentials. Inject runners, environments, and sleepers instead.

Real Docker gates stay outside the unit suite. The footprint workflow uses fixed credential-free workloads on Ubuntu 24.04.

## Architecture

```text
CLI -> config and task policy -> prompt -> engine command -> container runtime -> parsed result
                                      |
                                      +-> internal network -> proxy -> allowed HTTPS hosts
```

| Area | Files | Responsibility |
|------|-------|----------------|
| CLI contract | `cli.py`, `result.py`, `schema.py` | Commands, exit codes, JSON results, machine discovery |
| Configuration | `config.py`, `userconfig.py`, `engine.py` | Fail-closed settings, credentials, engine selection |
| Task resolution | `task.py`, `jira.py`, `idempotency.py`, `baseref.py` | Input parsing, repo gates, base commits, duplicate-PR checks |
| Agent instructions | `prompt.py`, `persona.md`, `decompose.py`, `diagnosis.py`, `reviewpr.py`, `sentinel.py` | Prompts and bounded structured output |
| Runtime | `container.py`, `security.py`, `egress.py`, `franky-dind-entrypoint.sh`, `proxy/` | Docker lifecycle, isolation, policies, proxy |
| Transfer | `profile.py`, `setups.py`, `snapshot.py` | Bounded, secret-scanned profile and workspace bundles |
| Runs | `jobs.py`, `transcript.py`, `economics.py` | Records, redacted logs, diagnostics, usage |
| Threads | `threads.py` | Per-PR reviewer and author sessions, handoffs, locks, prune and purge |
| Host helpers | `github.py`, `update_check.py`, `_install.py` | `gh` passthrough, updates, install detection |

Keep policy code pure where practical. Keep subprocess calls injectable so tests remain hermetic.

## Resource rules

- Check image size, peak RAM, CPU time, and elapsed time for every feature.
- State the effect on two and four concurrent jobs in each PR.
- Run `make footprint` for fixed host comparisons.
- Run two-job and four-job memory smokes after container or storage changes.
- Run all relevant security, DinD, resume, profile, and thread smokes after runtime changes.
- Do not increase `scripts/footprint-budgets.json` without measured before-and-after evidence.
- Keep shared runtime layers separate from engine payloads. Do not add a tool to every image without need.
- Keep the proxy user as numeric `13:13` across image bases.
- Keep the Codex native launcher, platform package, companion executable, and managed-package metadata.

Resource defaults:

| Resource | Limit |
|----------|-------|
| Task tree | 2048 MiB, configurable from 256 through 8192 MiB |
| Proxy | 128 MiB |
| Task disk | 8192 MiB soft budget, configurable from 1024 through 32768 MiB |
| Disk helper | 64 MiB, short-lived and networkless |

`/work`, HOME, and `/tmp` use anonymous disk volumes. Small runtime paths use tmpfs.

The disk watchdog samples approximately every five seconds. It is not a filesystem quota. Volume deletion is not secure erasure.

Profiles allow 5,000 files and 20 MiB across explicit, swept, and MCP files. The profile file has a separate 20 MiB limit.

Glob and setup scans can inspect 5,000 entries. Setup traversal can visit 5,000 directories.

Skipped binary or unreadable sweep files consume the budget. Snapshot and transcript reads also use fail-closed size limits.

## Security invariants

Do not weaken these rules.

### Secrets and output

- Pass credentials to task containers as `-e NAME`, never as values on the command line.
- MCP credentials must come from named process environment variables.
- Send only policy data, such as `FRANKY_ALLOWED_DOMAINS`, by value to the proxy.
- Redact autonomous output, stored transcripts, diagnostics, and JSON errors before release.
- `config list --reveal` is explicit operator output. Do not claim that Franky redacts it.
- Codex subscription auth is the only persistent task volume. Scrub it to `auth.json` before each run.
- Never mount the subscription volume into helpers, proxies, or non-subscription tasks.

### Fail-closed policy

- Refuse an empty repository allowlist, missing `GH_TOKEN`, missing engine credentials, or unsupported OpenCode provider.
- Gate every task and PR repository with `FRANKY_ALLOWED_REPOS`.
- Scope parsed PR URLs to the task repository.
- Refuse malformed security data, proxy policy, profile data, snapshots, or structured output.
- Start the task only after the proxy passes its bounded health check.

`franky gh` is an explicit host-side exception. It exposes the full `gh` surface and is limited only by `GH_TOKEN`.

### Container boundary

- Run as uid 1001 with a read-only root, memory and process limits, no swap, and `--cap-drop=ALL`.
- Never add host bind mounts, the host Docker socket, `--privileged`, or `seccomp=unconfined`.
- Use the packaged seccomp policies for every container. Never depend on the daemon default.
- Use `franky-task` on AppArmor hosts. Never fall back to an unconfined profile.
- Limit AppArmor sysctl access to `net.ipv4.ip_unprivileged_port_start` and `net.ipv6.conf.*.disable_ipv6`.

Rootless Docker needs these exact task exceptions:

- Add `CAP_SETUID` and `CAP_SETGID` after dropping all capabilities.
- Omit `no-new-privileges` because uid-map helpers need file capabilities.
- Set `systempaths=unconfined` and pass `/dev/net/tun`.
- Keep `newuidmap` and `newgidmap` file capabilities. Do not use setuid bits.
- Permit only tested namespace, mount, `pivot_root`, hostname, keyring, and sysctl operations in task security policies.

Any policy delta must pass pinned-policy tests, `make smoke-security`, and `make smoke-dind`.

### Egress boundary

- Put the task on a Docker `--internal` network and set task DNS to `127.0.0.1`.
- Route all task and nested-Docker traffic through the HTTPS-only Squid proxy.
- Build the provider allowlist from the selected engine and model only.
- Keep package and container registries explicit in `DOCKER_REGISTRY_DOMAINS`.
- Treat allowlisted hosts as trusted destinations. The agent can move its credentials into nested containers.
- In Squid, `.example.com` matches the apex and subdomains. Do not also add the bare apex.

### Agent and transfer behavior

- Disable each engine's own approval and sandbox prompts. The container is the autonomous safety boundary.
- Keep `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, and OpenCode `--auto --pure`.
- Stream profile and snapshot archives through `docker exec -i`. Never place archive data in arguments or environment values.
- Thread sessions move by stream-in (tar over docker exec) and copy-out (docker cp) only - never mounted, regular files only, scrubbed and verified, host-only, never exported.
- Thread transfer carries only the engine session file and its side directory. Never carry an engine's project memory between runs.
- The session sidecar of a timed-out or killed `--thread` build (`<job_id>.session.tar.gz`) is regular files only, scrubbed with the run's full secret set, fail-closed verified, host-only, and never exported. A bind or `job resume` re-extracts it through the same regular-file-only filter; never copy it with links.
- Reviewer and author sessions never mix. An author run retries only on an engine startup rejection of its session. Never re-run a write pass blindly.
- Bind a session to an author thread only after the host confirms that the open PR on the run's branch is the PR the agent reported. Agent output alone never picks the thread.
- Secret-scan every injected profile file. Resolve symlinks before deny checks.
- Skip unscannable setup files. Never inject setup MCP configuration automatically.
- Keep Codex `--ignore-user-config`. Pass validated MCP servers as explicit overrides.
- Tell review agents to stay read-only. The host publisher only uses COMMENT or REQUEST_CHANGES.
- Keep iterate commits additive. Tell autonomous agents never to merge or force-push.

## Add an engine

1. Subclass `Engine` in `engine.py`.
2. Implement `inner_argv`, `parse_pr_url`, `required_env`, `provider_hosts`, and `cred_hint`.
3. Disable the engine's own approval and sandbox prompts.
4. Register it in `ENGINES`.
5. Add its package case to `Dockerfile` and its release variant to `release.yml`.
6. Add tests for command arguments, credentials, provider hosts, PR parsing, and image selection.
7. For a Pi provider, update both `PI_PROVIDER_VARS` and `PI_PROVIDER_HOSTS`.
8. Measure host, image, two-job, and four-job impact.
