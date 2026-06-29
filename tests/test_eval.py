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


# --- artifact checkers: _check_diff_touches_files ---------------------------


def _artifacts(changed_files=None, diff=""):
    return ev.PRArtifacts(changed_files=changed_files or [], diff=diff)


def test_diff_touches_files_suffix_match():
    # "cli.py" is a suffix of "franky/cli.py"
    arts = _artifacts(changed_files=["franky/cli.py"])
    task = _task(files=("cli.py",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is True


def test_diff_touches_files_no_suffix_match():
    # "xcli.py" ends with "cli.py" as a string but "other/xcli.py" does NOT end with "/cli.py"
    arts = _artifacts(changed_files=["other/xcli.py"])
    task = _task(files=("cli.py",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is False


def test_diff_touches_files_exact_full_path():
    arts = _artifacts(changed_files=["franky/cli.py"])
    task = _task(files=("franky/cli.py",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is True


def test_diff_touches_files_missing_file():
    arts = _artifacts(changed_files=["franky/engine.py"])
    task = _task(files=("cli.py",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is False


def test_diff_touches_files_empty_changed_files():
    arts = _artifacts(changed_files=[])
    task = _task(files=("cli.py",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is False


def test_diff_touches_files_empty_declared_string_no_match():
    # An empty declared string must not match any path (guards endswith("/") wildcard).
    arts = _artifacts(changed_files=["franky/cli.py"])
    task = _task(files=("",), expect=("diff_touches_files",))
    assert ev._check_diff_touches_files(arts, task) is False


# --- artifact checkers: _check_change_present -------------------------------


def test_change_present_all_substrings_present():
    arts = _artifacts(diff="+    parser.add_argument('--verbose')\n")
    task = _task(contains=("--verbose",), expect=("change_present",))
    assert ev._check_change_present(arts, task) is True


def test_change_present_one_missing():
    arts = _artifacts(diff="+    parser.add_argument('--verbose')\n")
    task = _task(contains=("--verbose", "--debug"), expect=("change_present",))
    assert ev._check_change_present(arts, task) is False


def test_change_present_empty_diff():
    arts = _artifacts(diff="")
    task = _task(contains=("--verbose",), expect=("change_present",))
    assert ev._check_change_present(arts, task) is False


# --- load_tasks: artifact checker validation --------------------------------


def test_load_tasks_diff_touches_files_without_files_raises(tmp_path):
    p = _write_tasks(
        tmp_path,
        [{"id": "a", "input": "x", "repo": "me/r", "expect": ["diff_touches_files"]}],
    )
    with pytest.raises(ValueError, match="diff_touches_files|files"):
        ev.load_tasks(p)


def test_load_tasks_change_present_without_contains_raises(tmp_path):
    p = _write_tasks(
        tmp_path,
        [{"id": "a", "input": "x", "repo": "me/r", "expect": ["change_present"]}],
    )
    with pytest.raises(ValueError, match="change_present|contains"):
        ev.load_tasks(p)


def test_load_tasks_valid_artifact_task(tmp_path):
    p = _write_tasks(
        tmp_path,
        [
            {
                "id": "a",
                "input": "x",
                "repo": "me/r",
                "expect": ["pr_opened", "diff_touches_files", "change_present"],
                "files": ["cli.py"],
                "contains": ["--verbose"],
            }
        ],
    )
    tasks = ev.load_tasks(p)
    assert tasks[0].files == ("cli.py",)
    assert tasks[0].contains == ("--verbose",)


def test_load_tasks_unknown_checker_lists_artifact_checkers(tmp_path):
    p = _write_tasks(
        tmp_path,
        [{"id": "a", "input": "x", "repo": "me/r", "expect": ["bogus"]}],
    )
    with pytest.raises(ValueError) as exc_info:
        ev.load_tasks(p)
    msg = str(exc_info.value)
    # Both artifact checker names must appear in the known: list.
    assert "diff_touches_files" in msg
    assert "change_present" in msg


# --- run_once with injected inspector ---------------------------------------


def make_inspector(artifacts):
    """Fake inspector that returns the given PRArtifacts and records calls."""
    calls = []

    def inspector(pr_url):
        calls.append(pr_url)
        return artifacts

    inspector.calls = calls
    return inspector


def test_run_once_artifact_checker_passes_with_matching_artifacts():
    runner = make_runner((0, f"opened {PR_URL}"))
    arts = _artifacts(diff="+    --verbose\n")
    insp = make_inspector(arts)
    task = _task(expect=("change_present",), contains=("--verbose",))
    assert ev.run_once(task, runner, inspector=insp) is True
    assert insp.calls == [PR_URL]


def test_run_once_artifact_checker_fails_when_not_matching():
    runner = make_runner((0, f"opened {PR_URL}"))
    arts = _artifacts(diff="+    something else\n")
    insp = make_inspector(arts)
    task = _task(expect=("change_present",), contains=("--verbose",))
    assert ev.run_once(task, runner, inspector=insp) is False


def test_run_once_inspector_not_called_for_output_only_checkers():
    # When only output checkers (pr_opened, exit_zero) are expected, inspector must not be called.
    runner = make_runner((0, f"opened {PR_URL}"))
    insp = make_inspector(_artifacts())
    task = _task(expect=("pr_opened", "exit_zero"))
    ev.run_once(task, runner, inspector=insp)
    assert insp.calls == []


def test_run_once_artifact_only_checker_with_pr_url_in_stdout():
    # Task with ONLY diff_touches_files and a PR URL in stdout -> inspector is called, result
    # depends on artifacts.
    runner = make_runner((0, f"result: {PR_URL}"))
    arts = _artifacts(changed_files=["franky/cli.py"])
    insp = make_inspector(arts)
    task = _task(expect=("diff_touches_files",), files=("cli.py",))
    assert ev.run_once(task, runner, inspector=insp) is True
    assert len(insp.calls) == 1


def test_run_once_artifact_checker_no_pr_url_fails_without_raising():
    # No PR URL in stdout -> PRArtifacts is empty -> artifact check fails, no exception.
    runner = make_runner((0, "no url here"))
    insp = make_inspector(_artifacts(changed_files=["franky/cli.py"]))
    task = _task(expect=("diff_touches_files",), files=("cli.py",))
    result = ev.run_once(task, runner, inspector=insp)
    assert result is False
    # Inspector must NOT be called when there is no PR URL to inspect.
    assert insp.calls == []


# --- main() end-to-end with inspector ---------------------------------------


def test_main_with_artifact_inspector(tmp_path, capsys):
    p = _write_tasks(
        tmp_path,
        [
            {
                "id": "a",
                "input": "x",
                "repo": "me/r",
                "expect": ["pr_opened", "change_present"],
                "contains": ["--verbose"],
            }
        ],
    )
    runner = make_runner((0, f"opened {PR_URL}"))
    arts = _artifacts(diff="+    --verbose\n")
    insp = make_inspector(arts)
    rc = ev.main(["--tasks", str(p)], runner=runner, inspector=insp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "a" in out and "OVERALL" in out
