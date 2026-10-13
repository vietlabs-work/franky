"""Shell check in franky-dind-entrypoint.sh: the Codex auth link must survive the engine run.

The function is cut out of the script between its BEGIN/END markers and run in bash against a
throwaway HOME, so no Docker is needed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "franky-dind-entrypoint.sh").read_text()
FUNC = re.search(r"# BEGIN check_codex_auth\n(.*?)# END check_codex_auth", SCRIPT, re.S).group(1)

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _check(home, link=1):
    proc = subprocess.run(
        ["bash", "-c", f"codex_link={link}\n{FUNC}\ncheck_codex_auth"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stderr


def _home(tmp_path):
    (tmp_path / ".codex-auth").mkdir()
    (tmp_path / ".codex-auth/auth.json").write_text("{}")
    (tmp_path / ".codex").mkdir()
    return tmp_path / ".codex/auth.json"


def test_an_intact_link_passes(tmp_path):
    _home(tmp_path)
    (tmp_path / ".codex/auth.json").symlink_to(tmp_path / ".codex-auth/auth.json")

    assert _check(tmp_path) == (0, "")


def test_a_replaced_regular_file_fails_loudly_without_copying_secrets(tmp_path):
    _home(tmp_path)
    auth = tmp_path / ".codex/auth.json"
    auth.write_text('{"refresh": "new"}')

    code, err = _check(tmp_path)

    assert code == 1 and err.startswith("franky: ERROR codex auth.json is no longer a link")
    assert (tmp_path / ".codex-auth/auth.json").read_text() == "{}"
    assert "{" not in err


def test_a_link_to_another_target_or_a_missing_link_fails(tmp_path):
    _home(tmp_path)
    auth = tmp_path / ".codex/auth.json"
    auth.symlink_to(tmp_path / "elsewhere.json")
    assert _check(tmp_path)[0] == 1
    auth.unlink()
    assert _check(tmp_path)[0] == 1


def test_a_run_without_the_auth_link_is_not_checked(tmp_path):
    assert _check(tmp_path, link=0) == (0, "")


def test_the_check_runs_after_the_engine_and_before_the_hold():
    assert SCRIPT.index("if ! check_codex_auth; then") > SCRIPT.index('wait "${child}"')
    assert SCRIPT.index("if ! check_codex_auth; then") < SCRIPT.index("holding for session copy")
