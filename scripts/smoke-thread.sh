#!/usr/bin/env bash
# Manual smoke test for `review-pr --thread` session transfer (franky/threads.py).
#
# WHY this is a script, not a pytest: the hermetic suite mocks the docker runner, so it cannot
# catch daemon-level refusals (see smoke-resume.sh / smoke-profile.sh for the history). A thread
# session travels the same stdin channel as the operator profile on the way in, and `docker cp`
# on the way out, so it drives the PRODUCTION helpers against the real hardened container:
#   - threads.session_tar packs only the session file + side dir (regular files, relative paths)
#   - container.deliver_profile streams it into HOME as uid 1001 and touches the marker last
#   - snapshot.copy_home_path streams only those paths back out (`docker cp ... -`), extracting
#     plain files and dirs; a planted symlink and the engine's project memory never come out
#   - threads.commit_session sanitizes, scrubs, verifies, and swaps the copy in
# It also gates releases on the UNPINNED claude CLI: the image's `claude --help` must still list
# `--session-id` and `--resume`, or resumed reviews would degrade to seeded ones.
#
# Run this gate before merging changes to container.deliver_profile, the session copy-out in
# container.run_in_container, franky/threads.py, or the task `_HARDENING` profile.
#
# Usage: scripts/smoke-thread.sh   (needs Docker running + an image with claude, default `franky`)
set -uo pipefail

FRANKY_IMG="${FRANKY_IMG:-franky}"
TASK="smoke-thread-task-$$"
WORKDIR="$(mktemp -d)"
SID="11111111-2222-3333-4444-555555555555"
REL=".claude/projects/-work"
CHOME="$(python3 -c 'from franky.profile import CONTAINER_HOME; print(CONTAINER_HOME)')"
export FRANKY_THREADS_DIR="$WORKDIR/threads"

cleanup() {
  docker rm -f -v "$TASK" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
fail() { echo "THREAD SMOKE FAIL: $*" >&2; exit 1; }

echo "== 1. the image's claude CLI still accepts the session flags (release gate) =="
python3 - "$FRANKY_IMG" <<'PY' || fail "claude --help lacks --session-id or --resume (or did not run)"
import subprocess, sys
proc = subprocess.run(
    ["docker", "run", "--rm", "--network", "none", "--entrypoint", "claude", sys.argv[1], "--help"],
    capture_output=True, text=True, timeout=60,
)
text = proc.stdout + proc.stderr
missing = [flag for flag in ("--session-id", "--resume") if flag not in text]
print("   claude --help:", "OK" if not missing else f"missing {missing}")
sys.exit(1 if proc.returncode or missing else 0)
PY

echo "== 2. start the image in profile-wait mode (real task _HARDENING profile) =="
python3 scripts/smoke-task.py "$FRANKY_IMG" "$TASK" profile \
  sh -c "touch /tmp/thread-engine-ran; sleep 60" >/dev/null || fail "container did not start"

echo "== 3. stream a stored session in with the production helpers =="
python3 - "$TASK" "$SID" "$REL" <<'PY' || fail "session stream-in failed"
import sys
from pathlib import Path
from franky import container, threads
task, sid, rel = sys.argv[1:4]
thread = threads.open_thread("smoke/repo", 1, "reviewer")
session = thread.session_dir / rel / f"{sid}.jsonl"
session.parent.mkdir(parents=True)
session.write_text('{"type":"user","message":"prior review"}\n')
(session.parent / "memory").mkdir()
(session.parent / "memory" / "MEMORY.md").write_text("must not travel\n")
tar = threads.session_tar(thread, threads.session_paths("claude", sid))
assert tar is not None, "session_tar packed nothing"
ok = container.deliver_profile(task, None, session_tar=str(tar))
tar.unlink()
thread.close()
sys.exit(0 if ok else 1)
PY

echo "== 4. the session is readable and writable by uid 1001, and the marker released the wait =="
docker exec "$TASK" sh -c "grep -q 'prior review' $CHOME/$REL/$SID.jsonl" \
  || fail "uid 1001 cannot READ the streamed session"
docker exec "$TASK" sh -c "echo '{\"type\":\"assistant\",\"message\":\"new turn\"}' >> $CHOME/$REL/$SID.jsonl" \
  || fail "uid 1001 cannot WRITE the streamed session"
docker exec "$TASK" sh -c "test ! -e $CHOME/$REL/memory" || fail "project memory was streamed in"
docker exec "$TASK" sh -c "mkdir -p $CHOME/$REL/$SID $CHOME/$REL/memory && echo side > $CHOME/$REL/$SID/a.jsonl \
  && echo planted > $CHOME/$REL/memory/MEMORY.md && ln -s /etc/passwd $CHOME/$REL/$SID/escape" \
  || fail "could not plant the side dir, memory, and a symlink"
ran=0
for _ in $(seq 1 15); do
  docker exec "$TASK" test -f /tmp/thread-engine-ran && { ran=1; break; }
  sleep 1
done
[ "$ran" = 1 ] || fail "engine never ran (profile-wait stuck after the session untar)"
echo "   OK"

echo "== 5. stream the session out (docker cp -) and commit it through the store =="
python3 - "$TASK" "$SID" "$REL" <<'PY' || fail "copy-out or commit failed"
import sys
from pathlib import Path
from franky import snapshot, threads
task, sid, rel = sys.argv[1:4]
thread = threads.open_thread("smoke/repo", 1, "reviewer")
incoming = threads.new_incoming(thread)
for path in threads.session_paths("claude", sid):
    target = incoming / Path(path).parent
    target.mkdir(parents=True, exist_ok=True)
    status, _used = snapshot.copy_home_path(task, path, target, max_bytes=threads.MAX_SESSION_BYTES)
    assert status == "ok", (path, status)
stored, reason = threads.commit_session(thread, incoming, [])
assert stored, reason
kept = sorted(str(p.relative_to(thread.session_dir)) for p in thread.session_dir.rglob("*") if p.is_file())
# The side dir came out; the planted symlink and the project memory did not.
assert kept == [f"{rel}/{sid}.jsonl", f"{rel}/{sid}/a.jsonl"], kept
text = (thread.session_dir / rel / f"{sid}.jsonl").read_text()
assert "prior review" in text and "new turn" in text, text
thread.close()
print("   OK (session + side dir round trip intact; symlink and memory never copied)")
PY

echo "SMOKE PASS: thread sessions stream in and copy out of the real hardened container."
