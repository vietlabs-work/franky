"""`franky schema`: a machine-readable self-description of the CLI's capability surface.

WHY this exists: the primary caller is an LLM/agent, so a stable, introspectable contract -
which commands exist, their arguments and flags, the shape of a `--json` result/error, and the exit-code
taxonomy - lets the caller drive Franky without scraping `--help`. `franky schema` emits this
as a single JSON object (no `--json` flag; it is always JSON).

PURE on purpose: this module takes the Click `main` group as an ARGUMENT and never imports
`cli.py` (that would be a cycle - cli imports this). It may import `result.py`, a dependency
leaf, which owns the static result/error shapes and the exit-code source of truth.
"""

from __future__ import annotations

import click

from . import result

# Static description of the build/iterate result object (the dict build_result shapes). Kept
# here, beside the command introspection, so `franky schema` describes both the flags AND the
# JSON the run emits. The field meanings mirror result.build_result.
_RESULT_SCHEMA: dict = {
    "status": "result class: pr_opened | no_pr | agent_error | timeout | already_open | "
    "iterate_complete | replay_complete | review_published | review_complete | no_findings | "
    "publish_blocked_stale_head | publish_failed",
    "pr_url": "the PR URL (string) or null when none was produced",
    "branch": "the PREDICTED branch name (`franky/<slug>`) computed host-side; MAY differ "
    "from the branch the agent actually created. null for iterate.",
    "reason": "a short human-readable explanation of the status",
    "exit_code": "the process exit code this result corresponds to (see exit_codes)",
    "economics": {
        "tokens_in": "input tokens (int) or null when unknown",
        "tokens_out": "output tokens (int) or null when unknown",
        "cost_usd": "estimated cost in USD (float) or null when unknown",
        "duration_s": "wall-clock seconds for the container pass (float); 0 for already_open "
        "(no run)",
    },
    "log_path": "absolute path to the redacted task log under FRANKY_RUNS_DIR/tasks/; empty "
    "string for already_open (no run)",
    "engine": "the resolved engine name (e.g. pi | claude | codex | opencode)",
    "repo": "the target owner/repo",
    "job_id": "the run handle (issue #63); use with `franky job status|logs|kill`. null when no "
    "container ran (e.g. already_open).",
    "attempts": "present ONLY for a `build --retry` run (issue #64): an array of "
    "{job_id, status, retry_hint} - one entry per attempt, in order. Absent on a plain build.",
    "replay_of": "present ONLY on a `franky job replay` run: the job_id of the original run "
    "being reproduced. Absent on build/iterate.",
    "resumed_from": "present ONLY on a `franky job resume` run: the job_id of the original run "
    "whose workspace was restored. Absent on build/iterate.",
    "reviewed_sha": "string; present ONLY for `review-pr`: the reviewed PR head commit",
    "findings_summary": "string; present ONLY for `review-pr`: the review findings summary",
    "checks": [
        {
            "name": "string; check name",
            "outcome": "string: pass | fail | skipped",
            "detail": "string; check result details, possibly empty",
        }
    ],
    "review_url": "string; present ONLY when `review-pr` publishes a GitHub review",
    "review_id": "integer; present ONLY when `review-pr` publishes a GitHub review",
}

# Static description of the `franky job diagnose` success object (build_diagnosis_result). A
# read-only meta-agent pass over a failed run's transcript (issue #64); DISTINCT envelope from
# build/iterate. Errors use the shared error_schema below.
_DIAGNOSIS_RESULT_SCHEMA: dict = {
    "job_id": "the diagnosed run's handle",
    "root_cause": "the agent's root-cause explanation of why the run failed",
    "category": "failure class: dind_daemon | egress_denied | test_failure | build_error | "
    "timeout | auth | no_pr | agent_confusion | rate_limit | unknown",
    "evidence": "array of quoted lines / concrete facts from the transcript supporting the cause",
    "proposed_fix": "the agent's suggested fix",
    "retryable": "bool: whether a fresh attempt following retry_hint could plausibly succeed "
    "(false for a deterministic failure). `build --retry` stops when this is false.",
    "retry_hint": "one concise instruction fed into a retry attempt (issue #64 #5)",
    "confidence": "the agent's confidence: low | medium | high",
    "engine": "the resolved engine name (e.g. pi | claude | codex | opencode)",
    "exit_code": "the process exit code this result corresponds to (0 on success)",
}

