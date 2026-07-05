# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Run registry + `franky jobs` / `franky job status|logs|kill` (#63): every `build`/`iterate`
  run is recorded under `~/.franky/runs/<job_id>.json` and prints its `job_id` at start (also a
  `job_id` field in `--json` output), so a run can be listed, inspected, its transcript read, or
  a stuck one reaped after the fact - or observed/killed from a second shell while it is still
  in flight. `job status` reports whether the container is still alive; `job kill` reaps the
  container + its proxy sidecar + internal network. Records store no secret values (names/paths/
  status/timings + a redacted task summary), the runs dir is pruned (never a `running` record),
  and job ids are validated so `job status <id>` can't traverse the filesystem. `run_id` now
  threads into `run_in_container` so container/net/proxy names derive from the job id. Deferred
  to follow-ups: `--detach`, `job shell`, `logs -f`, `job diagnose`.
- `franky gh <args>` (#62): a host-side passthrough to the real `gh` CLI using Franky's own
  GitHub token, so an agent caller (whose sandbox has no `gh`/token) can query and act on
  GitHub - confirm a PR landed, read CI checks, comment, `merge`, `api`, etc. - without a
  second credential. FULL power, bounded only by the token's scopes: no read-only gate and no
  repo allowlist on this surface (scope the token to limit it; the `build`/`iterate` allowlist
  is unaffected). The token value never leaks - it reaches `gh` via the environment (never on
  the argv) and `gh`'s output is redacted before printing. Stdout/stderr stay separated so
  `franky gh pr list --json` yields clean JSON on stdout; `gh`'s own exit code is passed
  through (missing token -> exit 5, `gh` not installed on the host -> exit 6). Non-interactive
  by design (output is captured then redacted).

## [0.0.5] - 2026-07-03

### Added
- Richer eval success checkers (#25): `diff_touches_files` (every declared file path was touched
  by the PR diff, using suffix-match semantics) and `change_present` (every declared substring
  appears in the PR diff). Both fetch PR artifacts via `gh pr view` / `gh pr diff` and are
  injectable for unit testing; tasks declare required data via new `files` / `contains` fields.
- `franky plan <task>` (#52): a read-only scope-assessment + decomposition command. Runs ONE
  read-only container pass that inspects the repo/issue and decides whether the task fits one
  focused PR or should be split, then emits a decomposition `{fits_one_pr, subtasks:[{title,
  summary, suggested_repo}], rationale}` (a DISTINCT `--json` envelope from build/iterate;
  errors share the `{"error":{...}}` envelope and exit-code taxonomy, with `kind: no_plan` /
  exit `7` when the agent produces no parseable plan). It accepts the same task forms as
  `build` (issue URL / JIRA key / prose / `-` stdin), the same repo allowlist gate, and the
  same `--engine` / `--profile` / `--max-duration` / `--json` / `-q` flags. It builds nothing
  (no branch, commits, or PR) - the caller orchestrates per sub-task. A per-run nonce fences
  the machine-readable block so a hostile issue body / repo file cannot plant a fixed sentinel
  to hijack the reported decomposition. `build --help` gains a static advisory pointing at it
  ("one franky run = one focused PR").
- Machine-friendly CLI for agent callers (#50). `franky build` / `iterate` gain:
  - `--json`: a single result object on stdout - `{status, pr_url, branch, reason, exit_code,
    economics{tokens_in, tokens_out, cost_usd, duration_s}, log_path, engine, repo}` - or a
    `{"error": {code, kind, message, hint}}` object on failure. Always fully redacted.
  - A stable exit-code taxonomy (SemVer contract): `0` success, `2` usage / interactive-input-
    required, `3` config, `4` task rejection, `5` auth/creds, `6` docker, `7` agent (nonzero or
    no PR), `8` network/JIRA. The process exit code always equals the failure's code.
  - `-q/--quiet` (implied by `--json`): suppresses progress output and the update hint; stdout
    stays pure (exactly the bare PR URL, or exactly one JSON object).
  - `-y/--yes`: auto-approve `--plan-first` for unattended runs.
  - `build -` reads the prose task from stdin.
  - Never-hang guarantee: every interactive prompt (`--plan-first` confirm, `config set` /
    `config init`, `build -`) fails fast with exit `2` in a non-TTY instead of blocking.
- Agent-friendly P2 additions (#50):
  - `--max-duration SECONDS` (on `build` and `iterate`): abort a runaway run. The container is
    killed and the result is `status: timeout` / exit `9` (a new, additive taxonomy code). A
    token/cost cap is out of scope (token usage is only known after the run).
  - Idempotency / retry-safety: `build` computes a deterministic branch (`franky/issue-<n>`,
    `franky/<jira-key>`, or `franky/<prose-slug>`) and pre-checks GitHub for an open Franky PR
    on it. If one exists it reports `status: already_open` with the existing `pr_url` at exit
    `0` and opens no duplicate; `--force` skips the check. Best-effort (any error proceeds with
    the build). `branch` is now populated in the result as the predicted branch.
  - `franky schema`: a read-only command that prints one JSON object describing every command +
    flags, the result/error shapes, and the exit-code table - machine introspection so an agent
    discovers the contract instead of parsing `--help`.
- `franky profile` setup commands so a profile no longer has to be hand-written:
  `profile init` (interactive wizard that scaffolds / merges `~/.franky/profile.toml`),
  `profile check` (dry-run the build's gate - expand globs + secret-scan, report what would
  inject, nonzero exit naming the offending file on a credential hit), `profile show`, and
  `profile path`. `check` reuses the exact `load_profile` + `scan_for_secrets` path the build
  runs, so a profile that passes `check` cannot fail the build's fail-closed secret gate. `franky
  config init` now also offers to set up a profile so the feature is discoverable (#49).

### Changed
- `franky build` now exits `7` (was `0`) when the agent finishes cleanly but produces no PR URL,
  so a no-PR outcome is distinguishable from success by exit code alone (#50).
- The branch the build prompt pins is now deterministic and task-distinguishing
  (`franky/issue-<n>` / `franky/<jira-key>` / `franky/<prose-slug>`, replacing the loose
  repo-name hint) so the host can predict it for the idempotency pre-check; `branch` in the
  JSON result carries that predicted value (was always null) (#50).

## [0.0.4] - 2026-06-25

### Added
- Operator profile injection: declare skill / instruction / knowledge files in
  `~/.franky/profile.toml` and Franky secret-scans them host-side, then injects the bundle into
  the task container so the in-container agent sees your curated context (skills, CLAUDE.md-style
  instructions, knowledge docs) instead of only the generic persona. New `--profile` flag,
  auto-discovery of `~/.franky/profile.toml`, and a `FRANKY_PROFILE_PATH` override. The bundle is
  base64-passed by value (no bind mount, not a credential) and the run fails closed if any
  credential pattern is detected in a listed file. Tier-1 (static prose) only; MCP servers are
  out of scope (#23).
- Live progress for `franky build` / `franky iterate`, ending the silent multi-minute run.
  Distilled milestones (`franky: editing src/foo.py`, `franky: running: make test`, ...) print to
  stderr by default; `--verbose` / `-v` (or `FRANKY_VERBOSE=1`) streams the raw engine output
  instead. Output is redacted line-by-line and stdout still carries only the PR URL (#48).

## [0.0.3] - 2026-06-24

### Fixed
- `--engine claude` no longer exits 1 with "When using --print, --output-format=stream-json
  requires --verbose". The claude CLI now requires `--verbose` alongside `-p` + `stream-json`
  (which Franky needs to parse the JSONL stream for the PR URL), so the flag is added.

## [0.0.2] - 2026-06-23

### Added
- Public distribution: Franky is published to PyPI as `franky-agent` (the command stays
  `franky`), so `uv tool install franky-agent` / `pipx` / `pip` work with no GitHub auth while
  the repo stays private. The release workflow publishes the wheel + sdist via PyPI Trusted
  Publishing (OIDC, no stored token), and the GHCR images are public.
- `FRANKY_GHCR_REPO` env/config knob to retarget the image namespace without a code change;
  the default (`ghcr.io/vietlabs-work`) and the release workflow both track the repo's owning
  org.

### Changed
- `franky update` and the build-time update hint now check the public PyPI JSON API for the
  latest version and reinstall `franky-agent==X.Y.Z`, replacing the private-repo
  `gh`/GitHub-releases fetch and the `git+ssh` install spec (no `GH_TOKEN` needed).
- `make release` no longer prints a misleading `Released vX.Y.Z` the instant the tag is pushed.
  The push only triggers the async Release workflow (wheel + GHCR images + GitHub Release,
  several minutes), so the command now says so and, by default, tails that workflow to
  completion via `gh run watch` - printing the published Release URL on success or a failure
  pointer otherwise. `--no-watch` prints the Actions + Release links instead of waiting.
  Watching is best-effort: it degrades to links (never errors) when `gh` is missing/unauthed
  or the run can't be found, and caps the wait at 20 min.

## [0.0.1] - 2026-06-23

### Added
- `franky config` subgroup with four subcommands: `path` (print config file location),
  `list [--reveal]` (show all keys, secrets masked by default), `set KEY [VALUE]` (set a
  key; secrets must be entered at a hidden prompt - positional value refused to protect shell
  history), and `init` (interactive wizard that walks engine, allowlist, GH_TOKEN, engine
  creds, and optional JIRA settings). Config is stored in `~/.franky/config` as TOML at
  mode 0600. `FRANKY_CONFIG_FILE` env var overrides the path (used by tests for hermeticity).
  The file is injected into `franky build` and `franky iterate` via `setdefault` so the
  process environment always wins. `franky version` and `franky config` are deliberately
  not affected (config must be writable even when the file is malformed).
- Tiered repo allowlist with per-segment glob patterns in `FRANKY_ALLOWED_REPOS`:
  `my-org/*` (whole org), `my-org/team-*` (prefix), exact `owner/repo`, and `*`
  (every repo the token can reach - opt-in, not the default). Matching is case-insensitive
  and segment-wise (globs cannot cross `/`). Malformed entries are rejected at load time.
- `config.example.toml`: replaces `.env.example` as the reference config template.
  Shows the `[franky]` TOML table with placeholder string values and comments.
- `franky/userconfig.py`: the persistence module backing the config subgroup. Atomic
  write (temp + `os.replace`), 0600 mode, hand-rolled TOML serializer for the constrained
  single-table schema (no extra write dependency), `SECRET_KEYS` frozenset as the single
  source of truth for which keys are masked/argv-refused.
- `tomli>=2.0; python_version<"3.11"` runtime dependency for TOML parsing on Python 3.10
  (stdlib `tomllib` was added in 3.11).
- `franky iterate <pr-url>`: a second entry point that responds to PR review comments and
  failing CI with ADDITIVE follow-up commits on the existing branch, instead of re-running
  from scratch. Runs the identical hardened + egress-controlled container as `franky build`;
  checks out the PR's branch (`gh pr checkout`), gathers feedback in-container via `gh`, runs
  tests green, then pushes. Never force-pushes, rewrites history, opens a new PR, or merges
  (all prompt-level, same trust model as build's never-merge). A prompt-level own-PR guard
  (head `franky/*` + same-repo, not a fork) keeps it off arbitrary branches; the hard bounds
  stay the repo allowlist + egress cage + PR-not-merge. The PR URL is authoritative (no
  `--repo`), and the parsing regex is anchored + canonicalized so the allowlist gate and the
  branch the agent acts on cannot diverge (#24).
- Eval harness (`scripts/eval.py` + `make eval`, opt-in/out-of-band): runs a golden task set
  (`evals/tasks.json`) through the real `franky build` flow N times and reports pass-rate, plus
  a comparison mode for the delta between two engines. Success checkers (`pr_opened`,
  `exit_zero`) are derived from the build result; the harness logic is unit-tested with an
  injected fake runner so the fast suite stays docker-free. Needs real Docker + creds + a
  sandbox repo to actually run (#25).
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
