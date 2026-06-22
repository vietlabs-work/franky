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
6. Bumps the version refs in `README.md` (the `@vX.Y.Z` install pins and the `## Status`
   line) so the docs track the release. Fails loudly if no install pin is found.
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
3. Builds and pushes both Docker images to GHCR:
   - `ghcr.io/vietlabs-work/franky:X.Y.Z`
   - `ghcr.io/vietlabs-work/franky-proxy:X.Y.Z`
   - (also `:latest` convenience tags)
4. Creates a GitHub Release with the wheel/sdist attached and the changelog section as
   release notes.

The GitHub Release is the last job so its existence implies wheel + images were published.

## GHCR auth (while the repo is private)

The CLI pulls its images from GHCR on first run. While the repo and packages are private:

```bash
docker login ghcr.io
# enter your GitHub username and a PAT with read:packages scope
```

Set `FRANKY_IMAGE` and `FRANKY_PROXY_IMAGE` to point at local builds to bypass GHCR
entirely during development (see `.env.example`).

## Trust model for images

The CLI always resolves to the version-pinned tag (`ghcr.io/vietlabs-work/franky:X.Y.Z`),
not `:latest`. That tag is treated as immutable: once published it is never overwritten.
`:latest` is a human convenience tag; the CLI never uses it.

## Making the repo public (follow-up)

Once the GitHub repo and packages are public:
- The `docker login ghcr.io` prerequisite for `read:packages` drops away.
- Add PyPI trusted publishing (OIDC) to publish the wheel to PyPI without a token; update
  the release workflow to add a `pypi-publish` step using `pypa/gh-action-pypi-publish`.
- Update the README install section to use `pip install franky` / `uv tool install franky`.
