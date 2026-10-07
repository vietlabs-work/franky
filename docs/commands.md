# Commands

Run `franky COMMAND --help` for flags and examples. Run `franky schema` for the complete machine-readable contract.

## Job commands

| Command | Purpose |
|---------|---------|
| `franky job status JOB_ID` | Show the state, the one next step, the record, and runtime diagnostics. |
| `franky job logs JOB_ID` | Print the redacted transcript. |
| `franky job kill JOB_ID` | Stop a run and remove its task, proxy, network, and volumes. A `--thread` build also keeps its engine session. |
| `franky job export JOB_ID` | Export the record and redacted transcript as a portable archive. |
| `franky job diagnose JOB_ID` | Request a read-only failure analysis. |
| `franky job replay JOB_ID` | Reproduce a build from saved inputs in a fresh container. Refuses a `build --no-publish` run. |
| `franky job resume JOB_ID` | Continue a stopped run from its saved workspace, and for a `--thread` build also its engine session. Refuses a `build --no-publish` run. |
| `franky job attach JOB_ID` | Send one correction to a running steerable engine. |

`replay` starts from saved inputs. `resume` restores `/work`, plus the engine session when it can (see [Author threads](threads.md#author-threads)). Use `replay --open-pr` only when the reproduced run should open a PR.

`attach` supports `pi`, `claude`, and `codex`. OpenCode does not support steering.

## Thread commands

| Command | Purpose |
|---------|---------|
| `franky threads list` | List stored review and author threads with engine, last head, and session size. |
| `franky threads prune` | Remove orphaned, idle (`--older-than`, default 30d), and optionally closed-PR (`--closed`) threads. Cap stored sessions (`--max-bytes`, default 2G). |
| `franky threads purge OWNER/REPO#N` | Delete one PR's threads. `--role reviewer` or `--role author` limits it to one role. `--all` deletes every thread. |

`prune --repo OWNER/REPO` applies every pass to one repository, so a token scoped to that repository covers `--closed`. It skips the global disk cap and reports `disk_skipped`.

## Authentication, configuration, and profile commands

| Command | Purpose |
|---------|---------|
| `franky auth login codex` | Create persistent Codex subscription authentication. |
| `franky auth status codex` | Check the stored Codex authentication. |
| `franky auth logout codex` | Delete the Codex authentication volume. |
| `franky auth login claude` | Save a Claude Code subscription token to the config file. |
| `franky auth status claude` | Check that a Claude token is set. |
| `franky auth logout claude` | Remove the Claude token from the config file. |
| `franky config init` | Create or update the user configuration. |
| `franky config set KEY [VALUE]` | Set one configuration value. Secret values use a hidden prompt, or `--stdin` to read one piped line. |
| `franky config list` | List configuration values with secrets masked. |
| `franky config path` | Print the configuration path. |
| `franky profile init` | Discover setups and merge profile selections. |
| `franky profile check` | Validate files, MCP policy, limits, and secrets. |
| `franky profile show` | Print the profile and expanded file list. |
| `franky profile path` | Print the resolved profile path. |
