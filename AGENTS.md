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

python3 scripts/private_terms.py files
python3 scripts/private_terms.py commits origin/main..HEAD

make footprint
make smoke-security
make smoke-dind
make smoke-resume
make smoke-profile
make smoke-thread
make smoke-memory
make smoke-memory ARGS="--jobs 4"

make eval ARGS="-n 3 --engine pi --compare-engine codex"
make review-eval ARGS="--cases ~/.franky/evals/review_cases.json -n 3"
```

Unit tests must not use real Docker, networks, GitHub, or credentials. Inject runners, environments, and sleepers instead.

Real Docker gates stay outside the unit suite. The footprint workflow uses fixed credential-free workloads on Ubuntu 24.04.

A PR that changes shipped code (`franky/`, `proxy/`, `Dockerfile`, the entrypoint or install scripts, `pyproject.toml`) must add a bullet under `## [Unreleased]` in `CHANGELOG.md`. The `changelog` workflow runs `scripts/release.py changelog-check` and fails otherwise. Label the PR `no-changelog` only when users see no change.

## Documentation

The README is a fast scan for a new user. Detail lives in `docs/`. `tests/test_doc_coherence.py` enforces these rules in CI.

- Keep `README.md` at 120 lines or fewer.
- Keep only these README sections: Install, Configure, Engines, Commands, Docs, Status. `scripts/release.py` bumps the Status line.
- Give each fact one home. Link to it from other files. Do not copy it.
- Link every `docs/*.md` file from the README Docs table.
- Use relative links that resolve. Point to a heading with `file.md#anchor`.

| Topic | Home |
|-------|------|
| First run, engine table, main commands | `README.md` |
| Settings, AppArmor, engine logins, model names, Atlassian tools, images | `docs/configuration.md` |
| Job, thread, auth, config, and profile command tables | `docs/commands.md` |
| Build, iterate, and review-pr behavior | `docs/review.md` |
| Review and author threads, session transfer | `docs/threads.md` |
| Schema, JSON events, job state, exit codes | `docs/automation.md` |
| Profiles, profile limits, MCP | `docs/profiles.md` |
| Resource defaults and concurrency | `docs/resources.md` |
| User-facing threat model | `docs/security.md` |
| Maintainer rules, invariants, dev commands | `AGENTS.md` |
| Release steps | `docs/releasing.md` |

If a new topic has no home, add a row here and a docs file. Do not grow the README.

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
| Atlassian | `atlassian.py` | Host-side Atlassian OAuth connection and MCP wiring |
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

Resource defaults and their settings live in [`docs/resources.md`](docs/resources.md).

Profile limits live in [`docs/profiles.md`](docs/profiles.md).

Snapshot and transcript reads also use fail-closed size limits.

## Security invariants

Do not weaken these rules.

### Secrets and output

- Pass credentials to task containers as `-e NAME`, never as values on the command line.
- Only `FRANKY_ATLASSIAN_MCP_HEADER` (`Bearer` plus a short-lived access token) enters a task container for JIRA and Confluence, as `-e NAME`, and only with a stored `franky connect jira` connection and a repo GitHub confirms private. The connection file `~/.franky/atlassian-jira.json` is 0600, host-only, never mounted, and its refresh token never enters a container. Profile and setup scans deny it. The classic `JIRA_API_TOKEN` is host-only (ticket fetch). Every JIRA credential form (token, email, base64 forms, refresh and access tokens, previous access tokens) is redacted and scrubbed whether or not the tools are on.
- MCP credentials must come from named process environment variables.
- Send only policy data, such as `FRANKY_ALLOWED_DOMAINS`, by value to the proxy.
- Redact autonomous output, stored transcripts, diagnostics, and JSON errors before release.
- `config list --reveal` is explicit operator output. Do not claim that Franky redacts it.
- Codex subscription auth is the only persistent task volume. Scrub it to `auth.json` before each run.
- Never mount the subscription volume into helpers, proxies, or non-subscription tasks.

### Private terms

This repo is public. Never write employer, private repo or agent names, real ticket keys, eval sources, spend figures or personal emails into code, commits, PR text or comments. Use made-up names in tests and examples.

The term list lives outside the repo: the Actions secret `FRANKY_PRIVATE_TERMS` and, locally, `~/.config/franky/private-terms`. Before pushing, run `python3 scripts/private_terms.py files` and `python3 scripts/private_terms.py commits origin/main..HEAD`. The script never prints a term, only `<location>: private term #N`.

### Fail-closed policy

- Refuse an empty repository allowlist, missing `GH_TOKEN`, missing engine credentials, or unsupported OpenCode provider.
- Gate every task and PR repository with `FRANKY_ALLOWED_REPOS`.
- Enable the Atlassian tools only for a repo GitHub confirms private.
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
- Add `mcp.atlassian.com` only when the Atlassian tools are on. The host calls `auth.atlassian.com` itself; no other Atlassian host enters the allowlist.
- Treat allowlisted hosts as trusted destinations. The agent can move its credentials into nested containers.
- In Squid, `.example.com` matches the apex and subdomains. Do not also add the bare apex.

### Agent and transfer behavior

- Disable each engine's own approval and sandbox prompts. The container is the autonomous safety boundary.
- Keep `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, and OpenCode `--auto --pure`.
- Stream profile and snapshot archives through `docker exec -i`. Never place archive data in arguments or environment values.
- Thread sessions move by stream-in (tar over docker exec) and copy-out (docker cp) only - never mounted, regular files only, scrubbed and verified, host-only, never exported.
- Thread transfer carries only the engine session file and its side directory. Never carry an engine's project memory between runs.
- The session sidecar of a timed-out or killed `--thread` build (`<job_id>.session.tar.gz`) is regular files only, scrubbed with the run's full secret set, fail-closed verified, host-only, and never exported. A bind or `job resume` re-extracts it through the same regular-file-only filter; never copy it with links.
- A `build --no-publish` export is a bundle made by networkless, read-only, capability-free helpers during the entrypoint's `--rm` session hold, never by a stopped container and never by host git on the container's checkout. The host reads only the bundle header (`snapshot.parse_bundle_header`), scans the exported patch text for the run's secret values (values only; the pushing caller owns pattern scans), and deletes the bundle on every status except `branch_ready`. Franky never pushes it, and a `--no-publish` run is never resumed.
- Reviewer and author sessions never mix. An author run retries only on an engine startup rejection of its session. Never re-run a write pass blindly.
- Bind a session to an author thread only after the host confirms that the open PR on the run's branch is the PR the agent reported. Agent output alone never picks the thread.
- Secret-scan every injected profile file. Resolve symlinks before deny checks.
- Skip unscannable setup files. Never inject setup MCP configuration automatically.
- Keep Codex `--ignore-user-config`. Pass validated MCP servers as explicit overrides.
- Franky requests read and search scopes only and denies the Atlassian write tools in the engine config (Claude deny patterns, a Codex read-tool allowlist), plus the prompt. If Atlassian grants broader scopes, read-only is not enforced by Atlassian; `--status` shows the granted scopes. Only claude and codex get the tools. A missing, expired or revoked connection turns the tools off with one stderr line and never fails a task.
- Tell review agents to stay read-only. The host publisher uses COMMENT or REQUEST_CHANGES. It uses APPROVE only behind `--allow-approve`, when no finding above nit is open, nothing was dropped as malformed, and no check failed; the caller must enforce branch protection that dismisses stale approvals on push. An APPROVE with an uncertain outcome is reconciled, never blindly reposted (`publish_uncertain`). `--resolve-fixed` resolves only the publishing bot's own unambiguous matching threads.
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
