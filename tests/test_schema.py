"""Unit tests for `franky schema` and the pure schema builder (docker-free, network-free)."""

import json

import franky.cli as cli
from click.testing import CliRunner

from franky import result
from franky.schema import build_schema


def test_schema_command_emits_one_valid_json_object():
    res = CliRunner().invoke(cli.main, ["schema"])
    assert res.exit_code == 0, res.output
    # Exactly one JSON object on stdout (stdout purity).
    data = json.loads(res.output)
    assert isinstance(data, dict)


def test_schema_has_top_level_keys():
    schema = build_schema(cli.main)
    for key in ("commands", "result_schema", "error_schema", "exit_codes"):
        assert key in schema


def test_schema_exit_codes_cover_every_exit_constant():
    # Every EXIT_* constant (discovered via reflection) must appear in the schema's exit_codes.
    schema = build_schema(cli.main)
    exit_values = [
        v for k, v in vars(result).items() if k.startswith("EXIT_") and k != "EXIT_CODES"
    ]
    for code in exit_values:
        assert str(code) in schema["exit_codes"]
        assert schema["exit_codes"][str(code)]


def test_schema_lists_core_commands_with_flags():
    schema = build_schema(cli.main)
    cmds = schema["commands"]
    for name in ("build", "iterate", "plan", "schema"):
        assert name in cmds

    build_flag_names = {f["name"] for f in cmds["build"]["flags"]}
    assert "max_duration" in build_flag_names
    assert "force" in build_flag_names
    assert "as_json" in build_flag_names

    iterate_flag_names = {f["name"] for f in cmds["iterate"]["flags"]}
    assert "max_duration" in iterate_flag_names


def test_schema_recurses_into_subgroups():
    schema = build_schema(cli.main)
    cmds = schema["commands"]
    # The config group is itself a command with nested children.
    assert "config" in cmds
    assert "commands" in cmds["config"]
    assert "set" in cmds["config"]["commands"]
    assert set(cmds["auth"]["commands"]) == {"login", "logout", "status"}


def test_result_schema_documents_fields_and_predicted_branch():
    schema = build_schema(cli.main)
    rs = schema["result_schema"]
    for field in (
        "status",
        "pr_url",
        "branch",
        "reason",
        "exit_code",
        "economics",
        "log_path",
        "engine",
        "repo",
    ):
        assert field in rs
    # The branch field MUST flag that it is the PREDICTED branch (may differ from actual).
    assert "predicted" in rs["branch"].lower()
    for econ_field in ("tokens_in", "tokens_out", "cost_usd", "duration_s"):
        assert econ_field in rs["economics"]


def test_plan_result_schema_is_distinct_envelope():
    schema = build_schema(cli.main)
    assert "plan_result_schema" in schema
    prs = schema["plan_result_schema"]
    for field in ("fits_one_pr", "subtasks", "rationale", "engine", "repo", "exit_code"):
        assert field in prs
    # subtasks is a list of {title, summary, suggested_repo}.
    assert isinstance(prs["subtasks"], list)
    for field in ("title", "summary", "suggested_repo"):
        assert field in prs["subtasks"][0]
    # It is NOT merged into result_schema (a separate top-level envelope).
    assert "fits_one_pr" not in schema["result_schema"]


def test_job_record_schema_documents_diagnostics():
    schema = build_schema(cli.main)
    assert "job_record_schema" in schema
    jrs = schema["job_record_schema"]
    for field in ("job_id", "command", "repo", "engine", "status", "diagnostics"):
        assert field in jrs
    # diagnostics is issue #69's addition; assert its sub-fields are named in the description.
    for sub_field in ("task_exit_code", "oom_killed", "dind_ready", "tmpfs_full", "egress_denied"):
        assert sub_field in jrs["diagnostics"]


def test_result_schema_documents_replay_of():
    schema = build_schema(cli.main)
    rs = schema["result_schema"]
    assert "replay_of" in rs
    assert "replay_complete" in rs["status"]


def test_job_record_schema_documents_replay_fields():
    schema = build_schema(cli.main)
    jrs = schema["job_record_schema"]
    for field in ("source", "task_full", "base_sha", "replay_of"):
        assert field in jrs
    assert "replay" in jrs["command"]
    assert "replay_complete" in jrs["status"]


def test_error_schema_shape():
    schema = build_schema(cli.main)
    err = schema["error_schema"]["error"]
    for field in ("code", "kind", "message", "hint"):
        assert field in err


def test_result_schema_documents_resumed_from():
    from franky.cli import main
    from franky.schema import build_schema

    schema = build_schema(main)
    assert "resumed_from" in schema["result_schema"]


def test_job_record_schema_documents_resume_fields():
    from franky.cli import main
    from franky.schema import build_schema

    schema = build_schema(main)
    jrs = schema["job_record_schema"]
    assert "resumed_from" in jrs
    assert "snapshot_path" in jrs
    assert "resume" in jrs["command"]


def test_schema_lists_job_attach_command():
    schema = build_schema(cli.main)
    job_cmds = schema["commands"]["job"]["commands"]
    assert "attach" in job_cmds
    attach_flags = {f["name"] for f in job_cmds["attach"]["flags"]}
    assert "message" in attach_flags
    assert "as_json" in attach_flags


def test_job_record_schema_documents_steer_notes():
    schema = build_schema(cli.main)
    jrs = schema["job_record_schema"]
    assert "steer_notes" in jrs


def test_static_engine_descriptions_include_opencode():
    schema = build_schema(cli.main)
    for section in ("result_schema", "plan_result_schema", "job_record_schema"):
        assert "opencode" in schema[section]["engine"]
