# Releasing Franky

## Cutting a release

```bash
make release VERSION=x.y.z
```

This runs `scripts/release.py x.y.z`, which:
1. Validates the version string (plain `x.y.z` only - no prerelease suffixes).
2. Asserts the repo is on `main`, the working tree is clean, and local `main` matches
   `origin/main`.
3. Asserts the tag `vX.Y.Z` does not already exist.
4. Bumps `version` in `pyproject.toml` and `__version__` in `franky/__init__.py` in lockstep.
5. Retitles the `## [Unreleased]` section in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`
   and inserts a fresh empty `## [Unreleased]` block above it.
6. Bumps the `vX.Y.Z` on the `## Status` line in `README.md` so the docs track the release.
   (The PyPI install command has no version pin, so there is nothing else to bump.) Fails
   loudly if the Status line is missing.
7. Commits (`release: vX.Y.Z`), creates an annotated tag (`vX.Y.Z`), and pushes both
   in a single `git push origin main vX.Y.Z`.

A CI doc-coherence guard (`tests/test_doc_coherence.py`, in the pytest job) asserts on every
PR that the README version refs and the newest CHANGELOG section stay in step with
`pyproject.toml`, and that no literal `X.Y.Z` placeholder lingers in a README command block -
so drift cannot creep back in between releases.

## Dry run

```bash
make release-dry VERSION=x.y.z
# or
python3 scripts/release.py x.y.z --dry-run
```

Prints exactly what WOULD change (version bumps, changelog diff, git commands) without
writing any files or running any git mutations.

## Version-skew guard

CI runs `python3 scripts/release.py guard vX.Y.Z` on every tag push before publishing
anything. It asserts that `pyproject.toml`, `franky/__init__.py`, and the tag all carry
the same version. Any skew exits nonzero and blocks the publish jobs.

You can run it locally:

```bash
python3 scripts/release.py guard v0.1.0
```

## Recovery: tag subcommand

If the commit was created but the push failed, you can re-push just the tag:

```bash
python3 scripts/release.py tag x.y.z
# or with --dry-run to check first
python3 scripts/release.py tag x.y.z --dry-run
```

`tag` asserts the versions in pyproject and `__init__.py` already match `x.y.z`,
the tag is absent, and the tree is clean - then creates and pushes the tag only.

## What CI publishes

On a `vX.Y.Z` tag push, the `release.yml` workflow:
1. Runs the guard.
2. Builds the Python wheel and sdist (`python -m build`).
3. Publishes the wheel + sdist to PyPI (as `franky-agent`) via Trusted Publishing (OIDC) -
   no stored token.
4. Builds and pushes both Docker images to GHCR under the repo's owning org
   (`ghcr.io/<owner>/franky:X.Y.Z` and `ghcr.io/<owner>/franky-proxy:X.Y.Z`, plus `:latest`
   convenience tags).
5. Creates a GitHub Release with the wheel/sdist attached and the changelog section as
   release notes.

The GitHub Release is the last job (`needs: [wheel, pypi, image]`) so its existence implies
the wheel, the PyPI publish, and the images all shipped.

## One-time publishing prerequisites

Before the first public release these must be set up out-of-band (no secret is stored in the
repo for either):

- **PyPI Trusted Publisher** for `franky-agent`: on PyPI, add a pending publisher bound to
  this repo, workflow `release.yml`, and environment `pypi`. The `pypi` job uses OIDC
  (`id-token: write`) - no API token.
- **Public GHCR packages**: set the `franky` and `franky-proxy` packages to public visibility
  in the owning org's package settings, so the CLI pulls them with no `docker login`.

The CLI's default GHCR namespace (`DEFAULT_GHCR_REPO` in `franky/container.py`) must match the
org that hosts the public packages. The release workflow pushes to
`ghcr.io/${{ github.repository_owner }}`, so moving the repo to a new org retargets the images
automatically; update `DEFAULT_GHCR_REPO` to the same org. Until they align, set
`FRANKY_GHCR_REPO=ghcr.io/<owner>` to point the CLI at wherever the images currently live.

## Image overrides for local dev

Set `FRANKY_IMAGE` and `FRANKY_PROXY_IMAGE` to point at local builds to bypass GHCR entirely,
or `FRANKY_GHCR_REPO` to retarget just the namespace (see `config.example.toml`, or run
`franky config path`).

## Trust model for images

The CLI always resolves to the version-pinned tag (`ghcr.io/<owner>/franky:X.Y.Z`),
not `:latest`. That tag is treated as immutable: once published it is never overwritten.
`:latest` is a human convenience tag; the CLI never uses it.
