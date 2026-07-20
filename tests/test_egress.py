from franky.egress import (
    DOCKER_REGISTRY_DOMAINS,
    GITHUB_DOMAINS,
    REGISTRY_DOMAINS,
    build_allowlist,
)
from franky.engine import (
    PI_PROVIDER_HOSTS,
    PI_PROVIDER_VARS,
    ClaudeEngine,
    CodexEngine,
    PiEngine,
)


def test_every_pi_provider_var_maps_to_a_host():
    # Guard against drift: PI_PROVIDER_VARS (cred gating) and PI_PROVIDER_HOSTS (allowlist)
    # are parallel and hand-maintained. A provider var with no host would make build_allowlist
    # silently omit that provider, so the task would run but never reach its model. OLLAMA_HOST
    # is the one exception - its host is parsed from the URL value, not the static map.
    for var in PI_PROVIDER_VARS:
        if var == "OLLAMA_HOST":
            continue
        assert var in PI_PROVIDER_HOSTS, (
            f"{var} is in PI_PROVIDER_VARS but missing from PI_PROVIDER_HOSTS"
        )


def test_claude_allowlist_has_anthropic_github_registries():
    allow = build_allowlist(ClaudeEngine(), {}, [])
    assert "api.anthropic.com" in allow
    for d in GITHUB_DOMAINS:
        assert d in allow
    for d in REGISTRY_DOMAINS:
        assert d in allow


def test_pi_openrouter_host():
    allow = build_allowlist(PiEngine(), {"OPENROUTER_API_KEY": "x"}, [])
    assert "openrouter.ai" in allow
    # provider gating is per-cred: no openai key set => its host is NOT opened
    assert "api.openai.com" not in allow


def test_pi_openai_host():
    allow = build_allowlist(PiEngine(), {"OPENAI_API_KEY": "x"}, [])
    assert "api.openai.com" in allow


def test_codex_allowlist_has_openai_when_key_present():
    allow = build_allowlist(CodexEngine(), {"CODEX_API_KEY": "x"}, [])
    assert "api.openai.com" in allow
    for d in GITHUB_DOMAINS:
        assert d in allow


def test_codex_allowlist_ignores_openai_key():
    allow = build_allowlist(CodexEngine(), {"OPENAI_API_KEY": "x"}, [])
    assert "api.openai.com" not in allow


def test_codex_allowlist_no_openai_when_no_key():
    allow = build_allowlist(CodexEngine(), {}, [])
    assert "api.openai.com" not in allow


def test_codex_allowlist_opens_openai_for_subscription_marker():
    allow = build_allowlist(CodexEngine(), {"FRANKY_CODEX_SUBSCRIPTION": "1"}, [])
    assert "api.openai.com" in allow


def test_pi_ollama_host_parsed_from_url():
    allow = build_allowlist(PiEngine(), {"OLLAMA_HOST": "http://ollama.internal:11434"}, [])
    assert "ollama.internal" in allow
    # the port and scheme are not part of the dstdomain
    assert "ollama.internal:11434" not in allow


def test_pi_multiple_provider_vars():
    allow = build_allowlist(
        PiEngine(),
        {"OPENROUTER_API_KEY": "a", "GEMINI_API_KEY": "b", "GROQ_API_KEY": "c"},
        [],
    )
    assert "openrouter.ai" in allow
    assert "generativelanguage.googleapis.com" in allow
    assert "api.groq.com" in allow


def test_extra_domains_merged():
    allow = build_allowlist(ClaudeEngine(), {}, ["example.com", "deps.internal"])
    assert "example.com" in allow
    assert "deps.internal" in allow


def test_allowlist_dedup_and_sorted():
    # github.com appears via GITHUB_DOMAINS; passing it again as an extra must not duplicate.
    allow = build_allowlist(ClaudeEngine(), {}, ["github.com", "  api.anthropic.com  "])
    assert allow == sorted(allow)
    assert len(allow) == len(set(allow))
    # whitespace stripped before dedup
    assert "api.anthropic.com" in allow
    assert allow.count("github.com") == 1


def test_docker_registries_always_allowlisted():
    # Always-on rootless DinD: the container image registries are in every allowlist so a
    # nested `docker pull` reaches them through the proxy. Engine-independent.
    allow = build_allowlist(PiEngine(), {"OPENROUTER_API_KEY": "x"}, [])
    for d in DOCKER_REGISTRY_DOMAINS:
        assert d in allow
    # Docker Hub is the load-bearing one (proven in the spike).
    assert ".docker.io" in allow


def test_docker_registries_use_dot_forms_without_apex_overlap():
    # Same Squid rule as GitHub: a dot-form and its bare apex together FATAL the proxy. Assert
    # no entry in the docker registry set has both forms present.
    allow = build_allowlist(ClaudeEngine(), {}, [])
    for d in DOCKER_REGISTRY_DOMAINS:
        if d.startswith("."):
            assert d[1:] not in allow, f"apex {d[1:]} overlaps dot-form {d} (FATAL in Squid)"


def test_github_uses_dot_forms_only():
    # Squid `dstdomain .github.com` matches the apex `github.com` AND every subdomain, so the
    # dot form alone covers the whole host family. Listing the bare apex `github.com` TOO is a
    # FATAL Squid 6 config error (overlap), so it must NOT appear. Verified end-to-end: a
    # proxied request to https://github.com succeeds with the dot form alone.
    allow = build_allowlist(ClaudeEngine(), {}, [])
    assert ".github.com" in allow
    assert "github.com" not in allow
    assert ".githubusercontent.com" in allow
