#!/usr/bin/env python3
"""Franky eval harness (issue #25) - opt-in, OUT-OF-BAND, like `make smoke-dind`.

WHY this lives in scripts/ and not the `franky` package: it is developer tooling, not product.
It exercises the REAL franky flow (it shells out to `franky build`), so it needs real docker +
creds + spend and is slow - the opposite of the sub-second, docker-free unit suite. Keeping it
a standalone script (loaded by tests via importlib, like scripts/release.py) keeps it off the
shipped CLI surface and out of the wheel.

WHAT it measures: agent QUALITY, which is non-deterministic. So the metric is pass-RATE over a
golden task set run N times - "succeeds on >= X%", never "worked once". Comparison mode runs the
same set under two configs (engine A vs B today; persona/model later) and reports the delta, so
a persona/model/profile change can be judged instead of guessed.

The harness logic is pure and takes an INJECTABLE runner and inspector so it is unit-testable
without docker or a live `gh`; only `main()` wires in the real subprocess runner and
`_gh_inspector`. Stdlib-only.

Richer artifact checkers (`diff_touches_files`, `change_present`) are implemented and work
against the live PR via `gh pr view` / `gh pr diff`. They require `gh` installed and
authenticated for a real run; in tests inject a fake inspector.

Run:  python3 scripts/eval.py [--tasks evals/tasks.json] [-n RUNS] [--engine E] [--compare-engine E2]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Reuse Franky's canonical PR-URL pattern so "did it open a PR" matches what `franky build`
# itself detects. `franky` is installed (editable) in the dev env, so this import is available
# to the script and to the importlib-loaded test module alike.
from franky.engine import PR_URL_RE

# A runner runs ONE `franky build` invocation and returns (returncode, stdout). The real one
# shells out (see _subprocess_runner); tests inject a fake so no container/cred/network runs.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

DEFAULT_TASKS = Path(__file__).resolve().parents[1] / "evals" / "tasks.json"


# --- PR artifact fetching ---------------------------------------------------


@dataclass
class PRArtifacts:
    """Artifacts fetched from a live PR via `gh`."""

    changed_files: list[str]
    diff: str


# Inspector: given a PR URL, returns PRArtifacts. The real impl calls `gh`; tests inject a fake.
Inspector = Callable[[str], PRArtifacts]


def _gh_inspector(pr_url: str) -> PRArtifacts:
    """Fetch PR artifacts (changed files + diff) via `gh`. Warns to stderr on failure."""
    # Changed files: gh emits one path per line when -q selects .files[].path.
    files_proc = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "files", "-q", ".files[].path"],
        capture_output=True,
        text=True,
    )
    if files_proc.returncode != 0:
        # gh is unavailable / unauthenticated / the PR is not visible: warn once and skip the
        # second gh call - both artifact checks fail on empty artifacts anyway, and a single
        # warning keeps the root cause clear (vs. two warnings for one underlying failure).
        print(
            f"eval: gh pr view failed for {pr_url} (rc={files_proc.returncode});"
            " artifact checks will fail",
            file=sys.stderr,
        )
        return PRArtifacts(changed_files=[], diff="")
    changed_files = [line for line in files_proc.stdout.splitlines() if line.strip()]

    # Diff text.
    diff_proc = subprocess.run(
        ["gh", "pr", "diff", pr_url],
        capture_output=True,
        text=True,
    )
    if diff_proc.returncode == 0:
        diff = diff_proc.stdout
    else:
        diff = ""
        print(
            f"eval: gh pr diff failed for {pr_url} (rc={diff_proc.returncode});"
            " artifact checks will fail",
            file=sys.stderr,
        )

    return PRArtifacts(changed_files=changed_files, diff=diff)


# --- success checkers -------------------------------------------------------
# Output checkers are pure over the subprocess RESULT (returncode, stdout) - no extra network.
# Artifact checkers operate on PRArtifacts fetched via `gh` and are therefore richer but slower.


def _check_pr_opened(returncode: int, stdout: str) -> bool:
    """A PR URL was printed. WHY stdout only (not stderr/combined): `franky build` prints the
    PR URL to stdout on success; economics + errors go to stderr. Scanning stdout alone avoids
    a stderr-only URL echo (e.g. inside an error hint) faking a pass."""
    return PR_URL_RE.search(stdout) is not None


def _check_exit_zero(returncode: int, stdout: str) -> bool:
    return returncode == 0


CHECKERS: dict[str, Callable[[int, str], bool]] = {
    "pr_opened": _check_pr_opened,
    "exit_zero": _check_exit_zero,
}


def _check_diff_touches_files(artifacts: PRArtifacts, task: "EvalTask") -> bool:
    """True iff task.files is non-empty and EVERY declared path matches some changed file.

    Matching semantics: a declared path `d` matches a changed path `c` iff `d` is non-empty
    AND (`c == d` OR `c.endswith("/" + d)`). The suffix-match lets callers declare bare
    filenames (e.g. "cli.py") that resolve correctly regardless of directory depth. An empty
    declared string never matches (guards against accidentally matching any nested path via
    `endswith("/")`)."""
    if not task.files or not artifacts.changed_files:
        return False
    for declared in task.files:
        if not declared:
            return False
        if not any(
            changed == declared or changed.endswith("/" + declared)
            for changed in artifacts.changed_files
        ):
            return False
    return True


def _check_change_present(artifacts: PRArtifacts, task: "EvalTask") -> bool:
    """True iff task.contains is non-empty and EVERY substring in task.contains appears in the
    diff (plain `in` substring test, NOT regex). Returns False when the diff is empty."""
    if not task.contains or not artifacts.diff:
        return False
    return all(substring in artifacts.diff for substring in task.contains)


ARTIFACT_CHECKERS: dict[str, Callable[[PRArtifacts, "EvalTask"], bool]] = {
    "diff_touches_files": _check_diff_touches_files,
    "change_present": _check_change_present,
}

# The two registries must stay disjoint: run_once dispatches each expected name to whichever
# registry holds it, so an overlapping name would be checked twice (and load_tasks, which
# validates against the union, would silently accept it).
assert not (set(CHECKERS) & set(ARTIFACT_CHECKERS)), "checker name collision across registries"


# --- task model -------------------------------------------------------------


@dataclass
class EvalTask:
    id: str
    input: str
    repo: str
    engine: str | None = None
    # The checker names ALL of which must pass for the run to count as a success. Tuple so it
    # is hashable/immutable; defaults to pr_opened (the baseline "did Franky do its job").
    expect: tuple[str, ...] = ("pr_opened",)
    # For diff_touches_files: paths (bare filename or full path) the PR diff must touch.
    files: tuple[str, ...] = ()
    # For change_present: substrings that must all appear in the PR diff.
    contains: tuple[str, ...] = ()


@dataclass
class TaskResult:
    task_id: str
    runs: int
    passes: int

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0


@dataclass
class Comparison:
    task_id: str
    rate_a: float
    rate_b: float
    delta: float = field(init=False)

    def __post_init__(self) -> None:
        self.delta = self.rate_b - self.rate_a


def _placeholder_in(task: EvalTask) -> str | None:
    """The offending `field 'value'` if `task` still carries a `<...>` placeholder, else None.
    The shipped evals/tasks.json uses `<your-sandbox-repo>` placeholders (in BOTH repo and an
    issue-URL input) so it is obvious the operator must substitute their own throwaway repo.
    Catch it early with a clear error rather than letting `franky build` fail opaquely on the
    allowlist gate."""
    for label, value in (("repo", task.repo), ("input", task.input)):
        if "<" in value or ">" in value:
            return f"{label} '{value}'"
    return None


def load_tasks(path: str | Path) -> list[EvalTask]:
    """Parse the golden task set (JSON list). JSON not TOML: stdlib `tomllib` is 3.11+, and
    Franky targets >=3.10. Validates each `expect` name against CHECKERS + ARTIFACT_CHECKERS
    so a typo'd criterion fails loudly at load time, not silently as a never-passing run.
    Also loud-fails if an artifact checker is declared without its required data field."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    all_known = set(CHECKERS) | set(ARTIFACT_CHECKERS)
    tasks: list[EvalTask] = []
    for entry in raw:
        expect = tuple(entry.get("expect", ["pr_opened"]))
        for name in expect:
            if name not in all_known:
                raise ValueError(
                    f"task '{entry.get('id')}' names unknown checker '{name}' "
                    f"(known: {', '.join(sorted(all_known))})"
                )
        files = tuple(entry.get("files", ()))
        contains = tuple(entry.get("contains", ()))
        task_id = entry.get("id")
        if "diff_touches_files" in expect and not files:
            raise ValueError(
                f"task '{task_id}' expects 'diff_touches_files' but 'files' is empty - "
                f"add a 'files' list to this task"
            )
        if "change_present" in expect and not contains:
            raise ValueError(
                f"task '{task_id}' expects 'change_present' but 'contains' is empty - "
                f"add a 'contains' list to this task"
            )
        tasks.append(
            EvalTask(
                id=entry["id"],
                input=entry["input"],
                repo=entry["repo"],
                engine=entry.get("engine"),
                expect=expect,
                files=files,
                contains=contains,
            )
        )
    return tasks


