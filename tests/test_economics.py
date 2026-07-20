"""Tests for franky/economics.py: parse_usage and format_economics."""

from __future__ import annotations

import json

import pytest

from franky.economics import Usage, format_economics, parse_usage


# ---------------------------------------------------------------------------
# parse_usage: basic recognition
# ---------------------------------------------------------------------------


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_parse_usage_anthropic_terminal_result():
    """Anthropic-style terminal 'result' event with usage + total_cost_usd -> correct Usage."""
    output = _line(
        {
            "type": "result",
            "usage": {"input_tokens": 1234, "output_tokens": 567},
            "total_cost_usd": 0.0421,
        }
    )
    u = parse_usage(output)
    assert u.input_tokens == 1234
    assert u.output_tokens == 567
    assert u.cost_usd == pytest.approx(0.0421)


def test_parse_usage_openai_style():
    """OpenAI-style prompt_tokens/completion_tokens + cost -> correct Usage."""
    output = _line(
        {
            "type": "done",
            "usage": {"prompt_tokens": 800, "completion_tokens": 200},
            "cost": 0.0012,
        }
    )
    u = parse_usage(output)
    assert u.input_tokens == 800
    assert u.output_tokens == 200
    assert u.cost_usd == pytest.approx(0.0012)


def test_parse_usage_picks_terminal_not_intermediate():
    """Multi-event stream: intermediate usage chunks must NOT be summed; the terminal event wins.

    We set up three events:
    - Two 'assistant' events with per-chunk usage (100+200 in, 10+20 out).
    - One 'result' event with the real cumulative total (999 in, 888 out).

    The correct answer is (999, 888). If the implementation sums, it would produce
    (100+200+999, 10+20+888) = (1299, 918) - clearly wrong.
    """
    lines = "\n".join(
        [
            _line({"type": "assistant", "usage": {"input_tokens": 100, "output_tokens": 10}}),
            _line({"type": "assistant", "usage": {"input_tokens": 200, "output_tokens": 20}}),
            _line(
                {
                    "type": "result",
                    "usage": {"input_tokens": 999, "output_tokens": 888},
                    "total_cost_usd": 0.05,
                }
            ),
        ]
    )
    u = parse_usage(lines)
    assert u.input_tokens == 999, "must pick terminal, not sum intermediates"
    assert u.output_tokens == 888, "must pick terminal, not sum intermediates"
    assert u.cost_usd == pytest.approx(0.05)


def test_parse_usage_codex_turn_completed_is_terminal():
    output = "\n".join(
        [
            _line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 900,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 75,
                    },
                }
            ),
            _line(
                {
                    "type": "item.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                }
            ),
        ]
    )
    assert parse_usage(output) == Usage(input_tokens=900, output_tokens=100)


def test_parse_usage_non_string_event_type_never_raises():
    output = _line({"type": [], "usage": {"input_tokens": 5, "output_tokens": 2}})
    assert parse_usage(output) == Usage(input_tokens=5, output_tokens=2)


def test_parse_usage_tokens_without_cost():
    """Usage present but no cost field -> cost_usd is None, tokens set correctly."""
    output = _line({"type": "result", "usage": {"input_tokens": 42, "output_tokens": 7}})
    u = parse_usage(output)
    assert u.input_tokens == 42
    assert u.output_tokens == 7
    assert u.cost_usd is None


def test_parse_usage_no_usage_anywhere():
    """Output with no recognizable usage -> all-None Usage."""
    output = "\n".join(
        [
            _line({"type": "assistant", "text": "hello"}),
            _line({"type": "result", "status": "ok"}),
        ]
    )
    u = parse_usage(output)
    assert u == Usage()


def test_parse_usage_skips_non_json_lines():
    """Non-JSON lines (banners, log output) must be skipped silently without raising."""
    output = "\n".join(
        [
            "Claude Code v1.2.3 starting up...",
            _line({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}),
            "some trailing banner",
        ]
    )
    # Must not raise.
    u = parse_usage(output)
    assert u.input_tokens == 10
    assert u.output_tokens == 5


