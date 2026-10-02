"""Tests for scripts/review_eval.py. Fake runner only: no franky, docker, gh, or network."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "review_eval.py"
    spec = importlib.util.spec_from_file_location("review_eval_harness", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


re_ = _load()

SHA, BASE = "a" * 40, "b" * 40


def case(**over):
    c = {"id": "c1", "pr_url": "https://github.com/x/y/pull/1", "sha": SHA, "base_sha": BASE}
    c.update(over)
    return c


def result(findings, **over):
    r = {
        "status": "review_complete",
        "reviewed_sha": SHA,
        "findings": findings,
        "findings_total": len(findings),
        "engine": "claude",
        "economics": {"cost_usd": 0.5, "duration_s": 10.0},
    }
    r.update(over)
    return r


def f(title="t", body="", severity="normal", file="a.py"):
    return {"title": title, "body": body, "severity": severity, "file": file}


def runner_of(payload, code=0):
    calls = []

    def run(argv):
        calls.append(list(argv))
        return code, payload if isinstance(payload, str) else json.dumps(payload)

    run.calls = calls
    return run


# --- case file ---------------------------------------------------------------


def test_example_file_is_valid():
    path = Path(__file__).resolve().parents[1] / "evals" / "review_cases.example.json"
    assert re_.load_cases(path)[0]["id"] == "example-missing-null-check"


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
        [case(sha="A" * 40)],
        [case(sha="abc")],
        [case(base_sha="z" * 40)],
        [case(), case()],
        [case(extra=1)],
        [case(expect=[{"name": "n", "any_of": ["("], "min_severity": "normal"}])],
        [case(expect=[{"name": "n", "any_of": ["x"], "min_severity": "huge"}])],
        [case(expect=[{"name": "n", "any_of": [], "min_severity": "nit"}])],
        [case(expect=[{"name": "n", "any_of": ["x"], "all_of": ["("], "min_severity": "nit"}])],
        [case(forbid=[{"name": "n", "any_of": ["x"], "severities": ["bogus"]}])],
        [case(clean="yes")],
    ],
)
def test_validate_cases_refuses_bad(bad):
    with pytest.raises(re_.CaseError):
        re_.validate_cases(bad)


def test_load_cases_refuses_unreadable_and_bad_json(tmp_path):
    with pytest.raises(re_.CaseError):
        re_.load_cases(tmp_path / "missing.json")
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    with pytest.raises(re_.CaseError):
        re_.load_cases(p)


# --- argv and classification --------------------------------------------------


def test_argv():
    assert re_.build_argv(case(), "franky", 900) == [
        "franky", "review-pr", "--no-publish", "--json", "--max-duration", "900",
        "--at-sha", SHA, "--diff-base", BASE, "--", "https://github.com/x/y/pull/1",
    ]  # fmt: skip


def test_run_once_valid():
    run = runner_of(result([f()]))
    rec = re_.run_once(case(), run, "fb", 100)
    assert rec["valid"] and len(rec["findings"]) == 1
    assert run.calls[0][0] == "fb"


@pytest.mark.parametrize(
    "payload,code",
    [
        (result([f()], status="no_findings"), 0),
        (result([f()], status="agent_error"), 7),
        (result([f()], reviewed_sha="c" * 40), 0),
        ({k: v for k, v in result([]).items() if k != "findings"}, 0),
        (result("nope"), 0),
        ("not json", 0),
        ("[1]", 0),
        (result([f()]), 1),
        ({"error": {"kind": "usage_error", "message": "m"}}, 2),
    ],
)
def test_error_runs_are_invalid(payload, code):
    rec = re_.run_once(case(), runner_of(payload, code), "franky", 100)
    assert not rec["valid"] and rec["reason"]


def test_runner_crash_is_error_run():
    def boom(argv):
        raise OSError("no franky")

    assert not re_.run_once(case(), boom, "franky", 1)["valid"]


def test_error_run_never_counts_as_clean():
    runs = [re_.run_once(case(clean=True), runner_of("junk"), "franky", 1)]
    s = re_.score_case(case(clean=True), runs)
    assert s["valid_runs"] == 0 and s["error_runs"] == 1
    assert s["false_positive"] == {"runs": 0, "of": 0}
    assert re_.totals([s])["false_positive"]["rate"] is None  # unknown, not a pass


# --- matching ----------------------------------------------------------------


def spec(**k):
    base = {"name": "n", "any_of": ["race"], "min_severity": "normal"}
    base.update(k)
    return base


def test_expect_matching():
    hit = re_.expect_hit
    assert hit([f("race in cache")], spec())
    assert not hit([f("Unrelated")], spec())
    assert hit([f("x", "a race here")], spec())  # body counts
    # all_of: every one must match
    assert hit([f("race and lock")], spec(all_of=["lock", "race"]))
    assert not hit([f("race only")], spec(all_of=["lock"]))
    # any_of: one is enough
    assert hit([f("deadlock")], spec(any_of=["race", "dead"]))
    # severity floor
    assert not hit([f("race", severity="nit")], spec())
    assert hit([f("race", severity="blocking")], spec())
    assert hit([f("race", severity="nit")], spec(min_severity="nit"))
    assert hit([f("race", severity="weird")], spec())  # unknown -> normal
    assert not hit([f("race", severity="weird")], spec(min_severity="blocking"))
    # file substring
    assert hit([f("race", file="src/cache.py")], spec(file="cache.py"))
    assert not hit([f("race", file="src/cache.py")], spec(file="cache"))
    assert not hit([f("race", file="src/other.py")], spec(file="cache"))
    assert not hit([f("race", file=None)], spec(file="cache"))
    assert not hit([], spec())


def test_forbid_matching():
    v = re_.forbid_violated
    s = {"name": "n", "any_of": ["rename"], "severities": ["normal", "blocking"]}
    assert v([f("rename it")], s)
    assert not v([f("rename it", severity="nit")], s)
    assert not v([f("other")], s)
    assert not v([f("rename it")], {**s, "all_of": ["zzz"]})


# --- scoring -----------------------------------------------------------------


def test_score_expect_forbid_and_clean():
    c = case(
        expect=[spec(name="e")],
        forbid=[{"name": "fb", "any_of": ["style"], "severities": ["normal"]}],
        clean=True,
    )
    runs = [
        re_.classify_run(c, 0, json.dumps(result([f("race"), f("style")]))),
        re_.classify_run(c, 0, json.dumps(result([f("nit only", severity="nit")]))),
        re_.classify_run(c, 0, "junk"),
    ]
    s = re_.score_case(c, runs)
    assert s["expect"]["e"] == {"hits": 1, "of": 2}
    assert s["forbid"]["fb"] == {"violations": 1, "of": 2}
    assert s["false_positive"] == {"runs": 1, "of": 2}
    assert s["error_runs"] == 1 and s["findings_total_mean"] == 1.5
    tot = re_.totals([s])
    assert tot["recall"]["rate"] == 0.5 and tot["error_runs"] == 1


def test_control_case_scores_false_positives_but_plain_case_does_not():
    runs = [re_.classify_run(case(), 0, json.dumps(result([f()])))]
    assert "false_positive" not in re_.score_case(case(), runs)
    assert re_.score_case(case(control_of="c0"), runs)["false_positive"] == {"runs": 1, "of": 1}


def test_cost_unknown_stays_null_never_zero():
    def mk(cost):
        return result([], economics={"cost_usd": cost, "duration_s": 5})

    runs = [re_.classify_run(case(), 0, json.dumps(mk(None)))] * 2
    s = re_.score_case(case(), runs)
    assert s["cost_usd_mean"] is None and s["duration_s_mean"] == 5
    mixed = [re_.classify_run(case(), 0, json.dumps(mk(c))) for c in (None, 1.0, 3.0)]
    assert re_.score_case(case(), mixed)["cost_usd_mean"] == 2.0
    assert "n/a" in re_.format_report([s], re_.totals([s]))


# --- main --------------------------------------------------------------------


def _write(tmp_path, cases):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(cases))
    return p


def test_main_only_filter_runs_and_json_out(tmp_path, capsys):
    p = _write(tmp_path, [case(id="a"), case(id="b", expect=[spec(name="e")])])
    out = tmp_path / "out.json"
    run = runner_of(result([f("race")]))
    rc = re_.main(
        ["--cases", str(p), "-n", "2", "--only", "b", "--label", "L", "--json", str(out),
         "--franky-bin", "fb"],
        runner=run,
    )  # fmt: skip
    assert rc == 0
    assert len(run.calls) == 2 and all(c[0] == "fb" for c in run.calls)
    rec = json.loads(out.read_text())
    assert rec["label"] == "L" and rec["franky_bin"] == "fb"
    assert list(rec["runs"]) == ["b"] and rec["scores"][0]["expect"]["e"]["hits"] == 2
    assert "overall recall: 2/2" in capsys.readouterr().out


def test_main_refuses_bad_file_unknown_only_and_bad_jobs(tmp_path):
    assert re_.main(["--cases", str(_write(tmp_path, [{"id": 1}]))], runner=runner_of({})) == 2
    p = _write(tmp_path, [case()])
    assert re_.main(["--cases", str(p), "--only", "nope"], runner=runner_of({})) == 2
    with pytest.raises(SystemExit):
        re_.main(["--cases", str(p), "--jobs", "3"], runner=runner_of({}))


def test_jobs_two_collects_every_run(tmp_path):
    p = _write(tmp_path, [case(id="a"), case(id="b")])
    run = runner_of(result([]))
    assert re_.main(["--cases", str(p), "-n", "3", "--jobs", "2"], runner=run) == 0
    assert len(run.calls) == 6


def _f(**over):
    f = {"title": "t", "body": "b", "severity": "normal", "file": "a.py", "line": 1}
    f.update(over)
    return f


@pytest.mark.parametrize("findings", [[None], [{"title": "t"}], [_f(severity=None)], [_f(file=3)]])
def test_malformed_finding_is_an_error_run(findings):
    rec = re_.classify_run(case(), 0, json.dumps(result(findings)))
    assert rec["valid"] is False and "malformed" in rec["reason"]


def test_truncated_findings_are_an_error_run():
    rec = re_.classify_run(case(), 0, json.dumps(result([_f()], findings_total=9)))
    assert rec["valid"] is False and "truncated" in rec["reason"]


@pytest.mark.parametrize(
    "kind,item",
    [
        ("expect", {"name": "n", "any_of": ["x"], "min_severity": "nit"}),
        ("forbid", {"name": "n", "any_of": ["x"], "severities": ["normal"]}),
    ],
)
def test_duplicate_names_are_refused(kind, item):
    with pytest.raises(re_.CaseError, match="duplicate name"):
        re_.validate_cases([case(**{kind: [item, dict(item)]})])


@pytest.mark.parametrize(
    "want,path,ok",
    [
        ("a.py", "a.py", True),
        ("a.py", "src/a.py", True),
        ("a.py", "src/data.py", False),
        ("migrations/", "app/db/migrations/x.sql", True),
        ("migrations/", "app/oldmigrations/x.sql", False),
    ],
)
def test_file_constraint_is_exact_suffix_or_directory(want, path, ok):
    spec = {"name": "n", "any_of": ["t"], "file": want}
    assert re_.finding_matches(_f(file=path), spec, severity_ok=lambda s: True) is ok


def test_subprocess_timeout_becomes_an_error_run(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="franky", timeout=k.get("timeout"))

    monkeypatch.setattr(re_.subprocess, "run", boom)
    rec = re_.run_once(case(), lambda cmd: re_._subprocess_runner(cmd, timeout=1), "franky", 10)
    assert rec["valid"] is False and "runner failed" in rec["reason"]


def test_example_file_marks_only_the_fixed_case_as_control():
    path = Path(__file__).resolve().parents[1] / "evals" / "review_cases.example.json"
    cases = re_.load_cases(path)
    controls = [c["id"] for c in cases if re_.is_negative_case(c)]
    assert controls == ["example-fixed-null-check"]
