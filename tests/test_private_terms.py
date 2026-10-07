import importlib.util
import io
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "private_terms", Path(__file__).parent.parent / "scripts" / "private_terms.py"
)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("FRANKY_PRIVATE_TERMS", "FRANKY_PRIVATE_TERMS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pt, "DEFAULT_FILE", tmp_path / "absent")


def _cp(out: str, code: int = 0):
    return subprocess.CompletedProcess([], code, stdout=out.encode(), stderr=b"")


def test_env_beats_file(monkeypatch, tmp_path):
    f = tmp_path / "t"
    f.write_text("fromfile\n")
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS_FILE", str(f))
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "# c\n\nfromenv\n")
    assert len(pt.load_terms()) == 1
    assert pt.load_terms()[0].search("FROMENV")
    monkeypatch.delenv("FRANKY_PRIVATE_TERMS")
    assert pt.load_terms()[0].search("fromfile")


def test_missing_terms_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        pt.load_terms()
    assert e.value.code == 2
    assert "no private terms configured" in capsys.readouterr().err


def test_bad_regex_exit_2(monkeypatch):
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "re:(unclosed")
    with pytest.raises(SystemExit) as e:
        pt.load_terms()
    assert e.value.code == 2


def test_text_substring_and_regex_never_print_term(monkeypatch, capsys):
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "other\nSecretProj\nre:\\bAB-\\d+\\b")
    stdin = io.StringIO("clean\nuses secretproj here\nticket ab-12\n")
    assert pt.main(["text", "pr-body"], stdin=stdin) == 1
    out = capsys.readouterr().out
    assert out == "pr-body:2: private term #2\npr-body:3: private term #3\n"
    assert "secretproj" not in out.lower()


def test_text_clean(monkeypatch):
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "secretproj")
    assert pt.main(["text", "x"], stdin=io.StringIO("fine\n")) == 0


def test_files_mode_scans_everything_and_masks_paths(monkeypatch, tmp_path, capsys):
    (tmp_path / "a.txt").write_text("ok\nsecretproj\n")
    (tmp_path / "latin1").write_bytes(b"\xff\xfe secretproj")
    (tmp_path / "nul").write_bytes(b"x\0secretproj")
    (tmp_path / "SecretProj-notes.md").write_text("clean\n")
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "secretproj")
    files = "a.txt\0latin1\0nul\0SecretProj-notes.md\0gone.txt\0"
    run = lambda argv, **kw: _cp(files)  # noqa: E731
    assert pt.scan_files(pt.load_terms(), run, tmp_path) == 4
    out = capsys.readouterr().out
    assert out == (
        "a.txt:2: private term #1\n"
        "latin1:1: private term #1\n"
        "nul:1: private term #1\n"
        "***-notes.md (path): private term #1\n"
    )
    assert "secretproj" not in out.lower()


def test_files_mode_unreadable_exit_2(monkeypatch, tmp_path):
    (tmp_path / "locked").write_text("x")
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "secretproj")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError()))
    run = lambda argv, **kw: _cp("locked\0")  # noqa: E731
    with pytest.raises(SystemExit) as e:
        pt.scan_files(pt.load_terms(), run, tmp_path)
    assert e.value.code == 2


def test_commits_flags_author_message_and_removed_later_content(monkeypatch, capsys):
    monkeypatch.setenv("FRANKY_PRIVATE_TERMS", "secretproj\nacme.example")
    sha = "a" * 40
    log = f"{sha}\0Dev\0dev@acme.example\0Dev\0dev@ok.test\0fix thing\n\nsecretproj\n\0\x01"
    # The first commit adds the term and a path with it; a later commit removes both.
    diff = (
        f"\x01{sha}\n\ndiff --git a/secretproj.py b/secretproj.py\n"
        "+++ b/secretproj.py\n@@ -0,0 +1 @@\n+x = 'SECRETPROJ'\n"
        f"\x01{'b' * 40}\n\ndiff --git a/secretproj.py b/secretproj.py\n"
        "+++ /dev/null\n@@ -1 +0,0 @@\n-x = 'SECRETPROJ'\n"
    )
    run = lambda argv, **kw: _cp(diff if "-p" in argv else log)  # noqa: E731
    assert pt.scan_commits(pt.load_terms(), "a..b", run) == 4
    out = capsys.readouterr().out
    assert f"{sha[:12]}:author: private term #2" in out
    assert f"{sha[:12]}:message: private term #1" in out
    assert f"{sha[:12]}:***.py (path): private term #1" in out
    assert f"{sha[:12]}:***.py:added: private term #1" in out
    assert "secretproj" not in out.lower() and "acme" not in out
