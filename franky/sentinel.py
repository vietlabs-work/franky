"""Shared scanner for nonce-fenced structured-output blocks in an engine transcript.

Both `franky plan` (decompose) and `franky job diagnose` (issue #64) end their read-only pass
with EXACTLY ONE machine-readable block `FRANKY_<LABEL>_<nonce>_BEGIN{...json...}_END`. This
module owns the one tricky part of consuming that - the JSONL-leaf walk plus the tempered-regex
fence scan - so the subtle logic lives in ONE place instead of drifting between two parsers.

WHY a per-run nonce: a hostile issue body or repo file could embed a fixed sentinel to hijack the
payload Franky reports. A per-run token the attacker cannot predict defeats that - the same
spirit as the repo-scoped PR-URL guard.
"""

from __future__ import annotations

import json
import re


def _collect_string_leaves(value: object, out: list[str]) -> None:
    """Recursively append every string leaf in `value` to `out`.

    Engines wrap the agent's answer text inside nested event dicts/lists; the sentinel block
    lives in one of those string leaves (json.loads already decoded the escaping when the line
    was parsed). Walking all leaves means we find it wherever the engine version happens to put
    it.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_string_leaves(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_string_leaves(v, out)


def scan_sentinel_json(output: str, label: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced `FRANKY_<label>_<nonce>_BEGIN{json}_END` dict, or None.

    Builds candidate strings to scan: every string leaf of every JSON line (the decoded agent
    text), plus those leaves joined by newlines (covers a sentinel word-wrapped across a single
    leaf), plus the raw output itself (covers a plain-text, non-JSONL engine).

    The fence regex captures `{` ... `}` greedily but stops at the `_END` sentinel via a tempered
    negative lookahead: it must reach the LAST `}` before `_END`, so a `}` inside a string value
    does NOT truncate the JSON (a plain non-greedy match would stop at the first inner `}`), yet
    it must NOT span across a `_END`...`_BEGIN` boundary into a later block (a plain `.*` greedy
    match would, swallowing two blocks into one invalid string). finditer then yields one match
    per block and the LAST valid match overall wins (the agent's final block is authoritative).
    Any failure - no match, bad JSON, or a non-dict payload - returns None; never raises.
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

    end = f"FRANKY_{re.escape(label)}_{re.escape(nonce)}_END"
    pat = re.compile(
        rf"FRANKY_{re.escape(label)}_{re.escape(nonce)}_BEGIN(\{{(?:(?!{end}).)*\}}){end}",
        re.DOTALL,
    )

    # Track the last candidate-match that BOTH matches the fence AND parses to a dict. WHY
    # parse-while-walking rather than capture-then-parse-once: the raw `output` candidate is a
    # fallback that re-contains the sentinel with the JSON quotes still backslash-escaped (it is
    # itself a JSON line), which does not json.loads. A decoded leaf earlier in the list is the
    # real payload, so we must not let a later non-parseable raw match clobber it.
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
