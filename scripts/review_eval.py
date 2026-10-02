#!/usr/bin/env python3
"""Franky review-pr quality eval - opt-in, OUT-OF-BAND (real docker, creds, spend), like eval.py.

Runs `franky review-pr --no-publish --json --at-sha S --diff-base B` on fixed historical cases N
times each and scores the findings against expectations, so a review-method change is judged by
score. The case file lives outside the repo (it names real PRs); see evals/review_cases.example.json.

Isolation is best-effort: the agent reads the PR title live (it can change after the case SHA),
and intent otherwise comes from the commit messages up to the case SHA, and the "no later PR state" rule is a prompt rule, not a token limit. `franky` refuses a
case SHA that is not in the PR's commit list (GitHub lists at most 250, and a force-pushed commit
is gone). The diff base is not checked as an ancestor of the case SHA.

Pure logic plus an INJECTABLE runner (argv -> (returncode, stdout)), so the unit tests need no
docker, network, or franky. Stdlib only.

Run:  python3 scripts/review_eval.py --cases ~/.franky/evals/review_cases.json [-n 3] [--jobs 2]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

Runner = Callable[[Sequence[str]], "tuple[int, str]"]

SEVERITY_RANK = {"nit": 0, "question": 1, "normal": 2, "blocking": 3}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CASE_KEYS = {"id", "pr_url", "sha", "base_sha", "expect", "forbid", "clean", "control_of"}
_EXPECT_KEYS = {"name", "any_of", "all_of", "min_severity", "file"}
_FORBID_KEYS = {"name", "any_of", "all_of", "severities"}


class CaseError(ValueError):
    """The cases file is invalid. The harness refuses to run on it."""


def rank(severity: object) -> int:
    return SEVERITY_RANK.get(severity, SEVERITY_RANK["normal"])  # unknown -> normal


# --- case file ---------------------------------------------------------------


def _regexes(where: str, value: object, *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise CaseError(f"{where}: must be a list of regex strings")
    if required and not value:
        raise CaseError(f"{where}: must not be empty")
    for r in value:
        try:
            re.compile(r)
        except re.error as exc:
            raise CaseError(f"{where}: bad regex {r!r}: {exc}") from exc
    return value


def _check_item(where: str, item: object, allowed: set[str], kind: str) -> dict:
    if not isinstance(item, dict):
        raise CaseError(f"{where}: must be an object")
    extra = set(item) - allowed
    if extra:
        raise CaseError(f"{where}: unknown keys {sorted(extra)}")
    if not isinstance(item.get("name"), str) or not item["name"]:
        raise CaseError(f"{where}: name is required")
    _regexes(f"{where}.any_of", item.get("any_of"), required=True)
    _regexes(f"{where}.all_of", item.get("all_of"), required=False)
    if kind == "expect":
        if item.get("min_severity") not in SEVERITY_RANK:
            raise CaseError(f"{where}.min_severity: must be one of {sorted(SEVERITY_RANK)}")
        if "file" in item and not isinstance(item["file"], str):
            raise CaseError(f"{where}.file: must be a string")
    else:
        sev = item.get("severities")
        if not isinstance(sev, list) or not sev or any(s not in SEVERITY_RANK for s in sev):
            raise CaseError(f"{where}.severities: must be a non-empty list of known severities")
    return item


def validate_cases(data: object) -> list[dict]:
    if not isinstance(data, list) or not data:
        raise CaseError("cases file must be a non-empty JSON list")
    seen: set[str] = set()
    for i, case in enumerate(data):
        where = f"case[{i}]"
        if not isinstance(case, dict):
            raise CaseError(f"{where}: must be an object")
        extra = set(case) - _CASE_KEYS
        if extra:
            raise CaseError(f"{where}: unknown keys {sorted(extra)}")
        for key in ("id", "pr_url", "sha", "base_sha"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise CaseError(f"{where}: {key} is required")
        if case["id"] in seen:
            raise CaseError(f"{where}: duplicate id {case['id']!r}")
        seen.add(case["id"])
        for key in ("sha", "base_sha"):
            if not _SHA_RE.match(case[key]):
                raise CaseError(f"{where}.{key}: must be 40 lowercase hex chars")
        if "control_of" in case and not isinstance(case["control_of"], str):
            raise CaseError(f"{where}.control_of: must be a string")
        if "clean" in case and not isinstance(case["clean"], bool):
            raise CaseError(f"{where}.clean: must be a boolean")
        for kind, allowed in (("expect", _EXPECT_KEYS), ("forbid", _FORBID_KEYS)):
            items = case.get(kind, [])
            if not isinstance(items, list):
                raise CaseError(f"{where}.{kind}: must be a list")
            names: set[str] = set()
            for j, item in enumerate(items):
                _check_item(f"{where}.{kind}[{j}]", item, allowed, kind)
                if item["name"] in names:
                    raise CaseError(f"{where}.{kind}[{j}]: duplicate name {item['name']!r}")
                names.add(item["name"])
    return data


def load_cases(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseError(f"cannot read cases file {path}: {exc}") from exc
    return validate_cases(data)


# --- one run -----------------------------------------------------------------


def build_argv(case: dict, franky_bin: str, max_duration: int) -> list[str]:
    return [
        franky_bin,
        "review-pr",
        "--no-publish",
        "--json",
        "--max-duration",
        str(max_duration),
        "--at-sha",
        case["sha"],
        "--diff-base",
        case["base_sha"],
        "--",
        case["pr_url"],
    ]


def _valid_finding(f: object) -> bool:
    return (
        isinstance(f, dict)
        and isinstance(f.get("title"), str)
        and isinstance(f.get("body"), str)
        and isinstance(f.get("severity"), str)
        and (f.get("file") is None or isinstance(f.get("file"), str))
    )


def classify_run(case: dict, code: int, stdout: str) -> dict:
    """Return a run record. valid=False is an ERROR run: it is never scored as clean."""
    rec: dict = {"valid": False, "status": None, "reason": None, "findings": []}
    try:
        data = json.loads(stdout)
    except ValueError:
        rec["reason"] = f"exit {code}: stdout is not JSON"
        return rec
    if not isinstance(data, dict):
        rec["reason"] = f"exit {code}: stdout JSON is not an object"
        return rec
    err = data.get("error")
    rec["status"] = data.get("status") or (err.get("kind") if isinstance(err, dict) else None)
    rec["reason"] = data.get("reason") or (err.get("message") if isinstance(err, dict) else None)
    econ = data.get("economics") if isinstance(data.get("economics"), dict) else {}
    rec["economics"] = econ
    rec["engine"] = data.get("engine")
    rec["model"] = data.get("model")
    rec["findings_total"] = data.get("findings_total")
    if code != 0:
        rec["reason"] = f"exit {code}: {rec['reason'] or 'nonzero exit'}"
    elif data.get("status") != "review_complete":
        rec["reason"] = f"status {data.get('status')!r}: {rec['reason'] or ''}".strip()
    elif data.get("reviewed_sha") != case["sha"]:
        rec["reason"] = f"reviewed_sha {data.get('reviewed_sha')!r} != case sha"
    elif not isinstance(data.get("findings"), list):
        rec["reason"] = "result has no findings list"
    elif not all(_valid_finding(f) for f in data["findings"]):
        rec["reason"] = "result has a malformed finding"
    elif data.get("findings_total") != len(data["findings"]):
        rec["reason"] = "result findings are truncated (findings_total does not match)"
    else:
        rec["valid"] = True
        rec["findings"] = data["findings"]
    return rec


def run_once(case: dict, runner: Runner, franky_bin: str, max_duration: int) -> dict:
    try:
        code, out = runner(build_argv(case, franky_bin, max_duration))
    except Exception as exc:  # a crashed runner is an ERROR run, never a clean one
        return {"valid": False, "status": None, "reason": f"runner failed: {exc}", "findings": []}
    return classify_run(case, code, out)


# --- matching and scoring -----------------------------------------------------


def finding_matches(finding: dict, spec: dict, *, severity_ok: Callable[[object], bool]) -> bool:
    text = f"{finding.get('title') or ''}\n{finding.get('body') or ''}"
    if not all(re.search(r, text) for r in spec.get("all_of") or []):
        return False
    if not any(re.search(r, text) for r in spec["any_of"]):
        return False
    if not severity_ok(finding.get("severity")):
        return False
    want = spec.get("file")
    if not want:
        return True
    path = finding.get("file") or ""
    if want.endswith("/"):  # a directory: match one whole path component run
        return f"/{want}" in f"/{path}"
    return path == want or path.endswith("/" + want)


def expect_hit(findings: list[dict], spec: dict) -> bool:
    floor = SEVERITY_RANK[spec["min_severity"]]
    return any(finding_matches(f, spec, severity_ok=lambda s: rank(s) >= floor) for f in findings)


def forbid_violated(findings: list[dict], spec: dict) -> bool:
    return any(
        finding_matches(f, spec, severity_ok=lambda s: s in spec["severities"]) for f in findings
    )


def _mean(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return round(sum(vals) / len(vals), 4) if vals else None  # unknown stays None, never 0


def is_negative_case(case: dict) -> bool:
    """A clean case: any blocking or normal finding is a false positive. A fix-SHA control
    (`control_of`) is scored only by its `forbid` list, because real unrelated findings are
    usually present in both the buggy and the fixed commit."""
    return bool(case.get("clean"))


def score_case(case: dict, runs: list[dict]) -> dict:
    valid = [r for r in runs if r["valid"]]
    n = len(valid)
    score: dict = {
        "id": case["id"],
        "runs": len(runs),
        "valid_runs": n,
        "error_runs": len(runs) - n,
        "expect": {
            e["name"]: {"hits": sum(expect_hit(r["findings"], e) for r in valid), "of": n}
            for e in case.get("expect", [])
        },
        "forbid": {
            f["name"]: {
                "violations": sum(forbid_violated(r["findings"], f) for r in valid),
                "of": n,
            }
            for f in case.get("forbid", [])
        },
        "findings_total_mean": _mean(
            [
                r.get("findings_total")
                if r.get("findings_total") is not None
                else len(r["findings"])
                for r in valid
            ]
        ),
        "cost_usd_mean": _mean([(r.get("economics") or {}).get("cost_usd") for r in runs]),
        "duration_s_mean": _mean([(r.get("economics") or {}).get("duration_s") for r in runs]),
        "engine": next((r["engine"] for r in runs if r.get("engine")), None),
        "model": next((r["model"] for r in runs if r.get("model")), None),
    }
    if is_negative_case(case):
        bad = sum(any(rank(f.get("severity")) >= 2 for f in r["findings"]) for r in valid)
        score["false_positive"] = {"runs": bad, "of": n}
    return score


def totals(scores: list[dict]) -> dict:
    hits = sum(v["hits"] for s in scores for v in s["expect"].values())
    slots = sum(v["of"] for s in scores for v in s["expect"].values())
    fp = sum(s["false_positive"]["runs"] for s in scores if "false_positive" in s)
    fp_of = sum(s["false_positive"]["of"] for s in scores if "false_positive" in s)
    return {
        "recall": {"hits": hits, "of": slots, "rate": hits / slots if slots else None},
        "false_positive": {"runs": fp, "of": fp_of, "rate": fp / fp_of if fp_of else None},
        "forbid_violations": sum(v["violations"] for s in scores for v in s["forbid"].values()),
        "error_runs": sum(s["error_runs"] for s in scores),
    }


def _fmt(v: float | None, spec: str = ".2f") -> str:
    return "n/a" if v is None else format(v, spec)


def format_report(scores: list[dict], tot: dict) -> str:
    lines = []
    for s in scores:
        lines.append(
            f"{s['id']}: valid {s['valid_runs']}/{s['runs']}  errors {s['error_runs']}  "
            f"findings {_fmt(s['findings_total_mean'], '.1f')}  cost ${_fmt(s['cost_usd_mean'])}  "
            f"time {_fmt(s['duration_s_mean'], '.0f')}s  {s['engine'] or '?'}/{s['model'] or '?'}"
        )
        for name, v in s["expect"].items():
            lines.append(f"    expect {name}: {v['hits']}/{v['of']}")
        for name, v in s["forbid"].items():
            lines.append(f"    forbid {name}: {v['violations']}/{v['of']} violations")
        if "false_positive" in s:
            fp = s["false_positive"]
            lines.append(f"    false-positive runs: {fp['runs']}/{fp['of']}")
    r, fp = tot["recall"], tot["false_positive"]
    lines += [
        "",
        f"overall recall: {r['hits']}/{r['of']} ({_fmt(r['rate'])})",
        f"control+clean false-positive rate: {fp['runs']}/{fp['of']} ({_fmt(fp['rate'])})",
        f"forbid violations: {tot['forbid_violations']}  error runs: {tot['error_runs']}",
    ]
    return "\n".join(lines)


# --- driver ------------------------------------------------------------------


def evaluate(
    cases: list[dict], runner: Runner, *, runs: int, jobs: int, franky_bin: str, max_duration: int
) -> tuple[list[dict], list[list[dict]]]:
    work = [(i, case) for i, case in enumerate(cases) for _ in range(runs)]

    def one(item: tuple[int, dict]) -> dict:
        return run_once(item[1], runner, franky_bin, max_duration)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, work))
    per_case: list[list[dict]] = [[] for _ in cases]
    for (i, _), rec in zip(work, results):
        per_case[i].append(rec)
    return [score_case(c, r) for c, r in zip(cases, per_case)], per_case


def _subprocess_runner(argv: Sequence[str], timeout: float | None = None) -> tuple[int, str]:
    # A timeout raises; run_once records it as an ERROR run.
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, errors="replace", timeout=timeout
    )
    return proc.returncode, proc.stdout


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score `franky review-pr` on pinned historical cases.")
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("-n", "--runs", type=int, default=3)
    ap.add_argument("--jobs", type=int, choices=(1, 2), default=1)
    ap.add_argument("--franky-bin", default="franky")
    ap.add_argument("--max-duration", type=int, default=1500)
    ap.add_argument("--only", action="append", default=[], metavar="ID")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", dest="json_out", type=Path)
    args = ap.parse_args(argv)
    if args.runs < 1 or args.max_duration < 1:
        ap.error("-n and --max-duration must be >= 1")
    if runner is None:

        def runner(cmd):
            return _subprocess_runner(cmd, timeout=args.max_duration + 300)

    try:
        cases = load_cases(args.cases)
    except CaseError as exc:
        print(f"review_eval: {exc}", file=sys.stderr)
        return 2
    if args.only:
        unknown = set(args.only) - {c["id"] for c in cases}
        if unknown:
            print(f"review_eval: unknown --only ids {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in args.only]
    scores, per_case = evaluate(
        cases,
        runner,
        runs=args.runs,
        jobs=args.jobs,
        franky_bin=args.franky_bin,
        max_duration=args.max_duration,
    )
    tot = totals(scores)
    print(format_report(scores, tot))
    if args.json_out:
        record = {
            "label": args.label,
            "franky_bin": args.franky_bin,
            "scores": scores,
            "totals": tot,
            "runs": {c["id"]: r for c, r in zip(cases, per_case)},
        }
        args.json_out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
