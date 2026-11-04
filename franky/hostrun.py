"""Host runtime: run the engine as the operator's own user inside a macOS Seatbelt profile.

Explicit opt-in (`FRANKY_RUNTIME=host`), never a fallback. The container runtime stays the
default and its code path is untouched. The host runtime drops what Docker gave: no egress
proxy, no read-only root, no memory or process limits. What it keeps: one outer Seatbelt profile
(`(deny default)`) around the engine and everything it starts, a per-run directory under a
dedicated 0300 root outside the operator home, a from-scratch child environment, process-group
job control verified by start time, the same redactor, transcript and watchdog, and the same
host-side publish validation. A host that cannot sandbox refuses; it never runs unsandboxed.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import secrets
import select
import shutil
import stat
import subprocess
import sys
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

# The container's own paths. The prompts and stored engine sessions name them literally, so the
# host run rewrites them to the per-run directory (and rewrites the session slug back on copy-out).
CONTAINER_WORK = "/work"
PRIVATE_PROMPT_PATH = "/tmp/franky-private-prompt"
# The container cwd slug inside a stored Claude session path (see engine.ClaudeEngine.session_dir).
CONTAINER_SLUG = "-work"

HOST_TIMEOUT_MSG = "franky: host run timed out after {timeout}s"
TERM_GRACE_SECS = 5.0
DRAIN_SECS = 1.5
# A run's processes start no later than this after the recorded start (timeout + setup + slack).
START_WINDOW_SLACK = 600
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_AUTH_MAX_BYTES = 256 * 1024
# `franky-repo-setup.py` ships in the wheel as a symlink-free copy beside this module.
REPO_SETUP_SCRIPT = Path(__file__).with_name("repo_setup_script.py")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"
RUN_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
RUNS_ROOT_VAR = "FRANKY_HOST_RUNS_DIR"
# Programs that talk to launchd, the keychain or the desktop. They are denied exec inside the
# profile (the Mach calls they use cannot be denied by Seatbelt on its own).
_DENY_EXEC = (
    "/bin/launchctl",
    "/usr/bin/security",
    "/usr/bin/osascript",
    "/usr/bin/open",
    "/usr/bin/sandbox-exec",
    "/usr/bin/ssh-add",
)
_MACH_SERVICES = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.logd",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
    "com.apple.dnssd.service",
    "com.apple.SystemConfiguration.configd",
    "com.apple.SystemConfiguration.DNSConfiguration",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.system.notification_center",
    "com.apple.diagnosticd",
    "com.apple.analyticsd",
)


def real_home() -> Path:
    return Path(os.path.realpath(Path.home()))


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def runs_root(env=None) -> Path:
    """The dedicated root of host run directories: outside the operator home, mode 0300 (the
    owner can create and enter, nobody can list it), one 128-bit random directory per run."""
    source = os.environ if env is None else env
    override = source.get(RUNS_ROOT_VAR)
    if override:
        return Path(override)
    return Path("/Users/Shared") / f"franky-host-{os.getuid()}"


def ensure_runs_root(env=None) -> Path:
    """Create (or verify) the runs root; return its realpath. Refuses a root under the home, a
    symlink, a foreign owner or a mode other than 0300."""
    root = runs_root(env)
    parent = Path(os.path.realpath(root.parent))
    root = parent / root.name
    if _under(root, real_home()) or root == real_home():
        raise ConfigError(
            f"{RUNS_ROOT_VAR} must be outside your home directory - refusing ({root})"
        )
    try:
        if not os.path.lexists(root):
            parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(root, 0o300)
            os.chmod(root, 0o300)
        st = os.lstat(root)
    except OSError as exc:
        raise ConfigError(f"cannot create the host runs root {root}: {exc}") from exc
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) != 0o300
    ):
        raise ConfigError(
            f"the host runs root {root} must be a directory you own with mode 0300 - refusing"
        )
    return root


def host_dir_of(record: dict, env=None) -> Path | None:
    """The run directory a record names, only when it is a random-token directory directly under
    the runs root and not a symlink. Never trusts the record's path blindly."""
    raw = record.get("host_dir")
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    if not RUN_TOKEN_RE.match(path.name):
        return None
    try:
        if Path(os.path.realpath(path.parent)) != Path(os.path.realpath(runs_root(env))):
            return None
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return None
    except OSError:
        return None
    return path


