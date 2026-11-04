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

- `(deny default)`, then narrow allows. Reads: everything outside `/Users` (your home directory and all other users' and shared content, including `/Users/Shared`), the runs root, `/private/var/folders`, `/private/tmp` and `/private/var/tmp`, plus the run directory, the engine's install directory and one control file. Writes: only the run's `home`, `work` and `tmp`, not the run directory itself, so `home` cannot become a symlink. Network: outbound TCP and the system resolver socket only, no other unix sockets (no ssh-agent, no tmux). Processes: exec only from read-only system and install roots (`/usr`, `/bin`, `/sbin`, `/System`, `/Library`, `/opt/homebrew`, `/Applications`, the engine's install directory), never from the run's own `home`, `work` or `tmp` or anything in the runs root, so a binary or script a run drops there cannot be started (run a script with `sh script.sh`, not `./script.sh`); fork; signal and inspect only inside the sandbox, so other processes' environments stay unreadable. Mach: a short service list; the keychain and launchd services are denied. TLS roots come from `/etc/ssl/cert.pem` for the same reason.
- Claude runs inside it with `--setting-sources ""` (no user, project or local settings load), `disableAllHooks`, its own sandbox off (a nested sandbox is refused) and `WebFetch` and `WebSearch` denied. `--dangerously-skip-permissions` stays for headless approval. Codex runs `--sandbox danger-full-access`, because its own sandbox cannot nest in the profile, so the profile is its sandbox.
- Run directories live in a dedicated root outside your home directory (`/Users/Shared/franky-host-<uid>`, mode `0300`, override `FRANKY_HOST_RUNS_DIR`, which must not equal, sit inside or contain your home directory or any exec root), one 128-bit random name per run. The whole root is denied except the run's own directory, so a concurrent run cannot read another run's credentials. The settings file and the profile sit in a `<run>.ctl` directory beside it (`0700`, files `0400`), readable by name only.
- A child environment built from scratch: `PATH`, `LANG`, `TERM`, `HOME`, `TMPDIR`, `XDG_*`, the configured credentials, and git and gh pointed inside the run (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL`, `GH_CONFIG_DIR`), so your keychain credential helper and GitHub login never apply.
- The same output redaction, transcript, wall-clock deadline and host-side publish validation. The deadline also bounds the output reader, so a `setsid` child that keeps the pipe cannot hang it.
- Job control by kernel evidence. A process belongs to a run only when `sandbox_check` says it runs under that run's profile (the home and the runs root denied to it, the run's own `work` directory allowed; a process of another run is denied this run's directory, an unsandboxed one is allowed the home). `sandbox_check` is asked about every process of yours with no pre-filter, so a process under the profile is the run's whatever its start time; a process it cannot answer for makes the teardown `uncertain`, never `clean`. The process group, the parent links and a working directory only help to find candidates; none authorizes a signal. One `teardown` is used by every caller (normal end, deadline, exceptions, `job kill`, the orphan reaper in `job status`): it discovers, sends TERM, then KILL after a grace, and repeats until a pass proves no process, so a child forked while terminating is caught in a later pass. The start time is re-read right before each signal. It returns `clean` or `uncertain` (a proven process outlived the 10 s deadline, or the process table could not be read). Sessions are captured and directories removed only on `clean`; on `uncertain` the run directory, its credentials and the record are kept, the record gets `host_unverified`, and `job kill` reports the run as still running or unverified. SIGINT and SIGTERM are held during teardown, and its identity exists before the spawn so an interrupt anywhere still reaches it. The deadline also bounds the output reader, so a `setsid` child that keeps the pipe cannot hang it.
- A session copied out of a run and a steering message are read and written only after the run's processes stop (steering while it runs, into its steer file only), walking each path component with `O_NOFOLLOW` from a directory descriptor, with the byte limit enforced while reading. A Codex login is copied into the run and never copied back: what a run writes to its `auth.json` cannot be trusted (it can forge any claim), and the id token cannot be verified without the issuer's keys, so only `franky auth login codex` writes the host store. When Codex rotates the refresh token during a run, the stored copy goes stale: run `franky auth login codex` again.

What it drops: the egress proxy (the engine can reach any host over TCP), the read-only root, the memory, process and disk limits, and the nested Docker daemon. A prompt-injected engine can send anything it can read to any host. What it can read is the run directory, the system and toolchain paths outside your home directory, and the engine's own install directory. The tokens in the engine's environment (`GH_TOKEN`, the engine credential) are readable by every process of the run. Tools that a run needs must live outside your home directory.

Residual risks:

- Seatbelt cannot deny the Mach calls launchd is reached through, so launchd is the only thing that stops job submission. Measured on macOS 26: a job submitted from outside the profile (through `launchctl submit` or liblaunch from Python) runs outside any sandbox, while the same submission from inside the profile is refused by launchd (`EPERM`; `launchctl submit` fails) and a copied `launchctl` cannot be executed or is killed. Read-only queries (`launchctl print`) are not refused, so the profile also denies exec of `launchctl`, `security`, `osascript`, `open`, `ssh-add`. If a macOS release stops refusing submissions from sandboxed callers, this runtime does not confine a run and must not be used on it.
- Run identity depends on `sandbox_check` from libsystem_sandbox. If a macOS release changes its answers, a process could go unproven (a run that outlives its teardown) or, worse, be mistaken for another run's. Preflight refuses when the library cannot be loaded.
- `ps` start times have one-second resolution; they only guard against signalling a reused pid.
- The profile allows POSIX shared memory (needed for macOS preferences): a run can share memory with other processes that open the same name.

Use it only for repositories and prompts you trust at the level you would run them yourself. Prefer the container runtime when Docker works.
