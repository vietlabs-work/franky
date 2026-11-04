# Security model

The autonomous engine disables its own approval and sandbox prompts. The container is the safety boundary.

Franky applies these controls:

- A non-root user, read-only root, process and memory limits, and no swap.
- No host bind mounts, host filesystem access, host Docker socket, or privileged container.
- A packaged seccomp policy and the named `franky-task` AppArmor profile on native hosts.
- A fail-closed repository allowlist.
- Name-only credential forwarding and redaction of autonomous output and stored transcripts.
- An internal Docker network with no direct internet route or task DNS.
- A default-deny, HTTPS-only Squid proxy with blind TLS tunnels.
- Prompts forbid autonomous merges. Token permissions enforce the available GitHub actions.

Each task has its own rootless Docker daemon. Reviews and planning passes defer startup until Docker-dependent reproduction needs it. Other passes start it before the engine. `franky-docker-start` ensures readiness within a 30-second startup budget; concurrent calls share one launch. Agents can build images, run Compose, and use testcontainers without reaching the host daemon. Startup mode does not change container hardening or egress policy.

```text
task and nested Docker -> internal network -> Squid proxy -> allowed HTTPS hosts
```

The default egress list includes the selected provider, GitHub, package registries, and container registries. Add required hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS`.

Allowlisted hosts remain trusted destinations, not inert endpoints. A hostile task can use available credentials against those hosts.

The agent can also pass its credentials to nested containers. The outer limits and egress policy still apply to the full task tree.

Opening a PR can start GitHub Actions. Review workflow changes before you allow a run to use repository secrets.

`build --no-publish` exports commits without a write credential. Two networkless, read-only, capability-free helpers mount only the task's `/work` volume, read-only, during the entrypoint's session hold, so no stopped container ever keeps the tokens and the helpers never see HOME, `/tmp`, or the Codex login. Git in the helpers ignores replace refs and global or system config. Franky scans the raw objects the bundle will carry for the run's credential values, and refuses the export on a hit. This is a guard against accidental leaks of exact values only: an encoded or split value passes, and Franky does not scan for credential patterns, so the caller that pushes the bundle must. The host reads only the bundle header, which must name the scanned tip, and never runs git on a checkout the container wrote. Helper output reaches the run log only, never a result. Franky cannot enforce that the supplied `GH_TOKEN` is read-only.

Codex subscription authentication is the only persistent task volume. Franky scrubs it to `auth.json` before each autonomous run. A task never mounts it over `CODEX_HOME`: `CODEX_HOME` is a per-run directory, and only `auth.json` is linked in from the volume that sits beside it. Skills, rules, and session files never reach the shared volume.

A profile with a `team` setup also loads the repository's own skills and rules before the engine starts. What Franky installs is read at the default-branch head, never at the PR head or the PR base (an author can pick a base branch they made), so a PR cannot add to the installed instructions. Residual: the working directory `/work` holds the PR checkout, so an engine that also reads instruction files from its working directory can still see PR-head files. Skill and agent headers are rewritten to an allow-list (`name`, `description`, and for agents `tools`, `model`), so a repository file cannot carry `hooks`, `mcpServers` or `permissionMode` (any `SKILL.md` depth; team skills get the same rewrite). Repository instruction text joins the instruction file inside one fenced code block (a fence longer than any backtick run in it), so `@path` imports and fake headings are inert. Only the setup paths are fetched, files over 256 KiB are never downloaded, and symlinks, executables, settings, hooks, MCP and env files, and `*.yaml`, `*.yml`, `*.toml`, `*.json` and `agents/` inside a skill directory are skipped, and the run starts in `/work`, so the repository's own hooks never run. A fetch error fails the run before the engine starts. Repository text is still untrusted prompt input: it can steer the agent within the container's limits, as any repository file can.

Read [`AGENTS.md`](../AGENTS.md) before changing security, container, egress, profile, or resource code.

## Host runtime

`FRANKY_RUNTIME=host` ([configuration](configuration.md#runtime)) trades the container boundary for one macOS Seatbelt profile around the engine. It is an explicit opt-in and never a fallback. The container stays the default and its code path is unchanged. It runs on macOS only: the profile is the only boundary, so any other platform refuses (exit `3`).

One outer profile confines both engines and everything they start. The engines' own sandboxes and settings are not the boundary: Claude can write its own settings and add hooks, and a hook runs outside Claude's Bash sandbox.

- `(deny default)`, then narrow allows. Reads: everything outside your home directory, the runs root, `/private/var/folders`, `/private/tmp` and `/private/var/tmp`, plus the run directory, the engine's install directory and one control file. Writes: only the run's `home`, `work` and `tmp`, not the run directory itself, so `home` cannot become a symlink. Network: outbound TCP and the system resolver socket only, no other unix sockets (no ssh-agent, no tmux). Processes: exec and fork; signal and inspect only inside the sandbox, so other processes' environments stay unreadable. Mach: a short service list; the keychain and launchd services are denied. TLS roots come from `/etc/ssl/cert.pem` for the same reason.
- Claude runs inside it with `--setting-sources ""` (no user, project or local settings load), `disableAllHooks`, its own sandbox off (a nested sandbox is refused) and `WebFetch` and `WebSearch` denied. `--dangerously-skip-permissions` stays for headless approval. Codex runs `--sandbox danger-full-access`, because its own sandbox cannot nest in the profile, so the profile is its sandbox.
- Run directories live in a dedicated root outside your home directory (`/Users/Shared/franky-host-<uid>`, mode `0300`, override `FRANKY_HOST_RUNS_DIR`), one 128-bit random name per run. The whole root is denied except the run's own directory, so a concurrent run cannot read another run's credentials. The settings file and the profile sit in a `<run>.ctl` directory beside it (`0700`, files `0400`), readable by name only.
- A child environment built from scratch: `PATH`, `LANG`, `TERM`, `HOME`, `TMPDIR`, `XDG_*`, the configured credentials, and git and gh pointed inside the run (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL`, `GH_CONFIG_DIR`), so your keychain credential helper and GitHub login never apply.
- The same output redaction, transcript, wall-clock deadline and host-side publish validation. The deadline also bounds the output reader, so a `setsid` child that keeps the pipe cannot hang it.
- Job control by process group. Every signal (TERM, KILL, kill, reaper) is preceded by an identity check: every member must have started inside the run's start window and the leader must carry the recorded start time. A group that cannot be verified is reported as running and is never signalled; `job kill` then keeps the run directory and the record. Teardown stops the verified group, then any process outside it whose working directory is inside the run directory. Every exit path does this before a directory is removed. `job status` stops a verified group whose owner process is gone.
- A session copied out of a run, a steering message and a refreshed Codex login are read and written only after the run's processes stop, walking each path component with `O_NOFOLLOW` from a directory descriptor, with the byte limit enforced while reading. A refreshed Codex `auth.json` is kept only when it names the same account as the login copied in.

What it drops: the egress proxy (the engine can reach any host over TCP), the read-only root, the memory, process and disk limits, and the nested Docker daemon. A prompt-injected engine can send anything it can read to any host. What it can read is the run directory, the system and toolchain paths outside your home directory, and the engine's own install directory. The tokens in the engine's environment (`GH_TOKEN`, the engine credential) are readable by every process of the run. Tools that a run needs must live outside your home directory.

Residual risks:

- Seatbelt cannot deny the Mach calls `launchctl` and `security` use, so the profile denies exec of those programs (and `osascript`, `open`, `ssh-add`). Code that speaks launchd's private XPC protocol itself is not stopped, and could start a job outside the sandbox.
- A process that leaves the group with `setsid`, closes the output pipe and keeps running is found by its working directory at teardown. One that changes directory away and was never seen is not.
- Process identity uses `ps` start times, which have one-second resolution.
- The profile allows POSIX shared memory (needed for macOS preferences): a run can share memory with other processes that open the same name.

Use it only for repositories and prompts you trust at the level you would run them yourself. Prefer the container runtime when Docker works.
