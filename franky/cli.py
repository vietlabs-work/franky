"""Franky CLI: wire config -> task -> prompt -> container -> PR-URL.

The primary caller is an LLM/agent, so the machine contract is load-bearing: `build` and
`iterate` raise typed FrankyError subclasses (config/task/jira/docker), each carrying a stable
exit code, and a single outer handler emits either a `--json` error object (stdout) or a prose
line (stderr) and exits with that code. Successful runs emit a `--json` result object or a bare
PR URL. Every printed or logged string is redacted first - a secret value must never reach the
terminal or the on-disk log. Interactive prompts (plan-first confirm, config wizard, `build -`
stdin) fail fast in a non-TTY rather than hang.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from collections.abc import Mapping
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path

import click

from . import baseref, franky_version
from .transcript import Transcript, Redactor, chunks, open_secure
from ._install import detect_install
from .config import GH_TOKEN_VAR, Config, load_config, redact, repo_allowed
from .decompose import build_plan_result, parse_decomposition
from .diagnosis import build_diagnosis_result, parse_diagnosis
from .economics import Usage, format_economics, parse_usage
from .container import (
    CONTAINER_TIMEOUT_CODE,
    codex_auth_login,
    codex_auth_logout,
    codex_auth_status,
    FRANKY_IMAGE_VAR,
    FRANKY_PROXY_IMAGE_VAR,
    capture_diagnostics,
    ensure_image_available,
    resolve_image,
    run_in_container,
)
from . import jobs, snapshot
from .container import container_running, deliver_steer, reap_run, run_names
from .engine import (
    CODEX_SUBSCRIPTION_VAR,
    ENGINES,
    PI_PROVIDER_VARS,
    codex_auth_volume,
    opencode_provider,
    resolve_engine,
)
from .github import run_gh
from .idempotency import fetch_pr_head_sha, find_open_pr
from .jira import JIRA_API_TOKEN_VAR, JIRA_EMAIL_VAR, fetch_jira_issue
from . import setups
from .profile import (
    PROFILE_CATEGORIES,
    PROFILE_FILE_CATEGORIES,
    build_bundle,
    claude_mcp_config_path,
    codex_mcp_overrides,
    load_profile,
    profile_file_path,
    profile_path,
    read_profile_raw,
    read_setups_raw,
    resolve_mcp_credentials,
    scan_profile_files,
    validate_mcp_engine,
    write_profile,
)
from .prompt import (
    build_decompose_prompt,
    build_diagnose_prompt,
    build_iterate_prompt,
    build_plan_prompt,
    build_prompt,
    build_replay_prompt,
    build_resume_prompt,
    build_review_pr_prompt,
    build_setup_block,
    task_slug,
)
from .reviewpr import build_review_findings, parse_review_findings, render_review_body, review_event
from .result import (
    EXIT_AGENT,
    EXIT_NETWORK,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    AuthError,
    ConfigError,
    DockerError,
    FrankyError,
    NetworkError,
    TaskRejected,
    build_error,
    build_result,
)
from .schema import build_schema
from .task import PROSE_MAX_CHARS, TaskSpec, parse_pr_task, parse_review_pr_task, parse_task
from .update_check import force_update, maybe_auto_update
from .userconfig import (
    SECRET_KEYS,
    SETTABLE_KEYS,
    config_file_path,
    load_config_file,
    mask_value,
    read_config_file,
    set_value,
    unset_value,
    write_config_file,
)

# Shape for `review-pr --expected-head-sha` - a bare git SHA, 7-40 hex chars (mirrors the
# the bridge's own `_SHA_RE` shape check, which Franky re-validates independently since
# this CLI is also reachable directly, not only via the bridge).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

FRANKY_VERBOSE_VAR = "FRANKY_VERBOSE"
# `franky gh` subprocess watchdog. A default cap honors the never-hang guarantee for the
# autonomous agent caller (a stalled `gh api` or a `gh run watch` must not wedge the caller
# forever); FRANKY_GH_TIMEOUT overrides it and 0 disables the cap for deliberately long-lived
# commands. This is a safety watchdog, not a capability limit - full gh power is unchanged.
FRANKY_GH_TIMEOUT_VAR = "FRANKY_GH_TIMEOUT"
_GH_DEFAULT_TIMEOUT = 120.0
# Cap on the redacted task summary stored in a run record (issue #63) - a handle for `jobs`,
# not the full prompt. Redact runs on the FULL string before this truncation.
_JOB_TASK_SUMMARY_MAX = 200

# Build statuses a `--retry` build may retry (issue #64 #5). pr_opened is success; config/auth/
# docker/task failures raise a FrankyError BEFORE the attempt loop, so they never reach here.
_RETRYABLE_STATUSES = frozenset({"timeout", "agent_error", "no_pr"})

# Default time budget for a `job diagnose` pass (seconds). Diagnosis is a read-only summarization
# of an existing transcript, not agentic work, and `--retry` fires it automatically, so it caps
# far below build's 1800s default. `--max-duration` overrides it on the standalone command.
_DIAGNOSE_DEFAULT_TIMEOUT = 300

# Bound on the audit trail (issue #72): a run that gets steered many times should not grow its
# record file unbounded - the last _STEER_NOTES_MAX corrections are plenty for a post-hoc look.
_STEER_NOTES_MAX = 20

# Run commands whose prompt carries the steer convention (see prompt.py's _STEER_CONVENTION), so
# the agent actually polls the mailbox. A diagnose/plan run never polls it, so `job attach` refuses
# those up front rather than reporting a misleading "delivered" success (issue #72).
_STEERABLE_COMMANDS = frozenset({"build", "replay", "resume", "iterate"})


def _stdin_is_interactive() -> bool:
    """True when stdin is a TTY (a human can answer a prompt).

    Wrapped in a module function so tests can monkeypatch it without touching sys.stdin.
    Every blocking prompt (plan-first confirm, `config set`/`init` wizard, `build -` stdin)
    guards on this so a non-TTY run fails fast (exit 2) instead of hanging - the never-hang
    guarantee.
    """
    return sys.stdin.isatty()


def _read_task_input(task_input: tuple[str, ...]) -> str:
    """Resolve the positional task argument, handling the `-` stdin convention.

    `<cmd> - --repo ...` reads the prose task from stdin. A TTY on `-` would block forever,
    so fail fast (never-hang, exit 2) rather than hang. Shared by `build` and `plan` so the
    stdin contract is identical across both.
    """
    if task_input == ("-",):
        if _stdin_is_interactive():
            raise FrankyError(
                "this command reads the task from stdin, but stdin is a TTY - pipe the task "
                "in or pass it as an argument",
                code=EXIT_USAGE,
                kind="interactive_input_required",
                hint="pipe a task into stdin or pass it as an argument",
            )
        return sys.stdin.read().strip()
    return " ".join(task_input)


def _resolve_task_spec(
    task_input_str: str,
    repo: str | None,
    engine: str | None,
    env: dict,
) -> tuple[Config, TaskSpec, str, list[str]]:
    """Run the shared config + task-parse + JIRA-fetch preamble; return (cfg, spec, branch, secrets).

    Factors out the body `build` and `plan` share so the allowlist gate, fail-closed config,
    JIRA fetch, and host-predicted branch are identical across both commands. Each underlying
    call already raises the right typed FrankyError (ConfigError/AuthError/TaskRejected/
    NetworkError); those re-raise untouched and only a residual plain ValueError is wrapped.

    The branch is computed BEFORE the JIRA fetch mutates spec.text on purpose: for a jira
    spec the slug keys on the bare KEY (stable), not the fetched body, so the host-predicted
    branch matches what build_prompt pins and what the idempotency pre-check looks up. `plan`
    ignores the branch (it builds nothing).
    """
    # Inject user config file values into env via setdefault (process env wins). A malformed
    # file -> ConfigError (exit 3) so --json gets the right code + JSON error.
    try:
        load_config_file(env)
    except FrankyError:
        raise
    except ValueError as exc:
        raise ConfigError(f"config file error: {exc}") from exc

    try:
        cfg = load_config(engine, env)
        secrets = cfg.secret_values()
        spec = parse_task(task_input_str, repo, cfg.allowed_repos)
        branch = f"franky/{task_slug(spec)}"
        if spec.source == "jira":
            # Fetch the JIRA issue host-side (the container has no JIRA creds or egress).
            body = fetch_jira_issue(spec.text, env)
            spec = dc_replace(spec, text=body[:PROSE_MAX_CHARS].strip())
    except FrankyError:
        raise
    except ValueError as exc:
        raise NetworkError(redact(str(exc), cfg_secrets_safe())) from exc
    return cfg, spec, branch, secrets


def _emit_error(exc: FrankyError, as_json: bool, secrets: list[str]) -> None:
    """Render a typed error: a JSON error object on stdout (--json) or prose on stderr.

    Redacts the message (and, under --json, the whole serialized object) so a secret VALUE
    can never reach the terminal even if a future error message interpolates env. The
    host-side JIRA token/email never reach `cfg.secret_values()`, so union in
    cfg_secrets_safe() too - defense-in-depth for the typed JIRA error path.
    """
    all_secrets = secrets + cfg_secrets_safe()
    if as_json:
        payload = build_error(exc.code, exc.kind, str(exc), exc.hint)
        click.echo(redact(json.dumps(payload), all_secrets))
    else:
        click.echo("franky: " + redact(str(exc), all_secrets), err=True)


def _emit_result(
    result: dict,
    as_json: bool,
    secrets: list[str],
    *,
    pr_url: str | None,
    status: str,
    quiet: bool,
) -> None:
    """Emit the result: one JSON object on stdout (--json) or the existing prose.

    --json: redact the serialized object and print it to stdout (the only stdout line).
    Non-json: keep stdout pure - the bare PR URL on stdout for a build pr_opened, the
    "no PR URL"/iterate-completion lines to stderr, never a secret value.
    """
    if as_json:
        click.echo(redact(json.dumps(result), secrets))
        return
    # already_open echoes the EXISTING PR URL on stdout (it IS a PR URL, like pr_opened) so an
    # agent scraping stdout for the URL still gets one on an idempotent short-circuit.
    if status in ("pr_opened", "already_open") and pr_url:
        click.echo(pr_url)
    elif status == "no_pr":
        click.echo(
            "franky: no PR URL found in agent output - see the redacted log "
            f"({result.get('log_path')})",
            err=True,
        )
    elif status == "iterate_complete" and not quiet:
        # iterate opens no new PR; report a labeled completion line (never a bare success URL)
        # on stderr so stdout stays pure. Suppressed under --quiet.
        click.echo(
            f"franky: iterate pass complete for {pr_url} - review the PR for the new commits",
            err=True,
        )
    elif status == "review_published" and pr_url:
        # The published review's URL, on stdout (mirrors pr_opened) - a caller scraping stdout
        # (or a log tail) for a result gets exactly this URL.
        click.echo(pr_url)
    elif status == "review_complete" and not quiet:
        # publish=False: nothing was written to GitHub; the findings live in the --json result
        # or the redacted log. Labeled completion line on stderr, stdout stays pure.
        click.echo(
            f"franky: review complete for {pr_url} (publish=False - nothing written to GitHub)",
            err=True,
        )


@click.group()
def main() -> None:
    """Franky - a lean personal coding agent that builds in a container and opens a PR."""


@main.command()
@click.argument("task_input", nargs=-1, required=True)
@click.option(
    "--repo", "repo", default=None, help="Target repo owner/repo (required for prose/jira tasks)."
)
@click.option(
    "--engine",
    "engine",
    default=None,
    # Derived from the registry so a new engine never has to be added in two places (the
    # --help listing comes out alphabetical, not resolution order - cosmetic, behaviour is
    # identical to the old hardcoded list).
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "--plan-first",
    "plan_first",
    is_flag=True,
    help="Run a read-only planning pass first, show the plan, and execute only after approval.",
)
@click.option(
    "--profile",
    "profile_path_opt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a profile.toml to inject operator files/MCP config into the container. "
    "Auto-discovered from ~/.franky/profile.toml if present.",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help="Stream raw agent output to stderr during the run (also: FRANKY_VERBOSE=1).",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a single JSON result/error object on stdout."
)
@click.option(
    "-q",
    "--quiet",
    "quiet",
    is_flag=True,
    help="Suppress progress + the update hint (implied by --json).",
)
@click.option(
    "-y", "--yes", "yes", is_flag=True, help="Auto-approve --plan-first (non-interactive)."
)
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the run after N seconds (default 1800).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help="Skip the idempotency pre-check and build even if a Franky PR is already open.",
)
@click.option(
    "--retry",
    "retry",
    type=click.IntRange(min=0, max=5),
    default=0,
    help="On a retryable failure (timeout/agent-error/no-PR), diagnose it and retry up to N "
    "times, feeding the root-cause back in (issue #64). Default 0 = no retry.",
)
@click.pass_context
def build(
    ctx: click.Context,
    task_input: tuple[str, ...],
    repo: str | None,
    engine: str | None,
    plan_first: bool,
    profile_path_opt: str | None,
    verbose: bool,
    as_json: bool,
    quiet: bool,
    yes: bool,
    max_duration: int | None,
    force: bool,
    retry: int,
) -> None:
    """Build TASK_INPUT (a GitHub issue URL, a JIRA key, or a prose request) and open a PR.

    Big task? Run `franky plan <task>` first to split it into PR-sized sub-tasks - one franky
    run is meant to be one focused PR.

    Examples:
      franky build https://github.com/you/repo/issues/42
      franky build jira FOO-123 --repo you/repo
      franky build "add a --json flag" --repo you/repo
      franky build - --repo you/repo          (read the prose task from stdin)

    With --plan-first, Franky first runs the engine in a read-only planning pass, prints the
    plan, and waits for explicit approval; nothing is built or PR'd until you confirm
    (--yes auto-approves; a non-interactive run without --yes fails fast, exit 2). The
    approval gate is the hard guarantee (the planning container is still autonomous), so a
    declined or non-interactive run writes nothing.

    --json emits one machine-readable result/error object on stdout; exit codes follow the
    documented taxonomy (0 ok, 2 usage, 3 config, 4 task, 5 auth, 6 docker, 7 agent, 8 net,
    9 timeout).
    """
    # --json implies --quiet: the JSON object is the only thing stdout/stderr should carry.
    quiet = quiet or as_json
    # Best-effort secret list for any error raised before cfg exists.
    secrets = cfg_secrets_safe()
    process_env = dict(os.environ)
    try:
        # stdin task input: `build - --repo ...`. A TTY on `-` would block forever, so fail
        # fast (never-hang); otherwise read the prose task from stdin.
        task_input_str = _read_task_input(task_input)

        # Best-effort, hint-only update check (never blocks/raises; ~1s budget, cached). Skip
        # under --quiet/--json so stderr stays clean. Silenced too by FRANKY_NO_UPDATE_CHECK=1.
        if not quiet:
            maybe_auto_update()

        # Shared config + task-parse + JIRA-fetch preamble (also computes the host-predicted
        # branch before the JIRA fetch mutates spec.text - see _resolve_task_spec).
        cfg, spec, branch, secrets = _resolve_task_spec(task_input_str, repo, engine, os.environ)

        # Idempotency pre-check (issue #50): if a Franky PR is already open on the predicted
        # branch, a retry must NOT open a second one. Report the existing PR and stop without
        # launching the container. Best-effort - find_open_pr returns None on any error, so a
        # flaky check never blocks a build. --force skips the check entirely.
        if not force:
            existing = find_open_pr(spec.repo, branch, os.environ)
            if existing:
                result = build_result(
                    status="already_open",
                    pr_url=existing,
                    reason="a Franky PR is already open for this task",
                    exit_code=EXIT_SUCCESS,
                    usage=Usage(),
                    duration=0.0,
                    log_path="",
                    engine=cfg.engine.name,
                    repo=spec.repo,
                    branch=branch,
                )
                _emit_result(
                    result, as_json, secrets, pr_url=existing, status="already_open", quiet=quiet
                )
                ctx.exit(EXIT_SUCCESS)

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)

        # Build the profile bundle (optional). Auto-discovers ~/.franky/profile.toml unless
        # overridden by --profile or FRANKY_PROFILE_PATH. Fails closed on detected credentials.
        bundle, setup_block = _load_profile_bundle(profile_path_opt, process_env, secrets, cfg)

        # Verbose mode: raw passthrough of agent output to stderr. Quiet (or --json) -> no
        # progress callback at all. Else distilled milestones (the default).
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)

        if plan_first:
            # Never-hang: an unattended run that cannot answer the approval gate must fail
            # fast BEFORE the planning container even starts (exit 2), not run-then-abort.
            if not yes and not _stdin_is_interactive():
                raise FrankyError(
                    "--plan-first needs interactive approval - pass --yes (or run in a TTY)",
                    code=EXIT_USAGE,
                    kind="interactive_input_required",
                    hint="pass --yes to auto-approve",
                )
            # PHASE 1: planning pass. Show the plan (to stderr - stdout stays pure), then gate.
            # A plan that errored is not a plan to approve. No economics on this pass.
            code, output, _plan_dur = _run_pass(
                cfg,
                build_plan_prompt(spec),
                franky_img,
                proxy_img,
                bundle,
                progress=progress,
                timeout=max_duration,
            )
            plan_log_path = _write_log(output, secrets, env=os.environ)
            if not quiet:
                click.echo("franky: --- plan (read-only, nothing written yet) ---", err=True)
                for chunk in chunks(output):
                    click.echo(chunk, err=True, nl=False)
                click.echo(err=True)
            # A timed-out planning pass returns the 124 sentinel; map it to the dedicated
            # timeout contract (exit 9) rather than the generic agent_error, same as PHASE 2.
            if code == CONTAINER_TIMEOUT_CODE:
                raise FrankyError(
                    "planning pass exceeded max-duration",
                    code=EXIT_TIMEOUT,
                    kind="timeout",
                )
            if code != 0:
                raise FrankyError(
                    f"planning pass exited non-zero ({code}) - see the redacted log "
                    f"({plan_log_path})",
                    code=EXIT_AGENT,
                    kind="agent_error",
                )
            # --yes auto-approves; else gate on the interactive confirm (fail-closed default).
            # Prompt to stderr (err=True) so stdout stays pure under --json even in a TTY.
            if not yes and not click.confirm(
                "franky: proceed to execute this plan?", default=False, err=True
            ):
                if not quiet:
                    click.echo(
                        "franky: plan-first aborted - nothing was built or opened.", err=True
                    )
                ctx.exit(EXIT_SUCCESS)

        # Capture the replay pin ONCE, before the attempt loop (issue #70): the base commit is
        # "the target repo's default-branch tip at build start", so every attempt of THIS build
        # (including retries) records the SAME base_sha - a retry re-diagnosing the same task is
        # still reproducing the same starting state, not a moving target. Best-effort: one extra
        # GitHub GET alongside the existing idempotency check; None (unresolved) just means a
        # later `job replay` of this run cannot pin a commit, never a build failure.
        base_sha = baseref.resolve_base_sha(spec.repo, os.environ)
        task_full = redact(spec.text, secrets)[:PROSE_MAX_CHARS]

        # PHASE 2 (or the only phase without --plan-first): build it and open the PR, with up to
        # `retry` diagnose-and-retry rounds (issue #64 #5). Each attempt is its own registered run
        # (issue #63); `--retry 0` runs the loop body exactly once, identical to before.
        prior_failures: list[str] = []
        attempts: list[dict] = []
        final: dict | None = None
        total_attempts = 1 + retry
        for attempt_no in range(1, total_attempts + 1):
            # Per-retry idempotency re-check (issue #50 + #64 review): an attempt classified as
            # failed may actually have opened a PR (e.g. a timeout AFTER `gh pr create`, or a
            # no_pr from truncated output). Re-check before spending another attempt so a retry
            # never opens a SECOND PR. --force skips it, matching the pre-loop pre-check.
            if attempt_no > 1 and not force:
                existing = find_open_pr(spec.repo, branch, os.environ)
                if existing:
                    final = {
                        "job_id": attempts[-1]["job_id"],
                        "status": "already_open",
                        "reason": "a Franky PR is already open (opened by an earlier attempt)",
                        "exit_code": EXIT_SUCCESS,
                        "pr_url": existing,
                        "usage": Usage(),
                        "duration": 0.0,
                        "log_path": "",
                    }
                    break

            final = _build_once(
                cfg,
                spec,
                branch=branch,
                franky_img=franky_img,
                proxy_img=proxy_img,
                bundle=bundle,
                setup_block=setup_block,
                progress=progress,
                timeout=max_duration,
                secrets=secrets,
                as_json=as_json,
                quiet=quiet,
                prior_failures=prior_failures,
                env=os.environ,
                source=spec.source,
                task_full=task_full,
                base_sha=base_sha,
            )
            attempts.append(
                {"job_id": final["job_id"], "status": final["status"], "retry_hint": ""}
            )
            if final["status"] == "pr_opened":
                break
            if attempt_no >= total_attempts or final["status"] not in _RETRYABLE_STATUSES:
                break

            # Interstitial diagnosis: analyze THIS failure and feed the hint into the next
            # attempt. A diagnose that fails, times out, or reports retryable=false STOPS the
            # loop - never a blind restart (issue #64 review).
            attempt_record = {
                "command": "build",
                "repo": spec.repo,
                "engine": cfg.engine.name,
                "status": final["status"],
                "exit_code": final["exit_code"],
                "task": redact(spec.text, secrets)[:_JOB_TASK_SUMMARY_MAX],
                # Runtime signals from THIS failed attempt (issue #69) so the diagnose pass
                # reasons over hard facts (exit code, OOM, egress denials) alongside the prose
                # transcript, not just the prose.
                "diagnostics": final.get("diagnostics"),
            }
            diagnosis, _diag_code = _diagnose(
                cfg,
                diagnosed_job_id=final["job_id"],
                repo=spec.repo,
                record=attempt_record,
                transcript=final["output"],
                franky_img=franky_img,
                proxy_img=proxy_img,
                progress=progress,
                secrets=secrets,
                as_json=as_json,
                quiet=quiet,
                timeout=_DIAGNOSE_DEFAULT_TIMEOUT,
                env=os.environ,
            )
            if diagnosis is None or not diagnosis.get("retryable"):
                break
            hint = (
                diagnosis.get("retry_hint")
                or diagnosis.get("root_cause")
                or "the previous attempt failed"
            )
            attempts[-1]["retry_hint"] = hint
            prior_failures.append(hint)

        # `attempts` is included ONLY when retries were requested, so a plain `build` (--retry 0)
        # emits the exact same keys as before (the review's byte-identical concern).
        result = build_result(
            status=final["status"],
            pr_url=final["pr_url"],
            reason=final["reason"],
            exit_code=final["exit_code"],
            usage=final["usage"],
            duration=final["duration"],
            log_path=str(final["log_path"]),
            engine=cfg.engine.name,
            repo=spec.repo,
            branch=branch,
            job_id=final["job_id"],
            attempts=attempts if retry > 0 else None,
        )
        _emit_result(
            result, as_json, secrets, pr_url=final["pr_url"], status=final["status"], quiet=quiet
        )
        ctx.exit(final["exit_code"])
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


@main.command()
@click.argument("pr_url")
@click.option(
    "--engine",
    "engine",
    default=None,
    # Same registry-derived choice as `build` (see that command's note).
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help="Stream raw agent output to stderr during the run (also: FRANKY_VERBOSE=1).",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a single JSON result/error object on stdout."
)
@click.option(
    "-q",
    "--quiet",
    "quiet",
    is_flag=True,
    help="Suppress progress + the update hint (implied by --json).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the run after N seconds (default 1800).",
)
@click.pass_context
def iterate(
    ctx: click.Context,
    pr_url: str,
    engine: str | None,
    verbose: bool,
    as_json: bool,
    quiet: bool,
    max_duration: int | None,
) -> None:
    """Address review feedback / failing CI on an existing Franky PR with follow-up commits.

    Example:
      franky iterate https://github.com/you/repo/pull/42

    Runs the SAME hardened, egress-controlled container as `franky build`, but instead of
    starting fresh it checks out the PR's existing branch, reads the review comments and
    failing checks via `gh`, and pushes ADDITIVE follow-up commits to that branch. It never
    force-pushes, never merges, and never opens a new PR - a human still reviews every change.
    The PR URL is authoritative (it carries owner/repo), so there is no --repo flag.

    --json emits one machine-readable result/error object on stdout (status iterate_complete
    on exit 0; the input PR URL is echoed back as pr_url). Exit codes follow the documented
    taxonomy (0 ok, 2 usage, 3 config, 4 task, 5 auth, 6 docker, 7 agent, 8 net, 9 timeout).
    """
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    process_env = dict(os.environ)
    try:
        if not quiet:
            maybe_auto_update()

        # Same config-file injection as `build` (see that command's WHY comment).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc

        try:
            cfg = load_config(engine, os.environ)
            secrets = cfg.secret_values()
            spec = parse_pr_task(pr_url, cfg.allowed_repos)
        except FrankyError:
            raise
        except ValueError as exc:
            raise NetworkError(redact(str(exc), cfg_secrets_safe())) from exc

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)

        bundle, setup_block = _load_profile_bundle(None, process_env, secrets, cfg)
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)
        # Register the run before it starts (issue #63), same as build - iterate has no
        # host-predicted branch, so branch is None in the record.
        job_id = jobs.new_job_id()
        _record_run_start(
            job_id, command="iterate", cfg=cfg, repo=spec.repo, summary=spec.text, branch=None
        )
        if not quiet:
            click.echo(f"franky: job {job_id} started", err=True)
        # Populated (best-effort) by run_in_container just before container teardown (issue #69).
        diagnostics: dict = {}
        code, output, duration = _run_pass(
            cfg,
            build_iterate_prompt(spec, operator_setup=setup_block),
            franky_img,
            proxy_img,
            bundle,
            progress=progress,
            timeout=max_duration,
            run_id=job_id,
            diagnostics_sink=diagnostics,
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ, run_id=job_id, env=os.environ)

        # iterate produces NO new PR; the existing PR gains commits. The PR URL is reported as
        # the input PR (not a fresh success artifact) - exit 0 means the pass ran, not that a
        # push necessarily landed (the agent pushes nothing on red tests or a failed own-PR
        # check). status is iterate_complete on a clean exit, agent_error otherwise.
        # Timeout first (124 is nonzero) -> dedicated timeout status, before generic agent_error.
        if code == CONTAINER_TIMEOUT_CODE:
            status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
        elif code != 0:
            status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
        else:
            status, reason, exit_code = "iterate_complete", "iterate pass complete", EXIT_SUCCESS

        _record_run_end(
            job_id,
            status=status,
            pr_url=spec.text,
            usage=usage,
            duration=duration,
            exit_code=exit_code,
            log_path=log_path,
            diagnostics=diagnostics,
        )
        result = build_result(
            status=status,
            pr_url=spec.text,
            reason=reason,
            exit_code=exit_code,
            usage=usage,
            duration=duration,
            log_path=str(log_path),
            engine=cfg.engine.name,
            repo=spec.repo,
            job_id=job_id,
        )
        _emit_result(result, as_json, secrets, pr_url=spec.text, status=status, quiet=quiet)
        ctx.exit(exit_code)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


@main.command("review-pr")
@click.argument("pr_url")
@click.argument("instructions", required=False, default="")
@click.option(
    "--expected-head-sha",
    "expected_head_sha",
    default=None,
    help="Refuse unless the PR's LIVE head SHA matches this (7-40 hex chars).",
)
@click.option(
    "--no-publish",
    "no_publish",
    is_flag=True,
    default=False,
    help="Review but write nothing to GitHub; report findings only (default: publish a review).",
)
@click.option(
    "--engine",
    "engine",
    default=None,
    # Same registry-derived choice as `build` (see that command's note).
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help="Stream raw agent output to stderr during the run (also: FRANKY_VERBOSE=1).",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a single JSON result/error object on stdout."
)
@click.option(
    "-q",
    "--quiet",
    "quiet",
    is_flag=True,
    help="Suppress progress + the update hint (implied by --json).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the run after N seconds (default 1800).",
)
@click.pass_context
def review_pr(
    ctx: click.Context,
    pr_url: str,
    instructions: str,
    expected_head_sha: str | None,
    no_publish: bool,
    engine: str | None,
    verbose: bool,
    as_json: bool,
    quiet: bool,
    max_duration: int | None,
) -> None:
    """Independently REVIEW an existing pull request - read-only, never merges or approves.

    Example:
      franky review-pr https://github.com/you/repo/pull/42
      franky review-pr --no-publish -- https://github.com/you/repo/pull/42 "focus on error handling"

    Runs the SAME hardened, egress-controlled container as `build`/`iterate`, but the agent only
    inspects the PR (diff, metadata, linked issue) and runs the repo's existing checks - it never
    edits, commits, pushes, merges, approves, dismisses reviews, or resolves conversations here.
    Franky itself (never the agent) posts the resulting GitHub review, and only ever as COMMENT
    or REQUEST_CHANGES - it never auto-approves.

    --expected-head-sha pins the PR head you last observed; a live head that disagrees (checked
    BEFORE the pass starts, and again immediately BEFORE publishing) refuses rather than
    reviewing or publishing stale state. --no-publish reviews without writing anything to
    GitHub - read the findings from the --json result or the redacted log instead.

    --json emits one machine-readable result/error object on stdout (status review_published or
    review_complete on success, including reviewed_sha/findings_summary/checks and, once
    published, review_url/review_id). Exit codes follow the documented taxonomy (0 ok, 2 usage,
    3 config, 4 task, 5 auth, 6 docker, 7 agent, 8 net, 9 timeout).
    """
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    process_env = dict(os.environ)
    try:
        if not quiet:
            maybe_auto_update()

        expected_head_sha = (expected_head_sha or "").strip().lower()
        if expected_head_sha and not _SHA_RE.match(expected_head_sha):
            raise FrankyError(
                f"--expected-head-sha {expected_head_sha!r} is not a valid git SHA "
                "(7-40 hex chars)",
                code=EXIT_USAGE,
                kind="usage_error",
            )
        instructions = (instructions or "").strip()[:PROSE_MAX_CHARS].strip()

        # Same config-file injection as `build`/`iterate` (see build's WHY comment).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc

        try:
            cfg = load_config(engine, os.environ)
            secrets = cfg.secret_values()
            repo, canonical_pr_url, pr_number = parse_review_pr_task(pr_url, cfg.allowed_repos)
        except FrankyError:
            raise
        except ValueError as exc:
            raise NetworkError(redact(str(exc), cfg_secrets_safe())) from exc

        # Pin the LIVE head SHA before anything else runs - the review is grounded against
        # exactly this commit. A caller-supplied --expected-head-sha must agree with it now, or
        # we refuse rather than reviewing state the caller no longer expects (issue: review-pr
        # MVP). Unlike find_open_pr's best-effort idempotency check, an unreachable/unparseable
        # fetch here is fail-closed (NetworkError), not silently skipped.
        live_sha = fetch_pr_head_sha(repo, pr_number, os.environ)
        if live_sha is None:
            raise NetworkError(
                f"could not read {canonical_pr_url}'s live head commit via the GitHub API - "
                "refusing"
            )
        if expected_head_sha and expected_head_sha != live_sha.lower():
            raise TaskRejected(
                f"expected head sha {expected_head_sha!r} does not match "
                f"{canonical_pr_url}'s current head {live_sha!r} - refusing (head changed)",
                kind="head_changed",
            )
        pinned_sha = live_sha

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)
        bundle, _setup_block = _load_profile_bundle(None, process_env, secrets, cfg)
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)

        # Per-run nonce fenced into the prompt + parser, same anti-injection register as `plan`
        # (a hostile PR body/diff cannot plant a fixed sentinel to hijack the findings reported).
        nonce = _make_nonce()
        job_id = jobs.new_job_id()
        _record_run_start(
            job_id, command="review-pr", cfg=cfg, repo=repo, summary=canonical_pr_url, branch=None
        )
        if not quiet:
            click.echo(f"franky: job {job_id} started", err=True)

        diagnostics: dict = {}
        code, output, duration = _run_pass(
            cfg,
            build_review_pr_prompt(repo, canonical_pr_url, instructions, nonce),
            franky_img,
            proxy_img,
            bundle,
            progress=progress,
            timeout=max_duration,
            run_id=job_id,
            diagnostics_sink=diagnostics,
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ, run_id=job_id, env=os.environ)

        review_url: str | None = None
        review_id: int | None = None
        findings_summary: str | None = None
        checks: list | None = None

        # Timeout first (124 is nonzero) -> dedicated timeout contract, before generic agent_error.
        if code == CONTAINER_TIMEOUT_CODE:
            status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
        elif code != 0:
            status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
        else:
            try:
                parsed = parse_review_findings(output, nonce)
            except Exception:
                parsed = None
            if parsed is None:
                status = "no_findings"
                reason = (
                    f"agent produced no parseable review findings - see the redacted log "
                    f"({log_path})"
                )
                exit_code = EXIT_AGENT
            else:
                shaped = build_review_findings(parsed)
                findings_summary = shaped["summary"]
                checks = shaped["checks"]
                if no_publish:
                    status = "review_complete"
                    reason = "review pass complete (publish=False, nothing written to GitHub)"
                    exit_code = EXIT_SUCCESS
                else:
                    # Re-check the LIVE head immediately before publishing - never post a review
                    # over a PR that moved on mid-run (same register as --expected-head-sha above).
                    recheck_sha = fetch_pr_head_sha(repo, pr_number, os.environ)
                    if recheck_sha is None or recheck_sha.lower() != pinned_sha.lower():
                        status = "publish_blocked_stale_head"
                        reason = (
                            f"{canonical_pr_url}'s head changed since the review started "
                            f"(reviewed {pinned_sha}) - refusing to publish a stale review"
                        )
                        exit_code = EXIT_AGENT
                    else:
                        event = review_event(shaped)  # COMMENT or REQUEST_CHANGES, never APPROVE
                        body = render_review_body(shaped)
                        try:
                            gcode, gout, gerr = run_gh(
                                [
                                    "api",
                                    f"repos/{repo}/pulls/{pr_number}/reviews",
                                    "-f",
                                    f"event={event}",
                                    "-f",
                                    f"body={body}",
                                ],
                                os.environ,
                                timeout=_resolve_gh_timeout(os.environ),
                            )
                        except subprocess.TimeoutExpired as exc:
                            raise FrankyError(
                                f"publishing the review to {canonical_pr_url} exceeded "
                                f"{int(exc.timeout)}s",
                                code=EXIT_TIMEOUT,
                                kind="timeout",
                            ) from exc
                        except OSError as exc:
                            raise DockerError(
                                "the `gh` CLI is not installed or not executable on the host - "
                                "review-pr publishes via gh"
                            ) from exc
                        if gcode != 0:
                            status = "publish_failed"
                            reason = (
                                "posting the GitHub review failed: "
                                f"{redact(gerr.strip(), secrets)[:500]}"
                            )
                            exit_code = EXIT_NETWORK
                        else:
                            try:
                                resp = json.loads(gout)
                            except (ValueError, TypeError):
                                resp = {}
                            review_url = resp.get("html_url") if isinstance(resp, dict) else None
                            review_id = resp.get("id") if isinstance(resp, dict) else None
                            status = "review_published"
                            reason = f"published a {event} review"
                            exit_code = EXIT_SUCCESS

        _record_run_end(
            job_id,
            status=status,
            pr_url=review_url or canonical_pr_url,
            usage=usage,
            duration=duration,
            exit_code=exit_code,
            log_path=log_path,
            diagnostics=diagnostics,
        )
        result = build_result(
            status=status,
            pr_url=canonical_pr_url,
            reason=reason,
            exit_code=exit_code,
            usage=usage,
            duration=duration,
            log_path=str(log_path),
            engine=cfg.engine.name,
            repo=repo,
            job_id=job_id,
            reviewed_sha=pinned_sha,
            findings_summary=findings_summary,
            checks=checks,
            review_url=review_url,
            review_id=review_id,
        )
        _emit_result(
            result,
            as_json,
            secrets,
            pr_url=(review_url or canonical_pr_url),
            status=status,
            quiet=quiet,
        )
        ctx.exit(exit_code)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


@main.command()
@click.argument("task_input", nargs=-1, required=True)
@click.option(
    "--repo", "repo", default=None, help="Target repo owner/repo (required for prose/jira tasks)."
)
@click.option(
    "--engine",
    "engine",
    default=None,
    # Same registry-derived choice as `build` (see that command's note).
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "--profile",
    "profile_path_opt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a profile.toml to inject operator files/MCP config into the container. "
    "Auto-discovered from ~/.franky/profile.toml if present.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a single JSON result/error object on stdout."
)
@click.option(
    "-q",
    "--quiet",
    "quiet",
    is_flag=True,
    help="Suppress progress + the update hint (implied by --json).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the run after N seconds (default 1800).",
)
@click.pass_context
def plan(
    ctx: click.Context,
    task_input: tuple[str, ...],
    repo: str | None,
    engine: str | None,
    profile_path_opt: str | None,
    as_json: bool,
    quiet: bool,
    max_duration: int | None,
) -> None:
    """Assess scope and decompose TASK_INPUT into PR-sized sub-tasks - READ-ONLY, builds nothing.

    Accepts the SAME task forms as `build` (a GitHub issue URL, a JIRA key, or a prose
    request, plus `plan - --repo ...` to read prose from stdin). Runs one read-only container
    pass that inspects the repo/issue, decides whether the task fits one focused PR or needs
    splitting, and emits a decomposition. It creates no branch, opens no PR, and writes
    nothing to the target repo - the caller orchestrates what to do with the sub-tasks.

    --json emits one machine-readable object on stdout: a decomposition
    `{fits_one_pr, subtasks:[{title, summary, suggested_repo}], rationale, engine, repo,
    exit_code}` on success (a DISTINCT envelope from build/iterate), or the shared
    `{"error":{...}}` object on failure. Exit codes follow the documented taxonomy (0 ok,
    2 usage, 3 config, 4 task, 5 auth, 6 docker, 7 agent/no-plan, 8 net, 9 timeout).
    """
    # --json implies --quiet: the JSON object is the only thing stdout/stderr should carry.
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    process_env = dict(os.environ)
    try:
        # stdin task input mirrors `build -` (never-hang on a TTY).
        task_input_str = _read_task_input(task_input)

        if not quiet:
            maybe_auto_update()

        # Shared config + task-parse + JIRA-fetch preamble (branch is unused - plan builds
        # nothing).
        cfg, spec, _branch, secrets = _resolve_task_spec(task_input_str, repo, engine, os.environ)

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)
        bundle, _setup_block = _load_profile_bundle(profile_path_opt, process_env, secrets, cfg)
        progress = None if quiet else _make_progress(cfg.engine, False)

        # Per-run nonce fenced into the prompt + parser: a hostile issue body / repo file
        # cannot plant a fixed sentinel to hijack the decomposition Franky reports (same
        # spirit as the repo-scoped PR-URL guard). Threaded into BOTH halves. Generated via
        # the module-level `secrets` (not the local secret-list var, which shadows it here).
        nonce = _make_nonce()
        code, output, duration = _run_pass(
            cfg,
            build_decompose_prompt(spec, nonce),
            franky_img,
            proxy_img,
            bundle,
            progress=progress,
            timeout=max_duration,
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        decompose_log_path = _write_log(output, secrets, footer=econ, env=os.environ)

        # Timeout first (124 is nonzero) -> dedicated timeout contract, before generic agent.
        if code == CONTAINER_TIMEOUT_CODE:
            raise FrankyError(
                "plan pass exceeded max-duration",
                code=EXIT_TIMEOUT,
                kind="timeout",
            )
        if code != 0:
            raise FrankyError(
                f"plan pass exited {code} - see the redacted log ({decompose_log_path})",
                code=EXIT_AGENT,
                kind="agent_error",
            )

        # parse_decomposition is best-effort; guard the call so ANY unexpected raise (e.g. a
        # RecursionError on a pathologically nested transcript) degrades to "no parseable plan"
        # and the shared error envelope, never a traceback that bypasses the --json contract.
        try:
            parsed = parse_decomposition(output, nonce)
        except Exception:
            parsed = None
        if parsed is None:
            # No partial object - route through the shared error envelope (exit 7).
            raise FrankyError(
                f"agent produced no parseable plan - see the redacted log ({decompose_log_path})",
                code=EXIT_AGENT,
                kind="no_plan",
            )

        result = build_plan_result(parsed, engine=cfg.engine.name, repo=spec.repo)
        if as_json:
            # The ONLY stdout line under --json.
            click.echo(redact(json.dumps(result), secrets))
        else:
            for line in _format_plan_summary(result):
                click.echo(redact(line, secrets))
        ctx.exit(EXIT_SUCCESS)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


def _make_nonce() -> str:
    """A per-run hex nonce for the `plan` sentinel fence.

    Wrapped so it reads `secrets.token_hex` off the MODULE (the `plan` body rebinds `secrets`
    to a secret-VALUE list, which would shadow the module), and so tests can monkeypatch
    `cli.secrets.token_hex` to a fixed value for a deterministic sentinel.
    """
    return secrets.token_hex(8)


def _format_plan_summary(result: dict) -> list[str]:
    """Render the human-readable (non-json) plan summary lines for stdout.

    Returns a list of lines the caller redacts and echoes: the fits-one-PR verdict, the
    numbered sub-tasks (`N. <title> [suggested_repo]` + their summaries), and the rationale.
    """
    lines: list[str] = []
    if result["fits_one_pr"]:
        lines.append("This task fits ONE focused PR.")
    else:
        lines.append("This task should be split into PR-sized sub-tasks.")
    subtasks = result["subtasks"]
    if subtasks:
        lines.append("")
        lines.append("Sub-tasks:")
        for i, st in enumerate(subtasks, start=1):
            lines.append(f"{i}. {st['title']} [{st['suggested_repo']}]")
            if st["summary"]:
                lines.append(f"   {st['summary']}")
    if result["rationale"]:
        lines.append("")
        lines.append(f"Rationale: {result['rationale']}")
    return lines


def _ensure_images(env: Mapping[str, str], engine: str) -> tuple[str, str]:
    """Resolve + ensure the franky and franky-proxy images are available locally.

    Returns (franky_image, proxy_image). Raises DockerError (exit 6, a clean message, no
    traceback) for the operator-facing failure modes: docker absent, auth needed, or pull
    failed - typed so the outer FrankyError handler emits the right code + JSON error too.
    Shared by every command that runs an engine container.
    """
    franky_img = resolve_image(env, FRANKY_IMAGE_VAR, "franky", engine=engine)
    proxy_img = resolve_image(env, FRANKY_PROXY_IMAGE_VAR, "franky-proxy")
    for label, img, dev_build, dev_var in (
        ("franky", franky_img, "docker build -t franky .", FRANKY_IMAGE_VAR),
        ("franky-proxy", proxy_img, "docker build -t franky-proxy proxy/", FRANKY_PROXY_IMAGE_VAR),
    ):
        ok, reason = ensure_image_available(img)
        if not ok:
            if reason == "no-docker":
                raise DockerError(
                    "docker is not available - is the daemon running and `docker` on PATH?"
                )
            if reason == "auth":
                raise DockerError(
                    f"{label} image '{img}' needs auth to pull - run `docker login ghcr.io` "
                    f"(a PAT with read:packages), or for local dev `{dev_build}` "
                    f"and set {dev_var}=<local-tag>."
                )
            raise DockerError(
                f"{label} image '{img}' not found locally and could not be pulled. "
                f"For local dev: `{dev_build}` and set {dev_var}=<local-tag>."
            )
    return franky_img, proxy_img


def _run_pass(
    cfg,
    prompt: str,
    franky_img: str,
    proxy_img: str,
    profile_bundle: bytes | None = None,
    progress=None,
    timeout: int | None = None,
    run_id: str | None = None,
    diagnostics_sink: dict | None = None,
    snapshot_sink: dict | None = None,
    resume_workspace: str | None = None,
) -> tuple[int, str, float]:
    """Run one container pass for `prompt` and return (exit_code, output, duration_secs).

    Duration is measured with time.monotonic() around run_in_container only. Logging and the
    economics summary are the CALLER's responsibility (the planning pass logs without an
    economics footer; the build and iterate passes attach one). Shared by build, iterate, and plan.

    `timeout` is the --max-duration budget in seconds; None preserves run_in_container's own
    default (FALLBACK_TIMEOUT_SECS), so the kwarg is only forwarded when explicitly set.
    `run_id` pins the container/net/proxy names to a job handle so the run registry can record
    them and `job status`/`kill` can target the SAME containers (issue #63); None -> a fresh id.
    `diagnostics_sink` (issue #69) is forwarded to run_in_container unchanged - see its
    docstring; None (the default) means no capture, byte-identical to pre-#69 behavior.
    `snapshot_sink`/`resume_workspace` (issue #71) are likewise forwarded unchanged - a
    snapshot-on-timeout sink and a workspace-to-restore path respectively; both None by default.
    """
    inner_argv = cfg.engine.inner_argv(prompt, model=cfg.model)
    for override in cfg.codex_mcp_overrides:
        inner_argv += ["-c", override]
    if cfg.claude_mcp_config_path:
        inner_argv += ["--mcp-config", cfg.claude_mcp_config_path, "--strict-mcp-config"]
    extra = {} if timeout is None else {"timeout": timeout}
    t0 = time.monotonic()
    code, output = run_in_container(
        cfg,
        inner_argv,
        image=franky_img,
        proxy_image=proxy_img,
        profile_bundle=profile_bundle,
        progress=progress,
        run_id=run_id,
        diagnostics_sink=diagnostics_sink,
        snapshot_sink=snapshot_sink,
        resume_workspace=resume_workspace,
        **extra,
    )
    duration = time.monotonic() - t0
    return code, output, duration


def _load_profile_bundle(
    profile_path_opt: str | None,
    credential_env: dict,
    secrets: list[str],
    cfg: Config,
) -> tuple[bytes | None, str]:
    """Load, scan, and pack the operator profile; return (bundle_bytes, prompt_setup_block).

    Resolution order: --profile flag path > FRANKY_PROFILE_PATH env var > auto-discovered
    ~/.franky/profile.toml.  Fails closed (ConfigError, exit 3) on a detected credential or a
    malformed profile, so the failure stays inside the machine contract (right exit code + a
    JSON error object under --json) instead of a bare exit-1 ClickException.
    An absent or empty profile is not an error; the caller treats (None, "") as "no bundle".

    The second element is the operator-setup prompt block (empty unless the profile declares
    `[setups]`): the bundle puts the files in the container, and that block is what tells the
    agent they are there, where its PR-description spec is, and which rules win on conflict.
    Both come from the SAME resolved spec, so what the prompt names is always what shipped.
    """
    from pathlib import Path

    if profile_path_opt:
        ppath = Path(profile_path_opt)
    else:
        ppath = profile_path(dict(os.environ))

    if ppath is None:
        return None, ""

    try:
        spec = load_profile(ppath)
        validate_mcp_engine(spec, cfg.engine.name)
        mcp_env = resolve_mcp_credentials(spec, credential_env)
    except ValueError as exc:
        raise ConfigError(redact(str(exc), secrets)) from exc

    cfg.passthrough_env.update(mcp_env)
    secrets.extend(mcp_env.values())
    cfg.extra_allowed_domains.extend(
        domain for domain in spec.mcp_domains if domain not in cfg.extra_allowed_domains
    )
    if cfg.engine.name == "codex":
        try:
            cfg.codex_mcp_overrides = codex_mcp_overrides(spec)
        except ValueError as exc:
            raise ConfigError(redact(str(exc), secrets)) from exc
    elif cfg.engine.name == "claude":
        cfg.claude_mcp_config_path = claude_mcp_config_path(spec)

    # A declared setup that sweeps to nothing is almost always a mistake (wrong directory, or a
    # layout the manifest does not know). Say so: the operator asked for their setup to be in the
    # container, and silently injecting nothing is the one failure they would not notice.
    for kind, scan in spec.setup_scans.items():
        if not scan.files:
            click.echo(
                f"franky: WARNING setup {kind!r} at {scan.root} matched no files - nothing from "
                "it will be injected. Run `franky profile check` to see what it expanded to.",
                err=True,
            )

    setup_block = build_setup_block(spec)
    if not spec.all_files():
        return None, ""

    try:
        return build_bundle(spec), setup_block
    except ValueError as exc:
        raise ConfigError(redact(str(exc), secrets)) from exc


def _make_progress(engine, verbose: bool):
    """Return the per-line progress callback for a container pass.

    verbose=True  -> Phase 1: raw passthrough; every redacted line is echoed to stderr as-is.
    verbose=False -> Phase 2: distilled view; only engine-parsed milestones reach stderr.

    In both cases stderr is the output channel so stdout remains PR-URL-only.
    """
    if verbose:

        def raw_cb(line: str) -> None:
            click.echo(line, err=True, nl=False)

        return raw_cb
    else:

        def distilled_cb(line: str) -> None:
            msg = engine.distill_line(line)
            if msg:
                click.echo(msg, err=True)

        return distilled_cb


def _parse_usage_safe(output: str) -> Usage:
    """parse_usage(output), degrading to an all-unknown Usage on any error.

    Parsed ONCE per pass; both the prose economics line (non-json) and the JSON economics
    block are derived from this single Usage so they can never disagree.
    """
    try:
        return parse_usage(output)
    except Exception:
        return Usage()


def _economics_line(usage: Usage, duration: float, secrets: list[str]) -> str:
    """The redacted one-line economics summary for a pass. Degrades to all-unknown on any
    format error so economics can never fail a run. Shared by build and iterate."""
    try:
        return redact(format_economics(usage, duration), secrets)
    except Exception:
        return redact(format_economics(Usage(), duration), secrets)


def cfg_secrets_safe() -> list[str]:
    """Best-effort secret list for redacting an error raised before cfg fully exists.

    Reads the JIRA token (and email) directly from os.environ so they are available even
    when load_config raised before cfg was assigned, and on the host-side JIRA fetch path
    which never reaches passthrough_env. Falls back gracefully (empty list) when the vars
    are absent - load_config/parse_task/fetch_jira_issue messages are already value-free,
    so this is a defensive backstop. One carve-out: a `codex_auth_volume` refusal message
    names the invalid FRANKY_CODEX_AUTH_VOLUME value verbatim - that value is a volume NAME
    (policy, like an allowlist entry), never a credential, so it is deliberately not redacted.
    """
    return [v for v in (os.environ.get(JIRA_API_TOKEN_VAR), os.environ.get(JIRA_EMAIL_VAR)) if v]


def _record_run_start(
    job_id,
    *,
    command,
    cfg,
    repo,
    summary,
    branch=None,
    source=None,
    task_full=None,
    base_sha=None,
    replay_of=None,
    resumed_from=None,
    env=None,
) -> None:
    """Write a status=running registry record before the container pass (issues #63, #64).

    Best-effort: any failure is swallowed so a registry hiccup can never block a run (mirrors
    economics' "never raises into a run"). `summary` is REDACTED first, THEN truncated - redacting
    the full string first so a secret can't be sliced in half and dodge the pattern. Takes `repo`
    + `summary` directly (not a TaskSpec) so build/iterate AND the specless `diagnose` pass can
    all register through this one helper.

    `source`/`task_full`/`base_sha`/`replay_of` (issue #70) are the replay-support fields; all
    default to None so a caller that never passes them is byte-identical to before. `task_full`
    MUST already be redacted by the caller (build passes `redact(spec.text, secrets)
    [:PROSE_MAX_CHARS]`) - unlike `summary`, this helper does not re-redact it, since the caller
    controls truncation length independently of `_JOB_TASK_SUMMARY_MAX`.
    """
    env = os.environ if env is None else env
    try:
        net, proxy, task = run_names(job_id)
        redacted = redact(summary, cfg.secret_values())[:_JOB_TASK_SUMMARY_MAX]
        record = jobs.new_record(
            job_id=job_id,
            command=command,
            repo=repo,
            engine=cfg.engine.name,
            task=redacted,
            container=task,
            network=net,
            proxy=proxy,
            branch=branch,
            started_at=jobs.now_iso(),
            source=source,
            task_full=task_full,
            base_sha=base_sha,
            replay_of=replay_of,
            resumed_from=resumed_from,
        )
        jobs.write_record(record, env)
        jobs.prune(env)  # only on the write path; never a side effect of a read
    except Exception:
        pass


def _record_run_end(
    job_id,
    *,
    status,
    pr_url,
    usage,
    duration,
    exit_code,
    log_path,
    diagnostics=None,
    snapshot_path=None,
    env=None,
) -> None:
    """Update the run record once the pass finishes. Best-effort - never raises into a build.

    `diagnostics` (issue #69) is the best-effort runtime-signal dict populated (or left empty)
    by a `diagnostics_sink` passed through `_run_pass`; an empty dict is normalized to None so
    the on-disk record matches `jobs.new_record`'s "null until populated" contract.

    `snapshot_path` (issue #71) is the host-local workspace snapshot path a timed-out run left
    behind (populated by a `snapshot_sink`), or None. It threads into the record so `franky job
    resume` and `prune` can find/reap the sidecar.
    """
    env = os.environ if env is None else env
    try:
        economics = {
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "duration_s": round(duration, 3),
        }
        jobs.update_record(
            job_id,
            {
                "status": status,
                "ended_at": jobs.now_iso(),
                "pr_url": pr_url,
                "economics": economics,
                "exit_code": exit_code,
                "log_path": str(log_path),
                "diagnostics": diagnostics or None,
                "snapshot_path": snapshot_path or None,
            },
            env,
        )
    except Exception:
        pass


def _build_once(
    cfg,
    spec,
    *,
    branch,
    franky_img,
    proxy_img,
    bundle,
    progress,
    timeout,
    secrets,
    as_json,
    quiet,
    prior_failures,
    env,
    source=None,
    task_full=None,
    base_sha=None,
    setup_block="",
) -> dict:
    """Run ONE build attempt end to end and return its outcome (issue #64 #5).

    Registers a fresh run (issue #63), runs the container pass with the branch pinned and any
    `prior_failures` learning-signal injected, writes the redacted log, classifies the result,
    and finalizes the record. Returns a dict the `build` retry loop consumes:
    {job_id, status, reason, exit_code, pr_url, output, log_path, usage, duration}. The
    classification (timeout > nonzero > pr_opened > no_pr) is identical to the pre-retry code.

    `source`/`task_full`/`base_sha` (issue #70) are threaded straight into `_record_run_start`
    so every build attempt's record carries the saved inputs a later `job replay` needs; all
    default to None (a caller that omits them records byte-identical to before).
    """
    job_id = jobs.new_job_id()
    _record_run_start(
        job_id,
        command="build",
        cfg=cfg,
        repo=spec.repo,
        summary=spec.text,
        branch=branch,
        source=source,
        task_full=task_full,
        base_sha=base_sha,
        env=env,
    )
    if not quiet:
        click.echo(f"franky: job {job_id} started", err=True)
    # Populated (best-effort) by run_in_container just before container teardown (issue #69).
    diagnostics: dict = {}
    # A timed-out build leaves a resumable workspace snapshot (issue #71) keyed to this job id.
    snapshot_sink: dict = {"dest": str(snapshot.snapshot_path_for(job_id, env))}
    code, output, duration = _run_pass(
        cfg,
        build_prompt(
            spec, branch=branch, prior_failures=prior_failures, operator_setup=setup_block
        ),
        franky_img,
        proxy_img,
        bundle,
        progress=progress,
        timeout=timeout,
        run_id=job_id,
        diagnostics_sink=diagnostics,
        snapshot_sink=snapshot_sink,
    )

    # Parse usage ONCE; both the prose econ line and the JSON economics block come from it.
    usage = _parse_usage_safe(output)
    econ = _economics_line(usage, duration, secrets)
    if not as_json:
        click.echo(econ, err=True)
    log_path = _write_log(output, secrets, footer=econ, run_id=job_id, env=env)

    # Scope PR-URL detection to the task's own repo so a hostile issue body cannot make Franky
    # report a PR URL for some other (attacker) repo. Timeout is checked FIRST: a timed-out run
    # returns the CONTAINER_TIMEOUT_CODE sentinel (124, nonzero), so it must be distinguished
    # before the generic agent_error branch and mapped to the dedicated timeout status.
    pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
    if code == CONTAINER_TIMEOUT_CODE:
        status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
    elif code != 0:
        status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
    elif pr_url:
        status, reason, exit_code = "pr_opened", "PR opened", EXIT_SUCCESS
    else:
        status, reason, exit_code = "no_pr", "agent produced no PR URL", EXIT_AGENT

    _record_run_end(
        job_id,
        status=status,
        pr_url=pr_url,
        usage=usage,
        duration=duration,
        exit_code=exit_code,
        log_path=log_path,
        diagnostics=diagnostics,
        snapshot_path=snapshot_sink.get("snapshot_path"),
        env=env,
    )
    return {
        "job_id": job_id,
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        "pr_url": pr_url,
        "output": output,
        "log_path": log_path,
        "usage": usage,
        "duration": duration,
        "diagnostics": diagnostics,
    }


def _diagnose(
    cfg,
    *,
    diagnosed_job_id,
    repo,
    record,
    transcript,
    franky_img,
    proxy_img,
    progress,
    secrets,
    as_json,
    quiet,
    timeout,
    env,
) -> tuple[dict | None, int]:
    """Run ONE read-only diagnose pass over a failed run's transcript (issue #64 #4).

    Returns (diagnosis, exit_code): the build_diagnosis_result dict + EXIT_SUCCESS on success, or
    (None, EXIT_TIMEOUT / EXIT_AGENT) on a timeout / nonzero-or-unparseable pass. Registers its
    own run (command="diagnose") so its cost shows in `franky jobs` / `jobs --stats`; the diagnose
    statuses (`diagnosed` / `diagnose_failed`) are outside compute_stats' success/failed sets, so
    they never skew build pass-rate. Shared by the standalone `job diagnose` command and the
    `build --retry` loop; never raises into either caller.
    """
    job_id = jobs.new_job_id()
    _record_run_start(
        job_id,
        command="diagnose",
        cfg=cfg,
        repo=repo,
        summary=f"diagnose {diagnosed_job_id}",
        branch=None,
        env=env,
    )
    if not quiet:
        click.echo(f"franky: diagnosing job {diagnosed_job_id} (job {job_id})", err=True)

    nonce = _make_nonce()
    code, output, duration = _run_pass(
        cfg,
        build_diagnose_prompt(record, transcript, nonce),
        franky_img,
        proxy_img,
        None,
        progress=progress,
        timeout=timeout,
        run_id=job_id,
    )
    usage = _parse_usage_safe(output)
    econ = _economics_line(usage, duration, secrets)
    if not as_json:
        click.echo(econ, err=True)
    log_path = _write_log(output, secrets, footer=econ, run_id=job_id, env=env)

    def _finish(status: str, exit_code: int) -> None:
        _record_run_end(
            job_id,
            status=status,
            pr_url=None,
            usage=usage,
            duration=duration,
            exit_code=exit_code,
            log_path=log_path,
            env=env,
        )

    if code == CONTAINER_TIMEOUT_CODE:
        _finish("diagnose_failed", EXIT_TIMEOUT)
        return None, EXIT_TIMEOUT
    if code != 0:
        _finish("diagnose_failed", EXIT_AGENT)
        return None, EXIT_AGENT
    try:
        parsed = parse_diagnosis(output, nonce)
    except Exception:
        parsed = None
    if parsed is None:
        _finish("diagnose_failed", EXIT_AGENT)
        return None, EXIT_AGENT
    _finish("diagnosed", EXIT_SUCCESS)
    return build_diagnosis_result(
        parsed, job_id=diagnosed_job_id, engine=cfg.engine.name
    ), EXIT_SUCCESS


def _format_diagnosis(d: dict) -> list[str]:
    """Render the human-readable (non-json) `job diagnose` report lines for stdout."""
    lines = [
        f"diagnosis for job {d['job_id']} (confidence: {d['confidence']})",
        f"category:     {d['category']}",
        f"retryable:    {d['retryable']}",
        "",
        f"root cause:   {d['root_cause']}",
        f"proposed fix: {d['proposed_fix']}",
    ]
    if d.get("retry_hint"):
        lines.append(f"retry hint:   {d['retry_hint']}")
    if d.get("evidence"):
        lines.append("")
        lines.append("evidence:")
        lines.extend(f"  - {item}" for item in d["evidence"])
    return lines


def _tasks_dir(env: Mapping[str, str] | None = None) -> Path:
    """Redacted-transcript directory, under the same overridable root as the job registry
    (FRANKY_RUNS_DIR) - so a redeployed/CWD-swapped caller (e.g. two Franky instances on one
    machine) never orphans its logs the way a CWD-relative tasks/ dir would."""
    return jobs.runs_dir(env) / "tasks"


def _write_log(
    output: str,
    secrets: list[str],
    footer: str | None = None,
    *,
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write the REDACTED agent output under `_tasks_dir(env)` and return its absolute Path.

    The file name is `<timestamp>-<suffix>.log`: `suffix` is the caller's run id when it has
    one (ties the transcript to its job record), else the current microsecond, so two logs
    started in the same second never clobber each other. When `footer` is given, it is
    appended after the transcript (also redacted) separated by a newline so the economics
    summary lands in the same file. The returned Path is surfaced as `log_path` in the JSON
    result and stored (absolute) in the run record.

    The runs dir and the `tasks` subdir are created 0700, and the transcript file 0600 -
    mirroring `jobs.write_record` - because the transcript can carry redacted-but-still
    task-shaped content. An unwritable runs dir surfaces as a clean ConfigError naming the
    path, not a raw traceback.
    """
    directory = _tasks_dir(env)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    suffix = run_id or now.strftime("%f")
    suffix_text = "\n" + (redact(footer, secrets) + "\n" if footer is not None else "")
    path = (directory / f"{stamp}-{suffix}.log").resolve()
    try:
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        if isinstance(output, Transcript):
            output.persist(path, suffix_text)
        else:
            with open_secure(path) as target:
                stream = Redactor(secrets)
                for chunk in chunks(output):
                    target.write(stream.feed(chunk))
                target.write(stream.feed("", final=True) + suffix_text)
    except OSError as exc:
        if isinstance(output, Transcript):
            output.close()
        raise ConfigError(f"could not write the run transcript under {directory}") from exc
    return path


def _engine_binary_version(
    name: str,
    runner=subprocess.run,
    timeout: float = 0.5,
) -> str | None:
    """Best-effort: return the first non-empty line of `name --version` output, or None.

    The engine binary runs INSIDE the container, so the host binary may be absent or a
    different version from the image's copy. This is informational only. All exceptions
    (FileNotFoundError, TimeoutExpired, OSError, etc.) are caught and return None.

    Args:
        name: The binary name (e.g. "pi" or "claude").
        runner: Injectable subprocess.run-compatible callable for tests.
        timeout: Seconds before giving up (default 0.5 - this should be instant).
    """
    try:
        proc = runner(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        combined = (proc.stdout or "") + (proc.stderr or "")
        for line in combined.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return None
    except Exception:
        return None


def _version_info(
    env: Mapping[str, str],
    executable: str | None = None,
    runner=subprocess.run,
) -> dict:
    """Build the version-info dict.

    Pinned keys:
    - "franky": franky_version() string.
    - "provenance": {"kind": str, "path": str} from detect_install.
    - "engine": {"name": str, "resolved": bool, "host_binary_version": str | None}.
    - "image": the resolved task image ref (FRANKY_IMAGE override or versioned GHCR ref).

    A bad FRANKY_ENGINE value must NOT crash this function - resolved will be False and
    host_binary_version will be None in that case.

    Args:
        env: Environment mapping (callers pass os.environ).
        executable: Python interpreter path for detect_install (default: sys.executable).
        runner: Injectable subprocess.run-compatible callable for _engine_binary_version.
    """
    install = detect_install(executable=executable)

    try:
        eng = resolve_engine(None, env)
        engine_name = eng.name
        resolved = True
        host_binary_version = _engine_binary_version(eng.name, runner)
    except ValueError:
        engine_name = env.get("FRANKY_ENGINE") or "<unknown>"
        resolved = False
        host_binary_version = None

    image = resolve_image(env, engine=engine_name if resolved else None)

    return {
        "franky": franky_version(),
        "provenance": {"kind": install.kind, "path": install.path},
        "engine": {
            "name": engine_name,
            "resolved": resolved,
            "host_binary_version": host_binary_version,
        },
        "image": image,
    }


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit version info as a single JSON object.")
def version(as_json: bool) -> None:
    """Print Franky version, install provenance, resolved engine, and task image."""
    info = _version_info(os.environ)
    if as_json:
        click.echo(json.dumps(info))
        return
    prov = info["provenance"]
    eng = info["engine"]
    img = info["image"]
    click.echo(f"franky {info['franky']}")
    click.echo(f"  provenance: {prov['kind']} ({prov['path']})")
    if not eng["resolved"]:
        click.echo(f"  engine:     {eng['name']} (unresolved - check FRANKY_ENGINE)")
    elif eng["host_binary_version"] is not None:
        click.echo(f"  engine:     {eng['name']} (host binary: {eng['host_binary_version']})")
    else:
        click.echo(f"  engine:     {eng['name']}")
    click.echo(f"  image:      {img}")


@main.command()
def schema() -> None:
    """Emit a machine-readable JSON description of Franky's commands, flags, result/error
    shapes, and exit-code taxonomy (the agent-facing capability contract).

    Always JSON - no --json flag - and the single JSON object is the only thing on stdout.
    """
    click.echo(json.dumps(build_schema(main)))


def _resolve_gh_timeout(env: Mapping[str, str]) -> float | None:
    """Seconds for the `franky gh` subprocess, from FRANKY_GH_TIMEOUT (default 120).

    A value of 0 (or negative) means no cap - for deliberately long-lived commands like
    `gh run watch`. An unset or unparseable value falls back to the default so a typo never
    silently removes the watchdog.
    """
    raw = env.get(FRANKY_GH_TIMEOUT_VAR)
    if not raw:
        return _GH_DEFAULT_TIMEOUT
    try:
        val = float(raw)
    except ValueError:
        return _GH_DEFAULT_TIMEOUT
    return val if val > 0 else None


@main.command(
    "gh",
    # Pass EVERYTHING through to gh: unknown flags are not Franky's, and no Franky --help is
    # added (so `franky gh --help` shows gh's own help - this command is a pure passthrough).
    context_settings={"ignore_unknown_options": True},
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def gh(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Run `gh ARGS...` with Franky's GitHub token (full gh surface, non-interactive).

    Franky's caller usually has no gh CLI or token; Franky does. `franky gh` lends Franky's
    GH_TOKEN to the real gh CLI so the caller can query and act on GitHub (pr status/checks,
    comment, merge, api, ...) without its own credential. FULL power: bounded only by the
    token's scopes, not by Franky - there is no read-only gate and no repo allowlist on this
    surface (scope the token to limit it). The token value is never leaked - it reaches gh via
    the environment (never on argv) and gh's output is redacted. gh's own exit code is passed
    through; a missing token -> exit 5, gh not installed on the host -> exit 6.

    Non-interactive by design: output is captured then redacted, so pass gh's own flags rather
    than relying on its interactive prompts. `franky gh --help` shows gh's help. A watchdog caps
    the run at FRANKY_GH_TIMEOUT seconds (default 120, exit 9 on hit); set it to 0 for no cap
    (e.g. a long-lived `gh run watch`).

    Examples:
      franky gh pr list --repo you/repo
      franky gh pr checks 62 --repo you/repo
      franky gh api /repos/you/repo/pulls
    """
    env = dict(os.environ)
    # A gh error message could echo a token from the env; redact every known secret value.
    secrets = [env[k] for k in SECRET_KEYS if env.get(k)]
    try:
        # Merge ~/.franky/config into env (process env wins) so a token stored there works,
        # exactly like build. A malformed file -> ConfigError (exit 3).
        try:
            load_config_file(env)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc
        # Re-read after the merge so a config-file token is included in the redaction set.
        secrets = [env[k] for k in SECRET_KEYS if env.get(k)]

        if not env.get(GH_TOKEN_VAR):
            raise AuthError(
                f"{GH_TOKEN_VAR} is unset or empty - refusing (franky gh lends Franky's "
                "GitHub token to gh)",
                hint=f"set {GH_TOKEN_VAR} in the env or via `franky config set {GH_TOKEN_VAR}`",
            )

        try:
            code, out, err = run_gh(args, env, timeout=_resolve_gh_timeout(env))
        except subprocess.TimeoutExpired as exc:
            # Never-hang: a stalled or long-lived gh command hit the watchdog. Distinct exit 9
            # (timeout), same as a build that exceeds --max-duration.
            raise FrankyError(
                f"gh command exceeded {int(exc.timeout)}s ({FRANKY_GH_TIMEOUT_VAR}) - set "
                f"{FRANKY_GH_TIMEOUT_VAR}=0 for no cap (e.g. long-lived `gh run watch`)",
                code=EXIT_TIMEOUT,
                kind="timeout",
                hint=f"raise or unset {FRANKY_GH_TIMEOUT_VAR} (0 = no cap)",
            ) from exc
        except OSError as exc:
            # gh missing (FileNotFoundError) or present-but-not-executable (PermissionError) -
            # both are OSError; surface a clean operator error, not a traceback.
            raise DockerError(
                "the `gh` CLI is not installed or not executable on the host - `franky gh` "
                "runs gh host-side",
                hint="install the GitHub CLI: https://cli.github.com",
            ) from exc

        # Preserve stdout purity: gh's stdout -> our stdout, gh's stderr -> our stderr, each
        # redacted. nl=False keeps gh's own trailing newline (no doubled newline). Then pass
        # gh's exit code through as our own.
        if out:
            click.echo(redact(out, secrets), nl=False)
        if err:
            click.echo(redact(err, secrets), nl=False, err=True)
        ctx.exit(code)
    except FrankyError as exc:
        # Raw passthrough (not a --json command): prose to stderr + the typed exit code.
        click.echo("franky: " + redact(str(exc), secrets), err=True)
        ctx.exit(exc.code)


@main.command()
@click.option("--force", is_flag=True, help="Reinstall even when already on the latest release.")
@click.pass_context
def update(ctx: click.Context, force: bool) -> None:
    """Install the latest published release via the detected installer (uv tool/pipx/pip).

    Dev checkout -> git hint, no-op. Undetectable installer -> manual hint, nonzero exit.
    """
    ctx.exit(force_update(force=force, out=click.echo))


# ---------------------------------------------------------------------------
# `franky jobs` (list) + `franky job` subgroup (status / logs / kill) - issue #63
# ---------------------------------------------------------------------------
# The run registry (~/.franky/runs) records every build/iterate run so a second shell can see
# and control an in-flight (or hung) run: `franky jobs` lists, `job status` shows live state,
# `job logs` prints the transcript, `job kill` reaps a stuck container + its proxy/network.


def _job_age(started_at: str | None) -> str:
    """Compact age from an ISO started_at (e.g. `45s`, `12m`, `3h`, `2d`); `?` if unparseable."""
    if not started_at:
        return "?"
    try:
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return "?"
    secs = max(0, int((datetime.now(start.tzinfo) - start).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _require_record(job_id: str) -> dict:
    """Read a run record or raise a clean job_not_found error (exit 2) - never a traceback.

    A missing OR malformed/unreadable record both surface here as "no run found" (fail-closed),
    so a corrupt registry file is reported cleanly rather than crashing.
    """
    record = jobs.read_record(job_id, os.environ)
    if record is None:
        raise FrankyError(
            f"no run found for job id {job_id!r} (see `franky jobs`)",
            code=EXIT_USAGE,
            kind="job_not_found",
            hint="run `franky jobs` to list known job ids",
        )
    return record


def _format_stats(stats: dict) -> list[str]:
    """Render the compact human-readable `jobs --stats` report (issue #64).

    A percentage/duration/cost that is None (no terminal runs / no economics) prints as `n/a`
    so the report never crashes on a sparse registry. The by-engine / by-repo blocks are
    omitted when empty (a zero-run registry shows only the `runs: 0` line).
    """

    def _pct(rate: float | None) -> str:
        return f"{rate * 100:.0f}%" if rate is not None else "n/a"

    def _dur(secs: float | None) -> str:
        return f"{secs:.0f}s" if secs is not None else "n/a"

    def _usd(cost: float | None) -> str:
        return f"${cost:.2f}" if cost is not None else "n/a"

    lines = [f"runs:          {stats['total']}"]
    if stats["total"] == 0:
        return lines
    lines += [
        f"success:       {stats['success']}  ({_pct(stats['success_rate'])})",
        f"failed:        {stats['failed']}",
        f"running:       {stats['running_fresh']}",
        f"hangs:         {stats['hangs']}  (timeout + stale-running)",
        f"median dur:    {_dur(stats['median_duration_s'])}",
        f"total cost:    {_usd(stats['total_cost_usd'])}",
    ]
    for title, key in (("engine", "by_engine"), ("repo", "by_repo")):
        groups = stats[key]
        if not groups:
            continue
        lines.append("")
        lines.append(f"by {title}:")
        for name, g in groups.items():
            lines.append(
                f"  {name:<24} n={g['n']:<3} ok={_pct(g['success_rate']):<5} "
                f"dur={_dur(g['median_duration_s']):<6} {_usd(g['total_cost_usd'])}"
            )
    return lines


@main.command("jobs")
@click.option(
    "--stats",
    "as_stats",
    is_flag=True,
    help="Show cross-run aggregate stats (success/hang rate, median duration & cost, by "
    "engine/repo) instead of the list. Covers ALL recorded runs; ignores -n.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit as JSON: the run-list array, or the stats object with --stats.",
)
@click.option(
    "-n", "--limit", type=click.IntRange(min=1), default=20, help="Max runs to show (default 20)."
)
def jobs_list(as_stats: bool, as_json: bool, limit: int) -> None:
    """List recent Franky runs (newest first): id, command, status, age, repo.

    Reads the run registry (~/.franky/runs) - a pure read, never mutates. Use `franky job
    status <id>` for one run's live state, `job logs <id>` for its transcript, `job kill <id>`
    to reap a stuck run, and `job export <id>` to bundle one for offline forensics. With
    --stats, prints cross-run aggregate health (over ALL runs, not just the -n most recent).
    """
    records = jobs.list_records(os.environ)
    if as_stats:
        stats = jobs.compute_stats(records)
        if as_json:
            click.echo(json.dumps(stats))
        else:
            for line in _format_stats(stats):
                click.echo(line)
        return
    records = records[:limit]
    if as_json:
        click.echo(json.dumps(records))
        return
    if not records:
        click.echo("franky: no runs recorded yet", err=True)
        return
    for record in records:
        click.echo(
            f"{record.get('job_id', '?'):<12}  {record.get('command', '?'):<7}  "
            f"{record.get('status', '?'):<15}  {_job_age(record.get('started_at')):>4}  "
            f"{record.get('repo', '?')}"
        )


@main.group("job")
def job_group() -> None:
    """Inspect and control a single Franky run by its job id (see `franky jobs`)."""


@job_group.command("status")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Emit the run record as a JSON object.")
@click.pass_context
def job_status(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Show one run's record plus whether its container is still alive.

    `container_running` is a live `docker inspect` on the recorded container name - it
    distinguishes a still-running (possibly stuck) run from one that has finished or been reaped.
    """
    try:
        record = _require_record(job_id)
        alive = container_running(record.get("container", ""))
        if as_json:
            click.echo(json.dumps({**record, "container_running": alive}))
        else:
            live = "running" if alive else "not running (container gone)"
            click.echo(f"job:        {record.get('job_id')}")
            click.echo(f"command:    {record.get('command')}")
            click.echo(f"repo:       {record.get('repo')}")
            click.echo(f"engine:     {record.get('engine')}")
            click.echo(f"status:     {record.get('status')}")
            click.echo(f"container:  {record.get('container')} ({live})")
            click.echo(f"started:    {record.get('started_at')}")
            click.echo(f"ended:      {record.get('ended_at')}")
            click.echo(f"pr_url:     {record.get('pr_url')}")
            click.echo(f"log_path:   {record.get('log_path')}")
            diag = record.get("diagnostics")
            if diag:
                click.echo("diagnostics:")
                for k in (
                    "task_exit_code",
                    "task_state",
                    "oom_killed",
                    "dind_ready",
                    "tmpfs_full",
                    "proxy_denied_count",
                ):
                    if k in diag:
                        click.echo(f"  {k}: {diag[k]}")
                denied = diag.get("egress_denied") or []
                for entry in denied:
                    click.echo(f"  egress_denied: {entry.get('host')} (x{entry.get('count')})")
    except FrankyError as exc:
        _emit_error(exc, as_json, [])
        ctx.exit(exc.code)


@job_group.command("logs")
@click.argument("job_id")
@click.pass_context
def job_logs(ctx: click.Context, job_id: str) -> None:
    """Print a run's redacted transcript (the FRANKY_RUNS_DIR/tasks/<ts>-<run_id>.log written
    when the pass finishes).

    The transcript is written at the END of a run, so a still-running job has no log yet - that
    is reported cleanly, not as a file-not-found trace. For a live view use `franky build -v`.
    """
    try:
        record = _require_record(job_id)
        log_path = record.get("log_path") or ""
        if not log_path or not Path(log_path).exists():
            raise FrankyError(
                f"no log available yet for job {job_id} - the run may still be in progress",
                code=EXIT_USAGE,
                kind="log_unavailable",
                hint="the transcript is written when the pass finishes; see `franky job status`",
            )
        # The on-disk log was written via _write_log and is ALREADY redacted; print verbatim.
        for chunk in Transcript(Path(log_path)).chunks():
            click.echo(chunk, nl=False)
    except FrankyError as exc:
        _emit_error(exc, False, [])
        ctx.exit(exc.code)


@job_group.command("kill")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Emit the kill result as a JSON object.")
@click.pass_context
def job_kill(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Force-remove a run's container and reap its proxy sidecar + network (stop a stuck run).

    Reaps in the same task -> proxy -> net order as a normal teardown. The record is marked
    `killed` only if the run was still running or a container was actually reaped, so killing an
    already-finished job never rewrites its real outcome.

    `job kill` is a SEPARATE process from the (likely wedged) `franky build` that started this
    run, so it is the primary place a stuck run's diagnostics (issue #69) get captured at all -
    the in-process capture in run_in_container never fires for a process that never returns.
    Capture happens BEFORE reap_run so the containers still exist to inspect; best-effort, same
    as everywhere else this dict is built.
    """
    try:
        record = _require_record(job_id)
        # Only a still-running record has live containers to inspect - a finished run's task/proxy
        # are already reaped, so capturing there is wasted work (and the patch guard below skips
        # relabelling it anyway). Called via the module-level name so tests can monkeypatch
        # franky.cli.capture_diagnostics. LIMITATION: on the kill path there is no live transcript
        # to read (we pass ""), so dind_ready/tmpfs_full are NOT populated here - only the
        # docker-inspect (exit/OOM/state) + squid-log (egress) signals are.
        diag: dict = {}
        snapshot_path = None
        if record.get("status") == "running":
            kill_secrets = [os.environ[k] for k in SECRET_KEYS if os.environ.get(k)]
            try:
                diag = capture_diagnostics(
                    record.get("container", ""),
                    record.get("proxy", ""),
                    "",
                    subprocess.run,
                    task_launched=True,
                    proxy_launched=True,
                    secrets=kill_secrets,
                    timeout=3.0,
                )
            except Exception:
                diag = {}
            # Snapshot /work BEFORE reap_run (issue #71): the container is still alive here, so a
            # killed run stays resumable. Only build/replay/resume runs carry resumable inputs -
            # an iterate/diagnose run has no workspace worth continuing, so snapshotting it would
            # just waste work and orphan a tar. Best-effort - a failure never changes the kill.
            if record.get("command") in ("build", "replay", "resume"):
                try:
                    snapshot_path = snapshot.snapshot_workspace(
                        record.get("container", ""),
                        snapshot.snapshot_path_for(job_id, os.environ),
                        kill_secrets,
                        subprocess.run,
                    )
                except Exception:
                    snapshot_path = None
        reaped = reap_run(job_id)
        if record.get("status") == "running" or reaped:
            patch = {"status": "killed", "ended_at": jobs.now_iso()}
            if diag:
                patch["diagnostics"] = diag
            if snapshot_path:
                patch["snapshot_path"] = snapshot_path
            jobs.update_record(job_id, patch, os.environ)
        if as_json:
            click.echo(
                json.dumps({"job_id": job_id, "status": "killed", "container_reaped": reaped})
            )
        elif reaped:
            click.echo(f"franky: killed job {job_id} (container reaped)")
        else:
            click.echo(f"franky: job {job_id} had no running container to reap", err=True)
    except FrankyError as exc:
        _emit_error(exc, as_json, [])
        ctx.exit(exc.code)


@job_group.command("export")
@click.argument("job_id")
@click.option(
    "-o",
    "--output",
    "output",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write the bundle here (default ./franky-job-<id>.tar.gz).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the export result as a JSON object.")
@click.pass_context
def job_export(ctx: click.Context, job_id: str, output: str | None, as_json: bool) -> None:
    """Bundle a run's record + redacted transcript into a portable .tar.gz for offline forensics.

    The archive holds `record.json` (the secret-free run record) and, when the transcript still
    exists, `transcript.log` (the already-redacted task log). Hand it to a human or another agent
    to inspect a failed or stuck run without access to this machine - both members are secret-free
    by construction, so nothing new is exposed. Unknown/corrupt job id -> exit 2.
    """
    try:
        record = _require_record(job_id)
        dest = Path(output) if output else Path.cwd() / f"franky-job-{job_id}.tar.gz"
        try:
            summary = jobs.export_bundle(record, dest)
        except OSError as exc:
            # No dedicated filesystem exit code in the taxonomy; reuse EXIT_USAGE (2), the same
            # code job_logs uses for its log_unavailable state error, with a clean typed message.
            raise FrankyError(
                f"could not write export bundle to {dest}: {exc}",
                code=EXIT_USAGE,
                kind="export_failed",
                hint="pass a writable path with --output",
            ) from exc
        if as_json:
            click.echo(json.dumps({"job_id": job_id, **summary}))
        else:
            click.echo(
                f"franky: exported job {job_id} -> {summary['output_path']} "
                f"({len(summary['included'])} files, {summary['bytes']} bytes)"
            )
    except FrankyError as exc:
        _emit_error(exc, as_json, [])
        ctx.exit(exc.code)


@job_group.command("diagnose")
@click.argument("job_id")
@click.option(
    "--engine",
    "engine",
    default=None,
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the diagnosis as a single JSON object.")
@click.option(
    "-v", "--verbose", "verbose", is_flag=True, default=False, help="Stream raw agent output."
)
@click.option("-q", "--quiet", "quiet", is_flag=True, help="Suppress progress (implied by --json).")
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the diagnose pass after N seconds (default 300).",
)
@click.pass_context
def job_diagnose(
    ctx: click.Context,
    job_id: str,
    engine: str | None,
    as_json: bool,
    verbose: bool,
    quiet: bool,
    max_duration: int | None,
) -> None:
    """Diagnose WHY a recorded run failed - a read-only meta-agent over its transcript (issue #64).

    Dispatches the engine in a read-only container pass over the run's persisted transcript +
    metadata (it clones nothing, edits nothing, opens no PR) and emits a structured root-cause,
    proposed fix, and a `retryable`/`retry_hint` learning signal - the same signal `franky build
    --retry` feeds back into a fresh attempt. Unknown/corrupt id -> exit 2; a run with no
    transcript yet -> exit 2; the pass timing out -> exit 9; no parseable diagnosis -> exit 7.

    --json emits one machine-readable diagnosis object (a DISTINCT envelope from build/iterate);
    see `franky schema` -> diagnosis_result_schema.
    """
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    try:
        record = _require_record(job_id)
        log_path = record.get("log_path") or ""
        if not log_path or not Path(log_path).exists():
            raise FrankyError(
                f"cannot diagnose job {job_id} - it has no transcript yet (still running?)",
                code=EXIT_USAGE,
                kind="log_unavailable",
                hint="the transcript is written when the pass finishes; see `franky job status`",
            )
        transcript = Transcript(Path(log_path))

        # Same config-file merge as build (a config-file token/engine must work here too).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc
        cfg = load_config(engine, os.environ)
        secrets = cfg.secret_values()

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)
        timeout = max_duration if max_duration is not None else _DIAGNOSE_DEFAULT_TIMEOUT

        diagnosis, code = _diagnose(
            cfg,
            diagnosed_job_id=job_id,
            repo=record.get("repo") or "?",
            record=record,
            transcript=transcript,
            franky_img=franky_img,
            proxy_img=proxy_img,
            progress=progress,
            secrets=secrets,
            as_json=as_json,
            quiet=quiet,
            timeout=timeout,
            env=os.environ,
        )
        if diagnosis is None:
            if code == EXIT_TIMEOUT:
                raise FrankyError(
                    "diagnose pass exceeded its time budget", code=EXIT_TIMEOUT, kind="timeout"
                )
            raise FrankyError(
                "agent produced no parseable diagnosis - see the redacted log (log_path)",
                code=EXIT_AGENT,
                kind="no_diagnosis",
            )

        if as_json:
            click.echo(redact(json.dumps(diagnosis), secrets))
        else:
            for line in _format_diagnosis(diagnosis):
                click.echo(redact(line, secrets))
        ctx.exit(EXIT_SUCCESS)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


@job_group.command("replay")
@click.argument("job_id")
@click.option(
    "--engine",
    "engine",
    default=None,
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the replay result as a single JSON object."
)
@click.option(
    "-v", "--verbose", "verbose", is_flag=True, default=False, help="Stream raw agent output."
)
@click.option("-q", "--quiet", "quiet", is_flag=True, help="Suppress progress (implied by --json).")
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the replay pass after N seconds (default 1800).",
)
@click.option(
    "--open-pr",
    "open_pr",
    is_flag=True,
    default=False,
    help="Opt into opening a real PR (branch + gh pr create) instead of the default "
    "reproduce-only pass.",
)
@click.pass_context
def job_replay(
    ctx: click.Context,
    job_id: str,
    engine: str | None,
    as_json: bool,
    verbose: bool,
    quiet: bool,
    max_duration: int | None,
    open_pr: bool,
) -> None:
    """Re-run a recorded build/replay from its SAVED inputs to reproduce a failure (issue #70).

    Reconstructs the original task (its source + REDACTED full text, saved at the time of the
    original run) and re-runs it in the SAME hardened, egress-controlled container `build`
    uses, pinned to the EXACT base commit the original run started from (the target repo's
    default-branch tip at that run's build start - see `franky/baseref.py`). REPRODUCE-ONLY by
    default: the container makes the change and reports what happened, but creates no branch,
    pushes nothing, and opens no PR - a replay is always safe to run without risking a
    duplicate PR. Pass --open-pr to opt into the normal build conventions (a `franky/<slug>`
    branch, tests-green-before-PR, `gh pr create`) once a fix is confirmed.

    NONDETERMINISM CAVEAT: replay reproduces the INPUTS (the task text + the base commit), NOT
    bit-identical output - the underlying LLM is not deterministic, so a replay's transcript can
    still diverge from the original even with identical inputs.

    SOURCE-FIDELITY NOTE: a jira- or prose-sourced replay uses the FROZEN task text recorded at
    the original run (jira: the fetched issue body as it was then; prose: the prose itself) - it
    reflects that run's inputs exactly. An ISSUE-sourced replay instead re-fetches the live issue
    via `gh issue view` inside the container (identical to a normal `build` on an issue URL), so
    it reflects the issue's CURRENT content, not necessarily its state at the original run.

    Only `build`/`replay` runs have saved, reproducible inputs (`iterate`/`diagnose` runs do
    not) - replaying anything else is exit 2. A run recorded before replay support was added has
    no saved inputs either and cannot be replayed (exit 2), and a base commit that no longer
    exists on the repo (force-pushed or garbage-collected) is refused up front (exit 2) rather
    than started and left to fail deep inside the container.

    --json emits one machine-readable result object on stdout (the SAME envelope as `build`,
    with a `replay_of` field naming the original job id); exit codes follow the documented
    taxonomy (0 ok, 2 usage, 3 config, 4 task, 7 agent, 9 timeout).
    """
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    try:
        record = _require_record(job_id)
        if record.get("command") not in ("build", "replay"):
            raise FrankyError(
                f"cannot replay a {record.get('command')!r} run - only build/replay runs have "
                "reproducible inputs",
                code=EXIT_USAGE,
                kind="not_replayable",
                hint="see `franky jobs` for build runs",
            )

        # Reconstruct the original TaskSpec from the saved inputs. A record written before
        # replay support was added has none of these - refuse cleanly rather than replay junk.
        source = record.get("source")
        text = record.get("task_full")
        repo = record.get("repo")
        if not source or not text or not repo:
            raise FrankyError(
                "this run predates replay support (no saved task inputs) - cannot replay",
                code=EXIT_USAGE,
                kind="replay_inputs_missing",
            )

        # Same config-file merge as build/diagnose (a config-file token/engine must work here too).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc
        cfg = load_config(engine, os.environ)
        secrets = cfg.secret_values()

        # Re-validate the allowlist: the repo may have been dropped from it since the original
        # run - a saved record must never bypass the fail-closed gate a fresh build goes through.
        if not repo_allowed(repo, cfg.allowed_repos):
            raise TaskRejected(f"repo {repo!r} is no longer in the allowlist - refusing to replay")

        base_sha = record.get("base_sha")
        if not base_sha:
            raise FrankyError(
                "no base commit was recorded for this run - cannot replay deterministically",
                code=EXIT_USAGE,
                kind="base_sha_unavailable",
                hint="only runs recorded after replay support was added carry a base commit",
            )
        # Shape-validate BEFORE it reaches the prompt (or commit_exists' None-on-uncertain path):
        # a corrupt/hand-edited record could carry a junk base_sha that is truthy but not a real
        # sha; refuse it rather than interpolate it into the replay prompt's checkout instruction.
        if not baseref.is_valid_sha(base_sha):
            raise FrankyError(
                "recorded base commit is malformed - cannot replay",
                code=EXIT_USAGE,
                kind="base_sha_unavailable",
            )

        # Pre-flight, host-side: fail fast (no container spent) if the commit is PROVABLY gone.
        # None (uncertain - a flaky check or a genuinely ambiguous response) proceeds anyway; the
        # in-container checkout fails cleanly if the commit truly no longer exists.
        exists = baseref.commit_exists(repo, base_sha, os.environ)
        if exists is False:
            raise FrankyError(
                f"base commit {base_sha} no longer exists on {repo} (force-pushed or "
                "garbage-collected) - cannot replay",
                code=EXIT_USAGE,
                kind="base_commit_gone",
            )

        spec = TaskSpec(repo=repo, text=text, source=source)
        # Prefer the ORIGINAL run's actual branch (if recorded) so an --open-pr replay targets
        # the same head the idempotency check already knows about; fall back to a freshly
        # predicted slug for very old records with no branch saved (iterate/diagnose have none,
        # but those are already rejected above).
        branch = record.get("branch") or f"franky/{task_slug(spec)}"

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)

        # --open-pr replay reuses build's idempotency guard: retrying a replay must not open a
        # second PR for the same branch.
        if open_pr:
            existing = find_open_pr(spec.repo, branch, os.environ)
            if existing:
                result = build_result(
                    status="already_open",
                    pr_url=existing,
                    reason="a Franky PR is already open for this task",
                    exit_code=EXIT_SUCCESS,
                    usage=Usage(),
                    duration=0.0,
                    log_path="",
                    engine=cfg.engine.name,
                    repo=spec.repo,
                    branch=branch,
                    replay_of=job_id,
                )
                _emit_result(
                    result, as_json, secrets, pr_url=existing, status="already_open", quiet=quiet
                )
                ctx.exit(EXIT_SUCCESS)

        new_id = jobs.new_job_id()
        _record_run_start(
            new_id,
            command="replay",
            cfg=cfg,
            repo=spec.repo,
            summary=f"replay of {job_id}",
            branch=branch,
            source=source,
            task_full=text,
            base_sha=base_sha,
            replay_of=job_id,
            env=os.environ,
        )
        if not quiet:
            click.echo(f"franky: job {new_id} started (replay of {job_id})", err=True)

        # Populated (best-effort) by run_in_container just before container teardown (issue #69).
        # Replay is the debugging command, so capturing runtime signals matters MORE here, not
        # less - same three-line sink pattern as _build_once/iterate.
        diagnostics: dict = {}
        # A timed-out replay is itself resumable (issue #71) - same snapshot-on-timeout sink.
        snapshot_sink: dict = {"dest": str(snapshot.snapshot_path_for(new_id, os.environ))}
        # No profile bundle for replay - keep it simple; the original run's own profile (if any)
        # already shaped how it worked, and replay is a debugging tool, not a full build re-run.
        code, output, duration = _run_pass(
            cfg,
            build_replay_prompt(spec, branch=branch, base_sha=base_sha, open_pr=open_pr),
            franky_img,
            proxy_img,
            profile_bundle=None,
            progress=progress,
            timeout=max_duration,
            run_id=new_id,
            diagnostics_sink=diagnostics,
            snapshot_sink=snapshot_sink,
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ, run_id=new_id, env=os.environ)

        pr_url = None
        if code == CONTAINER_TIMEOUT_CODE:
            status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
        elif code != 0:
            status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
        elif open_pr:
            pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
            if pr_url:
                status, reason, exit_code = "pr_opened", "PR opened", EXIT_SUCCESS
            else:
                status, reason, exit_code = "no_pr", "agent produced no PR URL", EXIT_AGENT
        else:
            # Reproduce-only clean exit: neither a build success nor failure (see jobs.py's
            # _SUCCESS_STATUSES/_FAILURE_STATUSES comment) - the pass ran and reported, that's it.
            status = "replay_complete"
            reason = "reproduce-only replay pass complete"
            exit_code = EXIT_SUCCESS

        _record_run_end(
            new_id,
            status=status,
            pr_url=pr_url,
            usage=usage,
            duration=duration,
            exit_code=exit_code,
            log_path=log_path,
            diagnostics=diagnostics,
            snapshot_path=snapshot_sink.get("snapshot_path"),
            env=os.environ,
        )

        result = build_result(
            status=status,
            pr_url=pr_url,
            reason=reason,
            exit_code=exit_code,
            usage=usage,
            duration=duration,
            log_path=str(log_path),
            engine=cfg.engine.name,
            repo=spec.repo,
            branch=branch,
            job_id=new_id,
            replay_of=job_id,
        )
        # Non-JSON output mirrors build's discipline: a real PR URL on stdout, a labeled
        # completion line (stderr) ONLY for a clean reproduce-only pass, a "no PR URL" note for
        # an --open-pr pass that produced none, and NOTHING extra for timeout/agent_error (the
        # exit code + the redacted log already carry the failure - never print "complete" for it).
        if as_json:
            click.echo(redact(json.dumps(result), secrets))
        elif status in ("pr_opened", "already_open") and pr_url:
            click.echo(pr_url)
        elif status == "replay_complete" and not quiet:
            click.echo(
                f"franky: replay of {job_id} complete (job {new_id}) - see `franky job logs {new_id}`",
                err=True,
            )
        elif status == "no_pr":
            click.echo(
                f"franky: no PR URL found in agent output - see the redacted log ({log_path})",
                err=True,
            )
        ctx.exit(exit_code)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


@job_group.command("resume")
@click.argument("job_id")
@click.option(
    "--engine",
    "engine",
    default=None,
    type=click.Choice(sorted(ENGINES)),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the resume result as a single JSON object."
)
@click.option(
    "-v", "--verbose", "verbose", is_flag=True, default=False, help="Stream raw agent output."
)
@click.option("-q", "--quiet", "quiet", is_flag=True, help="Suppress progress (implied by --json).")
@click.option(
    "--max-duration",
    "max_duration",
    type=click.IntRange(min=1),
    default=None,
    help="Abort the resume pass after N seconds (default 1800).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help="Skip the idempotency pre-check and resume even if a Franky PR is already open.",
)
@click.pass_context
def job_resume(
    ctx: click.Context,
    job_id: str,
    engine: str | None,
    as_json: bool,
    verbose: bool,
    quiet: bool,
    max_duration: int | None,
    force: bool,
) -> None:
    """Re-enter a hung/timed-out/killed run WITH its saved /work workspace so it CONTINUES (#71).

    A timed-out or killed run leaves behind a scrubbed, fail-closed-verified, host-local snapshot
    of its container's `/work` (the repo clone + branch state). `resume` launches a fresh
    (still fully hardened, egress-controlled) container, restores that workspace into it, and runs
    a fresh engine that picks the task up from where it left off - instead of restarting from a
    clean clone.

    Only build/replay runs are resumable (a fresh engine continues from the restored `/work`
    branch state); iterate/diagnose runs have no such workspace to carry forward.

    V1 LIMITATION: resume restores the FILESYSTEM, NOT the agent's LLM/session state. A fresh
    engine re-orients from the branch state on disk and continues; it does not remember the prior
    run's reasoning. Only timeout/killed runs produce a snapshot, so only those are resumable.
    (Git push still works after resume: the tokenized remote URL is stripped from the snapshot's
    `.git/config`, but the container re-authenticates from GH_TOKEN, so a bare remote is fine.)

    `--force` skips the idempotency pre-check (bypass, exactly like `build`): resume PUSHES to the
    branch and opens a PR, so it must not open a second one when a Franky PR is already open -
    hence the guard, and hence the bypass flag. (`replay` has no `--force` because its default
    reproduce-only mode pushes nothing; only its `--open-pr` mode gets build's idempotency guard.)

    --json emits the SAME envelope as `build`, with a `resumed_from` field naming the original
    job id; exit codes follow the documented taxonomy (0 ok, 2 usage, 3 config, 4 task, 7 agent,
    9 timeout).
    """
    quiet = quiet or as_json
    secrets = cfg_secrets_safe()
    try:
        record = _require_record(job_id)

        # Scope gate FIRST so the clearest error wins: a non-resumable command must say
        # not_resumable, not no_snapshot (an iterate/diagnose run never had resumable inputs).
        if record.get("command") not in ("build", "replay", "resume"):
            raise FrankyError(
                f"cannot resume a {record.get('command')!r} run - resume is only for "
                "build/replay runs",
                code=EXIT_USAGE,
                kind="not_resumable",
                hint="see `franky jobs`",
            )

        # A snapshot only exists for a hung/timed-out/killed run - guard on it up front so a run
        # that was never captured fails cleanly instead of launching an empty resume.
        snap = snapshot.snapshot_path_for(job_id, os.environ)
        if not snap.exists():
            raise FrankyError(
                "no workspace snapshot for this run - resume is only available for a "
                "hung/timed-out/killed run whose /work was captured",
                code=EXIT_USAGE,
                kind="no_snapshot",
                hint="see `franky jobs`; only timeout/killed runs produce a snapshot",
            )
        # A corrupt/truncated snapshot tar cannot be restored - refuse before spending a container.
        try:
            with tarfile.open(snap, "r:gz"):
                pass
        except Exception as exc:
            raise FrankyError(
                f"workspace snapshot for {job_id} is corrupt or unreadable - cannot resume",
                code=EXIT_USAGE,
                kind="snapshot_corrupt",
            ) from exc

        # Reconstruct the original TaskSpec from the saved inputs (identical to job_replay).
        source = record.get("source")
        text = record.get("task_full")
        repo = record.get("repo")
        if not source or not text or not repo:
            raise FrankyError(
                "this run predates replay/resume support (no saved task inputs) - cannot resume",
                code=EXIT_USAGE,
                kind="replay_inputs_missing",
            )

        # Same config-file merge as build/replay (a config-file token/engine must work here too).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc
        cfg = load_config(engine, os.environ)
        secrets = cfg.secret_values()

        # Re-validate the allowlist: the repo may have been dropped since the original run - a
        # saved record must never bypass the fail-closed gate a fresh build goes through.
        if not repo_allowed(repo, cfg.allowed_repos):
            raise TaskRejected(f"repo {repo!r} is no longer in the allowlist - refusing to resume")

        spec = TaskSpec(repo=repo, text=text, source=source)
        # Prefer the original run's actual branch so the continued work lands on the same head the
        # idempotency check knows about; fall back to a freshly predicted slug for old records.
        branch = record.get("branch") or f"franky/{task_slug(spec)}"

        franky_img, proxy_img = _ensure_images(os.environ, cfg.engine.name)
        verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
        progress = None if (quiet and not verbose) else _make_progress(cfg.engine, verbose)

        # Idempotency guard (like build): resuming must not open a SECOND PR for the same branch.
        if not force:
            existing = find_open_pr(spec.repo, branch, os.environ)
            if existing:
                result = build_result(
                    status="already_open",
                    pr_url=existing,
                    reason="a Franky PR is already open for this task",
                    exit_code=EXIT_SUCCESS,
                    usage=Usage(),
                    duration=0.0,
                    log_path="",
                    engine=cfg.engine.name,
                    repo=spec.repo,
                    branch=branch,
                    resumed_from=job_id,
                )
                _emit_result(
                    result, as_json, secrets, pr_url=existing, status="already_open", quiet=quiet
                )
                ctx.exit(EXIT_SUCCESS)

        new_id = jobs.new_job_id()
        _record_run_start(
            new_id,
            command="resume",
            cfg=cfg,
            repo=spec.repo,
            summary=f"resume of {job_id}",
            branch=branch,
            source=source,
            task_full=text,
            base_sha=record.get("base_sha"),
            resumed_from=job_id,
            env=os.environ,
        )
        if not quiet:
            click.echo(f"franky: job {new_id} started (resume of {job_id})", err=True)

        # Populated (best-effort) by run_in_container just before container teardown (issue #69).
        diagnostics: dict = {}
        # A timed-out resume is itself resumable (issue #71) - same snapshot-on-timeout sink.
        snapshot_sink: dict = {"dest": str(snapshot.snapshot_path_for(new_id, os.environ))}
        # No profile bundle for resume (mirrors job_replay): the restored /work already carries
        # the prior run's state, and resume is a continuation tool, not a full build re-run.
        code, output, duration = _run_pass(
            cfg,
            build_resume_prompt(spec, branch=branch),
            franky_img,
            proxy_img,
            None,
            progress=progress,
            timeout=max_duration,
            run_id=new_id,
            diagnostics_sink=diagnostics,
            snapshot_sink=snapshot_sink,
            resume_workspace=str(snap),
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ, run_id=new_id, env=os.environ)

        # Classify (reusing build statuses). Timeout first (124 is nonzero); then nonzero as
        # agent_error - note the entrypoint's exit 75 on a FAILED restore surfaces here as
        # agent_error, which is correct; then parse the PR URL.
        pr_url = None
        if code == CONTAINER_TIMEOUT_CODE:
            status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
        elif code != 0:
            status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
        else:
            pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
            if pr_url:
                status, reason, exit_code = "pr_opened", "PR opened", EXIT_SUCCESS
            else:
                status, reason, exit_code = "no_pr", "agent produced no PR URL", EXIT_AGENT

        _record_run_end(
            new_id,
            status=status,
            pr_url=pr_url,
            usage=usage,
            duration=duration,
            exit_code=exit_code,
            log_path=log_path,
            diagnostics=diagnostics,
            snapshot_path=snapshot_sink.get("snapshot_path"),
            env=os.environ,
        )

        result = build_result(
            status=status,
            pr_url=pr_url,
            reason=reason,
            exit_code=exit_code,
            usage=usage,
            duration=duration,
            log_path=str(log_path),
            engine=cfg.engine.name,
            repo=spec.repo,
            branch=branch,
            job_id=new_id,
            resumed_from=job_id,
        )
        # Non-JSON output mirrors build's discipline: a real PR URL on stdout, a "no PR URL" note
        # for a clean pass that produced none, and NOTHING extra for timeout/agent_error.
        if as_json:
            click.echo(redact(json.dumps(result), secrets))
        elif status in ("pr_opened", "already_open") and pr_url:
            click.echo(pr_url)
        elif status == "no_pr":
            click.echo(
                f"franky: no PR URL found in agent output - see the redacted log ({log_path})",
                err=True,
            )
        ctx.exit(exit_code)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


# Cap on a single stored steer-note message. A correction is short operator prose; truncating
# keeps one runaway paste from bloating the record file.
_STEER_NOTE_MSG_MAX = 500


def _append_steer_note(job_id: str, record: dict, safe_msg: str, *, delivered: bool) -> None:
    """Best-effort audit-trail append for `job attach` (issue #72).

    NOT concurrency-safe by design: the notes list is built from the STALE in-memory `record`
    snapshot job_attach captured before delivery, not re-read at append time, so two near-
    concurrent `job attach` calls on the same run can lose a note. That is an accepted trade-off
    for a single-operator CLI whose steer_notes are an informational audit trail, not a
    transactional log. Truncates each message to `_STEER_NOTE_MSG_MAX` and caps the list to the
    last `_STEER_NOTES_MAX` entries so the record file stays bounded. A registry hiccup here must
    never turn a delivered correction into a reported failure - wrap and swallow.
    """
    try:
        notes = list(record.get("steer_notes") or [])
        notes.append({"message": safe_msg[:_STEER_NOTE_MSG_MAX], "delivered": delivered})
        notes = notes[-_STEER_NOTES_MAX:]
        jobs.update_record(job_id, {"steer_notes": notes}, os.environ)
    except Exception:
        pass


@job_group.command("attach")
@click.argument("job_id")
@click.option(
    "-m",
    "--message",
    "message",
    default=None,
    help="The correction to inject (one-shot, unattended-safe). Omit for an interactive "
    "one-line prompt (TTY only).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON ack object.")
@click.pass_context
def job_attach(ctx: click.Context, job_id: str, message: str | None, as_json: bool) -> None:
    """Inject a mid-run correction into a running Franky job.

    Delivers a one-shot correction to a job that is still running, so you can redirect an agent
    that has gone off course without killing and restarting it. This is a best-effort,
    PROMPT-LEVEL channel: delivery into the container is guaranteed, but whether the agent
    actually incorporates the correction depends on it re-reading the message at its next major
    step - it may not react instantly. Only build/replay/resume/iterate runs can be steered.

    `-m` is the unattended-safe path (never hangs). Omit it for a single interactive prompt -
    but ONLY when stdin is a TTY; in a non-TTY it fails fast (exit 2) rather than hang.

    NOTE: never pass a secret via `-m` - chat history, shell history, and process argv are all
    unsafe places for a credential. The message IS redacted for any Franky-known secret value
    before it is delivered or recorded, but that is a safety net, not a reason to rely on it.

    Live output streaming (watching the run react in real time) is NOT part of this v1 - only
    one-shot injection is supported; see `franky job status`/`job logs` to check in afterward.
    """
    try:
        record = _require_record(job_id)

        # Steerability gate FIRST so the clearest error wins: only build-shaped runs carry the
        # steer convention in their prompt (see _STEERABLE_COMMANDS), so a diagnose/plan run never
        # polls the mailbox and attaching to it would report a misleading "delivered" success.
        command = record.get("command")
        if command not in _STEERABLE_COMMANDS:
            raise FrankyError(
                f"cannot steer a {command!r} run - only build/replay/resume/iterate runs poll "
                "for corrections",
                code=EXIT_USAGE,
                kind="not_steerable",
                hint="see `franky jobs`",
            )

        # Resolve the engine from the RECORD, not a fresh load_config - attach invokes no engine
        # and needs no creds, so it must work even if the operator's current env lacks them.
        engine_name = record.get("engine")
        engine_cls = ENGINES.get(engine_name)
        if engine_cls is None:
            raise FrankyError(
                f"unknown engine {engine_name!r} on this run - cannot steer",
                code=EXIT_USAGE,
                kind="unknown_engine",
                hint="see `franky jobs`",
            )
        engine = engine_cls()
        if not engine.supports_steering:
            raise FrankyError(
                f"engine {engine_name!r} does not support live steering",
                code=EXIT_USAGE,
                kind="steering_unsupported",
            )

        if not container_running(record.get("container", "")):
            raise FrankyError(
                f"run {job_id} is not running - nothing to steer",
                code=EXIT_USAGE,
                kind="run_not_alive",
                hint="see `franky job status`",
            )

        if message is None:
            if _stdin_is_interactive():
                message = click.prompt("correction", err=True)
            else:
                raise FrankyError(
                    'attach needs a correction - pass -m "..." (stdin is not a TTY for an '
                    "interactive prompt)",
                    code=EXIT_USAGE,
                    kind="interactive_input_required",
                    hint="pass -m",
                )
        if not message.strip():
            raise FrankyError(
                "the correction is empty - nothing to inject",
                code=EXIT_USAGE,
                kind="empty_message",
                hint="pass a non-empty -m",
            )

        # Merge the user config file into env (setdefault, so process env still wins) BEFORE
        # building the secret list: an operator may keep creds only in ~/.franky/config, not env,
        # and a mistakenly-pasted secret must be redacted regardless of where the cred lives.
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc

        # Defense-in-depth: an operator could paste a secret into the correction by mistake. This
        # scrubs any known Franky secret VALUE, but is a safety net, not a reason to rely on it -
        # the docstring/README warn against passing one via -m in the first place.
        env_secrets = [os.environ[k] for k in SECRET_KEYS if os.environ.get(k)]
        safe_msg = redact(message, env_secrets)
        # Frame the delivered payload with a banner so the agent can tell an operator correction
        # apart from its own scratch content. The audit note stores the bare safe_msg, not this.
        framed = f"\n----- operator correction -----\n{safe_msg}\n"

        delivered = deliver_steer(record.get("container", ""), framed)
        if not delivered:
            # Best-effort audit even on a failed delivery - a post-hoc look at `job status`
            # should show the attempt, not silently drop it.
            _append_steer_note(job_id, record, safe_msg, delivered=False)
            if not container_running(record.get("container", "")):
                # The run finished in the gap between our alive check and the exec - report the
                # race as run_not_alive rather than a confusing delivery failure.
                raise FrankyError(
                    f"run {job_id} is not running - nothing to steer",
                    code=EXIT_USAGE,
                    kind="run_not_alive",
                    hint="see `franky job status`",
                )
            raise FrankyError(
                "failed to deliver the correction to the running container",
                code=EXIT_USAGE,
                kind="steer_delivery_failed",
            )

        _append_steer_note(job_id, record, safe_msg, delivered=True)

        if as_json:
            click.echo(
                redact(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "delivered": True,
                            "engine": engine_name,
                            "kind": "steered",
                        }
                    ),
                    env_secrets,
                )
            )
        else:
            click.echo(
                f"franky: correction delivered to job {job_id} - the agent will pick it up at "
                "its next step",
                err=False,
            )
        ctx.exit(EXIT_SUCCESS)
    except FrankyError as exc:
        _emit_error(exc, as_json, [])
        ctx.exit(exc.code)


# ---------------------------------------------------------------------------
# `franky auth` subgroup
# ---------------------------------------------------------------------------


def _codex_auth_image() -> str:
    image = resolve_image(dict(os.environ), engine="codex")
    ok, reason = ensure_image_available(image)
    if not ok:
        raise click.ClickException(f"Codex auth image unavailable ({reason})")
    return image


def _codex_auth_marker_enabled() -> bool:
    env_value = os.environ.get(CODEX_SUBSCRIPTION_VAR)
    if env_value is not None:
        return env_value == "1"
    path = config_file_path(dict(os.environ))
    try:
        return read_config_file(path).get(CODEX_SUBSCRIPTION_VAR) == "1"
    except ValueError as exc:
        raise click.ClickException(f"config file error: {exc}") from exc


def _clear_codex_auth_marker() -> None:
    path = config_file_path(dict(os.environ))
    try:
        unset_value(path, CODEX_SUBSCRIPTION_VAR)
    except ValueError as exc:
        raise click.ClickException(f"could not update config: {exc}") from exc


@main.group("auth")
def auth_group() -> None:
    """Manage persistent engine subscription authentication."""


def _resolve_codex_auth_volume() -> str:
    """Resolve FRANKY_CODEX_AUTH_VOLUME from the same env the run paths use (process env,
    then the config file) so `auth login/status/logout` honour a config-file override the
    same way the container mount does."""
    try:
        env = dict(os.environ)
        load_config_file(env)
        return codex_auth_volume(env)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth_group.command("login")
@click.argument("engine", type=click.Choice(["codex"]))
def auth_login(engine: str) -> None:
    """Log in once from a browserless container using a code opened elsewhere."""
    image = _codex_auth_image()
    auth_volume = _resolve_codex_auth_volume()
    _clear_codex_auth_marker()
    if not codex_auth_login(image, auth_volume=auth_volume):
        raise click.ClickException("Codex subscription login failed")
    path = config_file_path(dict(os.environ))
    try:
        set_value(path, CODEX_SUBSCRIPTION_VAR, "1")
    except ValueError as exc:
        raise click.ClickException(f"login succeeded but config update failed: {exc}") from exc
    click.echo("Codex subscription login ready.")


@auth_group.command("status")
@click.argument("engine", type=click.Choice(["codex"]))
def auth_status(engine: str) -> None:
    """Check that the persistent Codex credential is present and validly shaped."""
    if not _codex_auth_marker_enabled():
        raise click.ClickException("Codex subscription login is not enabled")
    auth_volume = _resolve_codex_auth_volume()
    if not codex_auth_status(_codex_auth_image(), auth_volume=auth_volume):
        raise click.ClickException("Codex subscription login is not ready")
    click.echo("Codex subscription login is ready.")


@auth_group.command("logout")
@click.argument("engine", type=click.Choice(["codex"]))
def auth_logout(engine: str) -> None:
    """Delete the persistent Codex credential volume and disable subscription auth."""
    auth_volume = _resolve_codex_auth_volume()
    _clear_codex_auth_marker()
    if not codex_auth_logout(auth_volume=auth_volume):
        raise click.ClickException(
            "Codex subscription disabled, but the credential volume could not be removed"
        )
    click.echo("Codex subscription login removed.")


# ---------------------------------------------------------------------------
# `franky config` subgroup
# ---------------------------------------------------------------------------
# WHY a separate group (not just more top-level commands):
#   - Groups in Click produce a clean `franky config --help` with the sub-commands
#     listed, and `franky --help` shows the group as a single line.
#   - Config subcommands MUST be usable even when the config file is malformed
#     (the user needs `config set` to fix it).  Loading the file in the group
#     callback would block that - so we do NOT call load_config_file here.
#   - Config commands write nothing to tasks/*.log (they are not build passes).


@main.group("config")
def config_group() -> None:
    """Read and write the Franky user config file (~/.franky/config)."""


@config_group.command("path")
def config_path() -> None:
    """Print the path of the config file (whether or not it exists)."""
    click.echo(config_file_path(dict(os.environ)))


@config_group.command("list")
@click.option(
    "--reveal",
    is_flag=True,
    default=False,
    help="Show secret values in plain text instead of masking them.",
)
def config_list(reveal: bool) -> None:
    """List all keys in the config file.  Secrets are masked unless --reveal is given."""
    path = config_file_path(dict(os.environ))
    if not path.exists():
        click.echo(f"config file not found: {path}", err=True)
        return
    try:
        data = read_config_file(path)
    except ValueError as exc:
        raise click.ClickException(f"config file error: {exc}") from exc
    if not data:
        click.echo("(config file is empty)")
        return
    for key in sorted(data):
        value = data[key]
        display = value if reveal else mask_value(key, value)
        click.echo(f"{key} = {display}")


@config_group.command("set")
@click.argument("key")
@click.argument("value", required=False, default=None)
def config_set(key: str, value: str | None) -> None:
    """Set a config key.

    Secrets (GH_TOKEN, API keys, etc.) must be entered at the prompt;
    passing them as a positional VALUE leaks into shell history.
    """
    if key not in SETTABLE_KEYS:
        sorted_keys = ", ".join(sorted(SETTABLE_KEYS))
        raise click.ClickException(f"unknown config key {key!r}. Valid keys: {sorted_keys}")

    if key in SECRET_KEYS:
        if value is not None:
            # Refuse early: a secret value on argv is visible in `ps` and shell history.
            raise click.ClickException(
                f"{key} is a secret; run `franky config set {key}` and enter it at "
                "the prompt - a value on the command line leaks into shell history"
            )
        # A hidden prompt would block forever with no TTY - fail fast instead (never-hang).
        if not _stdin_is_interactive():
            raise click.UsageError(
                f"{key} is a secret and needs an interactive prompt, but stdin is not a TTY. "
                f"Run `franky config set {key}` in a terminal, or edit the config file directly."
            )
        # Hidden prompt - value never echoed to the terminal.
        value = click.prompt(key, hide_input=True)
    else:
        if value is None:
            if not _stdin_is_interactive():
                raise click.UsageError(
                    f"no value given for {key} and stdin is not a TTY. "
                    f"Pass it as `franky config set {key} <value>` or run in a terminal."
                )
            value = click.prompt(key)

    path = config_file_path(dict(os.environ))
    try:
        set_value(path, key, value)
    except ValueError as exc:
        raise click.ClickException(f"could not write config: {exc}") from exc
    click.echo(f"wrote {key} to {path}", err=True)


@config_group.command("init")
def config_init() -> None:
    """Interactive wizard to create or overwrite the config file.

    Walks through engine selection, repo allowlist, GitHub token,
    engine creds, and optional JIRA settings.
    """
    # The wizard is entirely interactive prompts; with no TTY it would block forever, so
    # fail fast (never-hang) before printing anything.
    if not _stdin_is_interactive():
        raise click.UsageError(
            "config init is interactive; with no TTY use `franky config set <KEY> <VALUE>` "
            "or edit the config file directly."
        )
    path = config_file_path(dict(os.environ))
    click.echo(f"franky config init - writing to {path}")
    click.echo(
        "Press Enter to skip optional fields. "
        "Existing values are overwritten only for keys you fill in."
    )
    click.echo()

    data: dict[str, str] = {}

    # Engine selection
    engine_choice = click.prompt(
        "Engine",
        type=click.Choice(sorted(ENGINES)),
        default="pi",
        show_default=True,
    )
    data["FRANKY_ENGINE"] = engine_choice

    # Repo allowlist - required for build to work
    click.echo()
    click.echo("FRANKY_ALLOWED_REPOS: comma-separated owner/repo entries Franky may act on.")
    click.echo("  Examples: my-org/my-repo  OR  my-org/*  (whole org)  OR  * (all repos).")
    click.echo("  WARNING: '*' trusts every repo the GH_TOKEN can reach - its FULL scope.")
    repos = click.prompt(
        "FRANKY_ALLOWED_REPOS (e.g. my-org/* ; leave empty to skip)", default="", show_default=False
    ).strip()
    if repos:
        data["FRANKY_ALLOWED_REPOS"] = repos

    # GitHub token
    click.echo()
    gh = click.prompt("GH_TOKEN", hide_input=True).strip()
    if gh:
        data["GH_TOKEN"] = gh

    # Engine-specific creds
    click.echo()
    if engine_choice == "pi":
        click.echo("pi engine: set one provider key (BYOK). Common choices:")
        click.echo("  OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY")
        provider_choice = click.prompt(
            "Provider key name",
            type=click.Choice(list(PI_PROVIDER_VARS)),
            default="OPENROUTER_API_KEY",
            show_default=True,
        )
        provider_val = click.prompt(f"{provider_choice}", hide_input=True).strip()
        if provider_val:
            data[provider_choice] = provider_val
    elif engine_choice == "claude":
        val = click.prompt("CLAUDE_CODE_OAUTH_TOKEN", hide_input=True).strip()
        if val:
            data["CLAUDE_CODE_OAUTH_TOKEN"] = val
    elif engine_choice == "codex":
        click.echo("Leave CODEX_API_KEY empty to use `franky auth login codex` after init.")
        val = click.prompt("CODEX_API_KEY", hide_input=True, default="").strip()
        if val:
            data["CODEX_API_KEY"] = val
    elif engine_choice == "opencode":
        model = click.prompt("FRANKY_MODEL (provider/model)").strip()
        if model:
            data["FRANKY_MODEL"] = model
        provider = opencode_provider(model)
        if provider:
            credential = provider[0]
            val = click.prompt(credential, hide_input=True).strip()
            if val:
                data[credential] = val

    # Optional JIRA
    click.echo()
    if click.confirm("Configure JIRA (for `franky build jira <KEY>`)?", default=False):
        base = click.prompt("JIRA_BASE_URL (e.g. https://your-org.atlassian.net)").strip()
        if base:
            data["JIRA_BASE_URL"] = base
        email = click.prompt("JIRA_EMAIL").strip()
        if email:
            data["JIRA_EMAIL"] = email
        token = click.prompt("JIRA_API_TOKEN", hide_input=True).strip()
        if token:
            data["JIRA_API_TOKEN"] = token

    # Write (non-empty values only)
    data = {k: v for k, v in data.items() if v}
    try:
        # Merge with existing file so we only overwrite keys the user filled in.
        existing: dict[str, str] = {}
        if path.exists():
            try:
                existing = read_config_file(path)
            except ValueError:
                existing = {}
        existing.update(data)
        write_config_file(path, existing)
    except ValueError as exc:
        raise click.ClickException(f"could not write config: {exc}") from exc

    click.echo()
    click.echo(f"wrote config to {path}")

    # Surface the operator-profile feature during onboarding (issue #49). Opt-in, so a
    # plain `config init` is unchanged for users who do not want a profile.
    click.echo()
    if click.confirm("Set up an operator profile now?", default=False):
        _profile_init_wizard(dict(os.environ))


# ---------------------------------------------------------------------------
# `franky profile` subgroup (issue #49)
# ---------------------------------------------------------------------------
# Mirrors the `config` subgroup: thin CLI wrappers over franky/profile.py. Like
# `config`, the group callback does NOT load the profile, so the subcommands stay
# usable even when ~/.franky/profile.toml is malformed (so `profile init` can fix it).


def _profile_init_wizard(env: dict[str, str]) -> None:
    """Interactive wizard: scaffold/merge ~/.franky/profile.toml.

    Shared by `franky profile init` and the `config init` profile prompt. Offers the agentic
    setups it can actually find on this machine (one confirm, no typing), then asks for any
    extra explicit skills / instructions / knowledge globs, then merge-not-clobbers an existing
    file (union per category, existing setup dirs kept unless re-confirmed). Honors
    FRANKY_PROFILE_PATH.
    """
    path = profile_file_path(env)
    click.echo(f"franky profile init - writing to {path}", err=True)
    click.echo(
        "Enter comma-separated paths or globs per category (~ and *, ? globs allowed). "
        "Defaults are globs that may match nothing on your machine - run `franky profile "
        "check` afterwards to see what would actually inject.",
        err=True,
    )

    def _prompt_list(label: str, default: str) -> list[str]:
        raw = click.prompt(label, default=default, show_default=True)
        return [item.strip() for item in raw.split(",") if item.strip()]

    # Whole-setup injection first: it is the high-value, zero-typing path. Only kinds whose
    # default dir actually exists on this machine are offered, so the prompt names real paths
    # instead of asking the operator to recall them.
    detected = {
        kind: manifest.default_root
        for kind, manifest in setups.SETUP_MANIFESTS.items()
        if Path(manifest.default_root).expanduser().is_dir()
    }
    chosen_setups: dict[str, str] = {}
    if detected:
        listed = ", ".join(f"{kind} ({root})" for kind, root in detected.items())
        click.echo(f"Detected agentic setups: {listed}", err=True)
        if click.confirm(
            "Inject these setups (instructions, skills, commands, agent definitions)?",
            default=True,
            err=True,
        ):
            chosen_setups = dict(detected)

    new_table: dict[str, list[str]] = {
        "skills": _prompt_list("Extra skills (beyond the setups above)", ""),
        "instructions": _prompt_list("Extra instructions", ""),
        "knowledge": _prompt_list("Knowledge", ""),
    }

    # Merge with the existing file so we never clobber entries the user already curated.
    # A malformed existing file is treated as empty so the wizard can repair it.
    existing: dict[str, list[str]] = {}
    existing_setups: dict[str, str] = {}
    if path.exists():
        try:
            existing = read_profile_raw(path)
        except ValueError:
            existing = {}
        try:
            existing_setups = read_setups_raw(path)
        except ValueError:
            existing_setups = {}
    merged_setups = {**existing_setups, **chosen_setups}

    merged: dict[str, list[str]] = {}
    for category in PROFILE_CATEGORIES:
        seen: list[str] = []
        for entry in existing.get(category, []) + new_table.get(category, []):
            if entry not in seen:
                seen.append(entry)
        if seen:
            merged[category] = seen

    if not merged and not merged_setups:
        click.echo("franky: no setups or paths entered - nothing written.", err=True)
        return

    try:
        write_profile(path, merged, merged_setups)
    except ValueError as exc:
        raise click.ClickException(f"could not write profile: {exc}") from exc
    click.echo(f"wrote profile to {path}", err=True)


@main.group("profile")
def profile_group() -> None:
    """Set up and inspect the operator profile (~/.franky/profile.toml)."""


@profile_group.command("path")
def profile_path_cmd() -> None:
    """Print the resolved profile path (whether or not it exists)."""
    click.echo(profile_file_path(dict(os.environ)))


@profile_group.command("show")
def profile_show() -> None:
    """Print the profile.toml and the glob-expanded file list it would inject.

    Lenient introspection: a load error (bad TOML / a listed file that does not exist yet)
    is reported as a warning, not a hard failure - use `profile check` for the strict gate.
    """
    path = profile_file_path(dict(os.environ))
    if not path.exists():
        click.echo(f"profile not found: {path}", err=True)
        return

    # The raw TOML is the operator's own declared path list (never file contents), so
    # echoing it cannot leak a fetched secret.
    click.echo(f"# {path}")
    click.echo(path.read_text(encoding="utf-8").rstrip("\n"))

    try:
        spec = load_profile(path)
    except ValueError as exc:
        click.echo(f"\nfranky: could not expand profile: {exc}", err=True)
        return

    click.echo("\nexpanded files:", err=True)
    files = spec.all_files()
    if not files:
        click.echo("  (none)", err=True)
        return
    for category in PROFILE_FILE_CATEGORIES:
        for fp in getattr(spec, category):
            click.echo(f"  [{category}] {fp} ({fp.stat().st_size} bytes)", err=True)
    # Swept setups are summarized, not enumerated: a real sweep is ~100 files, and the point of
    # declaring a directory is not having to read the file list. `profile check` shows the same
    # summary plus what was skipped.
    for kind, scan in spec.setup_scans.items():
        click.echo(
            f"  [setups] {kind}: {scan.root} -> {len(scan.files)} file(s), "
            f"{scan.total_bytes // 1024} KB",
            err=True,
        )
    pr_spec = spec.pr_spec()
    if pr_spec is not None:
        click.echo(f"  [setups] PR-description spec: {pr_spec}", err=True)


@profile_group.command("check")
def profile_check() -> None:
    """Dry-run the build's profile gate: expand globs + secret-scan, report what would inject.

    Uses the SAME load_profile + scan_for_secrets path as the real build, so a profile that
    passes here cannot fail the build's fail-closed secret gate. Exit code is a contract:
    a credential hit (or unreadable file) exits nonzero and names the offending file; a clean
    profile exits 0 with the inject manifest. Absent profile -> exit 0 (build treats it as no bundle).
    """
    path = profile_file_path(dict(os.environ))
    if not path.exists():
        click.echo(f"no profile configured at {path} - nothing would be injected", err=True)
        return

    try:
        spec = load_profile(path)
        resolve_mcp_credentials(spec, dict(os.environ))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Setup summary FIRST: a swept setup can be ~100 files, so per-file lines below would bury
    # the thing the operator actually wants to see (what each declaration expanded to, which PR
    # spec was found, and what was deliberately left out).
    setup_files = set(spec.setup_files)
    for kind, scan in spec.setup_scans.items():
        click.echo(
            f"  setup  {kind}: {scan.root} -> {len(scan.files)} file(s), "
            f"{scan.total_bytes // 1024} KB",
            err=True,
        )
        if scan.skipped_binary:
            click.echo(
                f"           skipped {len(scan.skipped_binary)} non-text file(s) "
                "(unscannable, never injected)",
                err=True,
            )
        for hint in scan.mcp_hints:
            click.echo(
                f"           {hint} declares MCP servers - NOT injected. Enabling MCP stays "
                "explicit (mcp_configs + mcp_credentials + mcp_domains): each server adds an "
                "egress host and forwards a credential.",
                err=True,
            )
    pr_spec = spec.pr_spec()
    if spec.setup_scans:
        click.echo(
            f"  pr spec: {pr_spec if pr_spec else 'none found - Franky keeps its own PR shape'}",
            err=True,
        )

    results = scan_profile_files(spec)
    if not results:
        # load_profile succeeded but every glob matched nothing (a valid but usually
        # unintended state). Echo the declared patterns so the operator knows what to fix.
        declared = read_profile_raw(path)
        click.echo("profile has no files to inject - declared patterns matched nothing:", err=True)
        for category in PROFILE_FILE_CATEGORIES:
            for pattern in declared.get(category, []):
                click.echo(f"  [{category}] {pattern}", err=True)
        return

    total = 0
    problems: list[str] = []
    for r in results:
        if r.error is not None:
            click.echo(f"  ERROR  {r.path}: {r.error}", err=True)
            problems.append(f"{r.path} (unreadable)")
            continue
        total += r.size
        if r.findings:
            click.echo(f"  SECRET {r.path}: {', '.join(r.findings)}", err=True)
            problems.append(f"{r.path} ({r.findings[0]})")
        elif r.path not in setup_files:
            # Explicitly listed files are named one by one; clean setup-swept files are already
            # accounted for in the per-setup summary above. Problems are ALWAYS named, whichever
            # they came from.
            click.echo(f"  ok     {r.path} ({r.size} bytes)", err=True)

    if problems:
        raise click.ClickException(
            f"profile check failed - {len(problems)} file(s) cannot be injected: "
            + "; ".join(problems)
        )
    click.echo(f"OK: {len(results)} file(s), {total} bytes would be injected", err=True)


@profile_group.command("init")
def profile_init() -> None:
    """Interactive wizard to create or extend ~/.franky/profile.toml (merge-not-clobber)."""
    _profile_init_wizard(dict(os.environ))


if __name__ == "__main__":
    main()
