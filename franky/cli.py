"""Franky CLI: wire config -> task -> prompt -> container -> PR-URL.

WHY config/task errors surface as click.ClickException: they are operator errors (bad
allowlist, off-list repo, missing creds), so a clean non-zero exit with a stderr message
beats a traceback. Every printed or logged string is redacted first - a secret value must
never reach the terminal or the on-disk log.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import click

from . import __version__
from .config import load_config, redact
from .container import ensure_image, run_in_container
from .prompt import build_prompt
from .task import parse_task

TASKS_DIR = Path("tasks")


@click.group()
def main() -> None:
    """Franky - a lean personal coding agent that builds in a container and opens a PR."""


@main.command()
@click.argument("task_input")
@click.option("--repo", "repo", default=None, help="Target repo owner/repo (required for prose tasks).")
@click.option("--engine", "engine", default=None, type=click.Choice(["pi", "claude"]),
              help="Engine override; else FRANKY_ENGINE, else pi.")
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

    if not ensure_image():
        raise click.ClickException("franky image not found - build it with `docker build -t franky .`")

    code, output = run_in_container(cfg, inner_argv)

    _write_log(output, secrets)

    # Scope PR-URL detection to the task's own repo so a hostile issue body cannot make
    # Franky report a PR URL for some other (attacker) repo.
    pr_url = cfg.engine.parse_pr_url(output, repo=spec.repo)
    if pr_url:
        click.echo(pr_url)
    else:
        click.echo("franky: no PR URL found in agent output - see the redacted log in tasks/", err=True)
    if code != 0:
        raise click.ClickException(f"agent exited non-zero ({code}) - see the redacted log in tasks/")


def cfg_secrets_safe() -> list[str]:
    """Best-effort secret list for redacting an error raised before cfg fully exists.
    Falls back to empty (load_config messages are already value-free)."""
    return []


def _write_log(output: str, secrets: list[str]) -> None:
    """Write the REDACTED agent output to tasks/<timestamp>.log."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (TASKS_DIR / f"{stamp}.log").write_text(redact(output, secrets) + "\n", encoding="utf-8")


@main.command()
def version() -> None:
    """Print the Franky version."""
    click.echo(f"franky {__version__}")


if __name__ == "__main__":
    main()
