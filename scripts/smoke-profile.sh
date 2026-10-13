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
#   - team setup + repository setup (franky-repo-setup): a team skill and a repo skill, loaded
#     before the engine starts, are both visible to codex at session start (`codex debug
#     prompt-input`, offline) and sit in claude's native skills dir; the repo copy of a same-named
#     skill wins; the mapper reads only the given commit; a bad commit fails closed
#     (set CLAUDE_CODE_OAUTH_TOKEN to also have `claude -p` list them; it is the only step that
#     needs a credential, and is skipped without one)
#
# Usage: scripts/smoke-profile.sh   (needs Docker running + the `franky` image built)
set -uo pipefail

FRANKY_IMG="${FRANKY_IMG:-franky}"
TASK="smoke-profile-task-$$"
TASK_REFUSE="smoke-profile-refuse-$$"
TASK_REPO="smoke-profile-repo-$$"
WORKDIR="$(mktemp -d)"
BUNDLE="$WORKDIR/profile.tar.gz"
MARKER="$(python3 -c 'from franky.container import PROFILE_READY_MARKER; print(PROFILE_READY_MARKER)')"
CHOME="$(python3 -c 'from franky.profile import CONTAINER_HOME; print(CONTAINER_HOME)')"

cleanup() {
  docker rm -f -v "$TASK" "$TASK_REFUSE" "$TASK_REPO" >/dev/null 2>&1 || true
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

echo "== 5. team + repository setup: both skills are loaded before the engine starts =="
docker rm -f "$TASK_REPO" >/dev/null 2>&1 || true
start_task "$TASK_REPO" 'touch /tmp/profile-engine-ran; sleep 120' || fail "repo-setup container did not start"
sleep 3
# The team part rides the bundle, exactly as profile.build_bundle packs it (codex native dirs here,
# claude's below); the repository part is a throwaway local git repo the mapper fetches over file://.
TEAM="$WORKDIR/team"
mkdir -p "$TEAM/.codex/skills/team-skill" "$TEAM/.codex/skills/shared" "$TEAM/.claude/skills/team-skill"
printf -- '---\nname: team-skill\ndescription: The team skill\n---\nbody\n' > "$TEAM/.codex/skills/team-skill/SKILL.md"
printf -- '---\nname: team-skill\ndescription: The team skill\n---\nbody\n' > "$TEAM/.claude/skills/team-skill/SKILL.md"
printf -- '---\nname: shared\ndescription: TEAM COPY\n---\n' > "$TEAM/.codex/skills/shared/SKILL.md"
printf '## Team rules (always on)\n\nTEAM-RULE-MARKER\n' > "$TEAM/.codex/AGENTS.md"
tar -czf "$WORKDIR/team.tar.gz" -C "$TEAM" .
docker exec -i "$TASK_REPO" tar --no-same-owner -xzf - -C "$CHOME" < "$WORKDIR/team.tar.gz" \
  2> >(grep -v 'LIBARCHIVE.xattr' >&2) || fail "team untar failed"
docker exec "$TASK_REPO" sh -c '
  set -e
  r=/tmp/smoke-src; mkdir -p $r/.claude/skills/repo-skill $r/.claude/skills/shared $r/.claude/hooks; cd $r
  git init -q
  git config uploadpack.allowFilter true; git config uploadpack.allowAnySHA1InWant true
  printf -- "---\nname: repo-skill\ndescription: The repo skill\n---\n" > .claude/skills/repo-skill/SKILL.md
  printf -- "---\nname: shared\ndescription: REPO COPY\n---\n" > .claude/skills/shared/SKILL.md
  echo "REPO-RULE-MARKER" > AGENTS.md; echo "hook" > .claude/hooks/pre.md
  echo "{}" > .claude/settings.json
  git add -A; git -c user.name=t -c user.email=t@example.com commit -qm base
  git rev-parse HEAD > /tmp/base-sha
  echo "PR-HEAD-ONLY" > AGENTS.md; mkdir -p .claude/skills/pr-only; echo x > .claude/skills/pr-only/SKILL.md
  git add -A; git -c user.name=t -c user.email=t@example.com commit -qm head
' || fail "could not build the throwaway repository in the container"
BASE_SHA="$(docker exec "$TASK_REPO" cat /tmp/base-sha)"
docker exec "$TASK_REPO" franky-repo-setup map --repo acme/service --ref "$BASE_SHA" --engine codex \
  --remote file:///tmp/smoke-src || fail "franky-repo-setup map failed in the real image"
docker exec "$TASK_REPO" franky-repo-setup map --repo acme/service --ref "$BASE_SHA" --engine claude \
  --remote file:///tmp/smoke-src >/dev/null || fail "franky-repo-setup map (claude) failed"
docker exec "$TASK_REPO" sh -c "test -f $CHOME/.codex/skills/repo-skill/SKILL.md && test ! -e $CHOME/.codex/skills/pr-only" \
  || fail "repo skill missing, or a skill from the PR head leaked in"
docker exec "$TASK_REPO" sh -c "grep -q 'REPO COPY' $CHOME/.codex/skills/shared/SKILL.md" \
  || fail "the repo copy of a same-named skill did not win over the team copy"
docker exec "$TASK_REPO" sh -c "grep -q TEAM-RULE-MARKER $CHOME/.codex/AGENTS.md && grep -q REPO-RULE-MARKER $CHOME/.codex/AGENTS.md && ! grep -q PR-HEAD-ONLY $CHOME/.codex/AGENTS.md" \
  || fail "instruction file is not team text then repository text from the base commit"
docker exec "$TASK_REPO" sh -c "test ! -e $CHOME/.codex/settings.json && test ! -e $CHOME/.claude/settings.json && test ! -e $CHOME/.claude/hooks" \
  || fail "a repository settings file or hook was copied"
docker exec "$TASK_REPO" sh -c "test -f $CHOME/.claude/skills/repo-skill/SKILL.md && test -f $CHOME/.claude/skills/team-skill/SKILL.md" \
  || fail "claude native skills dir does not hold both skills"
echo "   OK (mapper ran in the image as uid 1001; repo skill overrides team skill; base commit only)"

echo "== 5a. codex lists the team skill and the repo skill at session start (offline) =="
if docker exec "$TASK_REPO" sh -c 'command -v codex' >/dev/null 2>&1; then
  docker exec -e CODEX_HOME="$CHOME/.codex" "$TASK_REPO" sh -c 'cd /work && codex debug prompt-input hi' > "$WORKDIR/codex-prompt.json" \
    || fail "codex debug prompt-input failed in the image"
  grep -q 'team-skill' "$WORKDIR/codex-prompt.json" || fail "codex does not list the team skill"
  grep -q 'repo-skill' "$WORKDIR/codex-prompt.json" || fail "codex does not list the repo skill"
  grep -q 'REPO-RULE-MARKER' "$WORKDIR/codex-prompt.json" || fail "codex does not carry the repository rules"
  echo "   OK (codex sees both skills and the merged instructions)"
else
  echo "   SKIPPED (this image variant has no codex; the native dirs were checked in step 5)"
fi

echo "== 5b. claude lists them too (needs CLAUDE_CODE_OAUTH_TOKEN; skipped without) =="
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && docker exec "$TASK_REPO" sh -c 'command -v claude' >/dev/null 2>&1; then
  docker exec -e CLAUDE_CODE_OAUTH_TOKEN "$TASK_REPO" sh -c "cd /work && claude -p 'List the names of your available skills, one per line, nothing else.' --dangerously-skip-permissions" \
    > "$WORKDIR/claude-skills.txt" || fail "claude -p failed"
  grep -q 'team-skill' "$WORKDIR/claude-skills.txt" && grep -q 'repo-skill' "$WORKDIR/claude-skills.txt" \
    || fail "claude does not list both skills"
  echo "   OK (claude lists both skills)"
else
  echo "   SKIPPED (no CLAUDE_CODE_OAUTH_TOKEN or no claude in this image; the native skills dir was checked in step 5)"
fi

echo "== 5c. a bad commit fails closed with one short line =="
if docker exec "$TASK_REPO" franky-repo-setup map --repo acme/service --ref "$(printf 'b%.0s' $(seq 1 40))" \
  --engine codex --remote file:///tmp/smoke-src 2> "$WORKDIR/map-err.txt"; then
  fail "a bad commit did not fail"
fi
[ "$(wc -l < "$WORKDIR/map-err.txt")" -eq 1 ] || fail "failure was not one short line"
echo "   OK"

echo "SMOKE PASS: profile injection works into the real hardened container (stdin untar into HOME)."