# --- running ----------------------------------------------------------------


def build_argv(task: EvalTask, engine_override: str | None = None) -> list[str]:
    """The `franky build` argv for a task. NOTE: only --engine is passed - `franky build` has
    no --model flag today, so model/persona are not eval levers yet (engine is). engine_override
    (comparison mode) wins over the task's own engine."""
    # --plan-first is deliberately NOT passed: it sends the read-only plan pass to stdout
    # (cli.py click.echo(output)), which would let pr_opened match a PR URL mentioned in the
    # plan and fake a pass. The eval always runs the build pass.
    argv = ["franky", "build", task.input, "--repo", task.repo]
    engine = engine_override or task.engine
    if engine:
        argv += ["--engine", engine]
    return argv


def run_once(
    task: EvalTask,
    runner: Runner,
    engine_override: str | None = None,
    inspector: Inspector = _gh_inspector,
) -> bool:
    """One real `franky build` for `task`; True iff ALL of `task.expect` pass.

    Output checkers (pr_opened, exit_zero) run over the subprocess result. Artifact checkers
    (diff_touches_files, change_present) additionally call the inspector to fetch PR artifacts
    via `gh`. The inspector is called at most once per run, and only when needed."""
    placeholder = _placeholder_in(task)
    if placeholder is not None:
        raise ValueError(
            f"task '{task.id}' has a placeholder {placeholder} - edit evals/tasks.json to "
            f"point at your own throwaway sandbox repo before running the eval."
        )
    returncode, stdout = runner(build_argv(task, engine_override))

    artifact_names = [name for name in task.expect if name in ARTIFACT_CHECKERS]
    output_names = [name for name in task.expect if name in CHECKERS]

    # Run output checkers first; short-circuit if any fail.
    for name in output_names:
        if not CHECKERS[name](returncode, stdout):
            return False

    if not artifact_names:
        return True

    # Fetch artifacts once for all artifact checkers.
    url_match = PR_URL_RE.search(stdout)
    if url_match:
        artifacts = inspector(url_match.group(0))
    else:
        # No PR URL in stdout - artifact checks cannot pass (nothing to inspect).
        artifacts = PRArtifacts(changed_files=[], diff="")

    return all(ARTIFACT_CHECKERS[name](artifacts, task) for name in artifact_names)


