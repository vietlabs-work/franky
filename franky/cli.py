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
from .config import load_config, redact
from .economics import Usage, format_economics, parse_usage
from .container import (
    CONTAINER_TIMEOUT_CODE,
    FRANKY_IMAGE_VAR,
    FRANKY_PROXY_IMAGE_VAR,
    ensure_image_available,
    resolve_image,
    run_in_container,
)
from .engine import ENGINES, PI_PROVIDER_VARS, resolve_engine
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
from .prompt import build_iterate_prompt, build_plan_prompt, build_prompt, task_slug
from .result import (
    EXIT_AGENT,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    ConfigError,
    DockerError,
    FrankyError,
    NetworkError,
    build_error,
    build_result,
)
from .schema import build_schema
from .task import PROSE_MAX_CHARS, parse_pr_task, parse_task
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


def _stdin_is_interactive() -> bool:
    """True when stdin is a TTY (a human can answer a prompt).

    Wrapped in a module function so tests can monkeypatch it without touching sys.stdin.
    Every blocking prompt (plan-first confirm, `config set`/`init` wizard, `build -` stdin)
    guards on this so a non-TTY run fails fast (exit 2) instead of hanging - the never-hang
    guarantee.
    """
    return sys.stdin.isatty()


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
) -> None:
    """Build TASK_INPUT (a GitHub issue URL, a JIRA key, or a prose request) and open a PR.

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
        if task_input == ("-",):
            if _stdin_is_interactive():
                raise FrankyError(
                    "`build -` reads the task from stdin, but stdin is a TTY - pipe the task in "
                    "or pass it as an argument",
                    code=EXIT_USAGE,
                    kind="interactive_input_required",
                    hint="pipe a task into stdin or pass it as an argument",
                )
            task_input_str = sys.stdin.read().strip()
        else:
            task_input_str = " ".join(task_input)

        # Best-effort, hint-only update check (never blocks/raises; ~1s budget, cached). Skip
        # under --quiet/--json so stderr stays clean. Silenced too by FRANKY_NO_UPDATE_CHECK=1.
        if not quiet:
            maybe_auto_update()

        # Inject user config file values into os.environ via setdefault (process env wins). A
        # malformed file -> ConfigError (exit 3) so --json gets the right code + JSON error.
        try:
            load_config_file(os.environ)
        except FrankyError:
            raise
        except ValueError as exc:
            raise ConfigError(f"config file error: {exc}") from exc

        # Config + task parse + JIRA fetch are typed-error surfaces. Each already raises the
        # right FrankyError subclass (ConfigError/AuthError/TaskRejected/NetworkError); re-raise
        # those untouched and only wrap a residual plain ValueError defensively.
        try:
            cfg = load_config(engine, os.environ)
            secrets = cfg.secret_values()
            spec = parse_task(task_input_str, repo, cfg.allowed_repos)
            # Compute the predicted branch HERE, before the JIRA fetch mutates spec.text: for a
            # jira spec the slug keys on the bare KEY (stable), not the fetched body, so the
            # host-predicted branch matches what build_prompt pins below and what the
            # idempotency pre-check looks up.
            branch = f"franky/{task_slug(spec)}"
            if spec.source == "jira":
                # Fetch the JIRA issue host-side (the container has no JIRA creds or egress).
                body = fetch_jira_issue(spec.text, os.environ)
                spec = dc_replace(spec, text=body[:PROSE_MAX_CHARS].strip())
        except FrankyError:
            raise
        except ValueError as exc:
            raise NetworkError(redact(str(exc), cfg_secrets_safe())) from exc

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

        # PHASE 2 (or the only phase without --plan-first): build it and open the PR. Pin the
        # host-predicted branch so it matches the idempotency pre-check above.
        code, output, duration = _run_pass(
            cfg,
            build_prompt(spec, branch=branch),
            franky_img,
            proxy_img,
            bundle,
            progress=progress,
            timeout=max_duration,
        )

        # Parse usage ONCE; both the prose econ line and the JSON economics block come from it.
        usage = _parse_usage_safe(output)
        econ = _economics_line(usage, duration, secrets)
        # Non-json: emit the prose econ line before everything so spend is always visible.
        if not as_json:
            click.echo(econ, err=True)
        log_path = _write_log(output, secrets, footer=econ)

        # Scope PR-URL detection to the task's own repo so a hostile issue body cannot make
        # Franky report a PR URL for some other (attacker) repo.
        pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
        # Timeout is checked FIRST: a timed-out run returns the CONTAINER_TIMEOUT_CODE sentinel
        # (124), which is nonzero, so it must be distinguished before the generic agent_error
        # branch and mapped to the dedicated timeout status / EXIT_TIMEOUT.
        if code == CONTAINER_TIMEOUT_CODE:
            status, reason, exit_code = "timeout", "exceeded max-duration", EXIT_TIMEOUT
        elif code != 0:
            status, reason, exit_code = "agent_error", f"agent exited {code}", EXIT_AGENT
        elif pr_url:
            status, reason, exit_code = "pr_opened", "PR opened", EXIT_SUCCESS
        else:
            status, reason, exit_code = "no_pr", "agent produced no PR URL", EXIT_AGENT

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
        )
        _emit_result(result, as_json, secrets, pr_url=pr_url, status=status, quiet=quiet)
        ctx.exit(exit_code)
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
        code, output, duration = _run_pass(
            cfg,
            build_iterate_prompt(spec),
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
        )
        _emit_result(result, as_json, secrets, pr_url=spec.text, status=status, quiet=quiet)
        ctx.exit(exit_code)
    except FrankyError as exc:
        _emit_error(exc, as_json, secrets)
        ctx.exit(exc.code)


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
) -> tuple[int, str, float]:
    """Run one container pass for `prompt` and return (exit_code, output, duration_secs).

    Duration is measured with time.monotonic() around run_in_container only. Logging and the
    economics summary are the CALLER's responsibility (the planning pass logs without an
    economics footer; the build and iterate passes attach one). Shared by build and iterate.

    `timeout` is the --max-duration budget in seconds; None preserves run_in_container's own
    default (FALLBACK_TIMEOUT_SECS), so the kwarg is only forwarded when explicitly set.
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


@main.command()
@click.option("--force", is_flag=True, help="Reinstall even when already on the latest release.")
@click.pass_context
def update(ctx: click.Context, force: bool) -> None:
    """Install the latest published release via the detected installer (uv tool/pipx/pip).

    Dev checkout -> git hint, no-op. Undetectable installer -> manual hint, nonzero exit.
    """
    ctx.exit(force_update(force=force, out=click.echo))


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
