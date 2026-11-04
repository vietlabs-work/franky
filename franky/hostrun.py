"""Host runtime: run the engine as the operator's own user under the engine's OS sandbox.

Explicit opt-in (`FRANKY_RUNTIME=host`), never a fallback. The container runtime stays the
default and its code path is untouched. The host runtime drops what Docker gave: no egress
proxy, no read-only root, no memory or process limits, and a filesystem the engine can read as
the operator. What it keeps is a per-run directory (`<runs>/<job>.host/{home,work,tmp}`, 0700,
removed after the run), a from-scratch child environment, the engine's own OS sandbox (Seatbelt
on macOS) with writes limited to that directory, the same redactor and transcript, a hard
wall-clock watchdog, and the same host-side publish validation. A run that cannot get its OS
sandbox refuses; it never runs unsandboxed.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

from . import jobs, snapshot
from .container import (
    CONTAINER_TIMEOUT_CODE,
    REPO_SETUP_BIN,
    REPO_SETUP_FAILED_CODE,
    REPO_SETUP_TIMEOUT,
    STEER_FILE,
)
from .profile import CONTAINER_HOME
from .result import ConfigError
from .transcript import CHUNK_SIZE, MAX_EVENT_CHARS, Redactor, Transcript

HOST_SUFFIX = ".host"
# The container's own paths. The prompts and stored engine sessions name them literally, so the
# host run rewrites them to the per-run directory (and rewrites the session slug back on copy-out).
CONTAINER_WORK = "/work"
PRIVATE_PROMPT_PATH = "/tmp/franky-private-prompt"
# The container cwd slug inside a stored Claude session path (see engine.ClaudeEngine.session_dir).
CONTAINER_SLUG = "-work"

HOST_TIMEOUT_MSG = "franky: host run timed out after {timeout}s"
TERM_GRACE_SECS = 5.0
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_AUTH_MAX_BYTES = 256 * 1024
# `franky-repo-setup.py` ships in the wheel as a symlink-free copy beside this module.
REPO_SETUP_SCRIPT = Path(__file__).with_name("repo_setup_script.py")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_FILE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")
CTL_SUFFIX = ".ctl"


def host_dir(run_id: str, env=None) -> Path:
    """The per-run directory of `run_id`. The id is validated so it is one safe path component."""
    if not jobs._JOB_ID_RE.match(run_id):
        raise ValueError(f"invalid job id: {run_id!r}")
    return jobs.runs_dir(env) / f"{run_id}{HOST_SUFFIX}"


def ctl_dir(run_id: str, env=None) -> Path:
    """The per-run control directory: the engine's settings file and Seatbelt profile. A sibling
    of the run directory, never under it or under any path the sandbox lets the engine write."""
    if not jobs._JOB_ID_RE.match(run_id):
        raise ValueError(f"invalid job id: {run_id!r}")
    return jobs.runs_dir(env) / f"{run_id}{CTL_SUFFIX}"


def auth_dir(env=None) -> Path:
    """The host Codex subscription store, beside the config file (0700; `auth.json` 0600)."""
    from .userconfig import config_file_path

    return config_file_path(dict(os.environ if env is None else env)).parent / "codex-auth"


def preflight(cfg, which=shutil.which, platform: str = sys.platform) -> None:
    """Refuse a host run that cannot be sandboxed. Never falls back to the container."""
    if platform == "darwin":
        if not Path("/usr/bin/sandbox-exec").exists():
            raise ConfigError(
                "FRANKY_RUNTIME=host needs /usr/bin/sandbox-exec (Seatbelt) - refusing"
            )
    elif platform.startswith("linux"):
        if cfg.engine.name == "codex":
            raise ConfigError(
                "FRANKY_RUNTIME=host with codex needs macOS Seatbelt to confine reads - refusing"
            )
        if not which("bwrap"):
            raise ConfigError("FRANKY_RUNTIME=host needs bubblewrap (`bwrap`) on Linux - refusing")
    else:
        raise ConfigError(f"FRANKY_RUNTIME=host is not supported on {platform} - refusing")
    for binary in (cfg.engine.name, "git", "gh"):
        if not which(binary):
            raise ConfigError(
                f"FRANKY_RUNTIME=host needs `{binary}` on PATH - refusing "
                "(the host runtime runs the engine as you; install it or use the container runtime)"
            )
    if cfg.engine.name == "codex" and cfg.auth_volume and not codex_auth_ready(None):
        raise ConfigError(
            "Codex subscription login is missing or invalid - run `franky auth login codex`"
        )


# ---------------------------------------------------------------------------
# Path rewriting: the prompts name the container's HOME, /work and the private prompt path.
# ---------------------------------------------------------------------------


def rewrite_text(text: str, home: Path, work: Path, tmp: Path) -> str:
    """Point container paths at this run's directories. `/work` only as a whole path component
    (never `/workspace`, `a/work`, `/work-x`)."""
    text = text.replace(PRIVATE_PROMPT_PATH, str(tmp / "franky-private-prompt"))
    text = text.replace(CONTAINER_HOME, str(home))
    return re.sub(r"(?<![\w./~-])/work(?![\w-])", lambda _m: str(work), text)


# ---------------------------------------------------------------------------
# Engine argv and the OS sandbox
# ---------------------------------------------------------------------------

_CODEX_BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def _confine(root: Path, keep: Path) -> list[str]:
    """Paths to deny so that, under `root`, only `keep` stays reachable: every sibling along
    the way from `root` down to `keep`. A `keep` outside `root` denies `root` whole. The engine's
    deny rules win over allow rules, so the allowed directory is carved out by enumeration."""
    try:
        parts = keep.relative_to(root).parts
    except ValueError:
        return [str(root)]
    out: list[str] = []
    cur = root
    for part in parts:
        try:
            names = os.listdir(cur)
        except OSError as exc:
            raise ConfigError(f"FRANKY_RUNTIME=host cannot confine {cur}: {exc}") from exc
        out += [str(cur / n) for n in names if n != part]
        cur = cur / part
    return out


def _new_file_levels(root: Path, keep: Path) -> list[str]:
    """The directories from `root` down to `keep`'s parent. The engine could still create a NEW
    file directly in one of them (the enumeration only names what exists), so each gets a
    single-level write deny."""
    try:
        parts = keep.relative_to(root).parts
    except ValueError:
        return []
    levels = [str(root)]
    cur = root
    for part in parts[:-1]:
        cur = cur / part
        levels.append(str(cur))
    return levels


def _tool_rules(paths: list[str], tools=_FILE_TOOLS) -> list[str]:
    return [f"{t}(/{p})" for p in paths for t in tools] + [
        f"{t}(/{p}/**)" for p in paths for t in tools
    ]


def claude_settings(
    run: Path, real_home: Path, runs_dir: Path, extra_deny: tuple[Path, ...] = ()
) -> dict:
    """The per-run Claude Code settings. Allow-only: the engine can read and write only the run's
    own directory. Bash runs in Claude's mandatory OS sandbox (writes to the run's home, work and
    tmp, no reads of the operator home or the runs directory except the run directory, no
    unsandboxed commands). Claude's built-in file tools are not covered by that sandbox, so they
    get deny rules for everything under the home and the runs directory except the run
    directory, and for the settings and profile directory. WebFetch and WebSearch are denied;
    Bash keeps open network (git, gh, the engine API). `--dangerously-skip-permissions` stays on
    the argv for headless approval; deny rules still win over it."""
    deny = _confine(real_home, run)
    levels = _new_file_levels(real_home, run)
    if not _under(run, real_home):
        deny += _confine(runs_dir, run)
        levels += _new_file_levels(runs_dir, run)
    deny += [str(p) for p in extra_deny]
    deny = list(dict.fromkeys(deny))
    read_deny = [str(real_home)] + ([] if _under(run, real_home) else [str(runs_dir)])
    return {
        "permissions": {
            "deny": [
                *_tool_rules(deny),
                *[f"{t}(/{lvl}/*)" for lvl in levels for t in ("Edit", "Write")],
                "WebFetch",
                "WebSearch",
            ],
            "allow": [f"{t}(/{run}/**)" for t in ("Read", "Edit")],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "allowWrite": [str(run / "home"), str(run / "work"), str(run / "tmp")],
                "denyRead": read_deny,
                "allowRead": [str(run)],
            },
            "network": {"allowedDomains": ["*"]},
        },
    }


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sb(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_install_roots(which=shutil.which) -> list[Path]:
    """Read-only roots the codex launcher needs: its bin directory, its resolved install
    directory and the nearest enclosing package root (it may sit under the operator home)."""
    found = which("codex")
    if not found:
        return []
    real = Path(os.path.realpath(found))
    roots = [Path(found).parent, real.parent]
    for ancestor in list(real.parents)[:6]:
        if (ancestor / "package.json").is_file():
            roots.append(ancestor)
            break
    return list(dict.fromkeys(roots))


def seatbelt_profile(run: Path, real_home: Path, runs_dir: Path, install_roots: list[Path]) -> str:
    """The Seatbelt profile around the codex process. Reads: nothing under the operator home or
    the runs directory except the run directory and codex's own install roots (read only).
    Writes: nothing except the run directory, `/dev` and the per-user temp area. The network
    stays open. Later rules win in Seatbelt, so the allows come last.

    Codex's own `--sandbox workspace-write` cannot run inside this profile (macOS refuses a
    nested sandbox that is not a subset of the outer one), so codex runs with
    `--sandbox danger-full-access` and this profile is the sandbox."""
    lines = [
        "(version 1)",
        "(allow default)",
        '(deny file-write* (subpath "/"))',
        '(allow file-write* (subpath "/dev") (subpath "/private/var/folders"))',
    ]
    for root in dict.fromkeys([real_home, runs_dir]):
        lines.append(f"(deny file-read* file-write* (subpath {_sb(root)}))")
    for root in install_roots:
        lines.append(f"(allow file-read* (subpath {_sb(root)}))")
    lines.append(f"(allow file-read* file-write* (subpath {_sb(run)}))")
    return "\n".join(lines) + "\n"


def write_ctl_file(ctl: Path, name: str, text: str) -> Path:
    """Write a read-only (0400) control file into the 0700 control directory."""
    ctl.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ctl / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def engine_argv(inner_argv: list[str], engine: str, control_file: Path) -> list[str]:
    """Swap the container's sandbox-off flags for the engine's OS sandbox. `control_file` is the
    claude settings file or the codex Seatbelt profile. Raises ValueError when the argv has no
    engine flag to swap (a refusal, never an unsandboxed run)."""
    argv = list(inner_argv)
    if engine == "claude":
        if "claude" not in argv or "--dangerously-skip-permissions" not in argv:
            raise ValueError("claude argv has no engine command to sandbox")
        at = argv.index("claude")
        return [*argv[: at + 1], "--settings", str(control_file), *argv[at + 1 :]]
    if engine == "codex":
        if _CODEX_BYPASS not in argv or "codex" not in argv:
            raise ValueError("codex argv has no sandbox flag to swap")
        argv[argv.index(_CODEX_BYPASS) : argv.index(_CODEX_BYPASS) + 1] = [
            "--sandbox",
            "danger-full-access",
        ]
        at = argv.index("codex")
        return [*argv[:at], SANDBOX_EXEC, "-f", str(control_file), *argv[at:]]
    raise ValueError(f"engine {engine!r} has no host sandbox")


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------


def child_env(cfg, home: Path, tmp: Path, os_env) -> dict[str, str]:
    """Built from scratch: PATH, LANG, TERM, HOME, TMPDIR, XDG_*, then `cfg.passthrough_env`
    only. No other variable of the operator's environment reaches the engine."""
    env = {
        "PATH": os_env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os_env.get("LANG") or "en_US.UTF-8",
        "TERM": "dumb",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    env.update(cfg.passthrough_env)
    if cfg.engine.name == "codex":
        env["CODEX_HOME"] = str(home / ".codex")
    return env


# ---------------------------------------------------------------------------
# Bundles and sessions (regular files only, bounded)
# ---------------------------------------------------------------------------


def extract_bundle(data: bytes | None, dest: Path, path: str | None = None) -> None:
    """Extract a gzip tar (bytes, or a file at `path`) into `dest`: regular files and
    directories only, no links, nothing outside `dest`, bounded."""
    if data is None and path is None:
        return
    source = io.BytesIO(data) if data is not None else open(path, "rb")  # noqa: SIM115
    try:
        with gzip.GzipFile(fileobj=source) as stream:
            snapshot._extract_plain(stream, dest, max_bytes=MAX_BUNDLE_BYTES)
    finally:
        source.close()


def work_slug(work: Path) -> str:
    """Claude Code names a session directory after the cwd: every non-alphanumeric becomes `-`."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(work))


def _host_rel(rel: str, slug: str) -> str:
    return rel.replace(f"/{CONTAINER_SLUG}", f"/{slug}", 1)


def rename_session_slug(home: Path, session_dir: str, slug: str) -> None:
    """After a session tar landed under the container slug, move it to this run's slug so the
    engine finds it by its cwd."""
    if not session_dir or CONTAINER_SLUG not in session_dir:
        return
    base = home / Path(session_dir).parent
    old, new = base / CONTAINER_SLUG, base / slug
    if old.is_dir() and not old.is_symlink() and not new.exists():
        os.rename(old, new)


def copy_session(home: Path, paths: list[str], dest, slug: str, *, max_bytes: int) -> str:
    """Copy an engine session (`paths`: the session file, then its optional side dir; stored
    under the container slug) out of the run home into `dest`, regular files only, one byte
    budget. Mirrors `snapshot.copy_session`: ok, too_large or failed."""
    if not paths:
        return "failed"
    budget = max_bytes
    for index, rel in enumerate(paths):
        src = home / _host_rel(rel, slug)
        target_root = Path(dest) / rel
        try:
            st = os.lstat(src)
        except OSError:
            if index == 0:
                return "failed"
            continue
        files: list[tuple[Path, Path]] = []
        if stat.S_ISREG(st.st_mode):
            files.append((src, target_root))
        elif stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            for root, dirs, names in os.walk(src, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for name in names:
                    path = Path(root) / name
                    files.append((path, target_root / path.relative_to(src)))
        elif index == 0:
            return "failed"
        for path, out in files:
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                size = os.lstat(path).st_size
                budget -= size
                if budget < 0:
                    return "too_large"
                out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    ofd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(ofd, "wb") as dst:
                        shutil.copyfileobj(fh, dst)
            except OSError:
                return "failed"
    try:
        landed = stat.S_ISREG(os.lstat(Path(dest) / paths[0]).st_mode)
    except OSError:
        landed = False
    return "ok" if landed else "failed"


# ---------------------------------------------------------------------------
# Codex subscription auth (host store instead of the Docker volume)
# ---------------------------------------------------------------------------


def _read_auth(path: Path) -> dict | None:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _AUTH_MAX_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) and payload else None


def auth_strings(payload: dict) -> list[str]:
    """Every string of 8 or more characters in an auth payload, for in-memory redaction."""
    out: list[str] = []
    pending: list = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and len(value) >= 8:
            out.append(value)
    return out


def codex_auth_ready(env=None) -> bool:
    return _read_auth(auth_dir(env) / "auth.json") is not None


def codex_auth_secrets(env=None) -> list[str]:
    payload = _read_auth(auth_dir(env) / "auth.json")
    return auth_strings(payload) if payload else []


def codex_auth_login(runner=subprocess.run, env=None) -> bool:
    """Browserless Codex login into the host store: a private CODEX_HOME, then only `auth.json`
    (a regular, bounded, JSON file) is kept."""
    store = auth_dir(env)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    scratch = store / f".login-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(mode=0o700)
    try:
        login_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(scratch),
            "CODEX_HOME": str(scratch),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        proc = runner(
            ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"],
            env=login_env,
        )
        if getattr(proc, "returncode", 1) != 0:
            return False
        return _store_auth(scratch / "auth.json", store)
    except OSError:
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _store_auth(src: Path, store: Path) -> bool:
    """Atomically replace `<store>/auth.json` with `src` when it is a valid auth file."""
    payload = _read_auth(src)
    if payload is None:
        return False
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = store / f".auth-{uuid.uuid4().hex[:8]}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, store / "auth.json")
    return True


def codex_auth_logout(env=None) -> bool:
    shutil.rmtree(auth_dir(env), ignore_errors=True)
    return not auth_dir(env).exists()


# ---------------------------------------------------------------------------
# Job control: process group identity (macOS has no /proc)
# ---------------------------------------------------------------------------


def proc_start_marker(pid: int, runner=subprocess.run) -> str | None:
    """The process start time (`ps -o lstart=`), a reuse guard for a recorded process group."""
    try:
        proc = runner(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    return out if getattr(proc, "returncode", 1) == 0 and out else None


def group_state(record: dict, *, killpg=os.killpg, marker_of=proc_start_marker) -> bool | None:
    """Is the recorded host process group still the run's own? True / False / None.

    True only when the group exists AND its leader's start marker equals the recorded one. A
    group that exists but cannot be verified (marker gone or different, no permission, no record
    of one) is None: the caller reports it as running and never signals it."""
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        return None
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    recorded = record.get("pgid_started_at")
    if not recorded:
        return None
    return True if marker_of(pgid) == recorded else None


def terminate_group(
    pgid: int, *, killpg=os.killpg, sleeper=time.sleep, grace: float = TERM_GRACE_SECS
) -> None:
    """TERM the group, wait up to `grace` seconds for it to go, then KILL it. Never raises."""
    try:
        killpg(pgid, 15)
    except OSError:
        return
    waited = 0.0
    while waited < grace:
        sleeper(0.25)
        waited += 0.25
        try:
            killpg(pgid, 0)
        except OSError:
            return
    try:
        killpg(pgid, 9)
    except OSError:
        pass


def kill_run(
    record: dict, *, killpg=os.killpg, sleeper=time.sleep, marker_of=proc_start_marker
) -> bool:
    """Stop a host run by its verified process group. Returns True when a group was signalled.
    An unverifiable group is never signalled."""
    if group_state(record, killpg=killpg, marker_of=marker_of) is not True:
        return False
    terminate_group(record["pgid"], killpg=killpg, sleeper=sleeper)
    return True


def remove_run_dir(job_id: str, env=None) -> None:
    """Delete a run's host directory (never follows a symlink; never raises)."""
    try:
        path = host_dir(job_id, env)
    except ValueError:
        return
    if path.exists():
        snapshot._rmtree(path)
    try:
        ctl = ctl_dir(job_id, env)
    except ValueError:
        return
    if ctl.exists():
        _rm_ctl(ctl)


def _rm_ctl(ctl: Path) -> None:
    """Remove a control directory whose files are 0400 (never follows a symlink)."""
    try:
        os.chmod(ctl, 0o700)
    except OSError:
        pass
    snapshot._rmtree(ctl)


def deliver_steer(job_id: str, message: str, env=None) -> bool:
    """Append an operator correction to the run's steer file (O_NOFOLLOW, 0600)."""
    try:
        path = host_dir(job_id, env) / "home" / Path(STEER_FILE).name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(message)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _run_repo_setup(repo_setup, cfg, home: Path, env: dict, runner) -> tuple[bool, str]:
    """The repository-setup `map` stage on the host, bounded like the container's."""
    try:
        proc = runner(
            [
                sys.executable,
                str(REPO_SETUP_SCRIPT),
                "map",
                "--repo",
                repo_setup["repo"],
                "--ref",
                repo_setup["ref"],
                "--engine",
                repo_setup["engine"],
                "--home",
                str(home),
            ],
            capture_output=True,
            text=True,
            timeout=REPO_SETUP_TIMEOUT,
            env=env,
            cwd=str(home),
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(REPO_SETUP_TIMEOUT)}s"
    except OSError as exc:
        return False, exc.__class__.__name__
    if getattr(proc, "returncode", 1) != 0:
        detail = (getattr(proc, "stderr", "") or "").strip().splitlines()
        return False, (detail[-1] if detail else "no detail")[:300]
    return True, (getattr(proc, "stdout", "") or "").strip()[:300]


def run_on_host(
    cfg,
    inner_argv: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    profile_bundle: bytes | None = None,
    progress=None,
    popen=subprocess.Popen,
    runner=subprocess.run,
    run_id: str | None = None,
    session_tar: str | None = None,
    session_sink: dict | None = None,
    private_prompt_tar: bytes | None = None,
    extra_secrets: Iterable[str] = (),
    repo_setup: dict[str, str] | None = None,
    runs_env=None,
    **unsupported,
) -> tuple[int, Transcript]:
    """Run the inner engine on the host; return (returncode, redacted Transcript).

    Same contract as `container.run_in_container` for the parts a host run has: redaction before
    disk and callbacks, a hard wall-clock watchdog (timeout -> 124), repository setup before the
    engine (failure -> 78), a thread session copied out on a clean exit (or on timeout when the
    sink opts in). Snapshots, workspace export and resume need a container filesystem and are
    refused by the caller (exit 3); a sink passed here anyway is a bug and refuses loudly.
    """
    for name in ("resume_workspace", "workspace_sink"):
        if unsupported.get(name) is not None:
            raise ConfigError(f"FRANKY_RUNTIME=host does not support {name}")
    run_id = run_id or uuid.uuid4().hex[:12]
    os_env = os.environ if env is None else env
    base = host_dir(run_id, runs_env)
    real_home = Path(os.path.realpath(Path.home()))
    if base.exists():
        snapshot._rmtree(base)
    base.mkdir(mode=0o700, parents=True)
    base = Path(os.path.realpath(base))
    home, work, tmp = base / "home", base / "work", base / "tmp"
    for d in (home, work, tmp):
        d.mkdir(mode=0o700)

    secrets = [*cfg.secret_values(), *extra_secrets]
    output = Transcript()
    stream = Redactor(secrets)
    pending = ""
    code = 1
    proc = None
    codex_store = cfg.engine.name == "codex" and bool(cfg.auth_volume)

    def emit(text, *, final=False):
        nonlocal pending
        clean = stream.feed(text, final=final)
        output.write(clean)
        if progress is None:
            return
        pending += clean
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            progress(line + "\n")
        if len(pending) > MAX_EVENT_CHARS or final:
            if pending:
                progress(pending)
            pending = ""

    try:
        from .userconfig import config_file_path

        slug = work_slug(work)
        extract_bundle(profile_bundle, home)
        if session_tar is not None:
            try:
                extract_bundle(None, home, path=session_tar)
                rename_session_slug(home, cfg.engine.session_dir, slug)
            except Exception:
                pass  # the engine's resume fails and the thread store drops the session
        if private_prompt_tar is not None:
            extract_bundle(private_prompt_tar, tmp)
            prompt_file = tmp / "franky-private-prompt"
            prompt_file.write_text(
                rewrite_text(prompt_file.read_text(encoding="utf-8"), home, work, tmp),
                encoding="utf-8",
            )
            os.chmod(prompt_file, 0o600)
        if codex_store:
            payload = _read_auth(auth_dir(runs_env) / "auth.json")
            if payload is None:
                emit(
                    "franky: Codex subscription login is missing or invalid - "
                    "run `franky auth login codex`\n",
                    final=True,
                )
                return 1, output
            secrets.extend(auth_strings(payload))
            stream = Redactor(secrets)
            (home / ".codex").mkdir(mode=0o700, exist_ok=True)
            fd = os.open(home / ".codex" / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

        runs_dir = Path(os.path.realpath(base.parent))
        ctl = ctl_dir(run_id, runs_env)
        control = None
        if cfg.engine.name == "claude":
            extra_deny = (auth_dir(runs_env), config_file_path(dict(os_env)), ctl)
            control = write_ctl_file(
                ctl,
                "claude-settings.json",
                json.dumps(claude_settings(base, real_home, runs_dir, extra_deny)),
            )
        elif cfg.engine.name == "codex":
            control = write_ctl_file(
                ctl,
                "codex.sb",
                seatbelt_profile(base, real_home, runs_dir, codex_install_roots()),
            )
        argv = [rewrite_text(a, home, work, tmp) for a in inner_argv]
        # In-image helpers become the host's own interpreter and the packaged mapper.
        argv = [
            word
            for a in argv
            for word in (
                [sys.executable, str(REPO_SETUP_SCRIPT)]
                if a == REPO_SETUP_BIN
                else [sys.executable if a == "python3" else a]
            )
        ]
        try:
            argv = engine_argv(argv, cfg.engine.name, control)
        except ValueError as exc:
            raise ConfigError(f"FRANKY_RUNTIME=host refuses: {exc}") from exc
        env_map = child_env(cfg, home, tmp, os_env)

        if repo_setup is not None:
            ok, detail = _run_repo_setup(repo_setup, cfg, home, env_map, runner)
            if not ok:
                emit(
                    f"franky: repository setup failed (retryable, nothing was changed): {detail}\n",
                    final=True,
                )
                return REPO_SETUP_FAILED_CODE, output
            if detail:
                emit(f"franky: repository setup loaded {detail}\n")

        t_start = time.monotonic()
        try:
            proc = popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env_map,
                cwd=str(work),
                start_new_session=True,
            )
        except OSError as exc:
            emit(f"franky: could not launch the engine ({exc.__class__.__name__})", final=True)
            return 1, output
        jobs.update_record(
            run_id,
            {
                "runtime": "host",
                "pgid": proc.pid,
                "pgid_started_at": proc_start_marker(proc.pid, runner=runner),
                "host_dir": str(base),
            },
            runs_env,
        )
        watchdog_hit = threading.Event()

        def _on_deadline() -> None:
            watchdog_hit.set()
            terminate_group(proc.pid)

        watchdog = threading.Timer(timeout, _on_deadline)
        watchdog.daemon = True
        watchdog.start()
        timed_out = False
        try:
            import codecs

            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            reader = getattr(proc.stdout, "read1", None) or getattr(proc.stdout, "read", None)
            source = iter(lambda: reader(CHUNK_SIZE), b"") if reader else iter(proc.stdout)
            for chunk in source:
                if not chunk:
                    break
                emit(decoder.decode(chunk) if isinstance(chunk, bytes) else chunk)
                if watchdog_hit.is_set() or time.monotonic() - t_start >= timeout:
                    timed_out = True
                    terminate_group(proc.pid)
                    break
        finally:
            watchdog.cancel()
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=max(1.0, timeout - (time.monotonic() - t_start)))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_group(proc.pid)
                proc.wait(timeout=5)
            emit(decoder.decode(b"", final=True), final=True)
        if timed_out or watchdog_hit.is_set():
            code = CONTAINER_TIMEOUT_CODE
            output.write("\n" + HOST_TIMEOUT_MSG.format(timeout=timeout))
        else:
            code = proc.returncode
        # Leftover engine processes must not outlive the run, with the tokens.
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        if session_sink is not None and (
            code == 0 or (session_sink.get("on_timeout") and code == CONTAINER_TIMEOUT_CODE)
        ):
            try:
                session_sink["status"] = copy_session(
                    home,
                    session_sink["paths"],
                    session_sink["dest"],
                    slug,
                    max_bytes=session_sink["max_bytes"],
                )
            except Exception:
                pass
        if codex_store:
            try:
                _store_auth(home / ".codex" / "auth.json", auth_dir(runs_env))
            except OSError:
                pass
    except BaseException:
        output.close()
        raise
    finally:
        snapshot._rmtree(base)
        try:
            _rm_ctl(ctl_dir(run_id, runs_env))
        except OSError:
            pass
    return code, output
