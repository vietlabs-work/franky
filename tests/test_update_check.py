"""Tests for franky.update_check: version compare, latest-tag fetch, force_update.

No real network, no real subprocess, no real `gh` - every side effect is injected.
"""

from types import SimpleNamespace

import pytest

from franky._install import Install
from franky.update_check import (
    _TTL_AVAILABLE,
    _TTL_CURRENT,
    REPO_GIT_URL,
    UpdateError,
    _cache_fresh,
    _flag,
    _install_command,
    _read_cache,
    _spec,
    _tail,
    _write_cache,
    fetch_latest_tag,
    force_update,
    is_newer,
    maybe_auto_update,
    parse_version,
)


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# parse_version / is_newer
# ---------------------------------------------------------------------------


def test_parse_version_plain():
    assert parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_with_v_prefix():
    assert parse_version("v0.10.0") == (0, 10, 0)


def test_parse_version_unparseable():
    assert parse_version("2024-01-01") is None
    assert parse_version("1.2") is None
    assert parse_version("1.2.3-rc1") is None


def test_is_newer_numeric_ordering():
    assert is_newer("v0.2.0", "0.1.0") is True
    assert is_newer("v0.2.0", "0.2.0") is False
    assert is_newer("v0.1.0", "0.2.0") is False
    # 0.10 > 0.9 numerically (not a string compare)
    assert is_newer("v0.10.0", "0.9.0") is True


def test_is_newer_unparseable_falls_back_to_string_diff():
    # Tag does not parse -> exact string compare (v-stripped); differ -> newer.
    assert is_newer("nightly", "0.1.0") is True
    assert is_newer("nightly", "nightly") is False
    assert is_newer("vnightly", "nightly") is False  # leading v normalized away


# ---------------------------------------------------------------------------
# _spec / _install_command / _tail
# ---------------------------------------------------------------------------


def test_spec_pins_the_tag():
    assert _spec("v1.2.3") == f"{REPO_GIT_URL}@v1.2.3"


def test_install_command_uv_tool():
    cmd = _install_command(Install("uv tool", "/x/uv/tools/franky/bin/python"), "v1.2.3")
    assert cmd == ["uv", "tool", "install", "--force", _spec("v1.2.3")]


def test_install_command_pipx():
    cmd = _install_command(Install("pipx", "/x/pipx/venvs/franky/bin/python"), "v1.2.3")
    assert cmd == ["pipx", "install", "--force", _spec("v1.2.3")]


def test_install_command_pip_targets_resolved_interpreter():
    # pip must upgrade the EXACT running env, not a blind global install.
    cmd = _install_command(Install("pip", "/usr/bin/python3"), "v1.2.3")
    assert cmd == ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", _spec("v1.2.3")]


def test_install_command_unknown_kind_is_none():
    assert _install_command(Install("frozen-binary", "/x"), "v1.2.3") is None


def test_tail_returns_last_lines():
    text = "\n".join(str(i) for i in range(40))
    assert _tail(text, lines=3) == "37\n38\n39"


# ---------------------------------------------------------------------------
# fetch_latest_tag
# ---------------------------------------------------------------------------


