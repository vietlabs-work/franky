"""The `--no-publish` helper script, run with real git against throwaway repositories.

No Docker: the script is plain bash over a directory, so its behaviour on hostile repositories
(replace refs, hidden commit headers, history-hiding config) is checked here for real. The unit
tests in test_container.py cover everything around it with fakes.
"""

import os
import shutil
import subprocess

import pytest

from franky import container, snapshot

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash"
)

TOKEN = "tok_LIVE_0123456789abcdef0123456789"


def _env(home):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@example.test",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@example.test",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }


class Repo:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.dir = tmp_path / "work" / "repo"
        self.dir.mkdir(parents=True)
        self.env = _env(tmp_path)
        self.git("init", "-q", "-b", "main")
        self.commit("a.txt", "one\n", "init", subject_bytes=None)
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "franky/x")

    def git(self, *args, input=None):
        done = subprocess.run(
            ["git", *args],
            cwd=self.dir,
            env=self.env,
            input=input,
            capture_output=True,
            check=True,
        )
        return done.stdout.decode().strip()

    def commit(self, name, content, message, subject_bytes=None):
        (self.dir / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def tip(self):
        return self.git("rev-parse", "refs/heads/franky/x")

    def raw_commit(self, extra_headers=b"", message=b"msg\n", parent=None):
        """Write a commit object by hand (headers git itself would not add) and point the branch at it."""
        tree = self.git("write-tree")
        parent = parent or self.git("rev-parse", "HEAD")
        body = (
            (
                f"tree {tree}\nparent {parent}\nauthor a <a@example.test> 1767225600 +0000\n"
                "committer a <a@example.test> 1767225600 +0000\n"
            ).encode()
            + extra_headers
            + b"\n"
            + message
        )
        sha = (
            subprocess.run(
                ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                cwd=self.dir,
                env=self.env,
                input=body,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        self.git("update-ref", "refs/heads/franky/x", sha)
        return sha

    def run(self, mode, *, base=None, tip="", branch="franky/x", clone=None):
        argv = [
            "bash",
            "-c",
            container._EXPORT_SCRIPT,
            "franky-export",
            mode,
            branch,
            base or self.base,
            str(clone or self.dir),
            tip,
        ]
        return subprocess.run(
            argv, env={"PATH": os.environ["PATH"], "HOME": str(self.root)}, capture_output=True
        )


def _scan(repo, **kw):
    done = repo.run("scan", **kw)
    first, _, rest = done.stdout.partition(b"\n")
    return done.returncode, first.decode(), rest


def _has_token(data):
    return TOKEN.encode() in data


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_scan_prints_the_tip_then_the_raw_objects_and_finds_a_plain_token(repo):
    repo.commit("b.txt", f"key={TOKEN}\n", "feat: b")
    code, first, rest = _scan(repo)
    assert code == 0 and first == repo.tip()
    assert _has_token(rest)
    # The same stream goes through the host's value scan.
    import io

    assert snapshot.stream_contains_values(io.BytesIO(rest), [TOKEN])


def test_scan_of_clean_commits_finds_nothing(repo):
    repo.commit("b.txt", "nothing secret\n", "feat: b")
    code, first, rest = _scan(repo)
    assert code == 0 and not _has_token(rest)
    assert b"nothing secret" in rest  # it really did read the new blob


def test_scan_ignores_objects_already_in_the_base(repo):
    (repo.dir / "old.txt").write_text(f"{TOKEN}\n")
    repo.git("add", "old.txt")
    repo.git("commit", "-q", "-m", "base with a token")
    base = repo.git("rev-parse", "HEAD")
    repo.commit("b.txt", "fine\n", "feat: b")
    code, _, rest = _scan(repo, base=base)
    assert code == 0 and not _has_token(rest)


def test_bypass_replace_ref_cannot_hide_a_commit(repo):
    repo.commit("b.txt", f"key={TOKEN}\n", "feat: b")
    leaky = repo.tip()
    repo.git("checkout", "-q", "--detach", repo.base)
    repo.commit("c.txt", "decoy\n", "decoy")
    decoy = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "franky/x")
    repo.git("replace", leaky, decoy)
    code, first, rest = _scan(repo)
    assert code == 0 and first == leaky
    assert _has_token(rest)


def test_bypass_token_in_a_gpgsig_header_is_scanned(repo):
    (repo.dir / "b.txt").write_text("fine\n")
    repo.git("add", "b.txt")
    header = (
        f"gpgsig -----BEGIN PGP SIGNATURE-----\n {TOKEN}\n -----END PGP SIGNATURE-----\n".encode()
    )
    repo.raw_commit(extra_headers=header)
    code, _, rest = _scan(repo)
    assert code == 0 and _has_token(rest)


def test_bypass_encoding_header_cannot_scramble_the_message(repo):
    (repo.dir / "b.txt").write_text("fine\n")
    repo.git("add", "b.txt")
    repo.raw_commit(extra_headers=b"encoding UTF-16LE\n", message=f"{TOKEN}\n".encode())
    code, _, rest = _scan(repo)
    assert code == 0 and _has_token(rest)


def test_bypass_history_hiding_config_and_an_orphan_merge_are_scanned(repo):
    repo.git("config", "log.showRoot", "false")
    repo.git("config", "log.diffMerges", "first-parent")
    repo.git("config", "log.showSignature", "true")
    repo.git("config", "gpg.program", "/bin/false")
    repo.git("checkout", "-q", "--orphan", "other")
    repo.git("rm", "-rf", "-q", ".")
    (repo.dir / "secret.txt").write_text(f"{TOKEN}\n")
    repo.git("add", "secret.txt")
    repo.git("commit", "-q", "-m", "orphan root")
    repo.git("checkout", "-q", "franky/x")
    repo.git("merge", "-q", "-s", "ours", "--allow-unrelated-histories", "other", "-m", "merge")
    code, _, rest = _scan(repo)
    assert code == 0 and _has_token(rest)


def test_a_token_only_in_the_utf16_encoding_is_not_found(repo):
    # Documented limit: the scan guards against accidental leaks of exact values, not encodings.
    repo.commit("b.txt", "x\n", "feat: b")
    (repo.dir / "c.txt").write_bytes(TOKEN.encode("utf-16-le"))
    repo.git("add", "c.txt")
    repo.git("commit", "-q", "-m", "enc")
    _, _, rest = _scan(repo)
    assert not _has_token(rest)


def test_exit_codes(repo):
    # No commits beyond the base.
    repo.git("checkout", "-q", "-B", "franky/x", repo.base)
    assert repo.run("scan").returncode == 4
    # Missing branch, missing clone, symlinked clone.
    repo.commit("b.txt", "b\n", "feat: b")
    assert repo.run("scan", branch="franky/none").returncode == 3
    assert repo.run("scan", clone=repo.root / "nope").returncode == 3
    link = repo.root / "link"
    link.symlink_to(repo.dir)
    assert repo.run("scan", clone=link).returncode == 3
    # Base not an ancestor.
    repo.git("checkout", "-q", "main")
    repo.commit("m.txt", "m\n", "main moves")
    assert repo.run("scan", base=repo.git("rev-parse", "HEAD")).returncode == 5


def test_bundle_refuses_a_branch_that_moved_since_the_scan(repo):
    repo.commit("b.txt", "b\n", "feat: b")
    scanned = repo.tip()
    repo.commit("c.txt", "c\n", "feat: c")  # a surviving process moved the branch
    done = repo.run("bundle", tip=scanned)
    assert done.returncode == 6 and done.stdout == b""


def test_bundle_matches_the_scanned_tip_and_parses_even_with_a_non_ascii_base_subject(repo):
    repo.git("checkout", "-q", "main")
    (repo.dir / "n.txt").write_text("n\n")
    repo.git("add", "n.txt")
    repo.git("commit", "-q", "-m", "日本語 subject")
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-B", "franky/x")
    repo.commit("b.txt", "b\n", "feat: b")
    tip = repo.tip()
    done = repo.run("bundle", base=base, tip=tip)
    assert done.returncode == 0
    path = repo.root / "x.bundle"
    path.write_bytes(done.stdout)
    assert snapshot.parse_bundle_header(path, "franky/x") == tip
    assert "日本語".encode() in done.stdout[:4096]
