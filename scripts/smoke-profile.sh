#!/usr/bin/env bash
# Manual smoke test for the operator-profile INJECTION path (`[setups]` / `[profile]`).
#
# WHY this is a script, not a pytest: the hermetic suite never touches real Docker (a load-bearing
# rule - see AGENTS.md), so it mocks the docker runner and CANNOT catch daemon-level refusals. The
# sibling smoke-resume.sh exists because two such refusals shipped unnoticed in #71; this path uses
# the SAME mechanics against a different target (HOME instead of /work), so it can trip the same
# class of failure:
#   1. `docker cp` INTO a `--read-only` container is refused ("container rootfs is marked
#      read-only") even when the destination is a writable volume - hence stdin.
#   2. extracting as in-container root fails under `--cap-drop=ALL` (no CAP_DAC_OVERRIDE/CAP_CHOWN
#      for the uid-1001-owned HOME tmpfs) - hence extracting as uid 1001 with --no-same-owner.
#   3. a bundle big enough to matter (a swept ~/.claude + ~/.codex is ~400 KB gzipped) must pass
#      through the exec's stdin, which is exactly what the old by-value env var could not do.
# Run this gate before merging any change to container.deliver_profile, the profile-wait branch of
# franky-dind-entrypoint.sh, or the task `_HARDENING` profile in container.py.
#
# It asserts:
#   - the bundle pipes in over `docker exec -i` stdin (no `docker cp` into the read-only container)
#   - a nested tree (.claude/commands/pr.md) lands where the engine looks for it, readable by 1001
#   - a realistically sized bundle survives the stdin channel
#   - touching the marker releases the profile-wait entrypoint and the engine runs
#   - WITHOUT the marker the entrypoint refuses (exit 78) instead of running a profile-less build
#
# Usage: scripts/smoke-profile.sh   (needs Docker running + the `franky` image built)
set -uo pipefail

FRANKY_IMG="${FRANKY_IMG:-franky}"
TASK="smoke-profile-task-$$"
TASK_REFUSE="smoke-profile-refuse-$$"
WORKDIR="$(mktemp -d)"
BUNDLE="$WORKDIR/profile.tar.gz"
MARKER="$(python3 -c 'from franky.container import PROFILE_READY_MARKER; print(PROFILE_READY_MARKER)')"
CHOME="$(python3 -c 'from franky.profile import CONTAINER_HOME; print(CONTAINER_HOME)')"

cleanup() {
  docker rm -f -v "$TASK" "$TASK_REFUSE" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
fail() { echo "PROFILE SMOKE FAIL: $*" >&2; exit 1; }

start_task() {  # $1 = container name, $2 = inner command
  python3 scripts/smoke-task.py "$FRANKY_IMG" "$1" profile sh -c "$2" >/dev/null
}

echo "== build a bundle shaped like a real sweep (HOME-relative members, ~400 KB gzipped) =="
SRC="$WORKDIR/src"
mkdir -p "$SRC/.claude/commands" "$SRC/.claude/skills/cook" "$SRC/.codex"
echo "# global instructions" > "$SRC/.claude/CLAUDE.md"
echo "# PR spec: lead with a summary" > "$SRC/.claude/commands/pr.md"
echo "# cook skill" > "$SRC/.claude/skills/cook/SKILL.md"
echo "# codex agents" > "$SRC/.codex/AGENTS.md"
# Padding so the channel is exercised at a realistic size, not with a 200-byte toy. The filler is
# base64 of random bytes ON PURPOSE: repeated lorem-ipsum gzips down to a few KB, which would make
# this gate silently pass on a bundle far smaller than the ~400 KB a real sweep produces.
python3 - "$SRC" <<'PY'
import base64, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for i in range(60):
    filler = base64.b64encode(os.urandom(8 * 1024)).decode()
    (root / ".codex" / f"prompt-{i}.md").write_text(f"# prompt {i}\n{filler}\n")
PY
tar -czf "$BUNDLE" -C "$SRC" .
BUNDLE_KB=$(( $(wc -c < "$BUNDLE") / 1024 ))
echo "   bundle: ${BUNDLE_KB} KB gzipped"
# Guard the guard: a padding change that accidentally becomes compressible would turn this into a
# toy test. A real ~/.claude + ~/.codex sweep measured ~400 KB gzipped; stay in that ballpark.
[ "$BUNDLE_KB" -ge 200 ] || fail "test bundle is only ${BUNDLE_KB} KB - too small to exercise the stdin channel"

echo "== start the franky image in profile-wait mode (real task _HARDENING profile) =="
docker rm -f "$TASK" >/dev/null 2>&1 || true
start_task "$TASK" 'touch /tmp/profile-engine-ran; sleep 30' || fail "container did not start"
sleep 3   # let the entrypoint reach the profile-wait loop

echo "== 1. pipe the bundle over 'docker exec -i tar -xzf -' stdin into HOME, as uid 1001 =="
docker exec -i "$TASK" tar --no-same-owner -xzf - -C "$CHOME" < "$BUNDLE" \
  2> >(grep -v 'LIBARCHIVE.xattr' >&2) || fail "stdin untar into HOME failed"

echo "== 2. assert the tree landed where the engine looks, readable by uid 1001 =="
docker exec "$TASK" sh -c "grep -q 'lead with a summary' $CHOME/.claude/commands/pr.md" \
  || fail "uid 1001 cannot READ the injected PR spec"
docker exec "$TASK" sh -c "test -f $CHOME/.claude/skills/cook/SKILL.md" \
  || fail "nested skill file missing"
docker exec "$TASK" sh -c "test -f $CHOME/.codex/AGENTS.md" || fail "second setup missing"
docker exec "$TASK" sh -c "test \$(ls $CHOME/.codex/*.md | wc -l) -ge 60" \
  || fail "large bundle truncated over stdin"
echo "   OK (nested trees readable as uid 1001, large bundle intact)"

echo "== 3. touch the marker; the profile-wait entrypoint must proceed and exec the engine =="
docker exec "$TASK" touch "$MARKER" || fail "marker touch failed"
ran=0
for _ in $(seq 1 15); do
  docker exec "$TASK" test -f /tmp/profile-engine-ran && { ran=1; break; }
  sleep 1
done
[ "$ran" = 1 ] || fail "engine never ran (profile-wait stuck)"
echo "   OK (engine ran after injection)"

echo "== 4. fail-closed: with NO marker the entrypoint must refuse, not run a profile-less build =="
# The entrypoint caps its wait at 120s; we only need to prove it has not exec'd the engine and is
# still waiting (a full 120s wait would make this gate needlessly slow).
docker rm -f "$TASK_REFUSE" >/dev/null 2>&1 || true
start_task "$TASK_REFUSE" 'touch /tmp/profile-engine-ran; sleep 30' || fail "refuse-case container did not start"
sleep 8
docker exec "$TASK_REFUSE" test -f /tmp/profile-engine-ran \
  && fail "engine ran WITHOUT the profile - the fail-closed wait is broken"
docker inspect -f '{{.State.Running}}' "$TASK_REFUSE" | grep -q true \
  || fail "container exited early for some other reason (expected: still waiting)"
echo "   OK (still blocked on the marker, engine not started)"

echo "SMOKE PASS: profile injection works into the real hardened container (stdin untar into HOME)."
