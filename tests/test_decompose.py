"""Unit tests for the pure decomposition parser + shaper (no docker, no network)."""

import json

from franky.decompose import build_plan_result, parse_decomposition

NONCE = "deadbeefcafe1234"


def _block(payload: dict, nonce: str = NONCE) -> str:
    """Wrap a payload dict in the nonce-fenced sentinel block."""
    return f"FRANKY_PLAN_{nonce}_BEGIN{json.dumps(payload)}FRANKY_PLAN_{nonce}_END"


def _jsonl_assistant_line(text: str) -> str:
    """A realistic engine event line carrying `text` as JSON-escaped assistant content."""
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return json.dumps(event)


# ---------------------------------------------------------------------------
# parse_decomposition
# ---------------------------------------------------------------------------


def test_parse_sentinel_inside_jsonl_assistant_event():
    payload = {
        "fits_one_pr": True,
        "subtasks": [{"title": "do it", "summary": "the thing", "suggested_repo": "me/repo"}],
        "rationale": "small",
    }
    line = _jsonl_assistant_line("Here is my plan.\n" + _block(payload))
    parsed = parse_decomposition(line, NONCE)
    assert parsed is not None
    assert parsed["fits_one_pr"] is True
    assert parsed["subtasks"][0]["title"] == "do it"


def test_parse_plain_text_non_jsonl_output():
    payload = {"fits_one_pr": False, "subtasks": [], "rationale": "needs splitting"}
    output = "I inspected the repo.\n" + _block(payload)
    parsed = parse_decomposition(output, NONCE)
    assert parsed is not None
    assert parsed["fits_one_pr"] is False
    assert parsed["rationale"] == "needs splitting"


def test_parse_last_match_wins_with_two_blocks():
    first = _block({"fits_one_pr": True, "subtasks": [], "rationale": "first"})
    second = _block({"fits_one_pr": False, "subtasks": [], "rationale": "second"})
    output = first + "\nmore thinking\n" + second
    parsed = parse_decomposition(output, NONCE)
    assert parsed is not None
    assert parsed["rationale"] == "second"


def test_parse_brace_inside_summary_does_not_truncate():
    payload = {
        "fits_one_pr": False,
        "subtasks": [
            {"title": "t", "summary": "use a dict like {a: 1} here", "suggested_repo": "me/repo"}
        ],
        "rationale": "r",
    }
    output = _block(payload)
    parsed = parse_decomposition(output, NONCE)
    assert parsed is not None
    assert parsed["subtasks"][0]["summary"] == "use a dict like {a: 1} here"


def test_parse_wrong_nonce_returns_none():
    output = _block({"fits_one_pr": True, "subtasks": [], "rationale": "x"}, nonce="0000")
    assert parse_decomposition(output, NONCE) is None


def test_parse_malformed_inner_json_returns_none():
    output = f"FRANKY_PLAN_{NONCE}_BEGIN{{not valid json}}FRANKY_PLAN_{NONCE}_END"
    assert parse_decomposition(output, NONCE) is None


def test_parse_no_block_returns_none():
    assert parse_decomposition("just some plain prose with no sentinel", NONCE) is None


def test_parse_non_dict_payload_returns_none():
    output = f"FRANKY_PLAN_{NONCE}_BEGIN[1, 2, 3]FRANKY_PLAN_{NONCE}_END"
    # The greedy {.*} requires a leading `{`, so a list payload never matches -> None.
    assert parse_decomposition(output, NONCE) is None


# ---------------------------------------------------------------------------
# build_plan_result
# ---------------------------------------------------------------------------


def test_shape_normalizes_full_object():
    parsed = {
        "fits_one_pr": True,
        "subtasks": [{"title": "a", "summary": "s", "suggested_repo": "x/y"}],
        "rationale": "because",
    }
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert result == {
        "fits_one_pr": True,
        "subtasks": [{"title": "a", "summary": "s", "suggested_repo": "x/y"}],
        "rationale": "because",
        "engine": "pi",
        "repo": "me/repo",
        "exit_code": 0,
    }


def test_shape_defaults_suggested_repo_to_repo():
    parsed = {"fits_one_pr": False, "subtasks": [{"title": "a", "summary": "s"}], "rationale": ""}
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert result["subtasks"][0]["suggested_repo"] == "me/repo"


def test_shape_blank_suggested_repo_defaults_to_repo():
    parsed = {
        "fits_one_pr": False,
        "subtasks": [{"title": "a", "summary": "s", "suggested_repo": "  "}],
        "rationale": "",
    }
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert result["subtasks"][0]["suggested_repo"] == "me/repo"


def test_shape_coerces_fits_one_pr():
    parsed = {"fits_one_pr": "yes", "subtasks": [], "rationale": ""}
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert result["fits_one_pr"] is True

    parsed2 = {"fits_one_pr": 0, "subtasks": [], "rationale": ""}
    result2 = build_plan_result(parsed2, engine="pi", repo="me/repo")
    assert result2["fits_one_pr"] is False


def test_shape_drops_malformed_subtasks():
    parsed = {
        "fits_one_pr": False,
        "subtasks": [
            {"title": "keep", "summary": "ok"},
            "not a dict",
            {"summary": "no title"},
            {"title": "", "summary": "blank title"},
            {"title": "also-keep"},
        ],
        "rationale": "",
    }
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    titles = [s["title"] for s in result["subtasks"]]
    assert titles == ["keep", "also-keep"]
    # A subtask with no summary key still gets a str (default "").
    assert result["subtasks"][1]["summary"] == ""


def test_shape_drops_unknown_top_level_keys():
    parsed = {
        "fits_one_pr": True,
        "subtasks": [],
        "rationale": "r",
        "evil": "should be dropped",
        "extra": 123,
    }
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert set(result) == {"fits_one_pr", "subtasks", "rationale", "engine", "repo", "exit_code"}


def test_shape_coerces_non_str_rationale():
    parsed = {"fits_one_pr": True, "subtasks": [], "rationale": ["a", "list"]}
    result = build_plan_result(parsed, engine="pi", repo="me/repo")
    assert result["rationale"] == ""
