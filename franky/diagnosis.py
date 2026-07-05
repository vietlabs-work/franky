"""Parse and shape the `franky job diagnose` payload (pure - no I/O, no docker) - issue #64.

`diagnose` runs a read-only container pass that reads a FAILED run's transcript + record and
ends its response with EXACTLY ONE nonce-fenced sentinel block carrying a compact JSON
diagnosis. This module owns the two pure halves of consuming that:

- `parse_diagnosis` extracts the JSON via the shared `sentinel.scan_sentinel_json` scanner
  (label "DIAG"); see `sentinel` for the JSONL-leaf + tempered-regex details it shares with
  `franky plan`.
- `build_diagnosis_result` normalizes the parsed dict into the stable success envelope the CLI
  emits under --json (a DISTINCT envelope from build/iterate). The engine is autonomous, so
  every field is defensively coerced - a malformed shape degrades to safe defaults, never a
  crash - exactly like `decompose.build_plan_result`.

The `retryable` + `retry_hint` fields are the learning signal `franky build --retry` feeds back
into a fresh attempt (issue #64 item #5): a diagnosis that cannot be parsed or says
`retryable=false` STOPS the retry loop rather than blindly restarting.
"""

from __future__ import annotations

from .sentinel import scan_sentinel_json

# Known failure categories. The agent picks one; anything off-list coerces to "unknown" so the
# field stays a stable enum for a machine caller.
CATEGORIES = frozenset(
    {
        "dind_daemon",  # nested rootless Docker daemon never came up / died
        "egress_denied",  # proxy 403'd an off-allowlist host
        "test_failure",  # the repo's tests went red
        "build_error",  # compile/build/dependency failure
        "timeout",  # exceeded --max-duration
        "auth",  # missing/invalid creds, 401/403 from a provider or GitHub
        "no_pr",  # ran clean but produced no PR
        "agent_confusion",  # the agent went off-task / looped / gave up
        "rate_limit",  # provider or GitHub rate limit
        "unknown",  # anything else / could not tell
    }
)

_CONFIDENCE = frozenset({"low", "medium", "high"})


def parse_diagnosis(output: str, nonce: str) -> dict | None:
    """Extract the nonce-fenced diagnosis JSON from the engine transcript, or None.

    Thin wrapper over `sentinel.scan_sentinel_json(output, "DIAG", nonce)`: the block is
    `FRANKY_DIAG_<nonce>_BEGIN{...}_END`. Never raises.
    """
    return scan_sentinel_json(output, "DIAG", nonce)


def _str_list(raw: object, *, limit: int = 20, item_max: int = 500) -> list[str]:
    """Coerce `raw` into a bounded list of non-empty strings (drops non-strings).

    Bounded so a pathological agent payload (thousands of evidence lines) can't bloat the
    result object; each item is length-capped too.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:item_max])
        if len(out) >= limit:
            break
    return out


def _coerce_str(raw: object, *, max_chars: int = 2000) -> str:
    """Coerce `raw` to a trimmed, length-bounded string ("" for non-strings)."""
    return raw.strip()[:max_chars] if isinstance(raw, str) else ""


def build_diagnosis_result(
    parsed: dict,
    *,
    job_id: str,
    engine: str,
    exit_code: int = 0,
) -> dict:
    """Shape the parsed diagnosis into the stable `diagnose` success envelope.

    Every field is defensively coerced (the agent is autonomous, never trust the shape):
    `category` off the known set -> "unknown"; `confidence` off {low,medium,high} -> "low";
    `retryable` -> bool; strings trimmed + length-bounded; `evidence` a bounded string list.
    Unknown top-level keys are dropped. Pure - the caller redacts the serialized string.
    """
    category = parsed.get("category")
    category = category if isinstance(category, str) and category in CATEGORIES else "unknown"

    confidence = parsed.get("confidence")
    confidence = confidence if isinstance(confidence, str) and confidence in _CONFIDENCE else "low"

    return {
        "job_id": job_id,
        "root_cause": _coerce_str(parsed.get("root_cause")),
        "category": category,
        "evidence": _str_list(parsed.get("evidence")),
        "proposed_fix": _coerce_str(parsed.get("proposed_fix")),
        "retryable": bool(parsed.get("retryable")),
        "retry_hint": _coerce_str(parsed.get("retry_hint"), max_chars=1000),
        "confidence": confidence,
        "engine": engine,
        "exit_code": exit_code,
    }
