"""Host-side push gates for `franky run-skill --push-branch` (franky/push.py).

Hermetic: every test injects a fake `runner`, so nothing here touches git, gh, the network, or a
credential. The gates ARE the security surface, so each one gets a test proving it refuses and
that no push argv was ever produced. The load-bearing invariants asserted throughout:

  - no host git/gh subprocess ever receives `os.environ` (it gets the minimal env, and the push
    token only where the remote is actually contacted);
  - nothing is read out of the container's checkout: the destination URL is derived from --repo
    and the default branch comes from `gh api`.
"""

import subprocess

import pytest

from franky import push

REPO = "me/repo"
URL = "https://github.com/me/repo.git"
TOKEN = "ghp_push_token_value"
READ_TOKEN = "ghp_read_only_token"
BRANCH = "staging"
SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _ok(argv, stdout=""):
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _fail(argv, stderr=""):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)


def _bundle(tmp_path):
    path = tmp_path / "work.bundle"
    path.write_bytes(b"fake-bundle-bytes")
    return path


def _runner(calls, *, overrides=None, default_branch="main", remote_sha=None, post_push_sha=SHA):
    """A fake `subprocess.run` that satisfies the happy path and records every argv + env.

    Stateful where reality is: `ls-remote` reports `remote_sha` before the push and
    `post_push_sha` after it, so the new-branch path and the post-push confirmation are both
    exercised honestly. `git init --bare` really creates a bare layout, so `verify_no_secrets`
    runs against a real tree rather than a stub.
    """
    overrides = overrides or {}
    state = {"pushed": False}

    def runner(argv, **kwargs):
        calls.append({"argv": list(argv), "env": kwargs.get("env") or {}})
        joined = " ".join(str(a) for a in argv)
        for key, response in overrides.items():
            if key in joined:
                return response(argv) if callable(response) else response
        if argv[:3] == ["git", "init", "--bare"]:
            from pathlib import Path

            root = Path(argv[-1])
            (root / "objects").mkdir(parents=True)
            (root / "refs").mkdir()
            (root / "HEAD").write_text("ref: refs/heads/main\n")
            return _ok(argv)
        if argv[:2] == ["gh", "api"]:
            return _ok(argv, default_branch + "\n")
        if "ls-remote" in argv:
            sha = post_push_sha if state["pushed"] else remote_sha
            return _ok(argv, f"{sha}\trefs/heads/{BRANCH}\n" if sha else "")
        if "push" in argv:
            state["pushed"] = True
            return _ok(argv)
        if "rev-parse" in argv:
            return _ok(argv, SHA + "\n")
        return _ok(argv)

    return runner


def _push(tmp_path, runner, **kwargs):
    return push.push_branch(
        bundle_path=_bundle(tmp_path),
        repo=REPO,
        branch=BRANCH,
        push_token=TOKEN,
        read_env={"GH_TOKEN": READ_TOKEN},
        secrets=[TOKEN, READ_TOKEN],
        runner=runner,
        **kwargs,
    )


def _argvs(calls):
    return [" ".join(str(a) for a in c["argv"]) for c in calls]


def _pushes(calls):
    return [c for c in calls if "push" in c["argv"]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch,ok",
    [
        ("staging", True),
        ("release/2026-01", True),
        ("fix_thing-1.2", True),
        ("HEAD", False),
        ("--force", False),
        ("-staging", False),
        ("a..b", False),
        ("has space", False),
        ("bad\nname", False),
        ("ctrl\x01", False),
        ("main@{1}", False),
        ("x.lock", False),
        ("", False),
    ],
)
def test_valid_branch(branch, ok):
    assert push.valid_branch(branch) is ok


def test_remote_url_is_derived_from_the_repo_never_read_from_a_checkout():
    assert push.remote_url("me/repo") == URL
    for bad in ("me", "me/repo/extra", "me repo", "", "../evil"):
        with pytest.raises(ValueError):
            push.remote_url(bad)


def test_push_refspec_argv_is_exactly_one_commit_and_never_forces():
    argv = push.push_refspec_argv("/w/trusted.git", URL, SHA, BRANCH)
    assert argv[-3:] == ["push", URL, f"{SHA}:refs/heads/{BRANCH}"]
    assert "--force" not in argv
    assert "--force-with-lease" not in argv
    assert "--tags" not in argv
    # A container-authored history can carry tags; nothing implicit rides along.
    assert "push.followTags=false" in argv
    # The helper is cleared first, then pinned - order is load-bearing.
    assert argv.index("credential.helper=") < argv.index(
        "credential.helper=!gh auth git-credential"
    )


def test_base_env_is_minimal_and_disables_system_and_global_config(tmp_path):
    env = push._base_env(tmp_path)
    assert set(env) == {
        "PATH",
        "HOME",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "LANG",
    }
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GH_TOKEN" not in env


# ---------------------------------------------------------------------------
# The push itself
# ---------------------------------------------------------------------------