# Static description of the `franky plan` success object (the dict build_plan_result shapes).
# This is a SEPARATE top-level envelope from result_schema: `plan` is a read-only
# scope-assessment that emits a decomposition, NOT a build/iterate run result. Errors from
# `plan` still use the shared error_schema below; only its success object differs.
_PLAN_RESULT_SCHEMA: dict = {
    "fits_one_pr": "bool: true if the task fits one focused PR, false if it should be split",
    "subtasks": [
        {
            "title": "short title of one PR-sized sub-task",
            "summary": "what this sub-task entails",
            "suggested_repo": "owner/repo for this sub-task (defaults to the plan's repo)",
        }
    ],
    "rationale": "why the task fits one PR or how it was split",
    "engine": "the resolved engine name (e.g. pi | claude | codex | opencode)",
    "repo": "the target owner/repo",
    "exit_code": "the process exit code this result corresponds to (0 on success)",
}

# Static description of the `franky run-skill` success object. A SEPARATE envelope from
# result_schema: run-skill opens no PR and reports the agent's own PROOF verdict plus, when
# --push-branch was given, the one fast-forward branch the HOST pushed afterwards.
_RUN_SKILL_RESULT_SCHEMA: dict = {
    "status": "result class: proof_reported | no_proof | skill_failed | push_refused | "
    "push_failed | workspace_unavailable | agent_error | timeout",
    "pr_url": "always null; run-skill never opens a PR",
    "branch": "the --push-branch value, or null when the run was report-only",
    "reason": "a short human-readable explanation of the status",
    "exit_code": "the process exit code this result corresponds to (see exit_codes)",
    "economics": _RESULT_SCHEMA["economics"],
    "log_path": "absolute path to the redacted task log under FRANKY_RUNS_DIR/tasks/",
    "engine": "the resolved engine name (e.g. pi | claude | codex | opencode)",
    "repo": "the target owner/repo",
    "job_id": "the run handle; use with `franky job status|logs|kill`",
    "proof_verdict": "string: ok | fail - the verdict from the agent's final NONCE-FENCED "
    "`PROOF <nonce> ok|fail ...` line. Absent when the run produced no fenced line; an unfenced "
    "`PROOF ok` (for example one quoted out of the repo's own SKILL.md) never counts.",
    "proof": "string; the redacted free text after the PROOF verdict (e.g. `sha=abc1234`). "
    "Absent when the run produced no fenced PROOF line.",
    "pushed": "present ONLY when --push-branch was given AND the host push landed: "
    "{branch, sha} for the single fast-forward push. Absent otherwise, including on "
    "push_refused/push_failed.",
}

# Static description of the on-disk run record (~/.franky/runs/<job_id>.json), the same shape
# `franky job status --json` / `franky jobs --json` emit (jobs.new_record + the update_record
# patches applied on top). This was a pre-existing gap - no schema described the record shape
# before issue #69 added the `diagnostics` field, which is exactly the surface an agent caller
# needs described so it can consume `job status --json` without guessing at field meaning.
_JOB_RECORD_SCHEMA: dict = {
    "job_id": "the run handle (see result_schema.job_id)",
    "command": "the Franky command that produced this run: build | iterate | review-pr | "
    "diagnose | replay | resume | run-skill",
    "repo": "the target owner/repo",
    "engine": "the resolved engine name (e.g. pi | claude | codex | opencode)",
    "task": "a redacted, truncated summary of the task text (a handle, not the full prompt)",
    "container": "the task container's name",
    "network": "the internal egress network's name",
    "proxy": "the egress proxy sidecar's name",
    "branch": "the predicted branch name (`franky/<slug>`), or null when the command has none",
    "status": "running | pr_opened | no_pr | agent_error | timeout | already_open | "
    "iterate_complete | killed | diagnosed | diagnose_failed | replay_complete | "
    "review_published | review_complete | no_findings | publish_blocked_stale_head | "
    "publish_failed | proof_reported | no_proof | skill_failed | push_refused | push_failed | "
    "workspace_unavailable",
    "started_at": "ISO-8601 UTC timestamp when the run was registered",
    "ended_at": "ISO-8601 UTC timestamp when the run finished, or null while running",
    "pr_url": "the PR URL (string) or null when none was produced",
    "log_path": "absolute path to the redacted transcript under FRANKY_RUNS_DIR/tasks/, or "
    "empty string before it exists",
    "economics": _RESULT_SCHEMA["economics"],
    "exit_code": "the process exit code this run finished with, or null while running",
    "diagnostics": "best-effort runtime signals captured host-side just before container "
    "teardown (issue #69), or null. Fields (all optional/best-effort): task_exit_code (int), "
    "oom_killed (bool), task_state (str), dind_ready (bool|null: nested rootless Docker daemon "
    "readiness), tmpfs_full (bool), egress_denied (array of {host, count} the Squid proxy "
    "403'd in the recent log tail - hosts redacted), proxy_denied_count (int), "
    "proxy_log_truncated (bool: some log records were excluded).",
    "source": "the TaskSpec source this run was built from: issue | jira | prose | pr, or null "
    "(issue #70 - lets `job replay` reconstruct the original TaskSpec). Null on records "
    "written before replay support was added.",
    "task_full": "the REDACTED, PROSE_MAX_CHARS-capped full task text (issue #70) - the saved "
    "input `job replay` reconstructs its TaskSpec from, distinct from `task`'s 200-char display "
    "summary. Null on records written before replay support was added.",
    "base_sha": "the target repo's default-branch tip at build start (issue #70, see "
    "baseref.resolve_base_sha) - the commit `job replay` checks out. Null when unresolved or on "
    "records written before replay support was added.",
    "replay_of": "set ONLY on a run that IS a replay: the job_id of the original run being "
    "reproduced. Null otherwise.",
    "resumed_from": "set ONLY on a run that IS a resume (issue #71): the job_id of the original "
    "run whose workspace was restored. Null otherwise.",
    "snapshot_path": "path to the scrubbed, fail-closed-verified, host-local workspace snapshot "
    "used to resume this run (issue #71); never exported; null if none.",
    "steer_notes": "operator corrections injected via `franky job attach` while the run was "
    "live (issue #72): a bounded list of {message (redacted), delivered} - or null if none. "
    "Audit trail only.",
}