def test_parse_usage_nested_message_usage():
    """Usage nested under message.usage (claude stream-json assistant events) is recognised."""
    output = _line(
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 50, "output_tokens": 25}},
        }
    )
    u = parse_usage(output)
    assert u.input_tokens == 50
    assert u.output_tokens == 25


def test_parse_usage_cost_usd_key():
    """cost_usd top-level key is recognised as a cost source."""
    output = _line(
        {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0.001}
    )
    u = parse_usage(output)
    assert u.cost_usd == pytest.approx(0.001)


def test_parse_usage_rejects_negative_and_non_finite_values():
    """A broken engine emitting negative or non-finite numbers must drop them (not crash).

    Negative tokens, a negative cost, and JSON `Infinity` (which json.loads accepts) are all
    treated as absent -> the corresponding field is None rather than a garbage value.
    """
    # Negative tokens + negative cost -> all dropped.
    neg = _line(
        {"type": "result", "usage": {"input_tokens": -5, "output_tokens": -1}, "cost": -0.5}
    )
    u = parse_usage(neg)
    assert u == Usage()
    # JSON Infinity cost (json.loads parses `Infinity`) is non-finite -> dropped.
    inf = '{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": Infinity}'
    u2 = parse_usage(inf)
    assert u2.input_tokens == 10
    assert u2.output_tokens == 2
    assert u2.cost_usd is None


# ---------------------------------------------------------------------------
# format_economics: output format
# ---------------------------------------------------------------------------


def test_format_economics_full():
    """Full line: both token sides + cost + duration in Xm Ys form."""
    u = Usage(input_tokens=12345, output_tokens=6789, cost_usd=0.0421)
    line = format_economics(u, duration_secs=83.0)
    assert line == "franky: economics - 12,345 in + 6,789 out tokens, est. $0.0421, 1m 23s"


def test_format_economics_unknown_input_side():
    """When input_tokens is None, render 'unknown in + X out tokens'."""
    u = Usage(input_tokens=None, output_tokens=6789, cost_usd=0.01)
    line = format_economics(u, duration_secs=5.0)
    assert "unknown in + 6,789 out tokens" in line


def test_format_economics_unknown_output_side():
    """When output_tokens is None, render 'X in + unknown out tokens'."""
    u = Usage(input_tokens=100, output_tokens=None, cost_usd=0.01)
    line = format_economics(u, duration_secs=5.0)
    assert "100 in + unknown out tokens" in line


def test_format_economics_both_tokens_unknown():
    """When both token sides are None, render 'unknown tokens' (not 'unknown in + unknown out')."""
    u = Usage()
    line = format_economics(u, duration_secs=1.0)
    assert "unknown tokens" in line
    # Must NOT say "unknown in + unknown out"
    assert "unknown in" not in line


def test_format_economics_no_cost_segment():
    """When cost_usd is None, no cost segment appears in the line."""
    u = Usage(input_tokens=100, output_tokens=50, cost_usd=None)
    line = format_economics(u, duration_secs=2.0)
    assert "est." not in line
    assert "$" not in line


def test_format_economics_cost_label_is_est():
    """The cost segment must start with 'est.' to label it as an estimate."""
    u = Usage(input_tokens=1, output_tokens=1, cost_usd=0.0001)
    line = format_economics(u, duration_secs=1.0)
    assert "est. $0.0001" in line


def test_format_economics_duration_minutes():
    """Duration >= 60s formats as Xm Ys with integer seconds."""
    u = Usage()
    line = format_economics(u, duration_secs=90.0)
    assert "1m 30s" in line


def test_format_economics_duration_sub_minute():
    """Duration < 60s formats as Y.Ys with one decimal."""
    u = Usage()
    line = format_economics(u, duration_secs=0.4)
    assert "0.4s" in line


def test_format_economics_prefix():
    """Every line starts with 'franky: economics - '."""
    u = Usage()
    line = format_economics(u, duration_secs=1.0)
    assert line.startswith("franky: economics - ")
