# Accept VERSION=, version=, or v= (precedence: VERSION > version > v).
VERSION ?= $(version)
VERSION ?= $(v)

.PHONY: release release-dry
release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)"

release-dry:
	@test -n "$(VERSION)" || { echo "usage: make release-dry VERSION=X.Y.Z"; exit 2; }
	python3 scripts/release.py "$(VERSION)" --dry-run