_VERSION_RESULT_SCHEMA: dict = {
    "franky": "string; installed Franky version",
    "provenance": {
        "kind": "string; installation method",
        "path": "string; Python environment path",
    },
    "engine": {
        "name": "string; selected engine name",
        "resolved": "boolean; whether the engine name is valid",
        "host_binary_version": "string or null; detected host engine version",
    },
    "image": "string; resolved task image reference",
}

_JOB_LIST_SCHEMA: dict = {"type": "array", "items": "job_record_schema"}

_JOB_STATS_GROUP_SCHEMA: dict = {
    "n": "integer; records in this group",
    "success": "integer; successful terminal records",
    "failed": "integer; failed terminal records",
    "success_rate": "float or null; success divided by terminal records",
    "median_duration_s": "float or null; median recorded duration in seconds",
    "total_cost_usd": "float or null; sum of recorded cost estimates",
}

_JOB_STATS_SCHEMA: dict = {
    "total": "integer; all records",
    "by_status": "object mapping status names to integer counts",
    "success": "integer; successful terminal records",
    "failed": "integer; failed terminal records",
    "running_fresh": "integer; recent records still marked running",
    "hangs": "integer; timeout records plus stale running records",
    "success_rate": "float or null; success divided by terminal records",
    "median_duration_s": "float or null; median recorded duration in seconds",
    "total_cost_usd": "float or null; sum of recorded cost estimates",
    "by_engine": "object mapping engine names to job_stats_group_schema",
    "by_repo": "object mapping repository names to job_stats_group_schema",
}

_JOB_STATUS_SCHEMA: dict = {
    "includes": "all job_record_schema fields",
    "container_running": "boolean; live Docker container state",
}

_JOB_KILL_SCHEMA: dict = {
    "job_id": "string; run handle",
    "status": "string; always killed",
    "container_reaped": "boolean; whether a running container was removed",
}

_JOB_EXPORT_SCHEMA: dict = {
    "job_id": "string; run handle",
    "output_path": "string; archive path",
    "bytes": "integer; archive size",
    "included": "array of included archive member names",
}

_JOB_ATTACH_SCHEMA: dict = {
    "job_id": "string; run handle",
    "delivered": "boolean; true when delivery succeeded",
    "engine": "string; engine for the running job",
    "kind": "string; always steered",
}

_JSON_OUTPUTS: dict[tuple[str, ...], dict] = {
    ("build",): {"success": "result_schema", "error": "error_schema"},
    ("iterate",): {"success": "result_schema", "error": "error_schema"},
    ("review-pr",): {"success": "result_schema", "error": "error_schema"},
    ("run-skill",): {"success": "run_skill_result_schema", "error": "error_schema"},
    ("plan",): {"success": "plan_result_schema", "error": "error_schema"},
    ("version",): {"success": "version_result_schema"},
    ("jobs",): {"success": {"default": "job_list_schema", "--stats": "job_stats_schema"}},
    ("job", "status"): {"success": "job_status_schema", "error": "error_schema"},
    ("job", "kill"): {"success": "job_kill_schema", "error": "error_schema"},
    ("job", "export"): {"success": "job_export_schema", "error": "error_schema"},
    ("job", "diagnose"): {"success": "diagnosis_result_schema", "error": "error_schema"},
    ("job", "replay"): {"success": "result_schema", "error": "error_schema"},
    ("job", "resume"): {"success": "result_schema", "error": "error_schema"},
    ("job", "attach"): {"success": "job_attach_schema", "error": "error_schema"},
}

