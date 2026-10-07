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


def test_schema_lists_every_live_command_recursively():
    def assert_group_matches(group, commands):
        assert set(commands) == set(group.commands)
        for name, command in group.commands.items():
            if hasattr(command, "commands"):
                assert_group_matches(command, commands[name]["commands"])

    assert_group_matches(cli.main, build_schema(cli.main)["commands"])


def test_schema_command_help_is_bounded_to_one_line():
    def assert_concise(commands):
        for command in commands.values():
            assert "\n" not in command["help"]
            assert len(command["help"]) <= 160
            if "commands" in command:
                assert_concise(command["commands"])

    assert_concise(build_schema(cli.main)["commands"])


def test_schema_documents_positional_arguments():
    commands = build_schema(cli.main)["commands"]
    build_arg = commands["build"]["arguments"][0]
    assert build_arg == {
        "name": "task_input",
        "required": True,
        "nargs": -1,
        "type": {"param_type": "String", "name": "text"},
    }

    review_args = commands["review-pr"]["arguments"]
    assert [(arg["name"], arg["required"]) for arg in review_args] == [
        ("pr_url", True),
        ("instructions", False),
    ]

    login_arg = commands["auth"]["commands"]["login"]["arguments"][0]
    assert login_arg["type"]["choices"] == ["claude", "codex"]


def test_schema_documents_option_types_and_defaults():
    build_flags = {
        flag["name"]: flag for flag in build_schema(cli.main)["commands"]["build"]["flags"]
    }
    assert build_flags["engine"]["type"]["choices"] == [
        "claude",
        "codex",
        "opencode",
        "pi",
    ]
    assert build_flags["retry"]["default"] == 0


def test_schema_maps_every_json_command_to_output_schemas():
    schema = build_schema(cli.main)

    def assert_references_exist(value):
        if isinstance(value, str):
            assert value in schema
        elif isinstance(value, dict):
            for nested in value.values():
                assert_references_exist(nested)

    def assert_json_outputs(group, commands):
        for name, command in group.commands.items():
            entry = commands[name]
            if any(param.name == "as_json" for param in command.params):
                assert "json_output" in entry, name
                assert_references_exist(entry["json_output"])
            if hasattr(command, "commands"):
                assert_json_outputs(command, entry["commands"])

    assert_json_outputs(cli.main, schema["commands"])

    commands = schema["commands"]
    assert commands["build"]["json_output"]["success"] == "result_schema"
    assert commands["jobs"]["json_output"]["success"] == {
        "default": "job_list_schema",
        "--stats": "job_stats_schema",
    }
    assert commands["job"]["commands"]["status"]["json_output"]["success"] == ("job_status_schema")


def test_schema_documents_special_json_output_shapes():
    schema = build_schema(cli.main)
    for key in (
        "version_result_schema",
        "job_list_schema",
        "job_stats_schema",
        "job_status_schema",
        "job_kill_schema",
        "job_export_schema",
        "job_attach_schema",
    ):
        assert key in schema

    assert schema["job_list_schema"] == {"type": "array", "items": "job_record_schema"}
    assert schema["job_status_schema"]["includes"] == "all job_record_schema fields"
    assert "container_running" in schema["job_status_schema"]
    assert set(schema["job_kill_schema"]) == {"job_id", "status", "container_reaped"}
    assert set(schema["job_export_schema"]) == {
        "job_id",
        "output_path",
        "bytes",
        "included",
    }
    assert set(schema["job_attach_schema"]) == {"job_id", "delivered", "engine", "kind"}

    stats = schema["job_stats_schema"]
    for field in (
        "total",
        "by_status",
        "success",
        "failed",
        "running_fresh",
        "hangs",
        "success_rate",
        "median_duration_s",
        "total_cost_usd",
        "by_engine",
        "by_repo",
    ):
        assert field in stats


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


def test_result_schemas_document_review_outputs():
    schema = build_schema(cli.main)
    for status in (
        "review_published",
        "review_complete",
        "no_findings",
        "publish_blocked_stale_head",
        "publish_failed",
        "publish_uncertain",
    ):
        assert status in schema["result_schema"]["status"]
        assert status in schema["job_record_schema"]["status"]
    for field in (
        "reviewed_sha",
        "findings_summary",
        "checks",
        "review_url",
        "review_id",
    ):
        assert field in schema["result_schema"]
    checks = schema["result_schema"]["checks"]
    assert "review_body" in schema["result_schema"]
    assert isinstance(checks, list)
    assert set(checks[0]) == {"name", "outcome", "detail"}


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


def test_job_record_schema_documents_review_command():
    schema = build_schema(cli.main)
    assert "review-pr" in schema["job_record_schema"]["command"]


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


def test_schema_documents_thread_outputs():
    schema = build_schema(cli.main)
    threads = schema["commands"]["threads"]["commands"]
    assert threads["list"]["json_output"] == {"success": "threads_list_schema"}
    assert threads["prune"]["json_output"]["success"] == "threads_prune_schema"
    assert threads["purge"]["json_output"]["success"] == "threads_purge_schema"
    assert "disk_skipped" in schema["threads_prune_schema"]
    assert schema["threads_list_schema"]["items"] == "thread_record_schema"
    assert "repo" in schema["thread_record_schema"]
    for key in ("thread", "handoff"):
        assert key in schema["result_schema"]
    assert set(schema["result_schema"]["thread"]) >= {"id", "session", "session_reason"}
    assert "thread_id" in schema["job_record_schema"]
    review_flags = {f["name"] for f in schema["commands"]["review-pr"]["flags"]}
    assert {"use_thread", "rubric_version"} <= review_flags


def test_error_schema_lists_image_pull_timeout():
    kind = build_schema(cli.main)["error_schema"]["error"]["kind"]
    assert "image_pull_timeout" in kind


def test_result_schema_documents_review_findings():
    schema = build_schema(cli.main)["result_schema"]
    assert set(schema["findings"][0]) == {
        "title",
        "body",
        "evidence",
        "impact",
        "fix",
        "severity",
        "file",
        "line",
        "start_line",
    }
    assert "findings_total" in schema


def test_schema_advertises_review_context_sources():
    entry = build_schema(cli.main)["result_schema"]["context_sources"][0]
    assert set(entry) == {"kind", "ref", "status", "reason"}
    assert "unconfigured" in entry["status"]


def test_schema_context_sources_reason_lists_every_reason():
    reason = build_schema(cli.main)["result_schema"]["context_sources"][0]["reason"]
    for name in (
        "unconfigured",
        "public_repo",
        "auth",
        "not_found",
        "restricted",
        "config",
        "network",
    ):
        assert name in reason
