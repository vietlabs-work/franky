"""Franky CLI: wire config -> task -> prompt -> container -> PR-URL.

WHY config/task errors surface as click.ClickException: they are operator errors (bad
allowlist, off-list repo, missing creds), so a clean non-zero exit with a stderr message
beats a traceback. Every printed or logged string is redacted first - a secret value must
never reach the terminal or the on-disk log.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    FRANKY_IMAGE_VAR,
    FRANKY_PROXY_IMAGE_VAR,
    ensure_image_available,
    resolve_image,
    run_in_container,
)
from .engine import resolve_engine
from .jira import JIRA_API_TOKEN_VAR, JIRA_EMAIL_VAR, fetch_jira_issue
from .prompt import build_plan_prompt, build_prompt
from .task import PROSE_MAX_CHARS, parse_task
from .update_check import force_update, maybe_auto_update

TASKS_DIR = Path("tasks")


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
    type=click.Choice(["pi", "claude"]),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
@click.option(
    "--plan-first",
    "plan_first",
    is_flag=True,
    help="Run a read-only planning pass first, show the plan, and execute only after approval.",
)
def build(
    task_input: tuple[str, ...], repo: str | None, engine: str | None, plan_first: bool
) -> None:
    """Build TASK_INPUT (a GitHub issue URL, a JIRA key, or a prose request) and open a PR.

    Examples:
      franky build https://github.com/you/repo/issues/42
      franky build jira FOO-123 --repo you/repo
      franky build "add a --json flag" --repo you/repo

    With --plan-first, Franky first runs the engine in a read-only planning pass, prints the
    plan, and waits for explicit approval; nothing is built or PR'd until you confirm. The
    approval gate is the hard guarantee (the planning container is still autonomous), so a
    declined or non-interactive run writes nothing.
    """
    task_input_str = " ".join(task_input)

    # Best-effort, hint-only update check (never blocks/raises; ~1s budget, cached). Prints
    # a one-line stderr hint if a newer release exists. Silenced by FRANKY_NO_UPDATE_CHECK=1.
    maybe_auto_update()

    # Config + task parse are operator-error surfaces -> clean ClickException, no traceback.
    try:
        cfg = load_config(engine, os.environ)
        spec = parse_task(task_input_str, repo, cfg.allowed_repos)
        if spec.source == "jira":
            # Fetch the JIRA issue host-side (the container has no JIRA creds or egress).
            # A fetch failure raises ValueError, caught by the same handler below.
            body = fetch_jira_issue(spec.text, os.environ)
            spec = dc_replace(spec, text=body[:PROSE_MAX_CHARS].strip())
    except ValueError as exc:
        # load_config/parse_task/fetch_jira_issue never put a secret value in their
        # messages, but redact defensively in case a future message ever interpolates env.
        raise click.ClickException(redact(str(exc), cfg_secrets_safe())) from exc

    secrets = cfg.secret_values()

    franky_img = resolve_image(os.environ, FRANKY_IMAGE_VAR, "franky")
    proxy_img = resolve_image(os.environ, FRANKY_PROXY_IMAGE_VAR, "franky-proxy")
    for label, img, dev_build, dev_var in (
        ("franky", franky_img, "docker build -t franky .", FRANKY_IMAGE_VAR),
        ("franky-proxy", proxy_img, "docker build -t franky-proxy proxy/", FRANKY_PROXY_IMAGE_VAR),
    ):
        ok, reason = ensure_image_available(img)
        if not ok:
            if reason == "no-docker":
                raise click.ClickException(
                    "docker is not available - is the daemon running and `docker` on PATH?"
                )
            if reason == "auth":
                raise click.ClickException(
                    f"{label} image '{img}' needs auth to pull - run `docker login ghcr.io` "
                    f"(a PAT with read:packages), or for local dev `{dev_build}` "
                    f"and set {dev_var}=<local-tag>."
                )
            raise click.ClickException(
                f"{label} image '{img}' not found locally and could not be pulled. "
                f"For local dev: `{dev_build}` and set {dev_var}=<local-tag>."
            )

    def _run(prompt: str) -> tuple[int, str, float]:
        """Run one container pass for `prompt` and return (exit_code, output, duration_secs).

        Duration is measured with time.monotonic() around run_in_container only.
        Logging is the caller's responsibility so the build pass can attach an economics
        footer while the planning pass logs without one.
        """
        inner_argv = cfg.engine.inner_argv(prompt, model=None)
        t0 = time.monotonic()
        code, output = run_in_container(cfg, inner_argv, image=franky_img, proxy_image=proxy_img)
        duration = time.monotonic() - t0
        return code, output, duration

    if plan_first:
        # PHASE 1: planning pass. Show the plan, then gate on explicit approval. A plan that
        # errored is not a plan to approve, so abort before the gate. No economics on this
        # pass - economics is build-pass only.
        code, output, _plan_dur = _run(build_plan_prompt(spec))
        _write_log(output, secrets)
        click.echo("franky: --- plan (read-only, nothing written yet) ---", err=True)
        click.echo(output)
        if code != 0:
            raise click.ClickException(
                f"planning pass exited non-zero ({code}) - see the redacted log in tasks/"
            )
        # default=False and non-interactive abort both fail closed: no approval -> no build.
        if not click.confirm("franky: proceed to execute this plan?", default=False):
            click.echo("franky: plan-first aborted - nothing was built or opened.", err=True)
            return

    # PHASE 2 (or the only phase without --plan-first): build it and open the PR.
    code, output, duration = _run(build_prompt(spec))

    # Compute and emit economics before the PR-URL echo and before any ClickException so
    # the summary is always shown (even when the agent exits non-zero). Degrade to all-unknown
    # on any parse/format error so the run never fails due to economics.
    try:
        econ = redact(format_economics(parse_usage(output), duration), secrets)
    except Exception:
        econ = redact(format_economics(Usage(), duration), secrets)
    click.echo(econ, err=True)
    _write_log(output, secrets, footer=econ)

    # Scope PR-URL detection to the task's own repo so a hostile issue body cannot make
    # Franky report a PR URL for some other (attacker) repo.
    pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
    if pr_url:
        click.echo(pr_url)
    else:
        click.echo(
            "franky: no PR URL found in agent output - see the redacted log in tasks/", err=True
        )
    if code != 0:
        raise click.ClickException(
            f"agent exited non-zero ({code}) - see the redacted log in tasks/"
        )


def cfg_secrets_safe() -> list[str]:
    """Best-effort secret list for redacting an error raised before cfg fully exists.

    Reads the JIRA token (and email) directly from os.environ so they are available even
    when load_config raised before cfg was assigned, and on the host-side JIRA fetch path
    which never reaches passthrough_env. Falls back gracefully (empty list) when the vars
    are absent - load_config/parse_task/fetch_jira_issue messages are already value-free,
    so this is a defensive backstop.
    """
    return [v for v in (os.environ.get(JIRA_API_TOKEN_VAR), os.environ.get(JIRA_EMAIL_VAR)) if v]


def _write_log(output: str, secrets: list[str], footer: str | None = None) -> None:
    """Write the REDACTED agent output to tasks/<timestamp>.log.

    When `footer` is given, it is appended after the transcript (also redacted) separated
    by a newline so the economics summary lands in the same timestamped file.
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    body = redact(output, secrets) + "\n"
    if footer is not None:
        body += redact(footer, secrets) + "\n"
    (TASKS_DIR / f"{stamp}.log").write_text(body, encoding="utf-8")


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
@click.option("--force", is_flag=True, help="Reinstall even when already on the latest release.")
@click.pass_context
def update(ctx: click.Context, force: bool) -> None:
    """Install the latest published release via the detected installer (uv tool/pipx/pip).

    Dev checkout -> git hint, no-op. Undetectable installer -> manual hint, nonzero exit.
    """
    ctx.exit(force_update(force=force, out=click.echo))


if __name__ == "__main__":
    main()
