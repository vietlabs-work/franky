import json
import os
import signal
import subprocess
from pathlib import Path


INSTALLER = Path(__file__).parents[1] / "install-codex-native-launcher.sh"


def _make_package(package: Path, native_body: str, *, companion=True) -> None:
    bin_dir = (
        package / "node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin"
    )
    bin_dir.mkdir(parents=True)
    native = bin_dir / "codex"
    native.write_text(native_body)
    native.chmod(0o755)
    if companion:
        companion_path = bin_dir / "codex-code-mode-host"
        companion_path.write_text("#!/bin/sh\nexit 0\n")
        companion_path.chmod(0o755)


def _install(tmp_path: Path, native_body: str) -> Path:
    package = tmp_path / "codex"
    _make_package(package, native_body)
    launcher = tmp_path / "bin/codex"
    launcher.parent.mkdir()
    subprocess.run(["sh", INSTALLER, package, launcher], check=True)
    return launcher


def test_launcher_forwards_arguments_and_sets_npm_metadata(tmp_path):
    launcher = _install(
        tmp_path,
        """#!/usr/bin/env python3
import json
import os
import sys
print(json.dumps({"argv": sys.argv[1:], "env": {key: os.environ.get(key) for key in (
    "CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM",
    "CODEX_MANAGED_BY_VITE_PLUS", "CODEX_MANAGED_PACKAGE_ROOT")}}))
""",
    )
    env = os.environ | {
        "CODEX_MANAGED_BY_NPM": "old",
        "CODEX_MANAGED_BY_BUN": "1",
        "CODEX_MANAGED_BY_PNPM": "1",
        "CODEX_MANAGED_BY_VITE_PLUS": "1",
        "CODEX_MANAGED_PACKAGE_ROOT": "/old",
    }

    result = subprocess.run(
        [launcher, "alpha", "", "two words"], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "argv": ["alpha", "", "two words"],
        "env": {
            "CODEX_MANAGED_BY_NPM": "1",
            "CODEX_MANAGED_BY_BUN": None,
            "CODEX_MANAGED_BY_PNPM": None,
            "CODEX_MANAGED_BY_VITE_PLUS": None,
            "CODEX_MANAGED_PACKAGE_ROOT": str(tmp_path / "codex"),
        },
    }


def test_launcher_preserves_native_exit_code(tmp_path):
    launcher = _install(tmp_path, "#!/bin/sh\nexit 23\n")

    assert subprocess.run([launcher]).returncode == 23


def test_launcher_exec_preserves_native_signal(tmp_path):
    launcher = _install(tmp_path, "#!/bin/sh\nkill -TERM $$\n")

    assert subprocess.run([launcher]).returncode == -signal.SIGTERM


def test_installer_relocates_staging_paths_to_runtime_root(tmp_path):
    staging = tmp_path / "staging/codex"
    runtime = tmp_path / "runtime/codex"
    _make_package(staging, "#!/bin/sh\nexit 99\n")
    _make_package(runtime, '#!/bin/sh\nprintf "%s\\n" "$CODEX_MANAGED_PACKAGE_ROOT"\n')
    launcher = tmp_path / "bin/codex"
    launcher.parent.mkdir()

    subprocess.run(["sh", INSTALLER, staging, launcher, runtime], check=True)

    result = subprocess.run([launcher], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == f"{runtime}\n"


def test_installer_rejects_missing_native_layout(tmp_path):
    package = tmp_path / "codex"
    package.mkdir()

    result = subprocess.run(
        ["sh", INSTALLER, package, tmp_path / "launcher"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported Codex package layout" in result.stderr


def test_installer_rejects_missing_native_companion(tmp_path):
    package = tmp_path / "codex"
    _make_package(package, "#!/bin/sh\nexit 0\n", companion=False)

    result = subprocess.run(
        ["sh", INSTALLER, package, tmp_path / "launcher"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported Codex package layout" in result.stderr


def test_installer_rejects_ambiguous_native_layout(tmp_path):
    package = tmp_path / "codex"
    for platform in ("codex-linux-arm64", "codex-linux-x64"):
        bin_dir = package / f"node_modules/@openai/{platform}/vendor/target/bin"
        bin_dir.mkdir(parents=True)
        for name in ("codex", "codex-code-mode-host"):
            path = bin_dir / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

    result = subprocess.run(
        ["sh", INSTALLER, package, tmp_path / "launcher"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported Codex package layout" in result.stderr
