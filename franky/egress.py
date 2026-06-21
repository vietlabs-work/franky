"""Build the egress allowlist (default-deny) for the proxy. Pure - no docker, no I/O.

WHY a module of its own: the allowlist is policy (which hosts the agent may reach), distinct
from the docker mechanics in container.py. Keeping policy out of the argv builders means a
test can assert exactly which hosts are permitted without reasoning about docker flags, and a
future policy change (a new provider, a new registry) never touches the container plumbing.
"""
from __future__ import annotations

from collections.abc import Mapping

from .engine import Engine

# GitHub: clone, push, open the PR. The leading-dot form is deliberate and sufficient: Squid
# `dstdomain .github.com` matches the apex `github.com` AND every subdomain (api, codeload,
# ...), so it covers the whole host family in one entry. Listing the apex `github.com`
# alongside `.github.com` is NOT just redundant - Squid 6 treats the overlap as a FATAL config
# error ("'.github.com' is a subdomain of 'github.com'") and refuses to start. So: dot forms
# only. `.githubusercontent.com` covers raw blobs + release/LFS objects. (Verified against the
# real proxy: a proxied request to the apex github.com succeeds with the dot form alone.)
GITHUB_DOMAINS = [".github.com", ".githubusercontent.com"]

# Package registries the agent needs to install deps during a build.
REGISTRY_DOMAINS = ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"]


def build_allowlist(
    engine: Engine,
    passthrough_env: Mapping[str, str],
    extra_domains: list[str],
) -> list[str]:
    """The full default-deny allowlist: the engine's provider host(s) + GitHub + registries +
    any operator extras. Stripped, empties dropped, deduped, sorted for determinism.

    `passthrough_env` is the same mapping Franky resolved its config from, so the provider
    host(s) match the cred(s) actually being injected - no host is opened that the engine has
    no key for.
    """
    raw = (
        list(engine.provider_hosts(passthrough_env))
        + GITHUB_DOMAINS
        + REGISTRY_DOMAINS
        + list(extra_domains)
    )
    cleaned = {d.strip() for d in raw if d and d.strip()}
    return sorted(cleaned)
