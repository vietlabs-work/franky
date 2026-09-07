# Franky eval harness

The eval harness measures agent pass rate over a task set. Use it for prompt, persona, engine, model, or profile changes.

Runs use real Docker, credentials, model calls, GitHub access, time, and money. They are not part of the hermetic unit suite.

## Prerequisites

- Working Franky task and proxy images.
- `FRANKY_ALLOWED_REPOS`, `GH_TOKEN`, and engine credentials.
- A disposable repository where Franky can push branches and open PRs.
- The host `gh` CLI for artifact checks.

Never target a valuable repository. The evaluated agent is autonomous.

## Task file

Edit [`tasks.json`](tasks.json) and replace each placeholder repository.

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | Yes | Short report label |
| `input` | Yes | Prose, JIRA key, or GitHub issue URL |
| `repo` | Yes | Allowed `owner/repo` target |
| `engine` | No | Per-task engine unless a command flag overrides it |
| `expect` | No | Checks that must pass; default `["pr_opened"]` |
| `files` | For `diff_touches_files` | Required paths or path suffixes |
| `contains` | For `change_present` | Required literal diff strings |

Available checks:

| Check | Meaning |
|-------|---------|
| `pr_opened` | Franky returned a PR URL. |
| `exit_zero` | The build exited successfully. |
| `diff_touches_files` | The PR changed every declared path or path suffix. |
| `change_present` | The PR diff contains every declared literal string. |

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

`cli.py` matches `franky/cli.py` by path-component suffix. `contains` uses a plain substring match, not a regular expression.

## Run

```bash
make eval ARGS="-n 3"
python3 scripts/eval.py --tasks evals/tasks.json -n 3 --engine pi
python3 scripts/eval.py -n 3 --engine pi --compare-engine codex
```

Compare a run without an operator profile to a run with one:

```bash
printf '[profile]\n' > /tmp/empty-profile.toml
python3 scripts/eval.py -n 3 \
  --profile /tmp/empty-profile.toml \
  --compare-profile ~/.franky/profile.toml
```

An omitted profile auto-discovers `~/.franky/profile.toml`. Use an empty profile for a true disabled baseline.

`--compare-engine` and `--compare-profile` are mutually exclusive. Each report changes one axis.

The report shows passes, runs, rate per task, and pooled overall rate. Comparison mode also shows percentage-point changes.

Use several runs because agent behavior is probabilistic. Check both overall gains and individual task regressions.

The harness does not provide CI integration, a large benchmark suite, model selection on `franky build`, or an LLM judge.
