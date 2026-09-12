"""Host-side, fixed-scope branch push for `franky run-skill --push-branch`.

WHY the push lives on the HOST and not in the container: the task container's `GH_TOKEN` is
read-only by design, and nothing in Franky ever hands an autonomous agent a write credential.

WHY the host never runs git inside the container's checkout: that checkout is AUTHORED BY THE
CONTAINER. `.git/hooks/*`, `core.fsmonitor`, `core.sshCommand`, `core.pager`,
`url.<x>.insteadOf`, a rewritten `remote.origin.pushurl`, `push.followTags`, a forged
`origin/HEAD` - every one of them turns a host git command into container-chosen code execution
or a container-chosen destination, with the push token in the environment. So the container hands
over ONE inert artifact instead: a git bundle of exactly `refs/heads/<branch>`, produced by a
networkless sandbox (`container.bundle_workspace`). The host verifies that file and unpacks it
into a bare repository IT created from an EMPTY template, with a minimal environment, and pushes
to a URL IT derived from the validated `--repo`. Nothing is read out of the container's config.

The push is deliberately narrow, and every gate refuses rather than forces:
  - the bundle must pass `git bundle verify` and carry the branch;
  - the destination URL is `https://github.com/<owner>/<repo>.git`, built by the host;
  - the default branch comes from the GitHub API, never from the bundle;
  - the branch must not BE that default branch;
  - an optional `--require-ancestor SHA` must be contained in it;
  - the push must be a fast-forward over the real `refs/heads/<branch>` on the remote;
  - the history must pass `snapshot.verify_no_secrets` before anything is published;
  - the refspec is exactly `<sha>:refs/heads/<branch>` with tag-following off, no force, no lease.

Pure argv builders plus one entry point (`push_branch`) with an injectable `runner`, so the unit
suite exercises every gate without git, gh, network, or credentials.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import snapshot
from .config import PUSH_TOKEN_VAR, redact

__all__ = [
    "PUSH_TOKEN_VAR",
    "push_branch",
    "remote_url",
    "valid_branch",
    "valid_sha",
]

# A git branch name Franky is willing to push. Stricter than git's own refname rules on purpose:
# it must start alphanumeric (so no leading `-` can be read as a flag), carries no space or
# control character, and `..`/`@{`/a `.lock` tail are rejected below. It is also interpolated
# into the sandbox bundler's argv, so keeping it metacharacter-free is load-bearing there too.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# owner/repo, stricter than `config.repo_allowed`'s shape check: each segment must START
# alphanumeric, so `.`, `..`, and any other dot-leading segment are rejected outright. With a
# permissive allowlist entry (the bare `*`), a repo like `../evil` would otherwise reach
# `remote_url` and build `https://github.com/../evil.git` - a different destination than the one
# that was authorized.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")

# 7-40 hex chars, the same shape `review-pr --expected-head-sha` accepts.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A git branch name GitHub will report as a default branch. Validated before it is ever compared
# or interpolated, so a surprising API response cannot smuggle anything through.
_DEFAULT_BRANCH_RE = _BRANCH_RE

# `git push` stderr that means "GitHub said no, and it will say no again": a protected branch, a
# non-fast-forward, a token without permission. Permanent -> push_refused (4). Anything else is
# treated as transport -> push_failed (8), which a caller may retry.
_PERMANENT_PUSH_ERRORS = ("rejected", "protected branch", "non-fast-forward", "permission")

_STDERR_MAX = 500


def valid_branch(branch: str) -> bool:
    """True iff `branch` is a branch name Franky will push (see `_BRANCH_RE`'s WHY)."""
    if not branch or branch == "HEAD":
        return False
    if ".." in branch or "@{" in branch or branch.endswith((".lock", "/", ".")):
        return False
    return bool(_BRANCH_RE.match(branch))


def valid_sha(sha: str) -> bool:
    """True iff `sha` is a bare 7-40 char hex git SHA."""
    return bool(_SHA_RE.match(sha or ""))


def remote_url(repo: str) -> str:
    """The canonical push URL, built by the HOST from the allowlisted `owner/repo`.

    Never read from the checkout: `remote.origin.url`, `remote.origin.pushurl`, and
    `url.<base>.insteadOf` are all container-writable, so any of them could redirect the push to
    an attacker's host with the write token attached.
    """
    if not _REPO_RE.match(repo or ""):
        raise ValueError(f"invalid repo {repo!r}")
    return f"https://github.com/{repo}.git"


def credential_args() -> list[str]:
    """`-c` args pinning git's credential helper to `gh auth git-credential`.

    The first assignment CLEARS any inherited helper and the second installs the `gh` one, which
    reads the token from `GH_TOKEN` in the child ENV. The token is therefore never on an argv and
    never visible in `ps`. (The host environment is already minimal - see `_base_env` - so this
    is belt and braces, which is the right amount for a write credential.)
    """
    return ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


def push_refspec_argv(repo_dir: str | Path, url: str, sha: str, branch: str) -> list[str]:
    """The ONLY push Franky ever runs: one commit, one full destination ref, nothing implicit.

    `push.followTags=false` is explicit because a container-authored history can carry tags that
    a `true` inherited anywhere would drag along with the push.
    """
    return [
        "git",
        "-C",
        str(repo_dir),
        "-c",
        "push.followTags=false",
        *credential_args(),
        "push",
        url,
        f"{sha}:refs/heads/{branch}",
    ]


def _base_env(home: Path) -> dict[str, str]:
    """The ONLY environment any host git/gh subprocess gets. Never `os.environ`.

    `os.environ` carries the operator's whole session - engine credentials, JIRA tokens, the
    read `GH_TOKEN`, and any `GIT_*` override they happen to have set. Handing that to a git
    command operating on container-derived history is exactly the leak this module exists to
    prevent, so the environment is built from nothing: a PATH, a private HOME, and the three
    switches that keep git from reading system or global configuration at all.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
    }


def _run(runner, argv, env, timeout):
    """Run one argv, returning (rc, stdout, stderr). A spawn failure degrades to rc 1."""
    try:
        proc = runner(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=dict(env),
        )
    except Exception as exc:  # OSError (no git/gh), TimeoutExpired, ...
        return 1, "", str(exc)
    return (
        getattr(proc, "returncode", 1),
        getattr(proc, "stdout", "") or "",
        getattr(proc, "stderr", "") or "",
    )


def _outcome(status: str, reason: str, *, sha: str | None = None, branch: str | None = None):
    return {"status": status, "reason": reason, "branch": branch, "sha": sha}


def _ls_remote_sha(url, branch, env, runner, timeout) -> tuple[int, str | None]:
    """(rc, sha) for `refs/heads/<branch>` on the remote. sha is None when the ref is absent."""
    rc, out, _err = _run(
        runner,
        ["git", *credential_args(), "ls-remote", url, f"refs/heads/{branch}"],
        env,
        timeout,
    )
    if rc != 0:
        return rc, None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
            return 0, parts[0].strip()
    return 0, None


def push_branch(
    *,
    bundle_path: str | Path,
    repo: str,
    branch: str,
    push_token: str,
    read_env: Mapping[str, str] | None = None,
    require_ancestor: str | None = None,
    secrets: list[str] | None = None,
    runner=subprocess.run,
    timeout: float = 120.0,
) -> dict:
    """Unpack `bundle_path` into a host-owned repo and push `branch`, fast-forward only.

    Returns `{status, reason, branch, sha}` where status is one of:
      - `pushed`                - the push landed and the remote head matches; `sha` says which.
      - `push_refused`          - a gate said no, or GitHub refused permanently (exit 4).
      - `push_failed`           - the push or a remote read failed transiently (exit 8).
      - `workspace_unavailable` - the bundle is missing or unusable (exit 6).

    `push_token` reaches git ONLY through the child env of the three subprocesses that talk to
    the remote (fetch, ls-remote, push); it is never on an argv. `read_env` supplies the
    read-only `GH_TOKEN` used for the `gh api` default-branch lookup and nothing else. `secrets`
    is the full redaction set - every captured stderr is scrubbed against it before it is
    returned, so a `reason` is always safe to log.
    """
    secrets = list(secrets or [])
    try:
        url = remote_url(repo)
    except ValueError as exc:
        return _outcome("push_refused", str(exc))
    if not valid_branch(branch):
        return _outcome("push_refused", f"branch {branch!r} is not a branch name Franky will push")
    if require_ancestor is not None and not valid_sha(require_ancestor):
        return _outcome(
            "push_refused", f"--require-ancestor {require_ancestor!r} is not a 7-40 char hex sha"
        )
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        return _outcome("workspace_unavailable", "no workspace bundle was produced - refusing")

    # Everything the host git touches lives under a private 0700 directory we just created: an
    # EMPTY template (so no host hook or config is copied into the new repo), a private HOME, and
    # the bare repo itself.
    workdir = Path(tempfile.mkdtemp(prefix="franky-push-"))
    try:
        template = workdir / "template"
        home = workdir / "home"
        trusted = workdir / "trusted.git"
        template.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        env = _base_env(home)
        push_env = {**env, "GH_TOKEN": push_token}
        api_env = {**env, "GH_TOKEN": (read_env or {}).get("GH_TOKEN", "")}

        def refuse(reason: str) -> dict:
            return _outcome("push_refused", reason)

        def scrub(text: str) -> str:
            return redact((text or "").strip(), secrets)[:_STDERR_MAX]

        # 1. A repository the HOST owns, from an EMPTY template.
        rc, _out, err = _run(
            runner,
            ["git", "init", "--bare", f"--template={template}", str(trusted)],
            env,
            timeout,
        )
        if rc != 0:
            return _outcome(
                "workspace_unavailable", f"could not create the push repo: {scrub(err)}"
            )

        # 2. The bundle is an inert FILE. Verify it, then unpack exactly the one branch. No hook,
        # config, or ref from the container's checkout comes with it.
        rc, _out, err = _run(
            runner, ["git", "-C", str(trusted), "bundle", "verify", str(bundle_path)], env, timeout
        )
        if rc != 0:
            return _outcome("workspace_unavailable", f"workspace bundle is invalid: {scrub(err)}")
        rc, _out, err = _run(
            runner,
            [
                "git",
                "-C",
                str(trusted),
                "fetch",
                str(bundle_path),
                f"refs/heads/{branch}:refs/heads/{branch}",
            ],
            env,
            timeout,
        )
        if rc != 0:
            return _outcome(
                "workspace_unavailable",
                f"workspace bundle does not carry refs/heads/{branch}: {scrub(err)}",
            )

        # 3. The default branch comes from GitHub itself, with the READ token. Reading it from
        # the bundle would let the container forge `origin/HEAD` and get the default branch
        # pushed. Unavailable or malformed -> refuse (fail-closed).
        rc, out, err = _run(
            runner, ["gh", "api", f"repos/{repo}", "--jq", ".default_branch"], api_env, timeout
        )
        default_branch = out.strip()
        if rc != 0 or not _DEFAULT_BRANCH_RE.match(default_branch):
            return refuse(f"could not read {repo}'s default branch from GitHub: {scrub(err)}")
        if default_branch == branch:
            return refuse(f"{branch!r} is {repo}'s default branch - refusing to push it")

        # 4. Optional ancestry pin: the caller's known-good commit must be contained in the branch.
        if require_ancestor:
            rc, _out, _err = _run(
                runner,
                [
                    "git",
                    "-C",
                    str(trusted),
                    "merge-base",
                    "--is-ancestor",
                    require_ancestor,
                    f"refs/heads/{branch}",
                ],
                env,
                timeout,
            )
            if rc != 0:
                return refuse(f"{require_ancestor} is not an ancestor of {branch!r} - refusing")

        # 5. Fast-forward only, against the REAL remote branch (full ref names throughout, so a
        # tag sharing the branch's name can never be compared instead).
        rc, _out, err = _run(
            runner,
            [
                "git",
                "-C",
                str(trusted),
                *credential_args(),
                "fetch",
                url,
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            push_env,
            timeout,
        )
        if rc != 0:
            # Either the branch does not exist remotely yet (fine - a new branch is trivially a
            # fast-forward) or the remote is unreachable (not fine).
            ls_rc, remote_sha = _ls_remote_sha(url, branch, push_env, runner, timeout)
            if ls_rc != 0:
                return _outcome("push_failed", f"could not read {repo} on GitHub: {scrub(err)}")
            if remote_sha is not None:
                return _outcome(
                    "push_failed", f"could not fetch origin/{branch} from GitHub: {scrub(err)}"
                )
        else:
            rc, _out, _err = _run(
                runner,
                [
                    "git",
                    "-C",
                    str(trusted),
                    "merge-base",
                    "--is-ancestor",
                    f"refs/remotes/origin/{branch}",
                    f"refs/heads/{branch}",
                ],
                env,
                timeout,
            )
            if rc != 0:
                return refuse(
                    f"{branch!r} is not a fast-forward over origin/{branch} - refusing "
                    "(Franky never forces)"
                )

        # 6. Nothing is published before the history is scanned. Fail-closed and value-free: the
        # finding LABEL says where, never what. No scrub - scrubbing would change what is pushed.
        try:
            findings = snapshot.verify_no_secrets(trusted, list(secrets), runner=runner)
        except Exception:
            findings = ["workspace verification failed"]
        if findings:
            return refuse(f"refusing to push - secret scan flagged the history: {findings[0]}")

        # 7. Resolve what we are about to publish BEFORE publishing it, and push that exact
        # commit by id so the refspec cannot resolve to anything else in between.
        rc, out, err = _run(
            runner, ["git", "-C", str(trusted), "rev-parse", f"refs/heads/{branch}"], env, timeout
        )
        sha = out.strip()
        if rc != 0 or not _FULL_SHA_RE.match(sha):
            return refuse(f"could not resolve refs/heads/{branch} in the push repo: {scrub(err)}")

        rc, _out, err = _run(
            runner, push_refspec_argv(trusted, url, sha, branch), push_env, timeout
        )
        if rc != 0:
            detail = scrub(err)
            lowered = detail.lower()
            if any(marker in lowered for marker in _PERMANENT_PUSH_ERRORS):
                return refuse(f"GitHub refused the push: {detail}")
            return _outcome("push_failed", f"git push failed: {detail}", branch=branch)

        # 8. Confirm what actually landed rather than trusting the push's own exit code.
        ls_rc, remote_sha = _ls_remote_sha(url, branch, push_env, runner, timeout)
        if ls_rc != 0 or remote_sha != sha:
            return _outcome(
                "push_failed",
                "remote head does not match after push",
                branch=branch,
                sha=sha,
            )
        return _outcome("pushed", f"pushed {branch} to {repo}", branch=branch, sha=sha)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
