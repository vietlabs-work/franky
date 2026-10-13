#!/usr/bin/env bash
# Print the content key of the pi and proxy images built from a checkout (default: the cwd).
# The list is every repository file that Dockerfile and proxy/Dockerfile read. Each line of
# sha256sum carries a file name, so content cannot shift between files unnoticed.
set -euo pipefail
cd "${1:-.}"
files=(Dockerfile install-codex-native-launcher.sh franky-dind-entrypoint.sh)
# A base commit that predates the mapper has no such file; every other input must exist.
[ ! -e franky-repo-setup.py ] || files+=(franky-repo-setup.py)
files+=(proxy/Dockerfile proxy/entrypoint.sh)
sha256sum "${files[@]}" | sha256sum | cut -c1-16
