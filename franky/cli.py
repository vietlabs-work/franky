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
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path

import click

from . import franky_version
from ._install import detect_install
from .config import GH_TOKEN_VAR, Config, load_config, redact
from .decompose import build_plan_result, parse_decomposition
from .diagnosis import build_diagnosis_result, parse_diagnosis
from .economics import Usage, format_economics, parse_usage
from .container import (
    CONTAINER_TIMEOUT_CODE,
    FRANKY_IMAGE_VAR,
    FRANKY_PROXY_IMAGE_VAR,
    ensure_image_available,
    resolve_image,
    run_in_container,
)
from . import jobs
from .container import container_running, reap_run, run_names
from .engine import ENGINES, PI_PROVIDER_VARS, resolve_engine
from .github import run_gh
from .idempotency import find_open_pr
from .jira import JIRA_API_TOKEN_VAR, JIRA_EMAIL_VAR, fetch_jira_issue
from .profile import (
    PROFILE_CATEGORIES,
    build_bundle,
    load_profile,
    profile_file_path,
    profile_path,
    read_profile_raw,
    scan_profile_files,
    write_profile,
)
from .prompt import (
    build_decompose_prompt,
    build_diagnose_prompt,
    build_iterate_prompt,
    build_plan_prompt,
    build_prompt,
    task_slug,
)
from .result import (
    EXIT_AGENT,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    AuthError,
    ConfigError,
    DockerError,
    FrankyError,
    NetworkError,
    build_error,
    build_result,
)
from .schema import build_schema
from .task import PROSE_MAX_CHARS, TaskSpec, parse_pr_task, parse_task
from .update_check import force_update, maybe_auto_update
from .userconfig import (
    SECRET_KEYS,
    SETTABLE_KEYS,
    config_file_path,
    load_config_file,
    mask_value,
    read_config_file,
    set_value,
    write_config_file,
)

TASKS_DIR = Path("tasks")
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
            "franky: no PR URL found in agent output - see the redacted log in tasks/", err=True
        )
    elif status == "iterate_complete" and not quiet:
        # iterate opens no new PR; report a labeled completion line (never a bare success URL)
        # on stderr so stdout stays pure. Suppressed under --quiet.
        click.echo(
            f"franky: iterate pass complete for {pr_url} - review the PR for the new commits",
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
    help="Path to a profile.toml to inject skills/instructions/knowledge into the container. "
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

        franky_img, proxy_img = _ensure_images(os.environ)

        # Build the profile bundle (optional). Auto-discovers ~/.franky/profile.toml unless
        # overridden by --profile or FRANKY_PROFILE_PATH. Fails closed on detected credentials.
        bundle = _load_profile_bundle(profile_path_opt, os.environ, secrets)

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
            _write_log(output, secrets)
            if not quiet:
                click.echo("franky: --- plan (read-only, nothing written yet) ---", err=True)
                click.echo(output, err=True)
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
                    f"planning pass exited non-zero ({code}) - see the redacted log in tasks/",
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
                progress=progress,
                timeout=max_duration,
                secrets=secrets,
                as_json=as_json,
                quiet=quiet,
                prior_failures=prior_failures,
                env=os.environ,
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

        franky_img, proxy_img = _ensure_images(os.environ)

        bundle = _load_profile_bundle(None, os.environ, secrets)
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
        code, output, duration = _run_pass(
            cfg,
            build_iterate_prompt(spec),
            franky_img,
            proxy_img,
            bundle,
            progress=progress,
            timeout=max_duration,
            run_id=job_id,
        )

        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ)

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
    help="Path to a profile.toml to inject skills/instructions/knowledge into the container. "
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
    try:
        # stdin task input mirrors `build -` (never-hang on a TTY).
        task_input_str = _read_task_input(task_input)

        if not quiet:
            maybe_auto_update()

        # Shared config + task-parse + JIRA-fetch preamble (branch is unused - plan builds
        # nothing).
        cfg, spec, _branch, secrets = _resolve_task_spec(task_input_str, repo, engine, os.environ)

        franky_img, proxy_img = _ensure_images(os.environ)
        bundle = _load_profile_bundle(profile_path_opt, os.environ, secrets)
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
        _write_log(output, secrets, footer=econ)

        # Timeout first (124 is nonzero) -> dedicated timeout contract, before generic agent.
        if code == CONTAINER_TIMEOUT_CODE:
            raise FrankyError(
                "plan pass exceeded max-duration",
                code=EXIT_TIMEOUT,
                kind="timeout",
            )
        if code != 0:
            raise FrankyError(
                f"plan pass exited {code} - see the redacted log in tasks/",
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
                "agent produced no parseable plan - see the redacted log in tasks/",
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


def _ensure_images(env: Mapping[str, str]) -> tuple[str, str]:
    """Resolve + ensure the franky and franky-proxy images are available locally.

    Returns (franky_image, proxy_image). Raises DockerError (exit 6, a clean message, no
    traceback) for the operator-facing failure modes: docker absent, auth needed, or pull
    failed - typed so the outer FrankyError handler emits the right code + JSON error too.
    Shared by `build` and `iterate` - the only difference between them is the prompt.
    """
    franky_img = resolve_image(env, FRANKY_IMAGE_VAR, "franky")
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
    profile_bundle: str | None = None,
    progress=None,
    timeout: int | None = None,
    run_id: str | None = None,
) -> tuple[int, str, float]:
    """Run one container pass for `prompt` and return (exit_code, output, duration_secs).

    Duration is measured with time.monotonic() around run_in_container only. Logging and the
    economics summary are the CALLER's responsibility (the planning pass logs without an
    economics footer; the build and iterate passes attach one). Shared by build, iterate, and plan.

    `timeout` is the --max-duration budget in seconds; None preserves run_in_container's own
    default (FALLBACK_TIMEOUT_SECS), so the kwarg is only forwarded when explicitly set.
    `run_id` pins the container/net/proxy names to a job handle so the run registry can record
    them and `job status`/`kill` can target the SAME containers (issue #63); None -> a fresh id.
    """
    inner_argv = cfg.engine.inner_argv(prompt, model=None)
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
        **extra,
    )
    duration = time.monotonic() - t0
    return code, output, duration


