"""Host-side `gh` passthrough for `franky gh`.

WHY this exists: Franky's primary caller is an agent, and that agent usually lives in a
sandbox with no `gh` CLI and no GitHub token - so after it dispatches a build it cannot even
confirm the PR landed or read CI. Franky already holds a scoped GH_TOKEN. `franky gh <args>`
lends that token to the real `gh` CLI so the caller can query and act on GitHub (pr status,
checks, comment, merge, api, ...) without a second credential.

POWER MODEL: the full `gh` surface, bounded ONLY by the token's scopes - Franky imposes no
capability ceiling (no read-only gate, no repo allowlist on this surface). The operator limits
by scoping the token; the key IS the control lever. The one thing Franky guarantees is that the
token VALUE never leaks: it is handed to `gh` via the child ENV (never on the argv, so it is not
visible in `ps`), and `gh`'s captured output is redacted by the caller before it is printed.

HOST-SIDE, mirroring idempotency.find_open_pr (which already talks to GitHub host-side with the
token): a `franky gh` invocation is a deterministic, operator/agent-driven command, not the
autonomous engine, so it does not need the container's egress cage.

CAPTURED then redacted (not streamed) so redaction can never split the token across a chunk
boundary. A side effect: `gh` sees a non-TTY (piped) stdout and disables its interactive
prompts, so `franky gh` is non-interactive by design and never hangs waiting for input - pass
`gh`'s own flags rather than relying on its prompts. A consequence of capturing: a long-lived
streaming subcommand (e.g. `gh run watch`) buffers until it exits rather than streaming live -
this surface is meant for one-shot query/act commands.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping

GH_BIN = "gh"


def run_gh(
    args: tuple[str, ...] | list[str],
    env: Mapping[str, str],
    *,
    runner: Callable = subprocess.run,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run `gh <args>` with the caller's env; return (returncode, stdout, stderr).

    GH_TOKEN is expected to already be present in `env`; `gh` reads it from the environment,
    so the token reaches `gh` via the child ENV and never lands on the argv. Output is captured
    (not streamed); the CALLER redacts stdout/stderr before printing so a secret value can never
    reach the terminal. `runner` is injectable so tests never invoke real `gh`.

    Raises OSError (e.g. FileNotFoundError if `gh` is not on PATH, PermissionError if it is not
    executable) - the caller maps that to a clean operator error. A subprocess timeout, if
    `timeout` is set, propagates as subprocess.TimeoutExpired for the caller to map to exit 9.
    """
    argv = [GH_BIN, *args]
    proc = runner(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=dict(env),
    )
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