def auth_dir(env=None) -> Path:
    """The host Codex subscription store, beside the config file (0700; `auth.json` 0600)."""
    from .userconfig import config_file_path

    return config_file_path(dict(os.environ if env is None else env)).parent / "codex-auth"


def preflight(cfg, which=shutil.which, platform: str = sys.platform) -> None:
    """Refuse a host run that cannot be sandboxed. Never falls back to the container."""
    if platform != "darwin":
        raise ConfigError(
            f"FRANKY_RUNTIME=host needs macOS Seatbelt to confine the engine; {platform} is "
            "not supported - refusing"
        )
    if not Path(SANDBOX_EXEC).exists():
        raise ConfigError(f"FRANKY_RUNTIME=host needs {SANDBOX_EXEC} (Seatbelt) - refusing")
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
    ensure_runs_root()


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
# Engine argv and the OS sandbox: one outer Seatbelt profile for both engines
# ---------------------------------------------------------------------------

_CODEX_BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def claude_control_settings() -> dict:
    """The control settings passed to Claude (with `--setting-sources ""`, so no user, project
    or local settings load): hooks off, Claude's own sandbox off (a nested sandbox is refused;
    the outer profile is the boundary), and the web tools denied. `--dangerously-skip-permissions`
    stays on the argv for headless approval."""
    return {
        "disableAllHooks": True,
        "sandbox": {"enabled": False},
        "permissions": {"deny": ["WebFetch", "WebSearch"]},
    }


def _sb(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def engine_install_roots(engine_bin: Path, home: Path, root: Path) -> list[Path]:
    """Read-only roots the engine launcher needs when it sits under the operator home: its
    resolved directory and, for a node package, the enclosing package directory. A root that
    equals or contains the home or the runs root is refused, never allowed."""
    real = Path(os.path.realpath(engine_bin))
    roots = [real.parent]
    for ancestor in list(real.parents)[:6]:
        if (ancestor / "package.json").is_file():
            roots.append(ancestor)
            break
    out = []
    for r in dict.fromkeys(roots):
        if r == home or _under(home, r) or r == root or _under(root, r):
            continue  # a broad prefix (`/`, `/Users`) is never allowed through
        out.append(r)
    return out


def resolve_engine_bin(engine: str, path: str) -> Path:
    """The engine launcher's absolute, symlink-resolved path (found on the child PATH)."""
    found = shutil.which(engine, path=path)
    if not found:
        raise ConfigError(f"FRANKY_RUNTIME=host: `{engine}` not found on PATH - refusing")
    return Path(os.path.realpath(found))


def seatbelt_profile(
    run: Path,
    home: Path,
    root: Path,
    install_roots: list[Path],
    control_file: Path | None = None,
) -> str:
    """The outer Seatbelt profile around the engine and everything it starts. `(deny default)`.

    Reads: everything outside the operator home, the runs root, `/private/var/folders` and
    `/private/tmp`, plus the run directory, the engine's install roots and the one control
    file. Writes: only the run's `home`, `work` and `tmp` (never the run directory itself, so
    `home` cannot be swapped for a symlink). Network: outbound TCP and the system resolver
    socket only; no other unix sockets. Processes: exec and fork, signal and inspect only
    inside the sandbox (other processes' environments stay unreadable), a short Mach service
    list (no keychain, no launchd). Later rules win in Seatbelt, so the allows come last."""
    L = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow user-preference-read)",
        "(allow ipc-posix-shm)",
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.") '
        '(sysctl-name "kern.ostype" "kern.osrelease" "kern.osversion" "kern.version" '
        '"kern.hostname" "kern.argmax" "kern.maxfilesperproc" "kern.ngroups" "kern.boottime" '
        '"kern.secure_kernel" "vm.loadavg"))',
        "(allow file-read*)",
        "(deny file-read* file-write* "
        f"(subpath {_sb(home)}) (subpath {_sb(root)}) "
        '(subpath "/private/var/folders") (subpath "/private/tmp") (subpath "/private/var/tmp"))',
        f"(allow file-read-metadata (literal {_sb(root)}))",
    ]
    for r in install_roots:
        L.append(f"(allow file-read* (subpath {_sb(r)}))")
    if control_file is not None:
        # The engine vets every component of its settings path: the file and its directory are
        # readable by name, nothing else beside them is.
        L.append(f"(allow file-read-metadata (literal {_sb(control_file.parent)}))")
        L.append(f"(allow file-read* (literal {_sb(control_file)}))")
    L += [
        f"(allow file-read* (subpath {_sb(run)}))",
        "(allow file-write* "
        f"(subpath {_sb(run / 'home')}) (subpath {_sb(run / 'work')}) "
        f"(subpath {_sb(run / 'tmp')}))",
        '(allow file-write-data (literal "/dev/null") (literal "/dev/dtracehelper") '
        '(literal "/dev/tty"))',
        "(allow network-outbound (remote tcp))",
        '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
        "(allow mach-lookup (global-name " + " ".join(f'"{n}"' for n in _MACH_SERVICES) + "))",
        "(deny process-exec " + " ".join(f"(literal {_sb(p)})" for p in _DENY_EXEC) + ")",
        '(deny mach-lookup (global-name-prefix "com.apple.SecurityServer") '
        '(global-name-prefix "com.apple.security") (global-name-prefix "com.apple.xpc.launchd"))',
    ]
    return "\n".join(L) + "\n"


