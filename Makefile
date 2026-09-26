# Accept the version arg under ANY casing: VERSION=, version=, v=, V=, vERsiOn=, ...
# Make vars are case-sensitive, so we scan the raw command-line assignments
# ($(MAKEOVERRIDES)) and match the name case-insensitively against `version`/`v`.
# `?=` so an exact `VERSION=` on the command line short-circuits the scan.
# The `\#` is escaped: a bare `#` would start a Make comment and eat the `)`.
VERSION ?= $(shell for kv in $(MAKEOVERRIDES); do n=$${kv%%=*}; l=$$(printf '%s' "$$n" | tr A-Z a-z); if [ "$$l" = version ] || [ "$$l" = v ]; then printf '%s' "$${kv\#*=}"; break; fi; done)

.PHONY: footprint smoke-security smoke-dind smoke-resume smoke-profile smoke-thread smoke-memory eval release release-dry

# Credential-free host CPU and memory comparison against BASE. Real-Docker runtime checks stay
# in smoke-memory; CI also builds and checks every release image variant.
footprint:
	@set -eu; scratch=$$(mktemp -d); trap 'rm -rf "$$scratch"' EXIT; \
		mkdir "$$scratch/base"; \
		git archive "$${BASE:-origin/main}" | tar -x -C "$$scratch/base"; \
		base_sha=$$(git rev-parse "$${BASE:-origin/main}"); \
		python3 scripts/footprint.py host --repo "$$scratch/base" --source-sha "$$base_sha" \
			--output "$$scratch/base.json"; \
		python3 scripts/footprint.py host --repo . --output "$$scratch/head.json"; \
		python3 scripts/footprint.py compare-host --base "$$scratch/base.json" \
			--head "$$scratch/head.json" --budgets scripts/footprint-budgets.json
# Manual gate for always-on rootless Docker-in-Docker (#12). Needs real Docker. Run before
# merging changes to the Dockerfile,
# franky-dind-entrypoint.sh, container.py's _HARDENING, or egress.py.
smoke-dind:
	bash scripts/smoke-dind.sh

smoke-security:
	python3 scripts/smoke-security.py $(ARGS)

# Manual gate for the `franky job resume` restore path (#71). Needs real Docker + the `franky`
# image; not in CI. Run before merging changes to franky/snapshot.py's restore path, the
# resume-wait branch of franky-dind-entrypoint.sh, or container.py's task _HARDENING.
smoke-resume:
	bash scripts/smoke-resume.sh

# Manual gate for operator-profile injection ([setups]/[profile]). Needs real Docker + the franky
# image; not in CI. Run before merging changes to container.deliver_profile, the profile-wait
# branch of franky-dind-entrypoint.sh, or container.py's task _HARDENING.
smoke-profile:
	bash scripts/smoke-profile.sh

# Manual gate for `review-pr --thread` session transfer. Needs real Docker + an image with claude;
# not in CI. Run before merging changes to container.deliver_profile, session copy-out, or the
# task _HARDENING. It also gates releases on the unpinned claude CLI keeping its session flags.
smoke-thread:
	bash scripts/smoke-thread.sh

smoke-memory:
	python3 scripts/smoke-memory.py $(ARGS)

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
