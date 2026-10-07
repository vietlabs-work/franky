# Threads

## Review threads

`review-pr --thread` keeps one review session per PR and role under `~/.franky/threads` (override: `FRANKY_THREADS_DIR`). The next run continues it:

- With `claude`, Franky resumes the same session (`--resume`). Its id is saved before the container starts, so a crash leaves a known id.
- Other engines, and a session that is stale, changed, or rejected, start a new session seeded with the stored findings. A thread never blocks the review.
- If a resumed session fails, Franky drops it and retries once in the same run with a seeded session. The result reports `session: seeded` with `session_reason: resume_failed`.
- A new session is stored only after a clean run with parsed findings. A failed run keeps the previous session and findings.
- The prompt tells the agent to verify each prior finding (`open` or `resolved`), review the delta since the last reviewed head, and report new findings on unchanged code only at blocking severity.
- A second run for the same PR while one is active exits 4 (`thread_busy`).
- Thread ids are lowercase, so `Me/Repo` and `me/repo` share one thread.
- Without `--thread`, Franky ignores any finding `status` the agent reports.
- Re-reviews should report each prior finding under its exact prior title; `--resolve-fixed` matches threads by it.
- `--rubric-version` pins a rubric label. Changing it, the engine, or the model starts a new session.

Sessions move by stream-in (tar over `docker exec`) and copy-out (a `docker cp ... -` tar stream, capped at 64 MiB) only. No volume or mount is added. Only the session file and its side directory move. Claude's project memory never moves between runs. Stored sessions contain regular files only, are scrubbed of known secret values and token patterns, and are refused on any finding. They stay host-only (0700/0600) and are never exported. The stored findings are redacted the same way. `threads prune` and `threads purge` delete them.

A resumed session replays earlier content from the same PR to the read-only reviewer. It never crosses PRs or roles. Resume stops after 14 days without a successful review, or above 64 MiB of session files.

## Author threads

`build --thread` and `iterate --thread` keep one author session per PR (role `author`). The reviewer and author roles never share a session.

- `build --thread` with `claude` saves a session id in the job record before the container starts. When the PR exists, the session becomes the PR's author thread (`thread.id`). The result reports `session_reason: new_thread`.
- Before the bind, the host confirms that the open PR on the build's branch is the PR the agent reported, and records the session sidecar and PR URL in the job record. If either step fails, the bind stays pending (`bind_pending`).
- The bind waits at most 60 seconds for a busy author thread. After that it stays pending; the job record keeps the session sidecar and the PR URL. It never overwrites a stored author session (`thread_exists`). A session that is not stored is deleted unless `job resume` can still use it (a workspace snapshot exists).
- `iterate --thread` resumes the author session with `claude`. Other engines, and a session that is stale, changed, or too large, start a new session seeded with the stored PR head. The prompt fences that context as untrusted data and says that its own conventions override the earlier conversation.
- If `iterate --thread` finds no author thread, or one whose bind never finished, it first binds the newest unbound session sidecar of a `build --thread` for that PR whose branch the host confirms. A sidecar that fails to extract or verify is skipped for the next one. This recovers a build that stopped between its record write and the end of its bind.
- An author run retries only when the engine refuses the stored session at startup (`resume_failed`, one seeded retry). Any other failure never re-runs the pass, because it can already have pushed.
- An author session resumes at most 10 times in a row. The next run starts a seeded session (`resume_cap`) and resets the count.
- A second author run for the same PR while one is active exits 4 (`thread_busy`).

A timed-out `build --thread` copies its session out with its workspace. `job kill` copies it out before it removes the container, then scrubs it with the loaded configuration's secrets, the profile's MCP credentials, and every secret key in the environment. If the configuration or the profile cannot load, `job kill` captures no session. Each session goes to a `<job_id>.session.tar.gz` sidecar beside the run record: regular files only, scrubbed, fail-closed verified, host-only (0600), and never exported. Run-record pruning deletes it with its record.

`job resume` of such a run restores `/work` and the session in one container, and resumes the session (V2). Before it streams the session in, it verifies it again, with the profile's MCP credentials too. It falls back to a workspace-only resume (V1) with `thread.session: fresh` plus the reason: `session_missing`, `session_corrupt`, `engine_changed`, `model_changed`, `no_native_resume`, or `verify_failed` (also when the profile cannot load). The fallback also prints one stderr line, except under `--json` or `--quiet`, where the JSON reason carries it. If the engine refuses the restored session at startup, Franky retries once as V1 in a new container with a new session id (`resume_failed`). A resumed run that opens the PR binds its session like `build --thread`.

`threads purge OWNER/REPO#N --role author` deletes only the author thread.
