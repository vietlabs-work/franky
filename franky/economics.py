"""Per-run economics: token usage, estimated cost, and duration.

WHY a separate module: the economics summary is best-effort and must never affect
correctness. Keeping it isolated means the parser can degrade gracefully (all-unknown)
without any risk of leaking state into the container or config logic.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

# Event "type" values that mark a terminal/summary event carrying the run's cumulative
# usage. We read usage/cost from the LAST such event (never sum) - see parse_usage.
_TERMINAL_TYPES = {"result", "message_stop", "done", "final", "summary", "turn.completed"}


@dataclass
class Usage:
    """Parsed token and cost summary for one agent run.

    None means the value was absent or unparseable in the engine's output.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def parse_usage(output: str) -> Usage:
    """Walk engine JSONL output and extract the best-effort token/cost summary.

    OpenCode emits per-step totals, so its valid step_finish dimensions are summed. Other
    engines emit cumulative terminal totals: picking the last terminal event avoids double
    counting intermediate chunks. If no terminal event carries usage, use the last event
    with any usage.

    Non-JSON lines (banners, log lines) are silently skipped. Non-numeric or garbage
    values are treated as absent. The function never raises.
    """
    # Candidates: (is_terminal, event_dict) pairs for usage and cost separately.
    # We pick the last terminal candidate, else the last any candidate.
    usage_candidates: list[tuple[bool, dict]] = []
    cost_candidates: list[tuple[bool, dict]] = []
    opencode_input: int | None = None
    opencode_output: int | None = None
    opencode_cost: float | None = None
    opencode_cost_overflowed = False
    opencode_found = False

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # not JSON - skip, never raise
        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")
        if event_type == "step_finish":
            part = event.get("part")
            if isinstance(part, dict):
                tokens = part.get("tokens")
                inp = _int_or_none(tokens.get("input")) if isinstance(tokens, dict) else None
                out = _int_or_none(tokens.get("output")) if isinstance(tokens, dict) else None
                cost = _float_or_none(part.get("cost"))
                if inp is not None:
                    opencode_input = (opencode_input or 0) + inp
                if out is not None:
                    opencode_output = (opencode_output or 0) + out
                if cost is not None and not opencode_cost_overflowed:
                    total = (opencode_cost or 0.0) + cost
                    if math.isfinite(total):
                        opencode_cost = total
                    else:
                        opencode_cost = None
                        opencode_cost_overflowed = True
                opencode_found |= inp is not None or out is not None or cost is not None
        is_terminal = isinstance(event_type, str) and event_type in _TERMINAL_TYPES

        # Check for a recognizable usage block.
        if _extract_tokens(event) != (None, None):
            usage_candidates.append((is_terminal, event))

        # Check for a recognizable cost field.
        if _extract_cost(event) is not None:
            cost_candidates.append((is_terminal, event))

    if opencode_found:
        return Usage(
            input_tokens=opencode_input,
            output_tokens=opencode_output,
            cost_usd=None if opencode_cost_overflowed else opencode_cost,
        )

    # For each dimension: prefer the last terminal candidate, else the last any candidate.
    usage_event = _pick_best(usage_candidates)
    cost_event = _pick_best(cost_candidates)

    input_tokens, output_tokens = _extract_tokens(usage_event) if usage_event else (None, None)
    cost_usd = _extract_cost(cost_event) if cost_event else None

    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)


def _pick_best(candidates: list[tuple[bool, dict]]) -> dict | None:
    """Return the last terminal candidate, or the last candidate if none are terminal."""
    if not candidates:
        return None
    # Walk in reverse: first terminal found is the last terminal overall.
    for is_terminal, event in reversed(candidates):
        if is_terminal:
            return event
    # No terminal candidate - fall back to the last candidate.
    return candidates[-1][1]


def _extract_tokens(event: dict | None) -> tuple[int | None, int | None]:
    """Pull (input_tokens, output_tokens) from an event dict, or (None, None).

    Recognises:
    - Anthropic-style: event["usage"]["input_tokens"] / ["output_tokens"]
    - OpenAI-style:    event["usage"]["prompt_tokens"] / ["completion_tokens"]
    - Both shapes also nested under event["message"]["usage"].
    """
    if not event:
        return None, None

    # Locate the usage sub-object: try direct "usage" then "message"."usage".
    usage_obj = event.get("usage")
    if not isinstance(usage_obj, dict):
        msg = event.get("message")
        if isinstance(msg, dict):
            usage_obj = msg.get("usage")

    if not isinstance(usage_obj, dict):
        return None, None

    # Anthropic-style keys.
    inp = _int_or_none(usage_obj.get("input_tokens"))
    out = _int_or_none(usage_obj.get("output_tokens"))

    # OpenAI-style keys (fallback when Anthropic keys are absent).
    if inp is None:
        inp = _int_or_none(usage_obj.get("prompt_tokens"))
    if out is None:
        out = _int_or_none(usage_obj.get("completion_tokens"))

    if inp is None and out is None:
        return None, None
    return inp, out


def _extract_cost(event: dict | None) -> float | None:
    """Pull a cost (USD) from an event dict, or None.

    Recognises top-level keys: total_cost_usd, cost_usd, cost.
    """
    if not event:
        return None
    for key in ("total_cost_usd", "cost_usd", "cost"):
        val = event.get(key)
        result = _float_or_none(val)
        if result is not None:
            return result
    return None


def _int_or_none(val: object) -> int | None:
    """Return val as int when it is a non-negative integer-like number, else None."""
    if val is None:
        return None
    try:
        i = int(val)
        return i if i >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _float_or_none(val: object) -> float | None:
    """Return val as float when it is a non-negative finite number, else None."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) and f >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def format_economics(usage: Usage, duration_secs: float) -> str:
    """Format a one-line economics summary for display and logging.

    Format: franky: economics - <tokens>, [est. $<cost>,] <duration>

    Tokens: "{in:,} in + {out:,} out tokens"; "unknown" for a missing side;
            "unknown tokens" when both are missing.
    Cost:   omitted entirely when unknown; otherwise ", est. $X.XXXX".
    Duration: ">= 60s -> Xm Ys" (integer seconds); "< 60s -> Y.Ys" (one decimal).

    The "est." prefix is the estimate label - no redundant parenthetical needed.
    """
    # Tokens segment.
    if usage.input_tokens is None and usage.output_tokens is None:
        token_str = "unknown tokens"
    else:
        in_str = f"{usage.input_tokens:,}" if usage.input_tokens is not None else "unknown"
        out_str = f"{usage.output_tokens:,}" if usage.output_tokens is not None else "unknown"
        token_str = f"{in_str} in + {out_str} out tokens"

    # Cost segment (omitted when unknown).
    cost_str = f", est. ${usage.cost_usd:.4f}" if usage.cost_usd is not None else ""

    # Duration segment.
    secs = max(0.0, duration_secs)
    if secs >= 60:
        minutes = int(secs) // 60
        remaining_secs = int(secs) % 60
        dur_str = f"{minutes}m {remaining_secs}s"
    else:
        dur_str = f"{secs:.1f}s"

    return f"franky: economics - {token_str}{cost_str}, {dur_str}"
