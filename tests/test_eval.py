"""Tests for the out-of-band eval harness (issue #25).

`scripts/eval.py` is a standalone script (like `scripts/release.py`), so it is loaded via
importlib rather than imported as a package module. The harness logic is exercised with an
INJECTED fake runner - no real `franky build`, no docker, no creds, no network - so this stays
in the fast offline suite even though the harness itself drives the real flow out-of-band.

Unlike `test_release.py`, the loader pre-registers the module in `sys.modules` because
`eval.py` uses `@dataclass` with `from __future__ import annotations` (see load_eval).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PR_URL = "https://github.com/octocat/hello/pull/7"


def load_eval():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval.py"
    spec = importlib.util.spec_from_file_location("eval_harness", path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: with `from __future__ import annotations`, dataclasses resolves
    # field annotations lazily via sys.modules[cls.__module__], which fails for an unregistered
    # importlib module. (When run as `python3 scripts/eval.py` the module is "__main__", so this
    # only bites the test-load path.)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ev = load_eval()


def make_runner(scripted):
    """A fake runner returning successive (returncode, stdout) pairs from `scripted`, recording
    the argv of each call. A single (code, out) pair is reused for every call."""
    calls = []

    def runner(argv):
        calls.append(list(argv))
        if isinstance(scripted, tuple):
            return scripted
        return scripted[len(calls) - 1]

    runner.calls = calls
    return runner


def _task(**kw):
    base = {"id": "t1", "input": "do it", "repo": "me/sandbox", "expect": ("pr_opened",)}
    base.update(kw)
    return ev.EvalTask(**base)


# --- checkers ---------------------------------------------------------------


def test_pr_opened_true_when_url_in_stdout():
    assert ev.CHECKERS["pr_opened"](0, f"opened {PR_URL}") is True


def test_pr_opened_false_when_no_url():
    assert ev.CHECKERS["pr_opened"](0, "no url here") is False


def test_exit_zero_checker():
    assert ev.CHECKERS["exit_zero"](0, "") is True
    assert ev.CHECKERS["exit_zero"](1, f"but {PR_URL}") is False


# --- argv shape -------------------------------------------------------------


def test_build_argv_includes_repo_and_engine():
    argv = ev.build_argv(_task(engine="codex"))
    assert argv == ["franky", "build", "do it", "--repo", "me/sandbox", "--engine", "codex"]


def test_build_argv_omits_engine_when_none():
    argv = ev.build_argv(_task())
    assert argv == ["franky", "build", "do it", "--repo", "me/sandbox"]
    assert "--engine" not in argv


def test_build_argv_engine_override_wins():
    argv = ev.build_argv(_task(engine="pi"), engine_override="claude")
    assert argv[-2:] == ["--engine", "claude"]


# --- run_once / run_task ----------------------------------------------------


def test_run_once_pass_on_pr_url():
    runner = make_runner((0, f"done {PR_URL}"))
    assert ev.run_once(_task(), runner) is True


def test_exit_zero_but_no_pr_url_distinguishes_checkers():
    # The defining case: build exited 0 but printed no PR URL.
    runner = make_runner((0, "franky: no PR URL found in agent output"))
    assert ev.run_once(_task(expect=("pr_opened",)), runner) is False
    assert ev.run_once(_task(expect=("exit_zero",)), runner) is True


def test_run_once_requires_all_expected_checkers():
    runner = make_runner((1, f"opened {PR_URL}"))  # has URL but exited non-zero
    assert ev.run_once(_task(expect=("pr_opened", "exit_zero")), runner) is False


def test_run_task_counts_passes_over_n():
    # 3 runs: pass, fail (no url), pass -> 2/3.
    runner = make_runner([(0, PR_URL), (0, "nope"), (0, PR_URL)])
    res = ev.run_task(_task(), n=3, runner=runner)
    assert res.runs == 3
    assert res.passes == 2
    assert res.pass_rate == pytest.approx(2 / 3)


def test_run_task_passes_engine_override_into_argv():
    runner = make_runner((0, PR_URL))
    ev.run_task(_task(engine="pi"), n=1, runner=runner, engine_override="codex")
    assert runner.calls[0][-2:] == ["--engine", "codex"]


def test_placeholder_repo_raises():
    runner = make_runner((0, PR_URL))
    with pytest.raises(ValueError, match="placeholder"):
        ev.run_once(_task(repo="<your-sandbox-repo>"), runner)


def test_placeholder_in_input_raises():
    # The issue-driven task has a placeholder in its URL input, not just repo.
    runner = make_runner((0, PR_URL))
    with pytest.raises(ValueError, match="placeholder"):
        ev.run_once(_task(repo="me/sandbox", input="https://github.com/<x>/issues/1"), runner)


# --- load_tasks -------------------------------------------------------------


def test_load_tasks_from_json(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text(
        json.dumps(
            [
                {"id": "a", "input": "add x", "repo": "me/r", "expect": ["pr_opened"]},
                {"id": "b", "input": "fix y", "repo": "me/r", "engine": "codex"},
            ]
        ),
        encoding="utf-8",
    )
    tasks = ev.load_tasks(p)
    assert [t.id for t in tasks] == ["a", "b"]
    assert tasks[1].engine == "codex"
    # default expect when omitted
    assert tasks[1].expect == ("pr_opened",)


def test_load_tasks_unknown_checker_raises(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text(
        json.dumps([{"id": "a", "input": "x", "repo": "me/r", "expect": ["bogus"]}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bogus|unknown checker"):
        ev.load_tasks(p)


# --- score / compare / report ----------------------------------------------


def test_overall_pass_rate():
    results = [
        ev.TaskResult("a", runs=2, passes=2),
        ev.TaskResult("b", runs=2, passes=1),
    ]
    # 3 passes over 4 runs
    assert ev.overall_pass_rate(results) == pytest.approx(0.75)


def test_overall_pass_rate_empty_is_zero():
    assert ev.overall_pass_rate([]) == 0.0


def test_compare_reports_per_task_delta():
    a = [ev.TaskResult("t", runs=4, passes=1)]  # 0.25
    b = [ev.TaskResult("t", runs=4, passes=3)]  # 0.75
    comps = ev.compare(a, b)
    assert len(comps) == 1
    assert comps[0].task_id == "t"
    assert comps[0].delta == pytest.approx(0.5)


def test_format_report_shows_task_and_rate():
    out = ev.format_report([ev.TaskResult("alpha", runs=2, passes=1)])
    assert "alpha" in out
    assert "50" in out  # 50%


def test_compare_skips_tasks_missing_from_either_side():
    a = [ev.TaskResult("shared", runs=2, passes=1), ev.TaskResult("only_a", runs=2, passes=2)]
    b = [ev.TaskResult("shared", runs=2, passes=2), ev.TaskResult("only_b", runs=2, passes=0)]
    comps = ev.compare(a, b)
    # only the task present on BOTH sides is comparable
    assert [c.task_id for c in comps] == ["shared"]


def test_format_comparison_shows_delta():
    a = [ev.TaskResult("t", runs=2, passes=0)]
    b = [ev.TaskResult("t", runs=2, passes=2)]
    out = ev.format_comparison(a, b, "pi", "codex")
    assert "pi" in out and "codex" in out
    assert "t" in out


# --- main() (argument wiring) -----------------------------------------------


def _write_tasks(tmp_path, entries):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def test_main_report_path(tmp_path, capsys):
    p = _write_tasks(tmp_path, [{"id": "a", "input": "x", "repo": "me/r"}])
    rc = ev.main(["--tasks", str(p), "-n", "2"], runner=make_runner((0, PR_URL)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "a" in out and "OVERALL" in out


def test_main_compare_path(tmp_path, capsys):
    p = _write_tasks(tmp_path, [{"id": "a", "input": "x", "repo": "me/r"}])
    rc = ev.main(
        ["--tasks", str(p), "--engine", "pi", "--compare-engine", "codex"],
        runner=make_runner((0, PR_URL)),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "pi" in out and "codex" in out


def test_main_placeholder_exits_2(tmp_path, capsys):
    p = _write_tasks(tmp_path, [{"id": "a", "input": "x", "repo": "<sandbox>"}])
    rc = ev.main(["--tasks", str(p)], runner=make_runner((0, PR_URL)))
    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_main_empty_task_set_exits_2(tmp_path, capsys):
    p = _write_tasks(tmp_path, [])
    rc = ev.main(["--tasks", str(p)], runner=make_runner((0, PR_URL)))
    assert rc == 2