def test_fetch_prefers_gh():
    def runner(argv, **kw):
        assert argv[:2] == ["gh", "api"]
        return _proc(returncode=0, stdout="v0.3.0\n")

    def opener(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("REST should not be hit when gh succeeds")

    assert fetch_latest_tag(runner=runner, opener=opener, env={}) == "v0.3.0"


def test_fetch_falls_back_to_rest_with_token_header():
    captured = {}

    def runner(argv, **kw):
        return _proc(returncode=1, stderr="gh not logged in")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"tag_name": "v0.4.0"}'

    def opener(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    tag = fetch_latest_tag(runner=runner, opener=opener, env={"GH_TOKEN": "secret-tok"})
    assert tag == "v0.4.0"
    assert captured["auth"] == "Bearer secret-tok"


def test_fetch_rest_without_token_omits_auth():
    captured = {}

    def runner(argv, **kw):
        raise FileNotFoundError("no gh on PATH")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"tag_name": "v0.5.0"}'

    def opener(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    tag = fetch_latest_tag(runner=runner, opener=opener, env={})
    assert tag == "v0.5.0"
    assert captured["auth"] is None


def test_fetch_raises_update_error_when_all_paths_fail():
    def runner(argv, **kw):
        return _proc(returncode=1)

    def opener(req, timeout=None):
        raise OSError("network down")

    with pytest.raises(UpdateError) as exc:
        fetch_latest_tag(runner=runner, opener=opener, env={})
    assert "could not reach GitHub" in str(exc.value)


# ---------------------------------------------------------------------------
# force_update
# ---------------------------------------------------------------------------


def _collect():
    lines: list[str] = []
    return lines, lines.append


def test_force_update_dev_checkout_is_noop():
    lines, out = _collect()
    code = force_update(
        install=Install("dev checkout", "/repo"),
        current="0.1.0",
        fetch=lambda: pytest.fail("must not fetch for a dev checkout"),
        out=out,
    )
    assert code == 0
    assert any("dev checkout" in ln and "git" in ln for ln in lines)


def test_force_update_already_current():
    lines, out = _collect()
    code = force_update(
        install=Install("pip", "/usr/bin/python3"),
        current="0.2.0",
        fetch=lambda: "v0.2.0",
        runner=lambda *a, **k: pytest.fail("must not install when already current"),
        out=out,
    )
    assert code == 0
    assert any("already on the latest release" in ln for ln in lines)


def test_force_update_upgrades_and_reports_tag():
    calls = {}
    lines, out = _collect()

    def runner(argv, **kw):
        calls["argv"] = argv
        return _proc(returncode=0)

    code = force_update(
        install=Install("uv tool", "/x/uv/tools/franky/bin/python"),
        current="0.1.0",
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    assert code == 0
    assert calls["argv"] == ["uv", "tool", "install", "--force", _spec("v0.2.0")]
    assert any("updated to v0.2.0" in ln for ln in lines)


def test_force_update_force_reinstalls_same_version():
    calls = {}
    lines, out = _collect()

    def runner(argv, **kw):
        calls["argv"] = argv
        return _proc(returncode=0)

    code = force_update(
        force=True,
        install=Install("pipx", "/x/pipx/venvs/franky/bin/python"),
        current="0.2.0",
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    assert code == 0
    assert calls["argv"] == ["pipx", "install", "--force", _spec("v0.2.0")]
    assert any("reinstalled v0.2.0" in ln for ln in lines)


def test_force_update_undetectable_installer_exits_one():
    lines, out = _collect()
    code = force_update(
        install=Install("frozen-binary", "/x"),
        current="0.1.0",
        fetch=lambda: "v0.2.0",
        runner=lambda *a, **k: pytest.fail("must not install when undetectable"),
        out=out,
    )
    assert code == 1
    blob = "\n".join(lines)
    assert "could not detect how franky was installed" in blob
    assert "uv tool install --force" in blob


def test_force_update_fetch_failure_exits_one():
    lines, out = _collect()

    def fetch():
        raise UpdateError("could not reach GitHub to check for the latest release (timeout)")

    code = force_update(
        install=Install("pip", "/usr/bin/python3"),
        current="0.1.0",
        fetch=fetch,
        out=out,
    )
    assert code == 1
    assert any("could not reach GitHub" in ln for ln in lines)


def test_force_update_install_nonzero_surfaces_stderr_tail():
    lines, out = _collect()

    def runner(argv, **kw):
        return _proc(returncode=2, stderr="ERROR: could not build wheel\nfatal: boom")

    code = force_update(
        install=Install("pip", "/usr/bin/python3"),
        current="0.1.0",
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    assert code == 1
    blob = "\n".join(lines)
    assert "update failed (exit 2)" in blob
    assert "fatal: boom" in blob


def test_force_update_install_oserror_exits_one():
    lines, out = _collect()

    def runner(argv, **kw):
        raise FileNotFoundError("uv not on PATH")

    code = force_update(
        install=Install("uv tool", "/x/uv/tools/franky/bin/python"),
        current="0.1.0",
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    assert code == 1
    assert any("failed to start" in ln for ln in lines)


# ---------------------------------------------------------------------------
# auto-update helpers: _flag / cache round-trip / _cache_fresh
# ---------------------------------------------------------------------------


def test_flag_only_exact_one_is_true():
    assert _flag({"X": "1"}, "X") is True
    assert _flag({"X": " 1 "}, "X") is True  # surrounding whitespace tolerated
    assert _flag({"X": "0"}, "X") is False
    assert _flag({"X": "true"}, "X") is False
    assert _flag({}, "X") is False


def test_cache_round_trip(tmp_path):
    path = tmp_path / "sub" / "update_check.json"  # parent created on write
    entry = {"checked_at": 123.0, "latest_tag": "v0.2.0", "status": "available"}
    _write_cache(path, entry)
    assert _read_cache(path) == entry


def test_read_cache_missing_or_corrupt(tmp_path):
    assert _read_cache(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert _read_cache(bad) is None
    notdict = tmp_path / "list.json"
    notdict.write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_cache(notdict) is None


def test_cache_fresh_within_and_beyond_ttl():
    now = 1_000_000.0
    # available: fresh just under 24h, stale just over.
    assert _cache_fresh({"checked_at": now - 10, "status": "available"}, now) is True
    assert (
        _cache_fresh({"checked_at": now - (_TTL_AVAILABLE + 1), "status": "available"}, now)
        is False
    )
    # current: 1h TTL - fresh at 30m, stale at 2h.
    assert _cache_fresh({"checked_at": now - 1800, "status": "current"}, now) is True
    assert _cache_fresh({"checked_at": now - (_TTL_CURRENT + 1), "status": "current"}, now) is False


def test_cache_fresh_rejects_garbage():
    now = 1_000_000.0
    assert _cache_fresh(None, now) is False
    assert _cache_fresh({}, now) is False
    assert _cache_fresh({"checked_at": "soon", "status": "available"}, now) is False
    assert _cache_fresh({"checked_at": now, "status": "bogus"}, now) is False
    # future-dated timestamp (clock skew) -> stale, we refetch.
    assert _cache_fresh({"checked_at": now + 5000, "status": "available"}, now) is False


# ---------------------------------------------------------------------------
# maybe_auto_update
# ---------------------------------------------------------------------------


def _au(**kw):
    """Invoke maybe_auto_update with safe non-network defaults; override via kwargs."""
    defaults = dict(
        env={},
        install=Install("pip", "/usr/bin/python3"),
        current="0.1.0",
        fetch=lambda: pytest.fail("fetch should not be called"),
        now=lambda: 1_000_000.0,
        runner=lambda *a, **k: pytest.fail("runner should not be called"),
    )
    defaults.update(kw)
    return maybe_auto_update(**defaults)


def test_auto_update_dev_checkout_is_silent(tmp_path):
    lines, out = _collect()
    _au(install=Install("dev checkout", "/repo"), cache_path=tmp_path / "c.json", out=out)
    assert lines == []
    # No fetch, and nothing written.
    assert not (tmp_path / "c.json").exists()


def test_auto_update_no_check_flag_silences(tmp_path):
    lines, out = _collect()
    _au(env={"FRANKY_NO_UPDATE_CHECK": "1"}, cache_path=tmp_path / "c.json", out=out)
    assert lines == []
    assert not (tmp_path / "c.json").exists()


def test_auto_update_fresh_available_cache_hints_without_fetching(tmp_path):
    path = tmp_path / "c.json"
    _write_cache(
        path, {"checked_at": 1_000_000.0 - 10, "latest_tag": "v0.9.0", "status": "available"}
    )
    lines, out = _collect()
    _au(cache_path=path, out=out)  # default fetch fails the test if called
    assert any("v0.9.0 available (installed v0.1.0)" in ln for ln in lines)


def test_auto_update_fresh_current_cache_is_silent(tmp_path):
    path = tmp_path / "c.json"
    _write_cache(
        path, {"checked_at": 1_000_000.0 - 10, "latest_tag": "v0.1.0", "status": "current"}
    )
    lines, out = _collect()
    _au(cache_path=path, out=out)
    assert lines == []


def test_auto_update_stale_cache_refetches_and_hints(tmp_path):
    path = tmp_path / "c.json"
    lines, out = _collect()
    _au(cache_path=path, fetch=lambda: "v0.2.0", out=out)
    assert any("v0.2.0 available" in ln for ln in lines)
    # Cache rewritten as available with the fetched tag.
    entry = _read_cache(path)
    assert entry["status"] == "available"
    assert entry["latest_tag"] == "v0.2.0"
    assert entry["checked_at"] == 1_000_000.0


def test_auto_update_fetch_says_current_writes_cache_no_hint(tmp_path):
    path = tmp_path / "c.json"
    lines, out = _collect()
    _au(cache_path=path, current="0.2.0", fetch=lambda: "v0.2.0", out=out)
    assert lines == []
    assert _read_cache(path)["status"] == "current"


def test_auto_update_fetch_failure_negative_caches_silently(tmp_path):
    path = tmp_path / "c.json"

    def boom():
        raise UpdateError("offline")

    lines, out = _collect()
    _au(cache_path=path, fetch=boom, out=out)
    assert lines == []
    assert _read_cache(path)["status"] == "error"


def test_auto_update_never_raises_on_unexpected_error(tmp_path):
    # An install object that explodes on attribute access must not bubble out of the wrapper.
    class Boom:
        @property
        def kind(self):
            raise RuntimeError("kaboom")

    # Should return None, not raise.
    assert _au(install=Boom(), cache_path=tmp_path / "c.json", out=lambda _m: None) is None


def test_auto_update_opt_in_installs_for_next_run(tmp_path):
    path = tmp_path / "c.json"
    calls = {}
    lines, out = _collect()

    def runner(argv, **kw):
        calls["argv"] = argv
        return _proc(returncode=0)

    _au(
        env={"FRANKY_AUTO_UPDATE": "1"},
        install=Install("uv tool", "/x/uv/tools/franky/bin/python"),
        cache_path=path,
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    blob = "\n".join(lines)
    assert "v0.2.0 available" in blob  # hint still printed
    assert "installing v0.2.0 for your next build" in blob
    assert "next `franky build`" in blob
    assert calls["argv"] == ["uv", "tool", "install", "--force", _spec("v0.2.0")]


def test_auto_update_opt_in_install_failure_is_soft(tmp_path):
    lines, out = _collect()

    def runner(argv, **kw):
        return _proc(returncode=1, stderr="nope")

    _au(
        env={"FRANKY_AUTO_UPDATE": "1"},
        install=Install("pipx", "/x/pipx/venvs/franky/bin/python"),
        cache_path=tmp_path / "c.json",
        fetch=lambda: "v0.2.0",
        runner=runner,
        out=out,
    )
    blob = "\n".join(lines)
    assert "v0.2.0 available" in blob
    assert "auto-install of v0.2.0 failed" in blob
