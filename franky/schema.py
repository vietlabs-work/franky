"""`franky schema`: a machine-readable self-description of the CLI's capability surface.

WHY this exists: the primary caller is an LLM/agent, so a stable, introspectable contract -
which commands exist, their flags, the shape of a `--json` result/error, and the exit-code
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
    "iterate_complete | replay_complete",
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
    "log_path": "path to the redacted task log under tasks/; empty string for already_open "
    "(no run)",
    "engine": "the resolved engine name (e.g. pi | claude | codex)",
    "repo": "the target owner/repo",
    "job_id": "the run handle (issue #63); use with `franky job status|logs|kill`. null when no "
    "container ran (e.g. already_open).",
    "attempts": "present ONLY for a `build --retry` run (issue #64): an array of "
    "{job_id, status, retry_hint} - one entry per attempt, in order. Absent on a plain build.",
    "replay_of": "present ONLY on a `franky job replay` run: the job_id of the original run "
    "being reproduced. Absent on build/iterate.",
    "resumed_from": "present ONLY on a `franky job resume` run: the job_id of the original run "
    "whose workspace was restored. Absent on build/iterate.",
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
    "engine": "the resolved engine name",
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
    "engine": "the resolved engine name (e.g. pi | claude | codex)",
    "repo": "the target owner/repo",
    "exit_code": "the process exit code this result corresponds to (0 on success)",
}

# Static description of the on-disk run record (~/.franky/runs/<job_id>.json), the same shape
# `franky job status --json` / `franky jobs --json` emit (jobs.new_record + the update_record
# patches applied on top). This was a pre-existing gap - no schema described the record shape
# before issue #69 added the `diagnostics` field, which is exactly the surface an agent caller
# needs described so it can consume `job status --json` without guessing at field meaning.
_JOB_RECORD_SCHEMA: dict = {
    "job_id": "the run handle (see result_schema.job_id)",
    "command": "the Franky command that produced this run: build | iterate | diagnose | replay | "
    "resume",
    "repo": "the target owner/repo",
    "engine": "the resolved engine name (e.g. pi | claude | codex)",
    "task": "a redacted, truncated summary of the task text (a handle, not the full prompt)",
    "container": "the task container's name",
    "network": "the internal egress network's name",
    "proxy": "the egress proxy sidecar's name",
    "branch": "the predicted branch name (`franky/<slug>`), or null (iterate/diagnose have none)",
    "status": "running | pr_opened | no_pr | agent_error | timeout | already_open | "
    "iterate_complete | killed | diagnosed | diagnose_failed | replay_complete",
    "started_at": "ISO-8601 UTC timestamp when the run was registered",
    "ended_at": "ISO-8601 UTC timestamp when the run finished, or null while running",
    "pr_url": "the PR URL (string) or null when none was produced",
    "log_path": "path to the redacted transcript under tasks/, or empty string before it exists",
    "economics": _RESULT_SCHEMA["economics"],
    "exit_code": "the process exit code this run finished with, or null while running",
    "diagnostics": "best-effort runtime signals captured host-side just before container "
    "teardown (issue #69), or null. Fields (all optional/best-effort): task_exit_code (int), "
    "oom_killed (bool), task_state (str), dind_ready (bool|null: nested rootless Docker daemon "
    "readiness), tmpfs_full (bool), egress_denied (array of {host, count} the Squid proxy "
    "403'd - hosts redacted), proxy_denied_count (int).",
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


def _flag_schema(param: click.Parameter) -> dict | None:
    """Introspect one Click parameter into a flag descriptor, or None for non-options.

    Arguments (positional) are skipped - the schema documents the flag surface; the help text
    already conveys the positional contract. Only click.Option params produce an entry.
    """
    if not isinstance(param, click.Option):
        return None
    return {
        "name": param.name,
        "opts": list(param.opts),
        "is_flag": bool(param.is_flag),
        "required": bool(param.required),
        "help": param.help or "",
    }


def _command_schema(cmd: click.Command) -> dict:
    """Introspect one command into {help, flags}. (Sub-groups are walked by build_schema.)"""
    flags = []
    for param in cmd.params:
        fs = _flag_schema(param)
        if fs is not None:
            flags.append(fs)
    return {"help": cmd.help or cmd.short_help or "", "flags": flags}


def _walk(group: click.Group) -> dict:
    """Recursively collect {name: command_schema} for a group, recursing into sub-groups.

    A sub-group (e.g. config, profile) is itself a command in commands; we record its own
    help/flags AND nest its children under a `commands` key so the whole tree is described.
    """
    out: dict = {}
    for name, cmd in group.commands.items():
        entry = _command_schema(cmd)
        if isinstance(cmd, click.Group):
            entry["commands"] = _walk(cmd)
        out[name] = entry
    return out


def build_schema(group: click.Group) -> dict:
    """Build the full capability schema from the `main` Click group.

    Walks every command (recursing into sub-groups) for help + flags, then attaches the
    static result/error shapes and the exit-code taxonomy (sourced from result.EXIT_CODES,
    the single source of truth). Pure - no I/O, no import of cli.py.
    """
    return {
        "commands": _walk(group),
        "result_schema": _RESULT_SCHEMA,
        "plan_result_schema": _PLAN_RESULT_SCHEMA,
        "diagnosis_result_schema": _DIAGNOSIS_RESULT_SCHEMA,
        "job_record_schema": _JOB_RECORD_SCHEMA,
        "error_schema": _ERROR_SCHEMA,
        # JSON object keys are strings; stringify the int exit codes for a valid JSON map.
        "exit_codes": {str(code): meaning for code, meaning in result.EXIT_CODES.items()},
    }
