# Accept the version arg under ANY casing: VERSION=, version=, v=, V=, vERsiOn=, ...
# Make vars are case-sensitive, so we scan the raw command-line assignments
# ($(MAKEOVERRIDES)) and match the name case-insensitively against `version`/`v`.
# `?=` so an exact `VERSION=` on the command line short-circuits the scan.
# The `\#` is escaped: a bare `#` would start a Make comment and eat the `)`.
VERSION ?= $(shell for kv in $(MAKEOVERRIDES); do n=$${kv%%=*}; l=$$(printf '%s' "$$n" | tr A-Z a-z); if [ "$$l" = version ] || [ "$$l" = v ]; then printf '%s' "$${kv\#*=}"; break; fi; done)

.PHONY: smoke-dind eval release release-dry
# Manual gate for always-on rootless Docker-in-Docker (#12). Needs real Docker; not in CI (the
# pytest suite never touches real Docker). Run before merging changes to the Dockerfile,
# franky-dind-entrypoint.sh, container.py's _HARDENING, or egress.py.
smoke-dind:
	bash scripts/smoke-dind.sh

# Opt-in, out-of-band agent-quality eval (#25). Needs real Docker + engine creds + a sandbox
# repo (see evals/README.md); NOT in CI. Bare `make eval` runs evals/tasks.json once; pass
# flags via ARGS, e.g. `make eval ARGS="-n 3 --engine pi --compare-engine codex"`.
eval:
	python3 scripts/eval.py $(ARGS)

release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)"

release-dry:
	@test -n "$(VERSION)" || { echo "usage: make release-dry VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)" --dry-run
