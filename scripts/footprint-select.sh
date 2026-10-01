#!/usr/bin/env bash
# Write the bounded footprint checks and image variants for this CI event to GITHUB_OUTPUT.
# Every footprint job runs this at start, so no job waits on another to learn its work.
set -euo pipefail

if [ "$EVENT" != pull_request ] || [ "${REQUESTED_MODE:-}" = full ]; then
  checks="host,images,runtime"
  variants="all,claude,codex,opencode,pi,proxy"
else
  mapfile -d '' paths < <(git diff --name-only -z "$BASE_SHA" "$HEAD_SHA")
  checks=$(python3 scripts/footprint.py select "${paths[@]}")
  variants=$(python3 scripts/footprint.py variants "${paths[@]}")
fi
echo "checks=$checks" >> "$GITHUB_OUTPUT"
echo "variants=$variants" >> "$GITHUB_OUTPUT"
echo "Selected: $checks; images: ${variants:-none}"