def test_push_happy_path_runs_every_gate(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, remote_sha=SHA), require_ancestor="abc1234")

    assert out["status"] == "pushed"
    assert out["branch"] == BRANCH
    assert out["sha"] == SHA

    joined = _argvs(calls)
    assert any("init --bare --template=" in c for c in joined)
    assert any("bundle verify" in c for c in joined)
    assert any(
        f"fetch {tmp_path}/work.bundle refs/heads/{BRANCH}:refs/heads/{BRANCH}" in c for c in joined
    )
    assert any(f"gh api repos/{REPO} --jq .default_branch" in c for c in joined)
    assert any(f"merge-base --is-ancestor abc1234 refs/heads/{BRANCH}" in c for c in joined)
    assert any(
        f"fetch {URL} +refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}" in c for c in joined
    )
    assert any(
        f"merge-base --is-ancestor refs/remotes/origin/{BRANCH} refs/heads/{BRANCH}" in c
        for c in joined
    )
    assert any(f"push {URL} {SHA}:refs/heads/{BRANCH}" in c for c in joined)
    # The remote is never asked where it lives, and origin/HEAD is never consulted.
    assert not any("remote get-url" in c for c in joined)
    assert not any("symbolic-ref" in c for c in joined)


def test_no_host_subprocess_ever_gets_the_ambient_environment(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, remote_sha=SHA))
    assert out["status"] == "pushed"
    allowed = {
        "PATH",
        "HOME",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "LANG",
        "GH_TOKEN",
    }
    for call in calls:
        if call["argv"][0] not in ("git", "gh"):
            continue  # verify_no_secrets' python object scan passes no env at all
        assert set(call["env"]) <= allowed, call["argv"]
        assert call["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_push_token_is_scoped_to_the_remote_calls_only(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, remote_sha=SHA))
    assert out["status"] == "pushed"

    def env_of(fragment):
        return [c["env"] for c in calls if fragment in " ".join(str(a) for a in c["argv"])]

    # Exactly the three commands that contact GitHub carry the write token.
    for env in env_of(f"push {URL}") + env_of(f"fetch {URL}") + env_of("ls-remote"):
        assert env.get("GH_TOKEN") == TOKEN
    # The API lookup uses the READ token, and local-only git never sees either.
    assert env_of("gh api")[0]["GH_TOKEN"] == READ_TOKEN
    for env in env_of("bundle verify") + env_of("init --bare"):
        assert "GH_TOKEN" not in env
    # The token value never lands on an argv (it would be visible in `ps`).
    assert not any(TOKEN in str(a) for c in calls for a in c["argv"])


def test_push_refused_without_a_bundle(tmp_path):
    calls = []
    out = push.push_branch(
        bundle_path=tmp_path / "missing.bundle",
        repo=REPO,
        branch=BRANCH,
        push_token=TOKEN,
        runner=_runner(calls),
    )
    assert out["status"] == "workspace_unavailable"
    assert calls == []


def test_invalid_bundle_is_workspace_unavailable(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, overrides={"bundle verify": _fail([], "not a bundle")}))
    assert out["status"] == "workspace_unavailable"
    assert not _pushes(calls)


def test_bundle_without_the_branch_is_workspace_unavailable(tmp_path):
    calls = []
    runner = _runner(
        calls, overrides={f"refs/heads/{BRANCH}:refs/heads/{BRANCH}": _fail([], "no such ref")}
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "workspace_unavailable"
    assert not _pushes(calls)


def test_push_refused_on_the_default_branch(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, default_branch=BRANCH))
    assert out["status"] == "push_refused"
    assert "default branch" in out["reason"]
    assert not _pushes(calls)


def test_push_refused_when_the_api_cannot_name_the_default_branch(tmp_path):
    """Fail-closed, and never fall back to anything the container could have forged."""
    calls = []
    out = _push(tmp_path, _runner(calls, overrides={"gh api": _fail([], "404")}))
    assert out["status"] == "push_refused"
    assert "default branch" in out["reason"]
    assert not _pushes(calls)


def test_push_refused_on_a_malformed_default_branch(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, overrides={"gh api": _ok([], "--evil\n")}))
    assert out["status"] == "push_refused"
    assert not _pushes(calls)


def test_push_refused_when_required_ancestor_is_missing(tmp_path):
    calls = []
    runner = _runner(calls, overrides={"merge-base --is-ancestor abc1234": _fail([])})
    out = _push(tmp_path, runner, require_ancestor="abc1234")
    assert out["status"] == "push_refused"
    assert "ancestor" in out["reason"]
    assert not _pushes(calls)