def run_task(
    task: EvalTask,
    n: int,
    runner: Runner,
    engine_override: str | None = None,
    inspector: Inspector = _gh_inspector,
) -> TaskResult:
    """Run `task` n times (non-determinism => need a sample) and tally passes."""
    passes = sum(1 for _ in range(n) if run_once(task, runner, engine_override, inspector))
    return TaskResult(task_id=task.id, runs=n, passes=passes)


def run_set(
    tasks: Sequence[EvalTask],
    n: int,
    runner: Runner,
    engine_override: str | None = None,
    inspector: Inspector = _gh_inspector,
) -> list[TaskResult]:
    return [run_task(t, n, runner, engine_override, inspector) for t in tasks]


# --- scoring ----------------------------------------------------------------


def overall_pass_rate(results: Sequence[TaskResult]) -> float:
    """Pooled pass-rate across all runs of all tasks (total passes / total runs)."""
    total_runs = sum(r.runs for r in results)
    total_passes = sum(r.passes for r in results)
    return total_passes / total_runs if total_runs else 0.0


def compare(results_a: Sequence[TaskResult], results_b: Sequence[TaskResult]) -> list[Comparison]:
    """Per-task pass-rate delta (b - a), paired by task_id. Tasks missing from either side are
    skipped (only comparable tasks are reported)."""
    by_id_b = {r.task_id: r for r in results_b}
    out: list[Comparison] = []
    for ra in results_a:
        rb = by_id_b.get(ra.task_id)
        if rb is not None:
            out.append(Comparison(ra.task_id, ra.pass_rate, rb.pass_rate))
    return out


