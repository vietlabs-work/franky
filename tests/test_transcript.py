import json
import stat
import tracemalloc
import pytest

from franky import transcript
from franky.economics import parse_usage
from franky.engine import ENGINES
from franky.sentinel import scan_sentinel_json


def test_redaction_across_chunks_and_lines_matches_longest_first():
    from franky.config import redact

    raw = "before abcdef\n123 after abc and def\n123!"
    secrets = ["abc", "abcdef\n123", "def\n123"]
    for size in range(1, 15):
        stream = transcript.Redactor(secrets)
        chunks = [stream.feed(raw[i : i + size]) for i in range(0, len(raw), size)]
        chunks.append(stream.feed("", final=True))
        assert "".join(chunks) == redact(raw, secrets)


def test_duplicate_secrets_do_not_redact_the_replacement_twice():
    stream = transcript.Redactor(["*", "*"])
    assert stream.feed("*", final=True) == "***REDACTED***"


def test_file_backed_parsers_keep_early_pr_and_last_valid_sentinel(tmp_path):
    with transcript.Transcript() as output:
        output.write('{"text":"https://github.com/o/r/pull/12"}\n')
        output.write(json.dumps({"text": 'FRANKY_PLAN_n_BEGIN{"tasks":'}) + "\n")
        output.write(json.dumps({"text": "[]}FRANKY_PLAN_n_END"}) + "\n")
        for _ in range(400):
            output.write("x" * 10000 + "\n")
        output.write('{"type":"result","usage":{"input_tokens":12}}\n')
        assert ENGINES["pi"]().parse_pr_url(output, repo="o/r") == "https://github.com/o/r/pull/12"
        assert scan_sentinel_json(output, "PLAN", "n") == {"tasks": []}
        assert parse_usage(output).input_tokens == 12
        output.persist(tmp_path / "log")
        assert stat.S_IMODE((tmp_path / "log").stat().st_mode) == 0o600
        assert output.tail(4) == "2}}\n"


def test_giant_line_storage_and_iteration_have_bounded_memory():
    with transcript.Transcript() as output:
        tracemalloc.start()
        for _ in range(1024):
            output.write("x" * 32768)
        assert sum(map(len, output.chunks())) == 32 * 1024 * 1024
        assert output.tail(3) == "xxx"
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 3 * 1024 * 1024


def test_oversize_payload_fails_closed_and_preserves_full_log():
    with transcript.Transcript() as output:
        output.write('FRANKY_PLAN_n_BEGIN{"ok":true}FRANKY_PLAN_n_END\n')
        output.write('FRANKY_PLAN_n_BEGIN{"value":"')
        output.write("x" * (transcript.MAX_EVENT_CHARS + 1))
        output.write('"}FRANKY_PLAN_n_END')
        with pytest.warns(RuntimeWarning, match="parser limit"):
            assert scan_sentinel_json(output, "PLAN", "n") is None
        assert output.tail(18) == "}FRANKY_PLAN_n_END"


def test_usage_metadata_does_not_retain_prior_events():
    with transcript.Transcript() as output:
        event = (
            json.dumps({"type": "result", "usage": {"input_tokens": 12}, "text": "x" * 10000})
            + "\n"
        )
        for _ in range(500):
            output.write(event)
        tracemalloc.start()
        assert parse_usage(output).input_tokens == 12
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("early", ["", '{"text":"https://github.com/o/r/pull/12"}\n'])
def test_oversized_event_cannot_report_pr_success(early):
    with transcript.Transcript() as output:
        output.write(early + "x" * (transcript.MAX_EVENT_CHARS + 1))
        output.write("https://github.com/o/r/pull/13\n")
        with pytest.warns(RuntimeWarning, match="parser limit"):
            assert ENGINES["pi"]().parse_pr_url(output, repo="o/r") is None


def test_cli_persisted_transcript_still_parses_and_keeps_private_permissions(tmp_path):
    from franky.cli import _write_log

    with transcript.Transcript() as output:
        output.write('{"text":"https://github.com/o/r/pull/12"}\n')
        output.write('{"type":"result","usage":{"input_tokens":12}}\n')
        path = _write_log(output, [], env={"FRANKY_RUNS_DIR": str(tmp_path)}, run_id="abc123")
        assert output.path == path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert ENGINES["pi"]().parse_pr_url(output, repo="o/r").endswith("/12")
        assert parse_usage(output).input_tokens == 12