# Static description of the error object (the dict build_error shapes), emitted on stdout
# under --json on any failure. exit_code == error.code.
_ERROR_SCHEMA: dict = {
    "error": {
        "code": "the process exit code (see exit_codes)",
        "kind": "a stable machine slug for the failure class (e.g. config_error)",
        "message": "a redacted, human-readable message",
        "hint": "an optional operator-facing remediation hint (may be empty)",
    }
}


def _type_schema(param: click.Parameter) -> dict:
    """Return Click's type metadata in a JSON-native form."""
    type_info = dict(param.to_info_dict()["type"])
    if "choices" in type_info:
        type_info["choices"] = list(type_info["choices"])
    return type_info


def _flag_schema(param: click.Parameter) -> dict | None:
    """Return one option descriptor, or None for a positional argument."""
    if not isinstance(param, click.Option):
        return None
    return {
        "name": param.name,
        "opts": list(param.opts),
        "is_flag": bool(param.is_flag),
        "required": bool(param.required),
        "type": _type_schema(param),
        "default": param.to_info_dict()["default"],
        "help": param.help or "",
    }


def _argument_schema(param: click.Parameter) -> dict | None:
    """Return one positional argument descriptor, or None for an option."""
    if not isinstance(param, click.Argument):
        return None
    return {
        "name": param.name,
        "required": bool(param.required),
        "nargs": param.nargs,
        "type": _type_schema(param),
    }


def _command_schema(cmd: click.Command) -> dict:
    """Return one command's help, positional arguments, and flags."""
    return {
        "help": cmd.get_short_help_str(limit=160),
        "arguments": [value for param in cmd.params if (value := _argument_schema(param))],
        "flags": [value for param in cmd.params if (value := _flag_schema(param))],
    }


def _walk(group: click.Group, path: tuple[str, ...] = ()) -> dict:
    """Recursively collect {name: command_schema} for a group, recursing into sub-groups.

    A sub-group (e.g. config, profile) is itself a command in commands; we record its own
    help/flags AND nest its children under a `commands` key so the whole tree is described.
    """
    out: dict = {}
    for name, cmd in group.commands.items():
        command_path = (*path, name)
        entry = _command_schema(cmd)
        if command_path in _JSON_OUTPUTS:
            entry["json_output"] = _JSON_OUTPUTS[command_path]
        if isinstance(cmd, click.Group):
            entry["commands"] = _walk(cmd, command_path)
        out[name] = entry
    return out


def build_schema(group: click.Group) -> dict:
    """Build the full capability schema from the `main` Click group.

    Walks every command (recursing into sub-groups) for help, arguments, and flags. It attaches the
    static result/error shapes and the exit-code taxonomy (sourced from result.EXIT_CODES,
    the single source of truth). Pure - no I/O, no import of cli.py.
    """
    return {
        "commands": _walk(group),
        "result_schema": _RESULT_SCHEMA,
        "plan_result_schema": _PLAN_RESULT_SCHEMA,
        "run_skill_result_schema": _RUN_SKILL_RESULT_SCHEMA,
        "diagnosis_result_schema": _DIAGNOSIS_RESULT_SCHEMA,
        "job_record_schema": _JOB_RECORD_SCHEMA,
        "version_result_schema": _VERSION_RESULT_SCHEMA,
        "job_list_schema": _JOB_LIST_SCHEMA,
        "job_stats_group_schema": _JOB_STATS_GROUP_SCHEMA,
        "job_stats_schema": _JOB_STATS_SCHEMA,
        "job_status_schema": _JOB_STATUS_SCHEMA,
        "job_kill_schema": _JOB_KILL_SCHEMA,
        "job_export_schema": _JOB_EXPORT_SCHEMA,
        "job_attach_schema": _JOB_ATTACH_SCHEMA,
        "error_schema": _ERROR_SCHEMA,
        # JSON object keys are strings; stringify the int exit codes for a valid JSON map.
        "exit_codes": {str(code): meaning for code, meaning in result.EXIT_CODES.items()},
    }
