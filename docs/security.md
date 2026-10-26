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
