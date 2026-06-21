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
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import click

from . import franky_version
from ._install import detect_install
from .config import load_config, redact
from .container import (
    FRANKY_IMAGE_VAR,
    FRANKY_PROXY_IMAGE_VAR,
    ensure_image_available,
    resolve_image,
    run_in_container,
)
from .engine import resolve_engine
from .prompt import build_prompt
from .task import parse_task

TASKS_DIR = Path("tasks")


@click.group()
def main() -> None:
    """Franky - a lean personal coding agent that builds in a container and opens a PR."""


@main.command()
@click.argument("task_input")
@click.option(
    "--repo", "repo", default=None, help="Target repo owner/repo (required for prose tasks)."
)
@click.option(
    "--engine",
    "engine",
    default=None,
    type=click.Choice(["pi", "claude"]),
    help="Engine override; else FRANKY_ENGINE, else pi.",
)
def build(task_input: str, repo: str | None, engine: str | None) -> None:
    """Build TASK_INPUT (a GitHub issue URL or a prose request) and open a PR."""
    # Config + task parse are operator-error surfaces -> clean ClickException, no traceback.
    try:
        cfg = load_config(engine, os.environ)
        spec = parse_task(task_input, repo, cfg.allowed_repos)
    except ValueError as exc:
        # load_config/parse_task never put a secret value in their messages, but redact
        # defensively in case a future message ever interpolates env.
        raise click.ClickException(redact(str(exc), cfg_secrets_safe())) from exc

    secrets = cfg.secret_values()
    prompt = build_prompt(spec)
    inner_argv = cfg.engine.inner_argv(prompt, model=None)

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

    code, output = run_in_container(cfg, inner_argv, image=franky_img, proxy_image=proxy_img)

    _write_log(output, secrets)

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
    Falls back to empty (load_config messages are already value-free)."""
    return []


def _write_log(output: str, secrets: list[str]) -> None:
    """Write the REDACTED agent output to tasks/<timestamp>.log."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (TASKS_DIR / f"{stamp}.log").write_text(redact(output, secrets) + "\n", encoding="utf-8")


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


if __name__ == "__main__":
    main()
