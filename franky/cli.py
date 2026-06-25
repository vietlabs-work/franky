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
from .engine import ENGINES, PI_PROVIDER_VARS, resolve_engine
from .jira import JIRA_API_TOKEN_VAR, JIRA_EMAIL_VAR, fetch_jira_issue
from .profile import build_bundle, load_profile, profile_path
from .prompt import build_iterate_prompt, build_plan_prompt, build_prompt
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
def build(
    task_input: tuple[str, ...],
    repo: str | None,
    engine: str | None,
    plan_first: bool,
    profile_path_opt: str | None,
    verbose: bool,
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

    # Inject user config file values into os.environ via setdefault (process env wins).
    # WHY here and not in the group callback or `version`: the `config` subgroup must
    # remain usable even when the config file is malformed (so the user can `config set`
    # to fix it), and `version` is intentionally lightweight.  A malformed file raises
    # ValueError here -> caught below -> clean ClickException, no traceback.
    try:
        load_config_file(os.environ)
    except ValueError as exc:
        raise click.ClickException(f"config file error: {exc}") from exc

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
    franky_img, proxy_img = _ensure_images(os.environ)

    # Build the profile bundle (optional). Auto-discovers ~/.franky/profile.toml unless
    # overridden by --profile or FRANKY_PROFILE_PATH. Fails closed on detected credentials.
    bundle = _load_profile_bundle(profile_path_opt, os.environ, secrets)

    # Verbose mode: raw passthrough of agent output to stderr. Falls back to distilled
    # progress (the default) which shows compact milestones without the raw stream.
    verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
    progress = _make_progress(cfg.engine, verbose)

    if plan_first:
        # PHASE 1: planning pass. Show the plan, then gate on explicit approval. A plan that
        # errored is not a plan to approve, so abort before the gate. No economics on this
        # pass - economics is build-pass only.
        code, output, _plan_dur = _run_pass(
            cfg, build_plan_prompt(spec), franky_img, proxy_img, bundle, progress=progress
        )
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
    code, output, duration = _run_pass(
        cfg, build_prompt(spec), franky_img, proxy_img, bundle, progress=progress
    )

    # Emit economics before the PR-URL echo and before any ClickException so the summary is
    # always shown (even when the agent exits non-zero).
    econ = _economics_line(output, duration, secrets)
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
def iterate(pr_url: str, engine: str | None, verbose: bool) -> None:
    """Address review feedback / failing CI on an existing Franky PR with follow-up commits.

    Example:
      franky iterate https://github.com/you/repo/pull/42

    Runs the SAME hardened, egress-controlled container as `franky build`, but instead of
    starting fresh it checks out the PR's existing branch, reads the review comments and
    failing checks via `gh`, and pushes ADDITIVE follow-up commits to that branch. It never
    force-pushes, never merges, and never opens a new PR - a human still reviews every change.
    The PR URL is authoritative (it carries owner/repo), so there is no --repo flag.
    """
    # Best-effort, hint-only update check - same as build (never blocks/raises).
    maybe_auto_update()

    # Same config-file injection as `build` (see that command's WHY comment).
    try:
        load_config_file(os.environ)
    except ValueError as exc:
        raise click.ClickException(f"config file error: {exc}") from exc

    # Config + PR-URL parse are operator-error surfaces -> clean ClickException, no traceback.
    try:
        cfg = load_config(engine, os.environ)
        spec = parse_pr_task(pr_url, cfg.allowed_repos)
    except ValueError as exc:
        raise click.ClickException(redact(str(exc), cfg_secrets_safe())) from exc

    secrets = cfg.secret_values()
    franky_img, proxy_img = _ensure_images(os.environ)

    bundle = _load_profile_bundle(None, os.environ, secrets)
    verbose = verbose or bool(os.environ.get(FRANKY_VERBOSE_VAR))
    progress = _make_progress(cfg.engine, verbose)
    code, output, duration = _run_pass(
        cfg, build_iterate_prompt(spec), franky_img, proxy_img, bundle, progress=progress
    )

    # Economics first (same as build) so spend is visible even when the agent exits non-zero.
    econ = _economics_line(output, duration, secrets)
    click.echo(econ, err=True)
    _write_log(output, secrets, footer=econ)

    # iterate produces NO new PR; the existing PR gains commits. Report a labeled line rather
    # than a bare URL on stdout - exit 0 means the pass ran, NOT that a push necessarily landed
    # (the agent correctly pushes nothing on red tests or a failed own-PR check), so we never
    # present the URL as a fresh success artifact.
    click.echo(
        f"franky: iterate pass complete for {spec.text} - review the PR for the new commits",
        err=True,
    )
    if code != 0:
        raise click.ClickException(
            f"agent exited non-zero ({code}) - see the redacted log in tasks/"
        )


def _ensure_images(env: Mapping[str, str]) -> tuple[str, str]:
    """Resolve + ensure the franky and franky-proxy images are available locally.

    Returns (franky_image, proxy_image). Raises a clean ClickException (no traceback) for the
    operator-facing failure modes: docker absent, auth needed, or pull failed. Shared by
    `build` and `iterate` - the only difference between them is the prompt, not the plumbing.
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
    return franky_img, proxy_img


def _run_pass(
    cfg,
    prompt: str,
    franky_img: str,
    proxy_img: str,
    profile_bundle: str | None = None,
    progress=None,
) -> tuple[int, str, float]:
    """Run one container pass for `prompt` and return (exit_code, output, duration_secs).

    Duration is measured with time.monotonic() around run_in_container only. Logging and the
    economics summary are the CALLER's responsibility (the planning pass logs without an
    economics footer; the build and iterate passes attach one). Shared by build and iterate.
    """
    inner_argv = cfg.engine.inner_argv(prompt, model=None)
    t0 = time.monotonic()
    code, output = run_in_container(
        cfg,
        inner_argv,
        image=franky_img,
        proxy_image=proxy_img,
        profile_bundle=profile_bundle,
        progress=progress,
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
    ~/.franky/profile.toml.  Fails closed (ClickException) on a detected credential.
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
        raise click.ClickException(redact(str(exc), secrets)) from exc

    if not spec.all_files():
        return None

    try:
        return build_bundle(spec)
    except ValueError as exc:
        raise click.ClickException(redact(str(exc), secrets)) from exc


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


def _economics_line(output: str, duration: float, secrets: list[str]) -> str:
    """The redacted one-line economics summary for a pass. Degrades to all-unknown on any
    parse/format error so economics can never fail a run. Shared by build and iterate."""
    try:
        return redact(format_economics(parse_usage(output), duration), secrets)
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
        # Hidden prompt - value never echoed to the terminal.
        value = click.prompt(key, hide_input=True)
    else:
        if value is None:
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


if __name__ == "__main__":
    main()
