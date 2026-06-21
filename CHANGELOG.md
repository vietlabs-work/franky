# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Release pipeline: `make release VERSION=x.y.z` bumps version, commits, tags, pushes; CI
  publishes wheel + both GHCR images (franky, franky-proxy) + a GitHub Release (#8).
- Default-deny egress proxy (Squid) over a Docker `--internal` network; task container
  reaches only allowlisted hosts via a creds-blind CONNECT proxy (#1).
- CI workflow: pytest across Python 3.10-3.13 + ruff check/format on every PR and push
  to main (#3).