def write_ctl_file(ctl: Path, name: str, text: str) -> Path:
    """Write a read-only (0400) control file into the 0700 control directory."""
    ctl.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ctl / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def engine_argv(
    inner_argv: list[str],
    engine: str,
    *,
    profile: Path,
    engine_bin: Path,
    settings: Path | None = None,
) -> list[str]:
    """Wrap the engine command in the outer profile. The container's sandbox-off flags are
    dropped or replaced; the engine launcher becomes its absolute path (the profile hides the
    home, where a launcher symlink may live). Raises ValueError when the argv has nothing to
    swap (a refusal, never an unsandboxed run)."""
    argv = list(inner_argv)
    if engine == "claude":
        if "claude" not in argv or "--dangerously-skip-permissions" not in argv or settings is None:
            raise ValueError("claude argv has no engine command to sandbox")
        at = argv.index("claude")
        argv[at : at + 1] = [str(engine_bin), "--setting-sources", "", "--settings", str(settings)]
    elif engine == "codex":
        if _CODEX_BYPASS not in argv or "codex" not in argv:
            raise ValueError("codex argv has no sandbox flag to swap")
        argv[argv.index(_CODEX_BYPASS)] = "--sandbox"
        # The container's bypass flag also skipped codex's trusted-directory check (the work
        # directory is empty at launch); the explicit sandbox flag does not.
        argv[argv.index("--sandbox") + 1 : argv.index("--sandbox") + 1] = [
            "danger-full-access",
            "--skip-git-repo-check",
        ]
        argv[argv.index("codex")] = str(engine_bin)
    else:
        raise ValueError(f"engine {engine!r} has no host sandbox")
    at = argv.index(str(engine_bin))
    return [*argv[:at], SANDBOX_EXEC, "-f", str(profile), *argv[at:]]


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
    # The operator's git and gh configuration (osxkeychain credential helper, signing, aliases)
    # never applies: both resolve inside the run.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GH_CONFIG_DIR"] = str(home / ".config" / "gh")
    env["CLAUDE_CODE_TMPDIR"] = str(tmp)
    # TLS roots from a file: the system trust store needs the keychain service, which the
    # profile denies.
    if Path(SYSTEM_CA_BUNDLE).is_file():
        env["SSL_CERT_FILE"] = SYSTEM_CA_BUNDLE
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


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_TREE_DEPTH = 6
_MAX_TREE_ENTRIES = 10000


