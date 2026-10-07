#!/usr/bin/env python3
"""Guard against private terms entering the public repo.

The term list lives outside the repo. Sources, first found wins:
  1. env FRANKY_PRIVATE_TERMS       (newline-separated)
  2. env FRANKY_PRIVATE_TERMS_FILE  (path to a file)
  3. ~/.config/franky/private-terms

Line format: one term per line, case-insensitive substring; `re:` prefix = Python regex;
`#` comments and blank lines are ignored.

Subcommands:
  files              - scan every tracked file
  commits <range>    - scan commit messages and author/committer names and emails
  text <label>       - scan stdin

A hit prints `<location>: private term #N` (N is 1-based). The term and the matched
text are never printed. Exit 0 clean, 1 hit, 2 usage or configuration error.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_FILE = Path.home() / ".config" / "franky" / "private-terms"


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, check=False, **kw)


def parse_terms(text: str) -> list:
    """Return matchers (compiled regexes), in list order. Raises SystemExit(2) on a bad regex."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pat = line[3:] if line.startswith("re:") else re.escape(line)
        try:
            out.append(re.compile(pat, re.IGNORECASE))
        except re.error:
            print("private_terms: invalid regex in term list", file=sys.stderr)
            raise SystemExit(2) from None
    return out


def load_terms(env=None) -> list:
    env = os.environ if env is None else env
    text = env.get("FRANKY_PRIVATE_TERMS", "")
    if not parse_terms_raw(text):
        path = env.get("FRANKY_PRIVATE_TERMS_FILE")
        p = Path(path) if path else DEFAULT_FILE
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            text = ""
    terms = parse_terms(text)
    if not terms:
        print("private_terms: no private terms configured", file=sys.stderr)
        raise SystemExit(2)
    return terms


def parse_terms_raw(text: str) -> bool:
    return any(ln.strip() and not ln.strip().startswith("#") for ln in text.splitlines())


def scan(text: str, terms: list) -> list:
    """(line number, term number) for each line and term that match."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for i, rx in enumerate(terms, 1):
            if rx.search(line):
                hits.append((n, i))
    return hits


def _report(loc: str, hits) -> int:
    for _, i in hits:
        print(f"{loc}: private term #{i}")
    return len(hits)


def mask(text: str, terms: list) -> str:
    """Replace every term match with *** so a location never prints a term."""
    for rx in terms:
        text = rx.sub("***", text)
    return text


def scan_files(terms, run=_run, root: Path = Path(".")) -> int:
    res = run(["git", "ls-files", "-z"], cwd=root)
    if res.returncode != 0:
        raise SystemExit("private_terms: git ls-files failed")
    found = 0
    for name in res.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        shown = mask(name, terms)
        found += _report(f"{shown} (path)", scan(name, terms))
        p = root / name
        if not p.exists() and not p.is_symlink():
            continue  # deleted in the work tree; the commit scan covers its history
        try:
            # Terms are text, so binary and non-UTF-8 files are scanned with replacement chars.
            data = p.read_bytes() if p.is_file() else str(p.readlink()).encode()
        except OSError:
            print(f"private_terms: cannot read {shown}", file=sys.stderr)
            raise SystemExit(2) from None
        for n, i in scan(data.decode("utf-8", "replace"), terms):
            print(f"{shown}:{n}: private term #{i}")
            found += 1
    return found


def scan_commits(terms, rev_range: str, run=_run) -> int:
    fmt = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00%x01"
    res = run(["git", "log", f"--format={fmt}", rev_range])
    if res.returncode != 0:
        raise SystemExit("private_terms: git log failed")
    found = 0
    for rec in res.stdout.decode("utf-8", "replace").split("\x00\x01"):
        parts = rec.lstrip("\n").split("\x00")
        if len(parts) < 6:
            continue
        sha = parts[0][:12]
        found += _report(f"{sha}:author", scan("\n".join(parts[1:5]), terms))
        found += _report(f"{sha}:message", scan(parts[5], terms))
    return found + scan_diffs(terms, rev_range, run)


def scan_diffs(terms, rev_range: str, run=_run) -> int:
    """Scan the paths and added lines of every commit, so content a later commit removed
    still counts: it stays readable in the published history."""
    argv = ["git", "log", "--format=%x01%H", "-p", "--text", "--no-renames", "--unified=0"]
    res = run([*argv, "--no-color", "--no-ext-diff", rev_range])
    if res.returncode != 0:
        raise SystemExit("private_terms: git log -p failed")
    found, sha, path = 0, "", ""
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("\x01"):
            sha, path = line[1:13], ""
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
            found += _report(f"{sha}:{mask(path, terms)} (path)", scan(path, terms))
        elif line.startswith("+"):
            found += _report(f"{sha}:{mask(path, terms)}:added", scan(line[1:], terms))
    return found


def main(argv=None, run=_run, stdin=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("files")
    sub.add_parser("commits").add_argument("rev_range")
    sub.add_parser("text").add_argument("label")
    args = ap.parse_args(argv)
    terms = load_terms()
    if args.cmd == "files":
        n = scan_files(terms, run)
    elif args.cmd == "commits":
        n = scan_commits(terms, args.rev_range, run)
    else:
        data = (stdin or sys.stdin).read()
        n = 0
        for ln, i in scan(data, terms):
            print(f"{args.label}:{ln}: private term #{i}")
            n += 1
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
