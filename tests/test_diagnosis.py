"""Tests for `franky/diagnosis.py` (issue #64) - pure parse + shape, no I/O.

Mirrors test_decompose.py: the parser extracts the nonce-fenced DIAG block from an engine
transcript (via the shared sentinel scanner) and build_diagnosis_result defensively coerces the
autonomous agent's payload into a stable envelope.
"""

from franky.diagnosis import build_diagnosis_result, parse_diagnosis


def _block(nonce, payload):
    return f"prose before\nFRANKY_DIAG_{nonce}_BEGIN{payload}FRANKY_DIAG_{nonce}_END"


def test_parse_diagnosis_extracts_block():
    out = _block("n0nce", '{"root_cause": "the tests were red", "retryable": true}')
    parsed = parse_diagnosis(out, "n0nce")
    assert parsed == {"root_cause": "the tests were red", "retryable": True}


def test_parse_diagnosis_wrong_nonce_returns_none():
    out = _block("realnonce", '{"root_cause": "x"}')
    assert parse_diagnosis(out, "attacker") is None


def test_parse_diagnosis_missing_block_returns_none():
    assert parse_diagnosis("no sentinel here at all", "n0nce") is None


def test_parse_diagnosis_last_valid_wins():
    out = _block("n1", '{"root_cause": "first"}') + "\n" + _block("n1", '{"root_cause": "final"}')
    assert parse_diagnosis(out, "n1")["root_cause"] == "final"


def test_parse_diagnosis_tolerates_brace_in_string():
    # A `}` inside a value must not truncate the JSON (tempered-regex behavior).
    out = _block("n2", '{"root_cause": "failed at line if (x) { return }", "retryable": false}')
    parsed = parse_diagnosis(out, "n2")
    assert parsed["retryable"] is False
    assert "return }" in parsed["root_cause"]


def test_build_diagnosis_result_full_shape():
    parsed = {
        "root_cause": "  the nested docker daemon never came up  ",
        "category": "dind_daemon",
        "evidence": ["waited 30s for /var/run/docker.sock", 42, ""],
        "proposed_fix": "increase the daemon wait",
        "retryable": True,
        "retry_hint": "wait longer for DinD",
        "confidence": "high",
        "junk_key": "dropped",
    }
    result = build_diagnosis_result(parsed, job_id="ab12cd", engine="pi")
    assert result["job_id"] == "ab12cd" and result["engine"] == "pi"
    assert result["root_cause"] == "the nested docker daemon never came up"  # trimmed
    assert result["category"] == "dind_daemon"
    assert result["evidence"] == ["waited 30s for /var/run/docker.sock"]  # non-strings dropped
    assert result["retryable"] is True and result["confidence"] == "high"
    assert result["exit_code"] == 0
    assert "junk_key" not in result  # unknown top-level keys dropped


def test_build_diagnosis_result_coerces_unknown_category_and_confidence():
    result = build_diagnosis_result(
        {"category": "made_up", "confidence": "certain", "retryable": "yes"},
        job_id="ff00ff",
        engine="claude",
    )
    assert result["category"] == "unknown"  # off-list -> unknown
    assert result["confidence"] == "low"  # off-list -> low
    assert result["retryable"] is True  # truthy string -> bool


def test_build_diagnosis_result_empty_payload_safe():
    result = build_diagnosis_result({}, job_id="000000", engine="pi")
    assert result["root_cause"] == "" and result["proposed_fix"] == ""
    assert result["evidence"] == [] and result["retryable"] is False
    assert result["category"] == "unknown" and result["confidence"] == "low"


def test_build_diagnosis_result_evidence_non_list_becomes_empty():
    result = build_diagnosis_result({"evidence": "not a list"}, job_id="000000", engine="pi")
    assert result["evidence"] == []
