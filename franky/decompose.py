"""Parse and shape the `franky plan` decomposition payload (pure - no I/O, no docker).

The `plan` command runs a read-only container pass whose final output ends in a
nonce-fenced sentinel block carrying a compact JSON decomposition. This module owns the
two pure halves of consuming that:

- `parse_decomposition` extracts the JSON from the engine's transcript. Engines emit one
  JSON object per line (JSONL) and the agent's answer text is itself JSON-ESCAPED inside an
  event value, so json.loads on each line DECODES that text back to a real string we can
  scan. A plain-text (non-JSONL) engine is covered too by also scanning the raw output.

- `build_plan_result` normalizes the parsed dict into the stable success envelope the CLI
  emits under --json (a DISTINCT envelope from build/iterate's result_schema).

WHY a per-run nonce in the sentinel: a hostile issue body or repo file could embed a fixed
sentinel to hijack the decomposition payload Franky reports. A per-run token the attacker
cannot predict defeats that - the same spirit as the repo-scoped PR-URL guard.
"""

from __future__ import annotations

import json
import re


def _collect_string_leaves(value: object, out: list[str]) -> None:
    """Recursively append every string leaf in `value` to `out`.

    Engines wrap the agent's answer text inside nested event dicts/lists; the sentinel
    block lives in one of those string leaves (json.loads already decoded the escaping when
    the line was parsed). Walking all leaves means we find it wherever the engine version
    happens to put it.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_string_leaves(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_string_leaves(v, out)


def parse_decomposition(output: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced decomposition JSON from the engine transcript, or None.

    Builds candidate strings to scan: every string leaf of every JSON line (the decoded
    agent text), plus those leaves joined by newlines (covers a sentinel word-wrapped across
    a single leaf), plus the raw output itself (covers a plain-text, non-JSONL engine).

    The fence regex captures `{` ... `}` greedily but stops at the `_END` sentinel via a
    tempered negative lookahead: it must reach the LAST `}` before `_END`, so a `}` inside a
    subtask summary does NOT truncate the JSON (a plain non-greedy match would stop at the
    first inner `}`), yet it must NOT span across a `_END`...`_BEGIN` boundary into a later
    block (a plain `.*` greedy match would, swallowing two blocks into one invalid string).
    finditer then yields one match per block and the LAST match overall wins (the agent's
    final block is authoritative). Any failure - no match, bad JSON, or a non-dict payload -
    returns None; this function never raises.
    """
    leaves: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # not JSON (banner, log line) - skip, never raise
        _collect_string_leaves(event, leaves)

    candidates = list(leaves)
    if leaves:
        candidates.append("\n".join(leaves))
    candidates.append(output)

    end = f"FRANKY_PLAN_{re.escape(nonce)}_END"
    pat = re.compile(
        rf"FRANKY_PLAN_{re.escape(nonce)}_BEGIN(\{{(?:(?!{end}).)*\}}){end}",
        re.DOTALL,
    )

    # Track the last candidate-match that BOTH matches the fence AND parses to a dict. WHY
    # parse-while-walking rather than capture-then-parse-once: the raw `output` candidate is a
    # fallback that re-contains the sentinel with the JSON quotes still backslash-escaped (it
    # is itself a JSON line), which does not json.loads. A decoded leaf earlier in the list is
    # the real payload, so we must not let a later non-parseable raw match clobber it.
    result: dict | None = None
    for candidate in candidates:
        for m in pat.finditer(candidate):
            try:
                parsed = json.loads(m.group(1))
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                result = parsed  # keep walking; last valid wins
    return result


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
