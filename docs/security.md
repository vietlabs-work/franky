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

Each task runs its own rootless Docker daemon. Agents can build images, run Compose, and use testcontainers without reaching the host daemon.

```text
task and nested Docker -> internal network -> Squid proxy -> allowed HTTPS hosts
```

The default egress list includes the selected provider, GitHub, package registries, and container registries. Add required hosts with `FRANKY_EXTRA_ALLOWED_DOMAINS`.

Allowlisted hosts remain trusted destinations, not inert endpoints. A hostile task can use available credentials against those hosts.

The agent can also pass its credentials to nested containers. The outer limits and egress policy still apply to the full task tree.

Opening a PR can start GitHub Actions. Review workflow changes before you allow a run to use repository secrets.

`build --no-publish` exports commits without a write credential. Two networkless, read-only, capability-free helpers read the stopped task's volumes read-only during the entrypoint's session hold, so no stopped container ever keeps the tokens. Franky scans the exported patch text for the run's secret values and refuses the export on a hit. It does not scan for credential patterns, so the caller that pushes the bundle must. The host reads only the bundle header, and never runs git on a checkout the container wrote. Franky cannot enforce that the supplied `GH_TOKEN` is read-only.

Codex subscription authentication is the only persistent task volume. Franky scrubs it to `auth.json` before each autonomous run.

Read [`AGENTS.md`](../AGENTS.md) before changing security, container, egress, profile, or resource code.
