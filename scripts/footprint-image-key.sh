#!/usr/bin/env bash
# Print the content key of the pi and proxy images built from a checkout (default: the cwd).
# The list is every repository file that Dockerfile and proxy/Dockerfile read. Each line of
# sha256sum carries a file name, so content cannot shift between files unnoticed.
set -euo pipefail
cd "${1:-.}"
sha256sum Dockerfile install-codex-native-launcher.sh franky-dind-entrypoint.sh \
  proxy/Dockerfile proxy/entrypoint.sh | sha256sum | cut -c1-16
