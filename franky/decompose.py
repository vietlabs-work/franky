"""Parse and shape the `franky plan` decomposition payload (pure - no I/O, no docker).

The `plan` command runs a read-only container pass whose final output ends in a
nonce-fenced sentinel block carrying a compact JSON decomposition. This module owns the
two pure halves of consuming that:

- `parse_decomposition` extracts the JSON from the engine's transcript via the shared
  `sentinel.scan_sentinel_json` scanner (label "PLAN") - see that module for the JSONL-leaf
  walk + tempered-regex details it shares with `franky job diagnose` (#64).

- `build_plan_result` normalizes the parsed dict into the stable success envelope the CLI
  emits under --json (a DISTINCT envelope from build/iterate's result_schema).

WHY a per-run nonce in the sentinel: a hostile issue body or repo file could embed a fixed
sentinel to hijack the decomposition payload Franky reports. A per-run token the attacker
cannot predict defeats that - the same spirit as the repo-scoped PR-URL guard.
"""

from __future__ import annotations

from .sentinel import scan_sentinel_json


def parse_decomposition(output: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced decomposition JSON from the engine transcript, or None.

    Thin wrapper over `sentinel.scan_sentinel_json(output, "PLAN", nonce)`: the `plan` block is
    `FRANKY_PLAN_<nonce>_BEGIN{...}_END`. All the scanning subtlety (candidate building, tempered
    fence regex, last-valid-wins) lives in `sentinel`; never raises.
    """
    return scan_sentinel_json(output, "PLAN", nonce)


def _shape_subtask(raw: object, default_repo: str) -> dict | None:
    """Normalize one raw subtask into {title, summary, suggested_repo}, or None to drop it.

    A subtask must be a dict with a non-empty title; anything else is malformed and dropped
    (the engine is autonomous, so we never trust the shape). `suggested_repo` defaults to the
    plan's target repo when missing or blank so a caller always has a repo to act on.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    summary = raw.get("summary")
    suggested = raw.get("suggested_repo")
    if not isinstance(suggested, str) or not suggested.strip():
        suggested = default_repo
    return {
        "title": title.strip(),
        "summary": summary.strip() if isinstance(summary, str) else "",
        "suggested_repo": suggested.strip(),
    }


def build_plan_result(
    parsed: dict,
    *,
    engine: str,
    repo: str,
    exit_code: int = 0,
) -> dict:
    """Shape the parsed decomposition into the stable `plan` success envelope.

    Coerces `fits_one_pr` to bool, normalizes each subtask (dropping malformed ones and
    defaulting a blank `suggested_repo` to `repo`), coerces `rationale` to a str, and drops
    any unknown top-level keys. The returned dict is the DISTINCT plan envelope (separate
    from build/iterate's result_schema). Pure - the caller redacts the serialized string.
    """
    raw_subtasks = parsed.get("subtasks")
    subtasks: list[dict] = []
    if isinstance(raw_subtasks, list):
        for raw in raw_subtasks:
            shaped = _shape_subtask(raw, repo)
            if shaped is not None:
                subtasks.append(shaped)

    rationale = parsed.get("rationale")
    return {
        "fits_one_pr": bool(parsed.get("fits_one_pr")),
        "subtasks": subtasks,
        "rationale": rationale if isinstance(rationale, str) else "",
        "engine": engine,
        "repo": repo,
        "exit_code": exit_code,
    }
