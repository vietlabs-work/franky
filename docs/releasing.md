# Releasing Franky

## Create a release

```bash
make release VERSION=x.y.z
```

The release script:

1. Accepts a plain `x.y.z` version.
2. Requires a clean `main` that matches `origin/main`.
3. Refuses an existing `vX.Y.Z` tag.
4. Updates `pyproject.toml` and `franky/__init__.py` together.
5. Converts `Unreleased` into a dated changelog section.
6. Updates the README status version.
7. Commits, creates an annotated tag, and pushes `main` with the tag.

Tests reject README, changelog, and package version drift before release.

## Dry run

```bash
make release-dry VERSION=x.y.z
# Or:
python3 scripts/release.py x.y.z --dry-run
```

The dry run prints file changes and Git commands. It does not write files or change Git state.

## Recover a failed tag push

Use this only when the release commit exists but its tag push failed:

```bash
python3 scripts/release.py tag x.y.z --dry-run
python3 scripts/release.py tag x.y.z
```

The command requires matching package versions, an absent tag, and a clean tree.

## CI release gate

Each tag push runs the complete footprint workflow and checks that the tag and both package version files match.

Images build in parallel with that gate, on native amd64 and arm64 runners. Each build pushes an untagged digest. The version and `latest` tags are applied only after the gate passes.

A successful `vX.Y.Z` workflow publishes:

| Artifact | Destination |
|----------|-------------|
| Wheel and source archive | PyPI package `franky-agent` |
| Full task image | `ghcr.io/<owner>/franky:X.Y.Z` |
| Engine images | The same tag with `-pi`, `-claude`, `-codex`, or `-opencode` |
| Proxy image | `ghcr.io/<owner>/franky-proxy:X.Y.Z` |
| Convenience images | Matching `latest` tags |
| Release notes and archives | GitHub Release |

The GitHub Release runs last. Its presence means PyPI and all images completed.

PyPI publishing uses Trusted Publishing with OIDC. The repository stores no PyPI token.

## One-time setup

- Register a PyPI trusted publisher for `franky-agent`, `release.yml`, and the `pypi` environment.
- Make the `franky` and `franky-proxy` GHCR packages public.
- Keep `DEFAULT_GHCR_REPO` aligned with the repository owner.

The workflow derives its namespace from `github.repository_owner`. Set `FRANKY_GHCR_REPO` during a namespace migration.

## Image trust

The CLI uses version-pinned image tags, never `latest`. Treat each published version tag as immutable.

For local development, set `FRANKY_IMAGE` and `FRANKY_PROXY_IMAGE`. See [`config.example.toml`](../config.example.toml).