class _TooLarge(Exception):
    pass


def _open_dir(base: Path, parts) -> int:
    """Open `base`/`parts` one component at a time with O_NOFOLLOW, so no component of a path
    inside a run directory can be a symlink the run planted. The caller closes the fd."""
    fd = os.open(base, _DIR_FLAGS)
    try:
        for part in parts:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd_bounded(dfd: int, name: str, limit: int) -> bytes:
    """Read a regular file by name under a directory fd, never following a link, and fail as
    soon as more than `limit` bytes have been read (the limit applies while reading)."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks, total = [], 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > limit:
                raise _TooLarge()
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_file_nofollow(base: Path, parts, limit: int) -> bytes | None:
    """One regular file at `base`/`parts`, walked without following links; None on any failure."""
    try:
        dfd = _open_dir(base, parts[:-1])
    except OSError:
        return None
    try:
        return _read_fd_bounded(dfd, parts[-1], limit)
    except (OSError, _TooLarge):
        return None
    finally:
        os.close(dfd)


def _copy_tree(dfd: int, out: Path, budget: list[int], depth: int, count: list[int]) -> None:
    if depth > _MAX_TREE_DEPTH:
        raise OSError("tree too deep")
    for name in os.listdir(dfd):
        count[0] += 1
        if count[0] > _MAX_TREE_ENTRIES:
            raise OSError("too many entries")
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISREG(st.st_mode):
            data = _read_fd_bounded(dfd, name, budget[0])
            budget[0] -= len(data)
            out.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        elif stat.S_ISDIR(st.st_mode):
            sub = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            try:
                _copy_tree(sub, out / name, budget, depth + 1, count)
            finally:
                os.close(sub)


def copy_session(base: Path, paths: list[str], dest, slug: str, *, max_bytes: int) -> str:
    """Copy an engine session (`paths`: the session file, then its optional side dir; stored
    under the container slug) out of the run's home into `dest`. Call it only after the run's
    processes are stopped. Every component is opened with O_NOFOLLOW from a directory fd, only
    regular files and directories are read, and the byte limit is enforced while reading.
    Mirrors `snapshot.copy_session`: ok, too_large or failed."""
    if not paths:
        return "failed"
    budget = [max_bytes]
    for index, rel in enumerate(paths):
        parts = ["home", *_host_rel(rel, slug).split("/")]
        try:
            pfd = _open_dir(base, parts[:-1])
        except OSError:
            if index == 0:
                return "failed"
            continue
        try:
            try:
                st = os.stat(parts[-1], dir_fd=pfd, follow_symlinks=False)
            except OSError:
                if index == 0:
                    return "failed"
                continue
            target = Path(dest) / rel
            if stat.S_ISREG(st.st_mode):
                data = _read_fd_bounded(pfd, parts[-1], budget[0])
                budget[0] -= len(data)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            elif stat.S_ISDIR(st.st_mode):
                sub = os.open(parts[-1], _DIR_FLAGS, dir_fd=pfd)
                try:
                    _copy_tree(sub, target, budget, 0, [0])
                finally:
                    os.close(sub)
            elif index == 0:
                return "failed"
        except _TooLarge:
            return "too_large"
        except OSError:
            return "failed"
        finally:
            os.close(pfd)
    try:
        landed = stat.S_ISREG(os.lstat(Path(dest) / paths[0]).st_mode)
    except OSError:
        landed = False
    return "ok" if landed else "failed"


# ---------------------------------------------------------------------------
# Codex subscription auth (host store instead of the Docker volume)
# ---------------------------------------------------------------------------


def _parse_auth(raw: bytes | None) -> dict | None:
    if not raw or len(raw) > _AUTH_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) and payload else None


def _read_auth(path: Path) -> dict | None:
    return _parse_auth(read_file_nofollow(path.parent, [path.name], _AUTH_MAX_BYTES))


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


def account_id(payload: dict) -> str | None:
    """The account id an auth payload names (`account_id` anywhere in it), or None."""
    pending: list = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            found = value.get("account_id")
            if isinstance(found, str) and found:
                return found
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return None


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
        payload = _read_auth(scratch / "auth.json")
        return payload is not None and _store_auth(payload, store)
    except OSError:
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _store_auth(payload: dict, store: Path) -> bool:
    """Atomically replace `<store>/auth.json` with `payload`."""
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = store / f".auth-{uuid.uuid4().hex[:8]}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, store / "auth.json")
    return True


def keep_refreshed_auth(base: Path, original: dict, store: Path) -> bool:
    """After a run: keep the engine's refreshed `auth.json` only when it names the same
    account as the login copied in. The run could have replaced the file with anything."""
    wanted = account_id(original)
    after = _parse_auth(read_file_nofollow(base, ["home", ".codex", "auth.json"], _AUTH_MAX_BYTES))
    if wanted is None or after is None or account_id(after) != wanted:
        return False
    return _store_auth(after, store)


def codex_auth_logout(env=None) -> bool:
    shutil.rmtree(auth_dir(env), ignore_errors=True)
    return not auth_dir(env).exists()


# ---------------------------------------------------------------------------
# Job control: process group identity (macOS has no /proc)
# ---------------------------------------------------------------------------

_PS_ENV = {"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"}


def _lstart_epoch(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def process_table(runner=subprocess.run) -> list[tuple[int, int, str, float]] | None:
    """Every process as (pid, pgid, lstart text, start epoch), or None when `ps` fails."""
    try:
        proc = runner(
            ["ps", "-axo", "pid=,pgid=,lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            env=_PS_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    rows = []
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        epoch = _lstart_epoch(parts[2])
        if epoch is not None:
            rows.append((int(parts[0]), int(parts[1]), " ".join(parts[2].split()), epoch))
    return rows


def proc_start_marker(pid: int, runner=subprocess.run) -> str | None:
    """The process start time text of `pid` (`ps lstart`), a reuse guard."""
    rows = process_table(runner)
    for row in rows or []:
        if row[0] == pid:
            return row[2]
    return None


def group_members(pgid: int, runner=subprocess.run):
    rows = process_table(runner)
    if rows is None:
        return None
    return [(pid, text, epoch) for pid, group, text, epoch in rows if group == pgid]


def new_identity(proc_pid: int, timeout: int, runner=subprocess.run) -> dict:
    """What a record keeps to recognise the run's own process group later."""
    return {
        "pgid": proc_pid,
        "pgid_started_at": proc_start_marker(proc_pid, runner=runner),
        "pgid_started_epoch": time.time(),
        "window_secs": int(timeout) + START_WINDOW_SLACK,
    }


