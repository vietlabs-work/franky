#!/usr/bin/env bash
# Manual smoke test for the `franky job resume` RESTORE path (issue #71).
#
# WHY this is a script, not a pytest: the hermetic suite never touches real Docker (a load-bearing
# rule - see CLAUDE.md), so it mocks the docker runner and CANNOT catch daemon-level refusals. Two
# such refusals shipped in the original #71 and were only caught here:
#   1. `docker cp` INTO a `--read-only` container is refused ("container rootfs is marked
#      read-only") even when the destination is a writable volume.
#   2. extracting/chowning as in-container root fails under `--cap-drop=ALL` (root has no
#      CAP_DAC_OVERRIDE/CAP_CHOWN to write the uid-1001-owned /work tmpfs).
# Run this gate before merging any change to franky/snapshot.py's restore path, the resume-wait
# branch of franky-dind-entrypoint.sh, or the task `_HARDENING` profile in container.py.
#
# It stands up the franky image with the REAL task hardening profile in resume-wait mode, then
# drives the exact host-side restore mechanic snapshot.py emits and asserts:
#   - the tar pipes in over `docker exec -i` stdin (no `docker cp` into the read-only container)
#   - extraction as the default uid 1001 lands a tree uid 1001 can READ and WRITE (no chown step)
#   - the git repo survives the round-trip
#   - touching the marker lets the resume-wait entrypoint proceed and exec the engine
#
# Usage: scripts/smoke-resume.sh   (needs Docker running + the `franky` image built)
set -uo pipefail

FRANKY_IMG="${FRANKY_IMG:-franky}"
TASK="smoke-resume-task-$$"
WORKDIR="$(mktemp -d)"
TARBALL="$WORKDIR/ws.tar.gz"
cleanup() { docker rm -f -v "$TASK" >/dev/null 2>&1 || true; rm -rf "$WORKDIR"; }
trap cleanup EXIT
fail() { echo "RESUME SMOKE FAIL: $*" >&2; exit 1; }

echo "== build a fake workspace tar (mimics finalize_snapshot: a git repo, gzip tar, mode 0600) =="
SRC="$WORKDIR/src"
mkdir -p "$SRC"
( cd "$SRC" && git init -q && git config user.email s@s && git config user.name s \
  && echo "hello" > file.txt && git add file.txt && git commit -qm init )
tar -czf "$TARBALL" -C "$SRC" .
chmod 0600 "$TARBALL"   # snapshot tars are 0600 - the crux the cp-in + root-untar path tripped on

echo "== start the franky image in resume-wait mode (real task _HARDENING profile) =="
python3 scripts/smoke-task.py "$FRANKY_IMG" "$TASK" resume \
  sh -c 'touch /tmp/resume-engine-ran; sleep 30' >/dev/null \
  || fail "container did not start"
sleep 3   # let the entrypoint enter the resume-wait loop

echo "== 1. pipe the 0600 tar over 'docker exec -i tar -xzf -' stdin, as uid 1001 (the fixed path) =="
# No `docker cp` INTO the read-only container; extract as the default uid 1001 (owns /work) with
# --no-same-owner, so the tree lands 1001-owned and no chown is needed under --cap-drop=ALL.
docker exec -i "$TASK" tar --no-same-owner -xzf - -C /work < "$TARBALL" \
  2> >(grep -v 'LIBARCHIVE.xattr' >&2) || fail "stdin untar failed"

echo "== 2. assert uid 1001 can READ and WRITE the restored /work, and git survived =="
docker exec "$TASK" sh -c 'cat /work/file.txt | grep -q hello' || fail "uid 1001 cannot READ restored file"
docker exec "$TASK" sh -c 'echo more >> /work/file.txt && echo new > /work/newfile.txt' \
  || fail "uid 1001 cannot WRITE restored /work"
docker exec "$TASK" sh -c 'cd /work && git status >/dev/null 2>&1' || fail "restored git repo is not intact"
echo "   OK (read + write + git intact as uid 1001)"

echo "== 3. touch the marker; the resume-wait entrypoint must proceed and exec the engine =="
docker exec "$TASK" touch /work/.franky-resume-ready || fail "marker touch failed"
ran=0
for _ in $(seq 1 15); do
  docker exec "$TASK" test -f /tmp/resume-engine-ran && { ran=1; break; }
  sleep 1
done
[ "$ran" = 1 ] || fail "engine never ran after marker (resume-wait stuck)"
echo "   OK (engine ran after restore)"

echo "SMOKE PASS: resume restore works into the real hardened container (stdin untar as uid 1001)."
