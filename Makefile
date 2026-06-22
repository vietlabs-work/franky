# Accept VERSION=, version=, or v= (precedence: VERSION > version > v).
VERSION ?= $(version)
VERSION ?= $(v)

.PHONY: smoke-dind release release-dry
# Manual gate for always-on rootless Docker-in-Docker (#12). Needs real Docker; not in CI (the
# pytest suite never touches real Docker). Run before merging changes to the Dockerfile,
# franky-dind-entrypoint.sh, container.py's _HARDENING, or egress.py.
smoke-dind:
	bash scripts/smoke-dind.sh

release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)"

release-dry:
	@test -n "$(VERSION)" || { echo "usage: make release-dry VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)" --dry-run
