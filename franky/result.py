"""Machine contract for the CLI: exit-code taxonomy, typed errors, and JSON shapes.

The primary caller of `franky build` / `iterate` is an LLM/agent, so the machine contract IS
the product: a stable exit code per failure class, a `--json` result/error object, and typed
errors that carry their own exit code. This module is pure - no I/O, no Click, no docker, no
imports from the rest of `franky` - so it stays a dependency leaf (config/task/jira all import
it, never the reverse) and is trivially unit-testable.

WHY FrankyError subclasses ValueError: every existing call site already does
`except ValueError` (and the tests `pytest.raises(ValueError, match=...)`), so raising a
FrankyError keeps those byte-identical while ALSO carrying a `.code`/`.kind`/`.hint` the CLI's
single outer handler can map to an exit code and a JSON error object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import keeps result.py a runtime dependency leaf (no franky imports at
    # runtime), so config/task/jira can import it without a cycle.
    from .economics import Usage

# Exit-code taxonomy (SemVer contract - do not renumber).
EXIT_SUCCESS = 0
EXIT_USAGE = 2  # usage/flag error; interactive input required in a non-TTY (never-hang)
EXIT_CONFIG = 3  # bad config file, allowlist unset/empty/malformed, bad engine
EXIT_TASK_REJECTED = 4  # allowlist / task rejection
EXIT_AUTH = 5  # auth/creds missing (GH_TOKEN, engine creds, JIRA creds, JIRA 401/403)
EXIT_DOCKER = 6  # docker / image unavailable
EXIT_AGENT = 7  # agent ran but exited nonzero / produced no PR
EXIT_NETWORK = 8  # network/timeout (JIRA reach/HTTP/parse)


class FrankyError(ValueError):
    """A CLI error carrying its own exit code, machine `kind`, and operator `hint`.

    Subclasses ValueError so every existing `except ValueError` / `pytest.raises(ValueError)`
    keeps working and the message is unchanged; the CLI's outer handler picks up `.code` to
    set the process exit code and to shape the `--json` error object.
    """

    code: int = EXIT_USAGE
    kind: str = "error"

    def __init__(
        self, message: str, *, code: int | None = None, kind: str | None = None, hint: str = ""
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if kind is not None:
            self.kind = kind
        self.hint = hint


class ConfigError(FrankyError):
    """Bad config file, allowlist unset/empty/malformed, or unknown engine. Exit 3."""

    code = EXIT_CONFIG
    kind = "config_error"


class TaskRejected(FrankyError):
    """Task rejected: off-allowlist repo, missing --repo, bad URL/key. Exit 4."""

    code = EXIT_TASK_REJECTED
    kind = "task_rejected"


class AuthError(FrankyError):
    """Missing creds (GH_TOKEN, engine creds, JIRA creds) or JIRA 401/403. Exit 5."""

    code = EXIT_AUTH
    kind = "auth_error"


class DockerError(FrankyError):
    """Docker / image unavailable. Exit 6."""

    code = EXIT_DOCKER
    kind = "docker_error"


class NetworkError(FrankyError):
    """Network/timeout reaching JIRA, HTTP error, or unparseable response. Exit 8."""

    code = EXIT_NETWORK
    kind = "network_error"


def build_result(
    *,
    status: str,
    pr_url: str | None,
    reason: str,
    exit_code: int,
    usage: Usage,
    duration: float,
    log_path: str,
    engine: str,
    repo: str,
    branch: str | None = None,
) -> dict:
    """Shape the success/agent-result object emitted on stdout under `--json`.

    Pure dict shaping only - the caller redacts the serialized string. `branch` is reserved
    (always null this version; not reliably derivable host-side). `usage` is an
    economics.Usage (input_tokens/output_tokens/cost_usd, each int|None / float|None).
    """
    return {
        "status": status,
        "pr_url": pr_url,
        "branch": branch,
        "reason": reason,
        "exit_code": exit_code,
        "economics": {
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "duration_s": round(duration, 3),
        },
        "log_path": log_path,
        "engine": engine,
        "repo": repo,
    }


def build_error(code: int, kind: str, message: str, hint: str = "") -> dict:
    """Shape the failure object emitted on stdout under `--json`. Exit code == `code`."""
    return {"error": {"code": code, "kind": kind, "message": message, "hint": hint}}