def _identity_ok(ident: dict, members) -> bool:
    """Every member must have started inside the run's window, and the leader (when still
    there) must carry the recorded start time. A pgid reused later fails one of the two."""
    epoch = ident.get("pgid_started_epoch")
    window = ident.get("window_secs")
    if not isinstance(epoch, (int, float)) or not isinstance(window, (int, float)):
        return False
    for pid, text, started in members:
        if not epoch - 5 <= started <= epoch + window:
            return False
        if pid == ident.get("pgid") and ident.get("pgid_started_at") not in (None, text):
            return False
    return True


def group_state(record: dict, *, members_of=group_members) -> bool | None:
    """Is the recorded host process group still the run's own? True / False / None.

    False: no process is left in the group. True: it has members and every one passes the
    identity check. None: it cannot be told (ps failed, a member outside the run's start window,
    a different leader). A None group is reported as running and never signalled."""
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        return None
    members = members_of(pgid)
    if members is None:
        return None
    if not members:
        return False
    return True if _identity_ok(record, members) else None


def signal_group(record: dict, sig: int, *, killpg=os.killpg, members_of=group_members) -> bool:
    """Signal the group only after the identity check passed just now. True when signalled."""
    if group_state(record, members_of=members_of) is not True:
        return False
    try:
        killpg(record["pgid"], sig)
    except OSError:
        return False
    return True