def _load_profile_bundle(
    profile_path_opt: str | None,
    env: dict,
    secrets: list[str],
) -> str | None:
    """Load, scan, and pack the operator profile bundle; return None if no profile is found.

    Resolution order: --profile flag path > FRANKY_PROFILE_PATH env var > auto-discovered
    ~/.franky/profile.toml.  Fails closed (ConfigError, exit 3) on a detected credential or a
    malformed profile, so the failure stays inside the machine contract (right exit code + a
    JSON error object under --json) instead of a bare exit-1 ClickException.
    An absent or empty profile is not an error; the caller treats None as "no bundle".
    """
    from pathlib import Path

    if profile_path_opt:
        ppath = Path(profile_path_opt)
    else:
        ppath = profile_path(env)

    if ppath is None:
        return None

    try:
        spec = load_profile(ppath)
    except ValueError as exc:
        raise ConfigError(redact(str(exc), secrets)) from exc

    if not spec.all_files():
        return None

    try:
        return build_bundle(spec)
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
    so this is a defensive backstop.
    """
    return [v for v in (os.environ.get(JIRA_API_TOKEN_VAR), os.environ.get(JIRA_EMAIL_VAR)) if v]


def _record_run_start(job_id, *, command, cfg, repo, summary, branch=None, env=None) -> None:
    """Write a status=running registry record before the container pass (issues #63, #64).

    Best-effort: any failure is swallowed so a registry hiccup can never block a run (mirrors
    economics' "never raises into a run"). `summary` is REDACTED first, THEN truncated - redacting
    the full string first so a secret can't be sliced in half and dodge the pattern. Takes `repo`
    + `summary` directly (not a TaskSpec) so build/iterate AND the specless `diagnose` pass can
    all register through this one helper.
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
        )
        jobs.write_record(record, env)
        jobs.prune(env)  # only on the write path; never a side effect of a read
    except Exception:
        pass


def _record_run_end(
    job_id, *, status, pr_url, usage, duration, exit_code, log_path, env=None
) -> None:
    """Update the run record once the pass finishes. Best-effort - never raises into a build."""
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
) -> dict:
    """Run ONE build attempt end to end and return its outcome (issue #64 #5).

    Registers a fresh run (issue #63), runs the container pass with the branch pinned and any
    `prior_failures` learning-signal injected, writes the redacted log, classifies the result,
    and finalizes the record. Returns a dict the `build` retry loop consumes:
    {job_id, status, reason, exit_code, pr_url, output, log_path, usage, duration}. The
    classification (timeout > nonzero > pr_opened > no_pr) is identical to the pre-retry code.
    """
    job_id = jobs.new_job_id()
    _record_run_start(
        job_id, command="build", cfg=cfg, repo=spec.repo, summary=spec.text, branch=branch, env=env
    )
    if not quiet:
        click.echo(f"franky: job {job_id} started", err=True)
    code, output, duration = _run_pass(
        cfg,
        build_prompt(spec, branch=branch, prior_failures=prior_failures),
        franky_img,
        proxy_img,
        bundle,
        progress=progress,
        timeout=timeout,
        run_id=job_id,
    )

    # Parse usage ONCE; both the prose econ line and the JSON economics block come from it.
    usage = _parse_usage_safe(output)
    econ = _economics_line(usage, duration, secrets)
    if not as_json:
        click.echo(econ, err=True)
    log_path = _write_log(output, secrets, footer=econ)

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
    log_path = _write_log(output, secrets, footer=econ)

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


