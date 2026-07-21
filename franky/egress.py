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

# Container image registries for the always-on rootless Docker daemon (issue #12): a task's
# nested `docker pull` / `docker compose` / testcontainers reach these THROUGH the proxy (the
# nested daemon honours HTTP(S)_PROXY, so Squid does the DNS and the cage still bounds it - see
# the issue-12 spike). Broad on purpose ("any well-known registry works out of the box") - the
# cost is that these hosts are reachable on EVERY task, not just docker-using ones; this is an
# accepted residual of always-on DinD (documented in the README security section).
#
# Squid dot-form rules apply (see GITHUB_DOMAINS): a leading dot matches the apex AND all
# subdomains, and listing an apex alongside its dot-form is a FATAL Squid overlap - so each
# family appears in exactly one form. Many registries redirect layer BLOBS to a separate CDN;
# those backends are included too (and verified/trimmed empirically - see the issue-12 plan).
DOCKER_REGISTRY_DOMAINS = [
    # Docker Hub: registry-1/auth/index (.docker.io), Hub web + CDN (.docker.com), R2 blobs.
    ".docker.io",
    ".docker.com",
    ".cloudflarestorage.com",
    # GitHub Container Registry. ghcr.io blobs come from *.githubusercontent.com, already
    # covered by GITHUB_DOMAINS above.
    "ghcr.io",
    # Google Container/Artifact Registry (gcr.io + regional, *-docker.pkg.dev); blobs from GCS.
    ".gcr.io",
    ".pkg.dev",
    "storage.googleapis.com",
    # Quay.
    "quay.io",
    # AWS ECR Public.
    "public.ecr.aws",
    # Microsoft Container Registry (mcr.microsoft.com + *.data.mcr.microsoft.com blob hosts).
    ".mcr.microsoft.com",
    # Kubernetes registry.
    "registry.k8s.io",
    # GitLab registry.
    "registry.gitlab.com",
    # Shared CDN several of the above redirect layer blobs to. This is the broadest entry (ANY
    # cloudfront-hosted host becomes reachable) - kept for "registries work out of the box";
    # called out as accepted surface in the README.
    ".cloudfront.net",
]


def build_allowlist(
    engine: Engine,
    passthrough_env: Mapping[str, str],
    extra_domains: list[str],
) -> list[str]:
    """The full default-deny allowlist: the engine's provider host(s) + GitHub + language
    registries + container image registries (always-on DinD) + any operator extras. Stripped,
    empties dropped, deduped, sorted for determinism.

    `passthrough_env` is the same mapping Franky resolved its config from, so the provider
    host(s) match the cred(s) actually being injected - no host is opened that the engine has
    no key for.
    """
    raw = (
        list(engine.provider_hosts(passthrough_env))
        + GITHUB_DOMAINS
        + REGISTRY_DOMAINS
        + DOCKER_REGISTRY_DOMAINS
        + list(extra_domains)
    )
    cleaned = {d.strip() for d in raw if d and d.strip()}
    dot_families = {domain[1:].lower() for domain in cleaned if domain.startswith(".")}
    return sorted(
        domain
        for domain in cleaned
        if domain.startswith(".")
        or not any(
            domain.lower() == family or domain.lower().endswith(f".{family}")
            for family in dot_families
        )
    )