def test_push_refused_on_non_fast_forward(tmp_path):
    calls = []
    runner = _runner(
        calls,
        remote_sha=SHA,
        overrides={f"merge-base --is-ancestor refs/remotes/origin/{BRANCH}": _fail([])},
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "push_refused"
    assert "fast-forward" in out["reason"]
    assert not _pushes(calls)


def test_a_branch_that_is_new_remotely_skips_the_fast_forward_check(tmp_path):
    calls = []
    runner = _runner(
        calls,
        remote_sha=None,
        overrides={f"+refs/heads/{BRANCH}": _fail([], "couldn't find remote ref")},
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "pushed"


def test_an_unreachable_remote_is_push_failed_not_a_new_branch(tmp_path):
    calls = []
    runner = _runner(
        calls,
        overrides={
            f"+refs/heads/{BRANCH}": _fail([], "could not resolve host"),
            "ls-remote": _fail([], "could not resolve host"),
        },
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "push_failed"
    assert not _pushes(calls)


def test_a_fetch_failure_with_an_existing_remote_branch_is_push_failed(tmp_path):
    calls = []
    runner = _runner(
        calls, remote_sha=SHA, overrides={f"+refs/heads/{BRANCH}": _fail([], "shallow update")}
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "push_failed"
    assert not _pushes(calls)


def test_a_secret_scan_finding_refuses_the_push(tmp_path, monkeypatch):
    monkeypatch.setattr(
        push.snapshot, "verify_no_secrets", lambda *a, **k: ["secret value survived in config"]
    )
    calls = []
    out = _push(tmp_path, _runner(calls, remote_sha=SHA))
    assert out["status"] == "push_refused"
    assert "secret scan" in out["reason"]
    assert "config" in out["reason"]
    assert TOKEN not in out["reason"]
    assert not _pushes(calls)


def test_a_secret_scan_crash_refuses_the_push(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(push.snapshot, "verify_no_secrets", boom)
    calls = []
    out = _push(tmp_path, _runner(calls, remote_sha=SHA))
    assert out["status"] == "push_refused"
    assert not _pushes(calls)


def test_the_secret_scan_runs_against_the_trusted_bare_repo(tmp_path, monkeypatch):
    seen = {}

    def capture(root, secrets, runner=None):
        seen["root"] = root
        seen["secrets"] = list(secrets)
        seen["bare"] = push.snapshot._is_bare_repo(root)
        return []

    monkeypatch.setattr(push.snapshot, "verify_no_secrets", capture)
    out = _push(tmp_path, _runner([], remote_sha=SHA))
    assert out["status"] == "pushed"
    assert seen["root"].name == "trusted.git"
    assert seen["bare"] is True
    assert TOKEN in seen["secrets"]


@pytest.mark.parametrize(
    "stderr",
    [
        "! [remote rejected] staging -> staging (protected branch hook declined)",
        "Updates were rejected because the tip is behind (non-fast-forward)",
        "remote: Permission to me/repo.git denied",
    ],
)
def test_a_permanent_github_refusal_is_push_refused(tmp_path, stderr):
    calls = []
    out = _push(
        tmp_path, _runner(calls, remote_sha=SHA, overrides={f"push {URL}": _fail([], stderr)})
    )
    assert out["status"] == "push_refused"
    assert "GitHub refused" in out["reason"]


def test_a_transport_failure_is_push_failed_and_redacts_the_token(tmp_path):
    calls = []
    runner = _runner(
        calls,
        remote_sha=SHA,
        overrides={f"push {URL}": _fail([], f"fatal: unable to access (token {TOKEN})")},
    )
    out = _push(tmp_path, runner)
    assert out["status"] == "push_failed"
    assert TOKEN not in out["reason"]
    assert "***REDACTED***" in out["reason"]


def test_a_remote_head_that_does_not_match_after_the_push_is_push_failed(tmp_path):
    calls = []
    # The post-push confirmation reports a different commit than the one we pushed.
    runner = _runner(calls, remote_sha=SHA, post_push_sha="f" * 40)
    out = _push(tmp_path, runner)
    assert out["status"] == "push_failed"
    assert "remote head does not match" in out["reason"]


def test_an_unresolvable_branch_sha_refuses_before_pushing(tmp_path):
    calls = []
    out = _push(tmp_path, _runner(calls, overrides={"rev-parse": _ok([], "not-a-sha\n")}))
    assert out["status"] == "push_refused"
    assert not _pushes(calls)


def test_push_refused_on_a_bad_branch_name_without_running_anything(tmp_path):
    calls = []
    out = push.push_branch(
        bundle_path=_bundle(tmp_path),
        repo=REPO,
        branch="--force",
        push_token=TOKEN,
        runner=_runner(calls),
    )
    assert out["status"] == "push_refused"
    assert calls == []


def test_push_refused_on_an_off_shape_repo_without_running_anything(tmp_path):
    calls = []
    out = push.push_branch(
        bundle_path=_bundle(tmp_path),
        repo="me/repo/../attacker/repo",
        branch=BRANCH,
        push_token=TOKEN,
        runner=_runner(calls),
    )
    assert out["status"] == "push_refused"
    assert calls == []


def test_the_workdir_is_removed_whatever_happens(tmp_path, monkeypatch):
    seen = {}
    real = push.tempfile.mkdtemp

    def capture(*a, **k):
        seen["dir"] = real(*a, **k)
        return seen["dir"]

    monkeypatch.setattr(push.tempfile, "mkdtemp", capture)
    out = _push(tmp_path, _runner([], overrides={"bundle verify": _fail([], "nope")}))
    assert out["status"] == "workspace_unavailable"
    from pathlib import Path

    assert not Path(seen["dir"]).exists()