def terminate_group(
    record: dict,
    *,
    killpg=os.killpg,
    members_of=group_members,
    sleeper=time.sleep,
    grace: float = TERM_GRACE_SECS,
) -> None:
    """TERM the group, wait up to `grace` seconds, then KILL it. Each signal is identity-checked
    right before it is sent. Never raises."""
    if not signal_group(record, 15, killpg=killpg, members_of=members_of):
        return
    waited = 0.0
    while waited < grace:
        sleeper(0.25)
        waited += 0.25
        if group_state(record, members_of=members_of) is not True:
            break
    signal_group(record, 9, killpg=killpg, members_of=members_of)


def escaped_pids(base: Path, ident: dict, runner=subprocess.run) -> list[int]:
    """Processes outside the run's group (a `setsid` child) whose working directory is inside the
    run directory and whose start time is inside the run's window."""
    try:
        proc = runner(
            ["lsof", "-nP", "-w", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=20,
            env=_PS_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = process_table(runner)
    if rows is None or getattr(proc, "returncode", 1) not in (0, 1):
        return []
    starts = {pid: epoch for pid, _g, _t, epoch in rows}
    prefix = str(base)
    found, pid = [], None
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwd = line[1:]
            started = starts.get(pid)
            if (
                (cwd == prefix or cwd.startswith(prefix + "/"))
                and started is not None
                and ident["pgid_started_epoch"] - 5
                <= started
                <= ident["pgid_started_epoch"] + ident["window_secs"]
                and pid != os.getpid()
            ):
                found.append(pid)
    return found


def kill_escaped(base: Path, ident: dict, *, runner=subprocess.run, sleeper=time.sleep) -> None:
    """Stop run descendants that left the process group (found by cwd, start time checked)."""
    pids = escaped_pids(base, ident, runner)
    for sig in (15, 9):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if sig == 15 and pids:
            sleeper(0.5)


def teardown(base: Path, ident: dict, **kw) -> None:
    """Stop everything the run started: its verified group, then escaped descendants."""
    terminate_group(ident, **{k: v for k, v in kw.items() if k != "runner"})
    kill_escaped(base, ident, runner=kw.get("runner", subprocess.run))


def kill_run(record: dict, **kw) -> bool:
    """Stop a host run by its verified process group. Returns True when it was signalled.
    An unverifiable group is never signalled."""
    if group_state(record, members_of=kw.get("members_of", group_members)) is not True:
        return False
    terminate_group(record, **kw)
    return True


def remove_run_dir(record: dict, env=None) -> None:
    """Delete a run's directory and its control directory (never follows a link; never raises)."""
    base = host_dir_of(record, env)
    if base is None:
        return
    snapshot._rmtree(base)
    _rm_ctl(base.parent / f"{base.name}.ctl")


def _rm_ctl(ctl: Path) -> None:
    """Remove a control directory whose files are 0400."""
    try:
        os.chmod(ctl, 0o700)
    except OSError:
        return
    snapshot._rmtree(ctl)


def deliver_steer(record: dict, message: str, env=None) -> bool:
    """Append an operator correction to the run's steer file, walking the path with O_NOFOLLOW."""
    base = host_dir_of(record, env)
    if base is None:
        return False
    try:
        dfd = _open_dir(base, ["home"])
    except OSError:
        return False
    try:
        fd = os.open(
            Path(STEER_FILE).name,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
            dir_fd=dfd,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(message)
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


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
    disk and callbacks, a hard wall-clock deadline that also bounds the output reader (a `setsid`
    child holding the pipe cannot hang it; timeout -> 124), repository setup before the engine
    (failure -> 78), a thread session copied out on a clean exit (or on timeout when the sink opts
    in). Every exit path stops the run's verified process group, and any descendant that left it,
    before the run directory is touched. Snapshots, workspace export and resume need a container
    filesystem and are refused by the caller (exit 3).
    """
    for name in ("resume_workspace", "workspace_sink"):
        if unsupported.get(name) is not None:
            raise ConfigError(f"FRANKY_RUNTIME=host does not support {name}")
    run_id = run_id or uuid.uuid4().hex[:12]
    os_env = os.environ if env is None else env
    root = ensure_runs_root(runs_env)
    home_dir = real_home()
    token = secrets.token_hex(16)
    base = root / token
    ctl = root / f"{token}.ctl"
    os.mkdir(base, 0o700)
    home, work, tmp = base / "home", base / "work", base / "tmp"
    for d in (home, work, tmp):
        d.mkdir(mode=0o700)

    secret_values = [*cfg.secret_values(), *extra_secrets]
    output = Transcript()
    stream = Redactor(secret_values)
    pending = ""
    code = 1
    ident = None
    finished = False
    codex_payload = None

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
        if cfg.engine.name == "codex":
            (home / ".codex").mkdir(mode=0o700, exist_ok=True)
        if cfg.engine.name == "codex" and cfg.auth_volume:
            codex_payload = _read_auth(auth_dir(runs_env) / "auth.json")
            if codex_payload is None:
                emit(
                    "franky: Codex subscription login is missing or invalid - "
                    "run `franky auth login codex`\n",
                    final=True,
                )
                return 1, output
            secret_values.extend(auth_strings(codex_payload))
            stream = Redactor(secret_values)
            fd = os.open(
                home / ".codex" / "auth.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(codex_payload, fh)

        env_map = child_env(cfg, home, tmp, os_env)
        engine_bin = resolve_engine_bin(cfg.engine.name, env_map["PATH"])
        settings = None
        if cfg.engine.name == "claude":
            settings = write_ctl_file(
                ctl, "claude-settings.json", json.dumps(claude_control_settings())
            )
        profile = write_ctl_file(
            ctl,
            "profile.sb",
            seatbelt_profile(
                base, home_dir, root, engine_install_roots(engine_bin, home_dir, root), settings
            ),
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
            argv = engine_argv(
                argv, cfg.engine.name, profile=profile, engine_bin=engine_bin, settings=settings
            )
        except ValueError as exc:
            raise ConfigError(f"FRANKY_RUNTIME=host refuses: {exc}") from exc

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
        ident = new_identity(proc.pid, timeout, runner=runner)
        jobs.update_record(run_id, {"runtime": "host", **ident, "host_dir": str(base)}, runs_env)

        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = proc.stdout.fileno()
        deadline = t_start + timeout
        exited_at = None
        timed_out = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([fd], [], [], min(left, 0.5))
            if ready:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    break
                emit(decoder.decode(chunk))
            elif proc.poll() is not None:
                # The leader is gone and nothing is left to read: a straggler holding the pipe
                # gets a short drain, then the reader stops regardless.
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at > DRAIN_SECS:
                    break
        if not timed_out and proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            terminate_group(ident)
        try:
            proc.wait(timeout=TERM_GRACE_SECS + 5)
        except subprocess.TimeoutExpired:
            pass
        emit(decoder.decode(b"", final=True), final=True)
        if timed_out:
            code = CONTAINER_TIMEOUT_CODE
            output.write("\n" + HOST_TIMEOUT_MSG.format(timeout=timeout))
        else:
            code = proc.returncode if proc.returncode is not None else 1
        finished = True
    except BaseException:
        output.close()
        raise
    finally:
        try:
            if ident is not None:
                # Stop the producers before anything is read back, and before any dir goes.
                teardown(base, ident, runner=runner)
                try:
                    proc.stdout.close()
                except (OSError, NameError, UnboundLocalError):
                    pass
            if finished:
                try:
                    if session_sink is not None and (
                        code == 0
                        or (session_sink.get("on_timeout") and code == CONTAINER_TIMEOUT_CODE)
                    ):
                        session_sink["status"] = copy_session(
                            base,
                            session_sink["paths"],
                            session_sink["dest"],
                            work_slug(work),
                            max_bytes=session_sink["max_bytes"],
                        )
                    if codex_payload is not None:
                        keep_refreshed_auth(base, codex_payload, auth_dir(runs_env))
                except Exception:
                    pass
        finally:
            snapshot._rmtree(base)
            _rm_ctl(ctl)
    return code, output