def _write_log(output: str, secrets: list[str], footer: str | None = None) -> Path:
    """Write the REDACTED agent output to tasks/<timestamp>.log and return its Path.

    When `footer` is given, it is appended after the transcript (also redacted) separated
    by a newline so the economics summary lands in the same timestamped file. The returned
    Path is surfaced as `log_path` in the JSON result.
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    body = redact(output, secrets) + "\n"
    if footer is not None:
        body += redact(footer, secrets) + "\n"
    path = TASKS_DIR / f"{stamp}.log"
    path.write_text(body, encoding="utf-8")
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

    image = resolve_image(env)

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
    except FrankyError as exc:
        _emit_error(exc, as_json, [])
        ctx.exit(exc.code)


@job_group.command("logs")
@click.argument("job_id")
@click.pass_context
def job_logs(ctx: click.Context, job_id: str) -> None:
    """Print a run's redacted transcript (the tasks/<ts>.log written when the pass finishes).

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
        click.echo(Path(log_path).read_text(encoding="utf-8"), nl=False)
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
    """
    try:
        record = _require_record(job_id)
        reaped = reap_run(job_id)
        if record.get("status") == "running" or reaped:
            jobs.update_record(job_id, {"status": "killed", "ended_at": jobs.now_iso()}, os.environ)
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
        transcript = Path(log_path).read_text(encoding="utf-8")

        # Same config-file merge as build (a config-file token/engine must work here too).
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc
        cfg = load_config(engine, os.environ)
        secrets = cfg.secret_values()

        franky_img, proxy_img = _ensure_images(os.environ)
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
                "agent produced no parseable diagnosis - see the redacted log in tasks/",
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
        val = click.prompt(
            "CODEX_API_KEY (or press Enter to use OPENAI_API_KEY instead)",
            hide_input=True,
            default="",
        ).strip()
        if val:
            data["CODEX_API_KEY"] = val
        else:
            oai = click.prompt("OPENAI_API_KEY", hide_input=True).strip()
            if oai:
                data["OPENAI_API_KEY"] = oai

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

    Shared by `franky profile init` and the `config init` profile prompt. Prompts for
    skills / instructions / knowledge globs with sensible defaults, then merge-not-clobbers
    an existing file (union per category, order preserved). Honors FRANKY_PROFILE_PATH.
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

    new_table: dict[str, list[str]] = {
        "skills": _prompt_list("Skills", "~/.claude/skills/*.md"),
        "instructions": _prompt_list("Instructions", "~/.claude/CLAUDE.md"),
        "knowledge": _prompt_list("Knowledge", ""),
    }

    # Merge with the existing file so we never clobber entries the user already curated.
    # A malformed existing file is treated as empty so the wizard can repair it.
    existing: dict[str, list[str]] = {}
    if path.exists():
        try:
            existing = read_profile_raw(path)
        except ValueError:
            existing = {}

    merged: dict[str, list[str]] = {}
    for category in PROFILE_CATEGORIES:
        seen: list[str] = []
        for entry in existing.get(category, []) + new_table.get(category, []):
            if entry not in seen:
                seen.append(entry)
        if seen:
            merged[category] = seen

    if not merged:
        click.echo("franky: no paths entered - nothing written.", err=True)
        return

    try:
        write_profile(path, merged)
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
    for category in PROFILE_CATEGORIES:
        for fp in getattr(spec, category):
            click.echo(f"  [{category}] {fp} ({fp.stat().st_size} bytes)", err=True)


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
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    results = scan_profile_files(spec)
    if not results:
        # load_profile succeeded but every glob matched nothing (a valid but usually
        # unintended state). Echo the declared patterns so the operator knows what to fix.
        declared = read_profile_raw(path)
        click.echo("profile has no files to inject - declared patterns matched nothing:", err=True)
        for category in PROFILE_CATEGORIES:
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
        else:
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
