# Franky eval harness (#25)

Measure whether a change to the persona, prompt, model, or (later) profile makes Franky
**better or worse** - instead of guessing. Agent quality is non-deterministic, so the metric is
**pass-rate over a golden task set run N times**, not "it worked once".

This is **opt-in and out-of-band**, exactly like `make smoke-dind`: it runs the *real* Franky
flow, so it needs real Docker + engine creds + GitHub access and costs real spend/time. It is
deliberately NOT part of the fast, hermetic unit suite (which stays docker-free and sub-second).
Only the harness *logic* is unit-tested (`tests/test_eval.py`, with an injected fake runner).

## Prerequisites

Same as a normal `franky build` (see the top-level README "Security" + "Install"):

- Both images built/pulled (`docker build -t franky . && docker build -t franky-proxy proxy/`).
- `FRANKY_ALLOWED_REPOS`, `GH_TOKEN`, and the selected engine's creds configured - run
  `franky config init` (writes `~/.franky/config`) or export them as env vars.
- A **throwaway sandbox repo** you own and are happy to have PRs opened against. Use a junk
  repo - the agent is autonomous and will push branches + open PRs.

## Configure the task set

Edit [`tasks.json`](tasks.json) and replace every `<your-sandbox-repo>` with your sandbox
`owner/repo`. Each task is:

| field | meaning |
|-------|---------|
| `id` | short label shown in the report |
| `input` | the `franky build` task input: a prose request, a JIRA key, or a GitHub issue URL |
| `repo` | target `owner/repo` (must be in `FRANKY_ALLOWED_REPOS`) |
| `engine` | *(optional)* engine for this task; overridden by `--engine` / comparison mode |
| `expect` | list of success checkers, **all** of which must pass for a run to count |

Checkers (all must pass for a run to count as a success):

Output checkers - derived from the `franky build` result, no extra network needed:

- `pr_opened` - a PR URL was printed to stdout (the baseline "Franky did its job").
- `exit_zero` - the build exited 0.

Artifact checkers - fetch the live PR via `gh pr view` / `gh pr diff`; require `gh` installed
and authenticated. They warn to stderr and fail gracefully if `gh` is unavailable:

- `diff_touches_files` - every path in the task's `files` list was touched by the PR diff.
  Matching is suffix-based: a declared path `"cli.py"` matches a changed path `"franky/cli.py"`.
  Full paths also match exactly. Requires `files` to be non-empty (load-time error if missing).
- `change_present` - every string in the task's `contains` list appears as a substring of the
  PR diff. Plain substring test, not regex. Requires `contains` to be non-empty (load-time
  error if missing).

### Artifact checker task fields

Add `files` and/or `contains` alongside `expect`:

```json
{
  "id": "add-flag",
  "input": "Add a --verbose flag to the CLI.",
  "repo": "me/sandbox",
  "expect": ["pr_opened", "exit_zero", "diff_touches_files", "change_present"],
  "files": ["cli.py"],
  "contains": ["--verbose"]
}
```

`files` matching semantics: `"cli.py"` matches `"franky/cli.py"` (suffix match) and
`"franky/cli.py"` (exact match), but NOT `"other/xcli.py"` (not a path-component suffix).

`contains` matching semantics: plain Python `in` substring test against the full diff text.

## Run

```bash
# Pass-rate over the set, 3 runs per task (non-determinism needs a sample):
make eval ARGS="-n 3"

# Or directly:
python3 scripts/eval.py --tasks evals/tasks.json -n 3 --engine pi

# Comparison mode: run the set under two engines and report the per-task + overall delta.
python3 scripts/eval.py -n 3 --engine pi --compare-engine codex

# Profile comparison: does an operator profile (#23) make Franky better? Run the set with the
# profile OFF vs ON and report the delta. `franky build --profile P` injects that profile; an
# empty [profile] injects nothing, so it is the "off" side.
printf '[profile]\n' > /tmp/empty-profile.toml
python3 scripts/eval.py -n 3 --profile /tmp/empty-profile.toml --compare-profile ~/.franky/profile.toml
```

Two comparison axes are wired: `--compare-engine` (varies engine, holds `--profile` constant)
and `--compare-profile` (varies profile, holds `--engine` constant). They are mutually exclusive
- a single delta report varies one axis, so passing both is an error.

**Why the empty baseline:** with no `--profile`, `franky build` auto-discovers
`~/.franky/profile.toml`, so an unset baseline is NOT truly "off" (it would compare the profile
against itself). Pass an empty `[profile]` file as `--profile` for a genuine off-vs-on run; side
A then injects no bundle and side B injects the real profile.

Reading the report: `passes/runs` and a pass-rate per task, plus a pooled `OVERALL`. Comparison
mode shows `rate_a -> rate_b (+/- pts)` per task. A change is "better" when it moves the overall
pass-rate up without regressing individual tasks - that is the signal #23 (profiles) and #24
(iteration) need to justify their quality claims.

## Scope (MVP)

In: the golden set, the opt-in runner, pass-rate + comparison along two axes (engine and
profile). Out (for now): CI integration (needs creds + spend), a large benchmark suite,
model/persona levers on the build CLI (`franky build` has no `--model` flag or persona selector
yet), and LLM-as-judge for fuzzy criteria.