# --- reporting --------------------------------------------------------------


def _pct(rate: float) -> str:
    return f"{rate * 100:.0f}%"


def format_report(results: Sequence[TaskResult], label: str | None = None) -> str:
    head = f"eval results{f' ({label})' if label else ''}:"
    lines = [head]
    for r in results:
        lines.append(f"  {r.task_id:<24} {r.passes}/{r.runs:<4}  {_pct(r.pass_rate):>5}")
    # `--/--` in the passes/runs slot keeps the rate column aligned regardless of run count.
    lines.append(f"  {'OVERALL':<24} {'--/--':<6}  {_pct(overall_pass_rate(results)):>5}")
    return "\n".join(lines)


def format_comparison(
    results_a: Sequence[TaskResult],
    results_b: Sequence[TaskResult],
    label_a: str,
    label_b: str,
) -> str:
    lines = [f"eval comparison: {label_a} vs {label_b}"]
    for c in compare(results_a, results_b):
        sign = "+" if c.delta >= 0 else ""
        lines.append(
            f"  {c.task_id:<24} {_pct(c.rate_a):>5} -> {_pct(c.rate_b):>5}  "
            f"({sign}{c.delta * 100:.0f} pts)"
        )
    oa, ob = overall_pass_rate(results_a), overall_pass_rate(results_b)
    sign = "+" if (ob - oa) >= 0 else ""
    lines.append(
        f"  {'OVERALL':<24} {_pct(oa):>5} -> {_pct(ob):>5}  ({sign}{(ob - oa) * 100:.0f} pts)"
    )
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def _subprocess_runner(argv: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(list(argv), capture_output=True, text=True)
    # Surface the agent transcript on stderr so a human watching the eval sees progress; the
    # pass/fail decision uses stdout only (see _check_pr_opened).
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout


def main(
    argv: Sequence[str] | None = None,
    runner: Runner = _subprocess_runner,
    inspector: Inspector = _gh_inspector,
) -> int:
    p = argparse.ArgumentParser(description="Run the Franky eval golden-task set (out-of-band).")
    p.add_argument("--tasks", default=str(DEFAULT_TASKS), help="path to the golden task JSON.")
    p.add_argument("-n", "--runs", type=int, default=1, help="runs per task (non-determinism).")
    p.add_argument("--engine", default=None, help="engine for the (baseline) run.")
    p.add_argument(
        "--compare-engine",
        default=None,
        help="if set, run the set under --engine AND this engine, and report the delta.",
    )
    args = p.parse_args(argv)

    # load + placeholder errors are operator setup mistakes - surface them as a clean one-liner,
    # not a traceback (the run loop raises ValueError on an unedited placeholder repo).
    try:
        tasks = load_tasks(args.tasks)
        if not tasks:
            print("no tasks to run", file=sys.stderr)
            return 2

        if args.compare_engine:
            base_label = args.engine or "default"
            results_a = run_set(
                tasks, args.runs, runner, engine_override=args.engine, inspector=inspector
            )
            results_b = run_set(
                tasks, args.runs, runner, engine_override=args.compare_engine, inspector=inspector
            )
            print(format_comparison(results_a, results_b, base_label, args.compare_engine))
        else:
            results = run_set(
                tasks, args.runs, runner, engine_override=args.engine, inspector=inspector
            )
            print(format_report(results, label=args.engine))
    except (ValueError, FileNotFoundError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
