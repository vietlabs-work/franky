"""`review-pr --skill`: the policy, payload validation, host posting and the prompt (hermetic)."""

import json
import os

import pytest
from click.testing import CliRunner

from franky import cli, config, prompt, schema, skillreview
from franky.skillreview import SkillError, SkillPolicy

SHA = "a" * 40
BASE = "b" * 40
PR_URL = "https://github.com/me/repo/pull/11"
NONCE = "feedfacecafe0003"
LOGIN = "acme-bot[bot]"
SCHEMA = "acme-ledger:v1"
MARKER = "acme-ledger:v1"


def policy(**over) -> SkillPolicy:
    raw = {
        "ledger_schema": SCHEMA,
        "ledger_summary_marker": MARKER,
        "max_body_chars": 6000,
        "max_comments": 3,
        "max_comment_chars": 400,
        "bot_login": LOGIN,
        "allow_ledger_update": True,
    }
    raw.update(over)
    return skillreview.parse_policy(raw)


def finding(i="f1", loc="app.py:3", status=None, sev="blocking"):
    out = {"id": i, "loc": loc, "sev": sev, "title": "t"}
    if status:
        out["status"] = status
    return out


def ledger(findings=None, state=None, **over):
    findings = [finding()] if findings is None else findings
    if state is None:
        state = "findings" if any(f.get("status", "open") == "open" for f in findings) else "clean"
    out = {"schema": SCHEMA, "round": 1, "reviewed_sha": SHA, "state": state, "findings": findings}
    out.update(over)
    return out


def block(led, marker=MARKER):
    return f"<details><summary>{marker} - round 1</summary>\n\n```json\n{json.dumps(led)}\n```\n</details>"


def review_payload(led=None, body=None, comments=(), **over):
    led = ledger() if led is None else led
    out = {
        "kind": "review",
        "decision": "first-pass",
        "ledger": led,
        "review": {
            "body": body if body is not None else "Lead.\n\n" + block(led),
            "comments": list(comments),
        },
        "resolve": [],
        "replies": [],
        "ledger_update": None,
    }
    out.update(over)
    return out


def build(raw, pol=None, secrets=(), footer="<sub>x</sub>"):
    return skillreview.build_payload(raw, pol or policy(), SHA, secrets, footer)


def code_of(raw, **kw):
    with pytest.raises(SkillError) as exc:
        build(raw, **kw)
    return exc.value.code


# --- policy -------------------------------------------------------------------------------


def test_policy_is_strict():
    base = {
        "ledger_schema": "s",
        "ledger_summary_marker": "m",
        "max_body_chars": 2000,
        "max_comments": 2,
        "max_comment_chars": 200,
        "bot_login": "bot-x[bot]",
    }
    pol = skillreview.parse_policy(base)
    assert (pol.allow_ledger_update, pol.blocking_field, pol.blocking_values) == (
        False,
        "sev",
        ("blocking",),
    )
    assert pol.cleared_statuses == ("fixed", "resolved", "wontfix")
    assert pol.open_statuses == ("open",)
    for bad in (
        {**base, "extra": 1},
        {k: v for k, v in base.items() if k != "bot_login"},
        {**base, "max_comments": True},
        {**base, "max_comments": 51},
        {**base, "max_body_chars": 70000},
        {**base, "bot_login": "not a login"},
        {**base, "bot_login": "bot-x"},
        {**base, "cleared_statuses": []},
        {**base, "open_statuses": ["fixed"]},
        {**base, "allow_ledger_update": "yes"},
        {**base, "blocking_values": []},
        {**base, "ledger_schema": " padded "},
    ):
        with pytest.raises(ValueError):
            skillreview.parse_policy(bad)
    with pytest.raises(ValueError):
        skillreview.parse_policy_text("[1]")
    with pytest.raises(ValueError):
        skillreview.parse_policy_text("{nope")


# --- ledger block and payload --------------------------------------------------------------


def test_ledger_block_needs_exactly_one_marked_block():
    led = ledger()
    assert skillreview.ledger_block("intro\n" + block(led), MARKER) == led
    other = "<details><summary>other</summary>\n\n```json\n{}\n```\n</details>"
    assert skillreview.ledger_block(other + block(led), MARKER) == led
    for body in (
        "no block",
        block(led) + block(led),
        block(led, marker="x"),
        f"<details><summary>{MARKER}</summary>\n\nno fence\n</details>",
        f"<details><summary>{MARKER}</summary>\n\n```json\n{{broken\n```\n</details>",
        f"<details><summary>{MARKER}</summary>\n\n```json\n[1]\n```\n</details>",
    ):
        with pytest.raises(SkillError) as exc:
            skillreview.ledger_block(body, MARKER)
        assert exc.value.code == "skill_body_block"


def test_review_payload_is_built_with_neutralized_redacted_capped_text():
    comment = {"path": "app.py", "line": 3, "side": "RIGHT", "body": "bad <!-- x --> sk-secret"}
    led = ledger()
    body = "Lead <!-- bot-review:1 --> sk-secret\n\n" + block(led)
    payload = build(review_payload(led, body, [comment]), secrets=["sk-secret"])
    assert payload.kind == "review" and payload.ledger == led
    assert "<!--" not in payload.body and "&lt;!-- bot-review:1 -->" in payload.body
    assert "sk-secret" not in payload.body
    assert payload.body.endswith("\n\n<sub>x</sub>")
    assert "<!--" not in payload.comments[0]["body"]
    assert "sk-secret" not in payload.comments[0]["body"]
    assert payload.comments[0]["body"].startswith("bad &lt;!-- x -->")


def test_ledger_must_match_schema_sha_state_and_findings():
    assert code_of(review_payload(ledger(schema="other"))) == "skill_ledger_schema"
    assert code_of(review_payload(ledger(reviewed_sha="b" * 40))) == "skill_ledger_sha"
    assert code_of(review_payload(ledger(state="weird"))) == "skill_ledger_state"
    assert code_of(review_payload(ledger([finding()], state="clean"))) == "skill_ledger_state"
    assert code_of(review_payload(ledger([], state="findings"))) == "skill_ledger_state"
    assert code_of(review_payload(ledger([finding(status="fixed")], state="findings"))) == (
        "skill_ledger_state"
    )
    assert code_of(review_payload(ledger(round=0))) == "skill_ledger_round"
    assert code_of(review_payload({**ledger(), "findings": "x"})) == "skill_ledger_findings"
    # clean with only cleared findings is fine; so is a missing round
    ok = ledger([finding(status="fixed")])
    del ok["round"]
    assert build(review_payload(ok)).ledger["state"] == "clean"


def test_body_must_carry_the_same_single_ledger_block():
    led = ledger()
    assert code_of(review_payload(led, "no block")) == "skill_body_block"
    assert code_of(review_payload(led, block(ledger([finding("other")])))) == "skill_body_block"
    assert code_of(review_payload(led, block(led) + block(led))) == "skill_body_block"


def test_caps_are_checked_on_the_final_text():
    led = ledger()
    just = block(led)
    room = 6000 - len(just) - len("\n\n<sub>x</sub>")
    body = "x" * (room + 1) + just  # fits only when the footer is not counted
    assert code_of(review_payload(led, body)) == "skill_body_cap"
    assert build(review_payload(led, body), footer="").body == body
    many = [{"path": "a", "line": 1, "body": "b"}] * 4
    assert code_of(review_payload(led, comments=many)) == "skill_comment_cap"
    long = [{"path": "a", "line": 1, "body": "y" * 401}]
    assert code_of(review_payload(led, comments=long)) == "skill_comment_cap"
    # redaction can only shorten, neutralization lengthens: the cap sees the final text
    tight = policy(max_comment_chars=100)
    edge = [{"path": "a", "line": 1, "body": "<!--" * 25}]
    assert code_of(review_payload(led, comments=edge), pol=tight) == "skill_comment_cap"


def test_comment_shape_is_validated():
    led = ledger()
    for bad in (
        {"path": "", "line": 1, "body": "b"},
        {"path": "a", "line": 0, "body": "b"},
        {"path": "a", "line": True, "body": "b"},
        {"path": "a", "line": 1, "side": "UP", "body": "b"},
        {"path": "a", "line": 1, "body": "  "},
        "string",
    ):
        assert code_of(review_payload(led, comments=[bad])) == "skill_payload_shape"


def test_terminal_payload_posts_nothing_and_is_bounded():
    raw = {
        "kind": "terminal",
        "decision": "done",
        "reason": "r " * 400,
        "prior_ledger_review_id": 7,
    }
    payload = build(raw, secrets=["r"])
    assert payload.kind == "terminal" and payload.prior_review_id == 7
    assert len(payload.reason) <= skillreview.MAX_REASON_CHARS
    for bad in (
        {**raw, "decision": "first-pass"},
        {**raw, "prior_ledger_review_id": 0},
        {**raw, "reason": 5},
        {"kind": "review", "decision": "done"},
        {"kind": "other"},
    ):
        assert code_of(bad) in {"skill_payload_shape", "skill_ledger_schema"}
    assert code_of(None) == "skill_payload_missing"


def test_ledger_update_rules():
    led = ledger([finding(status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    quiet = review_payload(led, decision="delta", review=None, ledger_update=upd)
    payload = build(quiet)
    assert payload.body is None and payload.update["review_id"] == 9
    assert payload.update["body"].endswith("<sub>x</sub>")
    assert code_of(quiet, pol=policy(allow_ledger_update=False)) == "skill_update_refused"
    both = review_payload(led, ledger_update=upd)
    assert code_of(both) == "skill_update_with_review"
    wrong = {**upd, "body": "Quiet.\n\n" + block(ledger([finding("z", status="fixed")]))}
    assert code_of({**quiet, "ledger_update": wrong}) == "skill_body_block"
    assert code_of({**quiet, "ledger_update": {"review_id": 0, "body": upd["body"]}}) == (
        "skill_payload_shape"
    )
    # a round with nothing at all is allowed and posts nothing
    empty = build(review_payload(led, decision="delta", review=None))
    assert empty.body is None and empty.update is None


def test_summary_counts_open_and_blocking_by_policy_field():
    led = ledger(
        [
            finding("a", sev="blocking"),
            finding("b", sev="non-blocking"),
            finding("c", sev="blocking", status="fixed"),
        ]
    )
    payload = build(review_payload(led))
    assert skillreview.ledger_summary(payload, policy(), SHA, "u") == {
        "kind": "review",
        "decision": "first-pass",
        "state": "findings",
        "round": 1,
        "reviewed_sha": SHA,
        "open": 2,
        "blocking": 1,
        "review_url": "u",
    }
    custom = policy(blocking_field="tier", blocking_values=["p1", "p2"])
    led2 = ledger(
        [{"id": "a", "loc": "x:1", "tier": "p2"}, {"id": "b", "loc": "x:2", "tier": "p3"}]
    )
    p2 = build(review_payload(led2), pol=custom)
    assert skillreview.ledger_summary(p2, custom, SHA, None)["blocking"] == 1
    term = build({"kind": "terminal", "decision": "skip", "reason": "draft"})
    assert skillreview.ledger_summary(term, policy(), SHA, None) == {
        "kind": "terminal",
        "decision": "skip",
        "state": None,
        "round": None,
        "reviewed_sha": SHA,
        "open": None,
        "blocking": None,
        "review_url": None,
        "reason": "draft",
    }


# --- prior ledger, threads --------------------------------------------------------------------


def review_row(
    rid, led, login=LOGIN, at="2026-10-01T00:00:00Z", body=None, typ="Bot", state="COMMENTED"
):
    return {
        "id": rid,
        "state": state,
        "user": {"login": login, "type": typ},
        "submitted_at": at,
        "body": body if body is not None else block(led),
    }


def test_newest_own_ledger_review_wins_and_others_are_ignored():
    old, new = ledger([finding("a")]), ledger([finding("b")])
    rows = [
        review_row(2, new, at="2026-10-02T00:00:00Z"),
        review_row(1, old, at="2026-10-01T00:00:00Z"),
        review_row(3, ledger(), login="someone"),
        review_row(6, ledger(), login="acme-bot"),  # no [bot]: not the bot
        review_row(7, ledger(), typ="User"),
        review_row(4, ledger(), body="no block"),
        review_row(5, ledger(schema="other"), at="2026-10-03T00:00:00Z"),
        "junk",
    ]
    got = skillreview.bot_ledger_reviews(rows, policy())
    assert [r["id"] for r, _ in got] == [1, 2]
    assert got[-1][1] == new
    assert skillreview.bot_ledger_reviews([review_row(1, old, login="ACME-BOT[bot]")], policy())


def test_cleared_locs_need_exactly_one_open_prior_finding_that_is_now_cleared():
    pol = policy()
    prior = ledger(
        [
            finding("a", "x.py:1"),
            finding("b", "x.py:9"),
            finding("c", "y.py:2"),
            finding("d", "z.py:1", "fixed"),
            finding("e", "w.py:5"),
            finding("f", "w.py:5"),
        ]
    )
    new = ledger(
        [
            finding("a", "x.py:1", "fixed"),
            finding("b", "x.py:9"),
            finding("c", "y.py:2", "wontfix"),
            finding("e", "w.py:5", "fixed"),
            finding("f", "w.py:5"),
        ]
    )
    assert skillreview.cleared_locs(prior, new, pol) == {("x.py", 1), ("y.py", 2)}
    assert skillreview.cleared_locs(None, new, pol) == set()
    dropped = ledger([finding("a", "x.py:1", "fixed")])
    assert skillreview.cleared_locs(prior, dropped, pol) == {("x.py", 1)}


def test_finding_identity_and_statuses_are_strict():
    bad = [
        [finding("a"), finding("a", "b.py:1")],
        [finding("")],
        [{"loc": "a.py:1"}],
        [finding("a", "a.py")],
        [finding("a", "a.py:0")],
        [finding("a", "a b.py:3")],
        [finding("a", "a<b>.py:3")],
        [finding("a", status="dismissed")],
        [finding("a", status="Fixed")],
    ]
    for f in bad:
        assert code_of(review_payload({**ledger(), "findings": f, "state": "findings"})) == (
            "skill_ledger_findings"
        )
    custom = policy(cleared_statuses=["dismissed"], open_statuses=["open", "todo"])
    led = ledger([finding("a", status="todo"), finding("b", status="dismissed")], state="findings")
    assert build(review_payload(led), pol=custom).ledger["state"] == "findings"


def node(
    tid="T1",
    path="y.py",
    login="acme-bot",
    typename="Bot",
    resolved=False,
    cid=100,
    last=None,
    line=2,
):
    first = {"databaseId": cid, "author": {"login": login, "__typename": typename}, "body": "x"}
    return {
        "id": tid,
        "isResolved": resolved,
        "path": path,
        "line": line,
        "originalLine": None,
        "comments": {"nodes": [first]},
        "last": {"nodes": [last or first]},
    }


def test_own_thread_requires_an_open_thread_started_by_the_bot():
    pol = policy()
    assert skillreview.own_thread(node(), pol)
    assert skillreview.own_thread(node(login="Acme-Bot"), pol)
    assert not skillreview.own_thread(node(login="acme-bot[bot]"), pol)  # GraphQL has no suffix
    assert not skillreview.own_thread(node(login="someone"), pol)
    assert not skillreview.own_thread(node(typename="User"), pol)
    assert not skillreview.own_thread(node(resolved=True), pol)
    assert not skillreview.own_thread({"comments": "x"}, pol)
    assert not skillreview.own_thread(None, pol)


# --- prompt, config, schema, setups ----------------------------------------------------------------


def test_skill_prompt_names_the_skill_paths_login_and_payload_shapes():
    text = prompt.build_skill_review_prompt(
        "me/repo",
        PR_URL,
        "focus here",
        NONCE,
        skill="deep-review",
        where="/home/franky/.franky/team",
        policy=policy(),
        head_sha=SHA,
        operator_setup="\nTeam setup (injected):\n",
    )
    for needle in (
        "`/home/franky/.franky/team/skills/deep-review/SKILL.md`",
        "`SKILL_DIR` is `/home/franky/.franky/team/skills/deep-review`",
        "`CLAUDE_PLUGIN_ROOT` for plugin P is `/home/franky/.franky/team/tool/P`",
        "bash <file>",
        "python3 <file>",
        "GitHub writes fail in this container",
        f"Your GitHub login is `{LOGIN}`",
        f"git checkout --detach {SHA}",
        f"FRANKY_REVIEW_{NONCE}_BEGIN",
        '"kind": "review"',
        '"kind": "terminal"',
        SCHEMA,
        "body 6000 characters, 3 comments, 400 characters each",
        "focus here",
        "Team setup (injected)",
    ):
        assert needle in text, needle
    assert "FRANKY_PRIOR" not in text
    assert "`ledger_update` replaces the body" in text
    off = prompt.build_skill_review_prompt(
        "me/repo", PR_URL, "", NONCE, skill="s", where="/w", policy=policy(allow_ledger_update=False),
        head_sha=SHA,
    )  # fmt: skip
    assert "`ledger_update` must be null" in off


def test_login_is_not_a_secret():
    cfg = config.Config(
        engine=None, allowed_repos=[], passthrough_env={"GH_LOGIN": LOGIN, "GH_TOKEN": "tok"}
    )
    assert "tok" in cfg.secret_values() and LOGIN not in cfg.secret_values()


def test_schema_advertises_the_skill_flags_and_the_ledger_result():
    flags = (
        {f["name"]: f for f in schema.build_schema(cli.main)["commands"]["review-pr"]["flags"]}
        if hasattr(schema, "build_schema")
        else {}
    )
    opts = {o for f in flags.values() for o in f["opts"]} if flags else set()
    if not flags:  # fall back to the click command itself
        opts = {o for p in cli.review_pr.params for o in p.opts}
    assert {"--skill", "--skill-policy"} <= opts
    assert "ledger" in schema._RESULT_SCHEMA and "skill_notes" in schema._RESULT_SCHEMA


# --- the command ---------------------------------------------------------------------------------


class Gh(list):
    def __init__(self):
        super().__init__()
        self.inputs = []


def write_policy(tmp_path, **over):
    raw = {
        "ledger_schema": SCHEMA,
        "ledger_summary_marker": MARKER,
        "max_body_chars": 6000,
        "max_comments": 3,
        "max_comment_chars": 400,
        "bot_login": LOGIN,
        "allow_ledger_update": True,
    }
    raw.update(over)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    return path


def setup_run(
    monkeypatch,
    tmp_path,
    payload,
    *,
    reviews=None,
    threads=None,
    skills=("deep-review",),
    heads=None,
    post=None,
    put=None,
    log=None,
    files=(),
    globs=(),
    rules=(),
    contents=None,
    review_reads=None,
    bases=None,
):
    """A hermetic skill review: env, images, team profile, container output and `gh` all faked.

    `files`, `globs` and `rules` feed the human sign-off floor (PR files, `human-signoff-paths`
    globs, `human-signoff-content` (glob, string) rules); `contents` maps (path, "base"|"head")
    to bytes for the content reads (a missing key is a 404, None is a failed read). `bases` lists
    the live base shas the next head checks report (then BASE).
    `review_reads` is a list of (code, out, err) results the next PR-review reads return first."""
    env = {
        "FRANKY_ALLOWED_REPOS": "me/repo",
        "GH_TOKEN": "ghp_fake",
        "OPENROUTER_API_KEY": "sk-or-fake",
        "FRANKY_RUNS_DIR": str(tmp_path / "runs"),
        "FRANKY_LOGS_DIR": str(tmp_path / "logs"),
    }
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "ensure_image_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "resolve_image", lambda *a, **k: "franky")
    monkeypatch.setattr(cli.secrets, "token_hex", lambda *a, **k: NONCE)
    shas = list(heads or [])

    def fake_fetch(repo, number, e, **k):
        if k.get("meta_sink") is not None:
            k["meta_sink"].update(base_sha=bases.pop(0) if bases else BASE)
        return shas.pop(0) if shas else SHA

    reads = {}

    def fake_file(repo, path, ref, env, **k):
        reads[(path, ref)] = k.get("max_bytes")
        side = "base" if ref == BASE else "head"
        # a missing key is a 404, which the caller's `absent` turns into b"" or None
        return (contents or {}).get((path, side), k.get("absent", b""))

    monkeypatch.setattr(
        cli, "fetch_signoff_globs", lambda *a, **k: None if globs is None else list(globs)
    )
    monkeypatch.setattr(
        cli, "fetch_signoff_content_rules", lambda *a, **k: None if rules is None else list(rules)
    )
    monkeypatch.setattr(cli, "fetch_repo_file", fake_file)

    monkeypatch.setattr(cli, "fetch_pr_head_sha", fake_fetch)

    def fake_bundle(profile_path, cred_env, secrets, cfg, repo_setup_ref=None, team_sink=None):
        if team_sink is not None:
            team_sink.update(where="/home/franky/.franky/team", skills=set(skills))
        return None, "", ""

    monkeypatch.setattr(cli, "_load_profile_bundle", fake_bundle)
    seen = {}

    def fake_container(*a, **k):
        seen["cfg"] = a[0] if a else k.get("cfg")
        seen["prompt"] = a[1] if len(a) > 1 else k.get("prompt")
        out = (
            ""
            if payload is None
            else f"FRANKY_REVIEW_{NONCE}_BEGIN{json.dumps(payload)}FRANKY_REVIEW_{NONCE}_END"
        )
        return 0, out

    monkeypatch.setattr(cli, "run_in_container", fake_container)
    calls = Gh()

    def fake_gh(args, e, **k):
        path = args[1]
        if path.endswith("/reviews?per_page=100"):
            if review_reads:
                return review_reads.pop(0)
            return 0, json.dumps([reviews or []]), ""
        if "/files?" in path:
            return (1, "", "boom") if files is None else (0, json.dumps([list(files)]), "")
        if path == "graphql" and "reviewThreads" in " ".join(args):
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": threads or [],
                            }
                        }
                    }
                }
            }
            return 0, json.dumps(data), ""
        calls.append(list(args))
        calls.inputs.append(k.get("input"))
        if "PUT" in args:
            return put or (0, json.dumps({"html_url": f"{PR_URL}#pullrequestreview-9"}), "")
        if args[3].endswith("/reviews") and "POST" in args:
            return (
                post(len(calls))
                if callable(post)
                else (
                    post
                    or (
                        0,
                        json.dumps(
                            {
                                "html_url": f"{PR_URL}#pullrequestreview-555",
                                "id": 555,
                                "user": {"login": LOGIN, "type": "Bot"},
                            }
                        ),
                        "",
                    )
                )
            )
        return 0, "{}", ""

    monkeypatch.setattr(cli, "run_gh", fake_gh)
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    calls.reads = reads
    return calls, seen


def run(tmp_path, *extra, policy_over=None):
    pol = write_policy(tmp_path, **(policy_over or {}))
    argv = [
        "review-pr",
        PR_URL,
        "--skill",
        "deep-review",
        "--skill-policy",
        str(pol),
        "--json",
        *extra,
    ]
    with CliRunner().isolated_filesystem():
        res = CliRunner().invoke(cli.main, argv)
    return res, json.loads(res.stdout) if res.stdout.strip() else None


def inline(path="app.py", line=3, body="fix this"):
    return {"path": path, "line": line, "side": "RIGHT", "body": body}


def test_a_review_round_posts_one_comment_review_and_reports_the_ledger(monkeypatch, tmp_path):
    led = ledger([finding("a"), finding("b", sev="non-blocking")])
    payload = review_payload(led, comments=[inline()])
    calls, seen = setup_run(monkeypatch, tmp_path, payload)
    res, data = run(tmp_path)
    assert res.exit_code == 0, res.output
    assert calls[0] == ["api", "-X", "POST", "repos/me/repo/pulls/11/reviews", "--input", "-"]
    posted = json.loads(calls.inputs[0])
    assert posted["event"] == "COMMENT" and posted["commit_id"] == SHA
    assert posted["comments"] == [inline()]
    assert posted["body"].endswith("<sub>Automated review by Franky</sub>")
    assert data["status"] == "review_published" and data["review_event"] == "COMMENT"
    assert data["review_id"] == 555 and data["review_url"].endswith("#pullrequestreview-555")
    assert data["ledger"] == {
        "kind": "review",
        "decision": "first-pass",
        "state": "findings",
        "round": 1,
        "reviewed_sha": SHA,
        "open": 2,
        "blocking": 1,
        "review_url": data["review_url"],
    }
    assert seen["cfg"].passthrough_env["GH_LOGIN"] == LOGIN
    text = " ".join(seen["prompt"])
    assert "SKILL MODE" in text and "deep-review" in text and "FRANKY_PRIOR" not in text


def test_a_terminal_round_posts_nothing_and_succeeds(monkeypatch, tmp_path):
    payload = {
        "kind": "terminal",
        "decision": "done",
        "reason": "cap reached",
        "prior_ledger_review_id": 7,
    }
    calls, _ = setup_run(monkeypatch, tmp_path, payload, reviews=[review_row(7, ledger())])
    res, data = run(tmp_path)
    assert res.exit_code == 0 and not calls
    assert data["status"] == "review_complete" and "review_url" not in data
    assert data["ledger"]["kind"] == "terminal" and data["ledger"]["reason"] == "cap reached"
    assert data["ledger"]["review_url"] == f"{PR_URL}#pullrequestreview-7"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"kind": "review"},
        review_payload(ledger(schema="x")),
        review_payload(ledger(), "no block"),
    ],
)
def test_an_invalid_payload_posts_nothing_and_reports_a_code(monkeypatch, tmp_path, payload):
    calls, _ = setup_run(monkeypatch, tmp_path, payload)
    res, data = run(tmp_path)
    assert res.exit_code == 7 and not calls
    assert data["status"] == "skill_payload_invalid"
    assert "skill_" in data["reason"] and "ledger" not in data


def test_an_unreadable_payload_reports_invalid_not_a_crash(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    calls, _ = setup_run(monkeypatch, tmp_path, review_payload(ledger()))
    monkeypatch.setattr(skillreview, "build_payload", boom)
    res, data = run(tmp_path)
    assert res.exit_code == 7 and not calls
    assert data["status"] == "skill_payload_invalid" and "unreadable" in data["reason"]


def test_no_publish_returns_the_final_body_with_comments_folded(monkeypatch, tmp_path):
    led = ledger()
    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(led, comments=[inline(body="look <!-- x -->")])
    )
    res, data = run(tmp_path, "--no-publish")
    assert res.exit_code == 0 and not calls
    assert data["status"] == "review_complete" and data["ledger"]["open"] == 1
    body = data["review_body"]
    assert "`app.py:3`: look &lt;!-- x -->" in body and body.endswith("</sub>")
    assert skillreview.ledger_block(body, MARKER) == led


def test_no_publish_terminal_has_no_review_body(monkeypatch, tmp_path):
    payload = {
        "kind": "terminal",
        "decision": "skip",
        "reason": "draft",
        "prior_ledger_review_id": None,
    }
    setup_run(monkeypatch, tmp_path, payload)
    res, data = run(tmp_path, "--no-publish")
    assert res.exit_code == 0 and "review_body" not in data and data["ledger"]["kind"] == "terminal"


def test_no_publish_refuses_a_body_that_no_longer_fits(monkeypatch, tmp_path):
    big = [inline(body="z" * 390)] * 3
    setup_run(monkeypatch, tmp_path, review_payload(ledger(), "x" * 4700 + block(ledger()), big))
    res, data = run(tmp_path, "--no-publish", policy_over={"max_body_chars": 5000})
    assert res.exit_code == 7 and data["status"] == "skill_payload_invalid"


def test_a_moved_head_blocks_the_post(monkeypatch, tmp_path):
    calls, _ = setup_run(monkeypatch, tmp_path, review_payload(ledger()), heads=[SHA, "c" * 40])
    res, data = run(tmp_path)
    assert res.exit_code == 7 and not calls
    assert data["status"] == "publish_blocked_stale_head"


def test_rejected_anchors_fold_the_comments_into_one_more_post(monkeypatch, tmp_path):
    attempts = []

    def post(n):
        attempts.append(n)
        if len(attempts) == 1:
            return 1, "", "gh: Unprocessable Entity (HTTP 422)"
        resp = {
            "html_url": f"{PR_URL}#pullrequestreview-8",
            "id": 8,
            "user": {"login": LOGIN, "type": "Bot"},
        }
        return 0, json.dumps(resp), ""

    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(ledger(), comments=[inline(body="look")]), post=post
    )
    res, data = run(tmp_path)
    assert res.exit_code == 0, res.output
    first, second = (json.loads(i) for i in calls.inputs)
    assert first["comments"] and second["comments"] == []
    assert "`app.py:3`: look" in second["body"] and second["body"].endswith("</sub>")
    assert ledger_ok(second["body"])
    assert data["skill_notes"] == ["anchors_folded:1"]


def ledger_ok(body):
    return skillreview.ledger_block(body, MARKER)["schema"] == SCHEMA


def test_a_failed_post_is_publish_failed(monkeypatch, tmp_path):
    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(ledger()), post=(1, "", "boom ghp_fake")
    )
    res, data = run(tmp_path)
    assert res.exit_code == 8 and data["status"] == "publish_failed"
    assert "ghp_fake" not in res.stdout


def test_resolve_needs_the_flag_an_own_thread_and_a_cleared_prior_finding(monkeypatch, tmp_path):
    prior = ledger([finding("a", "y.py:2"), finding("b", "x.py:1")])
    new = ledger([finding("a", "y.py:2", "fixed"), finding("b", "x.py:1")])
    payload = review_payload(new, resolve=["T-y", "T-x", "T-other", "T-done", "T-missing"])
    threads = [
        node("T-y", "y.py"),
        node("T-x", "x.py"),
        node("T-other", "y.py", login="human", typename="User"),
        node("T-done", "y.py", resolved=True),
    ]
    reviews = [review_row(1, prior)]
    calls, _ = setup_run(monkeypatch, tmp_path, payload, reviews=reviews, threads=threads)
    res, data = run(tmp_path, "--thread", "--resolve-fixed")
    assert res.exit_code == 0, res.output
    resolves = [c for c in calls if c[:2] == ["api", "graphql"]]
    assert len(resolves) == 1 and "id=T-y" in resolves[0]
    assert resolves[0][2] == "-f" and resolves[0][3] == f"query={cli.RESOLVE_MUTATION}"
    assert data["threads_resolved"] == 1
    assert data["skill_notes"] == ["resolve_not_cleared:1", "resolve_not_ours:3"]

    calls, _ = setup_run(monkeypatch, tmp_path, payload, reviews=reviews, threads=threads)
    res, data = run(tmp_path, "--thread")  # flag off: the list is ignored
    assert not [c for c in calls if c[:2] == ["api", "graphql"]]
    assert "threads_resolved" not in data


def test_resolve_without_a_prior_ledger_resolves_nothing(monkeypatch, tmp_path):
    new = ledger([finding("a", "y.py:2", "fixed")])
    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(new, resolve=["T-y"]), threads=[node("T-y", "y.py")]
    )
    res, data = run(tmp_path, "--thread", "--resolve-fixed")
    assert res.exit_code == 0 and data["threads_resolved"] == 0
    assert data["skill_notes"] == ["resolve_not_cleared:1"]


def test_replies_follow_the_existing_eligibility_rules(monkeypatch, tmp_path):
    human = {"databaseId": 101, "author": {"login": "dev", "__typename": "User"}}
    payload = review_payload(
        ledger(),
        replies=[
            {"comment_id": 100, "body": "thanks <!-- x -->"},
            {"comment_id": 200, "body": "not due"},
            {"comment_id": 300, "body": "other"},
        ],
    )
    threads = [
        node("T1", cid=100, last=human),  # a human posted last: due
        node("T2", cid=200),  # we are last, nobody replied: due only for 200? see below
        node(
            "T3",
            cid=300,
            last={"databaseId": 301, "author": {"login": "acme-bot", "__typename": "Bot"}},
        ),
    ]
    calls, _ = setup_run(monkeypatch, tmp_path, payload, threads=threads)
    res, data = run(tmp_path, "--thread", "--reply-prior")
    assert res.exit_code == 0, res.output
    posted = [c for c in calls if "replies" in c[3]]
    # 100 (human last) and 200 (first comment is also last, no reply yet) are due; 300 is not
    assert {c[3] for c in posted} == {
        "repos/me/repo/pulls/11/comments/100/replies",
        "repos/me/repo/pulls/11/comments/200/replies",
    }
    assert any(a == "body=thanks &lt;!-- x -->" for c in posted for a in c)
    assert data["threads_replied"] == 2 and data["skill_notes"] == ["reply_not_due:1"]


def test_a_quiet_round_updates_one_own_ledger_review(monkeypatch, tmp_path):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    calls, _ = setup_run(monkeypatch, tmp_path, payload, reviews=[review_row(9, ledger())])
    res, data = run(tmp_path)
    assert res.exit_code == 0, res.output
    assert calls == [["api", "-X", "PUT", "repos/me/repo/pulls/11/reviews/9", "--input", "-"]]
    body = json.loads(calls.inputs[0])
    assert list(body) == ["body"] and ledger_ok(body["body"])
    assert data["status"] == "review_published" and data["review_id"] == 9
    assert data["review_event"] == "COMMENT"
    assert data["review_url"] == f"{PR_URL}#pullrequestreview-9"
    assert data["ledger"]["state"] == "clean" and data["ledger"]["open"] == 0


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [review_row(9, ledger(), login="someone")],
        [review_row(9, ledger(), body="no ledger")],
        [review_row(8, ledger())],
    ],
)
def test_a_ledger_update_only_edits_the_bots_own_ledger_review(monkeypatch, tmp_path, reviews):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    calls, _ = setup_run(monkeypatch, tmp_path, payload, reviews=reviews)
    res, data = run(tmp_path)
    assert res.exit_code == 7 and not calls
    assert data["status"] == "skill_payload_invalid" and "skill_update_target" in data["reason"]


def test_a_ledger_update_needs_the_policy_to_allow_it(monkeypatch, tmp_path):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(led, decision="delta", review=None, ledger_update=upd),
        reviews=[review_row(9, ledger())],
    )  # fmt: skip
    res, data = run(tmp_path, policy_over={"allow_ledger_update": False})
    assert res.exit_code == 7 and not calls and "skill_update_refused" in data["reason"]


def test_flag_combinations_are_refused_with_exit_2(monkeypatch, tmp_path):
    setup_run(monkeypatch, tmp_path, review_payload(ledger()))
    pol = str(write_policy(tmp_path))
    cases = [
        ["--skill", "deep-review", "--skill-policy", pol, "--allow-approve", "--no-publish"],
        ["--skill", "deep-review"],
        ["--skill-policy", pol],
        ["--skill", "bad name", "--skill-policy", pol],
        [
            "--skill",
            "deep-review",
            "--skill-policy",
            pol,
            "--at-sha",
            SHA,
            "--diff-base",
            SHA,
            "--no-publish",
        ],
    ]
    for extra in cases:
        with CliRunner().isolated_filesystem():
            res = CliRunner().invoke(cli.main, ["review-pr", PR_URL, "--json", *extra])
        assert res.exit_code == 2, (extra, res.output)


def test_a_bad_policy_file_is_refused_without_echoing_values(monkeypatch, tmp_path):
    setup_run(monkeypatch, tmp_path, review_payload(ledger()))
    pol = write_policy(tmp_path, max_comments="secret-value")
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", PR_URL, "--skill", "deep-review", "--skill-policy", str(pol), "--json"],
    )
    assert res.exit_code == 2 and "secret-value" not in res.output
    loose = write_policy(tmp_path)
    loose.chmod(0o666)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", PR_URL, "--skill", "deep-review", "--skill-policy", str(loose), "--json"],
    )
    assert res.exit_code == 2
    link = tmp_path / "link.json"
    os.symlink(write_policy(tmp_path), link)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", PR_URL, "--skill", "deep-review", "--skill-policy", str(link), "--json"],
    )
    assert res.exit_code == 2


def test_a_skill_missing_from_the_team_setup_is_a_config_error(monkeypatch, tmp_path):
    calls, _ = setup_run(monkeypatch, tmp_path, review_payload(ledger()), skills=("other",))
    res, _data = run(tmp_path)
    assert res.exit_code == 3 and not calls


# --- identity, ordering, anchors, paths (fix round) ---------------------------------------------


def resolve_case(monkeypatch, tmp_path, *, post=None, review=True, thread_line=2, heads=None):
    prior = ledger([finding("a", "y.py:2")])
    new = ledger([finding("a", "y.py:2", "fixed")])
    payload = review_payload(new, resolve=["T-y"])
    if not review:
        payload["review"] = None
    calls, _ = setup_run(
        monkeypatch,
        tmp_path,
        payload,
        reviews=[review_row(1, prior)],
        threads=[node("T-y", "y.py", line=thread_line)],
        post=post,
        heads=heads,
    )
    res, data = run(tmp_path, "--thread", "--resolve-fixed")
    return calls, res, data


def test_resolve_needs_the_thread_line_to_match_the_cleared_finding(monkeypatch, tmp_path):
    calls, res, data = resolve_case(monkeypatch, tmp_path, thread_line=7)
    assert data["threads_resolved"] == 0 and data["skill_notes"] == ["resolve_not_cleared:1"]
    calls, res, data = resolve_case(monkeypatch, tmp_path, thread_line=2)
    assert data["threads_resolved"] == 1


def test_a_poster_that_is_not_the_bot_blocks_resolve(monkeypatch, tmp_path):
    other = (
        0,
        json.dumps({"html_url": "u", "id": 5, "user": {"login": "x[bot]", "type": "Bot"}}),
        "",
    )
    calls, res, data = resolve_case(monkeypatch, tmp_path, post=other)
    assert data["threads_resolved"] == 0 and data["skill_notes"] == ["identity_mismatch:1"]
    assert not [c for c in calls if c[:2] == ["api", "graphql"]]
    human = (
        0,
        json.dumps({"html_url": "u", "id": 5, "user": {"login": LOGIN, "type": "User"}}),
        "",
    )
    _c, _r, data = resolve_case(monkeypatch, tmp_path, post=human)
    assert data["skill_notes"] == ["identity_mismatch:1"]


def test_resolve_needs_a_write_in_the_same_run(monkeypatch, tmp_path):
    calls, res, data = resolve_case(monkeypatch, tmp_path, review=False)
    assert res.exit_code == 0 and not calls
    assert data["threads_resolved"] == 0 and data["skill_notes"] == ["resolve_no_ledger:1"]


def test_the_head_is_checked_right_before_each_review_write(monkeypatch, tmp_path):
    # pin, publish-entry check, then the check before the POST sees a moved head
    calls, _ = setup_run(
        monkeypatch, tmp_path, review_payload(ledger()), heads=[SHA, SHA, "c" * 40]
    )
    res, data = run(tmp_path)
    assert data["status"] == "publish_blocked_stale_head" and not calls
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Q.\n\n" + block(led)}
    calls, _ = setup_run(
        monkeypatch,
        tmp_path,
        review_payload(led, decision="delta", review=None, ledger_update=upd),
        reviews=[review_row(9, ledger())],
        heads=[SHA, SHA, "c" * 40],
    )
    res, data = run(tmp_path)
    assert data["status"] == "publish_blocked_stale_head" and not calls


@pytest.mark.parametrize(
    "row",
    [
        review_row(9, ledger(), state="DISMISSED"),
        review_row(9, ledger(), body=block(ledger()) + "\n<!-- bot-review:abc -->"),
    ],
)
def test_a_ledger_update_never_edits_an_approval_or_a_marked_review(monkeypatch, tmp_path, row):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Q.\n\n" + block(led)}
    calls, _ = setup_run(
        monkeypatch,
        tmp_path,
        review_payload(led, decision="delta", review=None, ledger_update=upd),
        reviews=[row],
    )
    res, data = run(tmp_path)
    assert res.exit_code == 7 and not calls and "skill_update_target" in data["reason"]


def test_a_terminal_link_must_name_one_of_the_bots_ledger_reviews(monkeypatch, tmp_path):
    payload = {"kind": "terminal", "decision": "done", "reason": "r", "prior_ledger_review_id": 7}
    setup_run(monkeypatch, tmp_path, payload, reviews=[review_row(8, ledger())])
    res, data = run(tmp_path)
    assert data["ledger"]["review_url"] is None


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "a/../b", "..", "a b.py", "a`b.py", "a<b.py", "a\nb.py", "", "a\x00.py"],
)
def test_comment_paths_must_be_safe_repo_relative_names(path):
    bad = [{"path": path, "line": 1, "body": "b"}]
    assert code_of(review_payload(ledger(), comments=bad)) == "skill_payload_shape"
    assert (
        code_of(
            review_payload(ledger(), comments=[{"path": "sk-secret.py", "line": 1, "body": "b"}]),
            secrets=["sk-secret"],
        )
        == "skill_payload_shape"
    )
    assert build(
        review_payload(ledger(), comments=[{"path": "src/a-b_c.py", "line": 1, "body": "b"}])
    )


def test_folded_comments_are_escaped_and_the_final_body_is_rechecked():
    pol, led = policy(), ledger()
    body = "Lead.\n\n" + block(led)
    comments = [
        {
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "x <details><summary>acme-ledger:v1</summary> `y`",
        }
    ]
    final = skillreview.fold_comments(body, comments, "", pol, led)
    assert (
        "<details><summary>acme-ledger:v1</summary> `y`" not in final.split("```\n</details>", 1)[1]
    )
    assert skillreview.ledger_block(final, MARKER) == led
    with pytest.raises(SkillError) as exc:
        skillreview.fold_comments(body, comments, "", pol, ledger([finding("z")]))
    assert exc.value.code == "skill_body_block"
    big = [{"path": "a.py", "line": 3, "body": "z" * 7000}]
    with pytest.raises(SkillError) as exc:
        skillreview.fold_comments(body, big, "", pol, led)
    assert exc.value.code == "skill_body_cap"


def test_the_policy_file_may_not_sit_inside_a_bundle_input(monkeypatch, tmp_path):
    setup_run(monkeypatch, tmp_path, review_payload(ledger()))
    team = tmp_path / "team"
    team.mkdir()
    inside = team / "policy.json"
    inside.write_text(write_policy(tmp_path).read_text())
    inside.chmod(0o600)

    def fake_bundle(profile_path, cred_env, secrets, cfg, repo_setup_ref=None, team_sink=None):
        team_sink.update(where="/w", skills={"deep-review"}, roots=[team.resolve()], files=set())
        return None, "", ""

    monkeypatch.setattr(cli, "_load_profile_bundle", fake_bundle)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", PR_URL, "--skill", "deep-review", "--skill-policy", str(inside), "--json"],
    )
    assert res.exit_code == 3, res.output
    outside = write_policy(tmp_path)
    res = CliRunner().invoke(
        cli.main,
        ["review-pr", PR_URL, "--skill", "deep-review", "--skill-policy", str(outside), "--json"],
    )
    assert res.exit_code == 0, res.output


def test_a_skill_is_admitted_only_as_skills_name_skill_md_with_a_readable_header(tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "team"
    (root / "skills/ok").mkdir(parents=True)
    (root / "skills/ok/SKILL.md").write_text("---\nname: ok\n---\nbody\n")
    (root / "skills/bad").mkdir(parents=True)
    (root / "skills/bad/SKILL.md").write_text('---\n"hooks": 1\n---\n')
    (root / "skills/deep/nested").mkdir(parents=True)
    (root / "skills/deep/nested/SKILL.md").write_text("x\n")
    (root / "rules").mkdir()
    (root / "rules/SKILL.md").write_text("x\n")
    team = SimpleNamespace(root=root, files=[p for p in root.rglob("SKILL.md")])
    assert cli._team_skill_names(team) == {"ok"}


# --- approval ----------------------------------------------------------------------------------

MARK = "<!-- bot-review:"
CLEAN = ledger([])


def pf(name, status="modified", previous=None):
    return {
        "filename": name,
        "status": status,
        **({"previous_filename": previous} if previous else {}),
    }


def posts(calls):
    return [json.loads(i) for c, i in zip(calls, calls.inputs) if "POST" in c]


def approve_run(monkeypatch, tmp_path, payload=None, extra=("--allow-approve",), **kw):
    calls, _ = setup_run(monkeypatch, tmp_path, payload or review_payload(CLEAN), **kw)
    res, data = run(tmp_path, *extra)
    return calls, res, data


def test_a_clean_round_with_allow_approve_posts_an_approve_with_the_marker(monkeypatch, tmp_path):
    calls, res, data = approve_run(monkeypatch, tmp_path)
    assert res.exit_code == 0, res.output
    (posted,) = posts(calls)
    assert posted["event"] == "APPROVE" and posted["comments"] == []
    assert posted["body"].rstrip().endswith("-->") and MARK in posted["body"]
    assert ledger_ok(posted["body"].split(MARK)[0])
    assert data["status"] == "review_published" and data["review_event"] == "APPROVE"
    assert "signoff" not in data and data["ledger"]["state"] == "clean"


def test_without_allow_approve_a_clean_round_is_a_comment_and_carries_no_signoff(
    monkeypatch, tmp_path
):
    calls, res, data = approve_run(monkeypatch, tmp_path, extra=())
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and MARK not in posted["body"]
    assert data["review_event"] == "COMMENT" and "signoff" not in data


def test_a_prior_finding_that_the_new_ledger_settles_still_approves(monkeypatch, tmp_path):
    new = ledger([finding("a", status="fixed")])
    calls, res, data = approve_run(
        monkeypatch, tmp_path, review_payload(new), reviews=[review_row(7, ledger([finding("a")]))]
    )
    assert data["review_event"] == "APPROVE"


@pytest.mark.parametrize(
    "payload,reviews",
    [
        (review_payload(ledger([finding("a")])), []),  # an open finding
        (review_payload(CLEAN, comments=[inline()]), []),  # an inline comment
        (review_payload(CLEAN), [review_row(7, ledger())]),  # the prior open finding was dropped
        (  # a prior open finding stays open under another name is a drop of the first
            review_payload(ledger([finding("b", status="fixed")])),
            [review_row(7, ledger())],
        ),
    ],
)
def test_a_round_that_is_not_clean_or_drops_a_prior_finding_never_approves(
    monkeypatch, tmp_path, payload, reviews
):
    calls, res, data = approve_run(monkeypatch, tmp_path, payload, reviews=reviews)
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and data["review_event"] == "COMMENT"
    assert "signoff" not in data


def test_unreadable_pr_reviews_never_approve(monkeypatch, tmp_path):
    calls, res, data = approve_run(monkeypatch, tmp_path, review_reads=[(1, "", "boom")])
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and "signoff" not in data


def test_a_path_floor_posts_a_comment_with_the_reason_and_reports_signoff(monkeypatch, tmp_path):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, files=[pf("pay/X.java")], globs=["pay/**"]
    )
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and MARK not in posted["body"]
    assert "**Looks good: no findings above nit.**" in posted["body"]
    assert "`pay/X.java` matches `pay/**` in `.github/human-signoff-paths`" in posted["body"]
    assert "<!--" not in posted["body"] and ledger_ok(posted["body"])
    assert posted["body"].endswith("</sub>")
    assert data["review_event"] == "COMMENT"
    assert data["signoff"] == {"reason": "match", "path": "pay/X.java", "rule": "pay/**", "more": 0}


def test_the_floor_is_computed_for_a_clean_round_without_allow_approve(monkeypatch, tmp_path):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, extra=(), files=[pf("pay/X.java")], globs=["pay/**"]
    )
    assert "Looks good" in posts(calls)[0]["body"] and data["signoff"]["reason"] == "match"


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"files": None}, "unreadable"),
        ({"files": [pf("a.py")], "globs": None}, "unreadable"),
        ({"files": [pf("a.py")], "rules": None}, "unreadable"),
        ({"files": [pf(".github/human-signoff-content", "added")]}, "list_changed"),
        ({"files": [pf(".github/human-signoff-paths")]}, "list_changed"),
        (
            {"files": [pf("x/New.java", "renamed", "pay/Old.java")], "globs": ["pay/**"]},
            "match",
        ),
    ],
)
def test_unreadable_or_changed_sign_off_inputs_are_a_floor(monkeypatch, tmp_path, kw, reason):
    calls, res, data = approve_run(monkeypatch, tmp_path, **kw)
    assert posts(calls)[0]["event"] == "COMMENT" and data["signoff"]["reason"] == reason
    assert "Looks good" in posts(calls)[0]["body"]


CRULES = [("**/src/main/**", "CurrencyHelper")]


@pytest.mark.parametrize(
    "contents,floor",
    [
        (
            {("src/main/A.java", "base"): b"use CurrencyHelper", ("src/main/A.java", "head"): b""},
            True,
        ),
        (
            {("src/main/A.java", "base"): b"", ("src/main/A.java", "head"): b"use CurrencyHelper"},
            True,
        ),
        ({("src/main/A.java", "base"): b"ok", ("src/main/A.java", "head"): b"nothing here"}, False),
        ({("src/main/A.java", "base"): b"ok"}, "unreadable"),  # head is a 404: it must exist
        ({("src/main/A.java", "head"): b"ok"}, "unreadable"),  # base is a 404 for a modified file
    ],
)
def test_content_rules_floor_on_a_string_at_base_or_head(monkeypatch, tmp_path, contents, floor):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, files=[pf("src/main/A.java")], rules=CRULES, contents=contents
    )
    (posted,) = posts(calls)
    if floor == "unreadable":
        assert posted["event"] == "COMMENT" and data["signoff"]["reason"] == "unreadable"
        return
    if floor:
        assert posted["event"] == "COMMENT"
        assert "`src/main/A.java` may contain a string listed in" in posted["body"]
        assert data["signoff"] == {
            "reason": "content",
            "path": "src/main/A.java",
            "rule": "**/src/main/**",
            "more": 0,
        }
    else:
        assert posted["event"] == "APPROVE" and "signoff" not in data
    # base is the PR base commit, head the reviewed commit, and reads are size capped
    assert all(ref in (BASE, SHA) for _p, ref in calls.reads)
    assert set(calls.reads.values()) == {cli.CONTENT_FILE_MAX_BYTES}


def test_content_rules_over_the_file_cap_or_with_a_failed_read_are_a_floor(monkeypatch, tmp_path):
    many = [pf(f"src/main/F{i}.java") for i in range(21)]
    calls, res, data = approve_run(monkeypatch, tmp_path, files=many, rules=CRULES)
    assert data["signoff"]["reason"] == "unreadable" and posts(calls)[0]["event"] == "COMMENT"
    calls, res, data = approve_run(
        monkeypatch,
        tmp_path,
        files=[pf("src/main/A.java")],
        rules=CRULES,
        contents={("src/main/A.java", "base"): None},
    )
    assert data["signoff"]["reason"] == "unreadable" and posts(calls)[0]["event"] == "COMMENT"


def test_a_refused_approval_is_posted_as_a_comment(monkeypatch, tmp_path):
    refused = (1, "", "GitHub wrapper refused: approval is not allowed here")
    ok = (0, json.dumps({"html_url": "u", "id": 6, "user": {"login": LOGIN, "type": "Bot"}}), "")
    calls, res, data = approve_run(monkeypatch, tmp_path, post=lambda n: refused if n == 1 else ok)
    first, second = posts(calls)
    assert first["event"] == "APPROVE" and second["event"] == "COMMENT"
    assert MARK not in second["body"]
    assert res.exit_code == 0 and data["review_event"] == "COMMENT"
    assert data["skill_notes"] == ["approve_refused:1"] and data["review_id"] == 6


def test_an_unknown_approval_outcome_is_uncertain_and_never_reposted(monkeypatch, tmp_path):
    # the first read finds no prior ledger; the reconcile read then fails
    calls, res, data = approve_run(
        monkeypatch,
        tmp_path,
        post=(1, "", "timed out"),
        review_reads=[(0, "[[]]", ""), (1, "", "boom")],
    )
    assert len(posts(calls)) == 1 and posts(calls)[0]["event"] == "APPROVE"
    assert res.exit_code == 8 and data["status"] == "publish_uncertain"


def test_an_approve_that_landed_but_looked_failed_is_reported_from_the_pr(monkeypatch, tmp_path):
    def post(n):
        return 1, "", "timed out"

    calls, _ = setup_run(monkeypatch, tmp_path, review_payload(CLEAN), post=post)
    seen = {}

    real = cli.run_gh

    def with_marker(args, e, **k):
        out = real(args, e, **k)
        if args[1].endswith("/reviews?per_page=100") and calls.inputs:
            body = json.loads(calls.inputs[0])["body"]
            row = {
                "id": 77, "state": "APPROVED", "commit_id": SHA, "body": body,
                "html_url": "u", "user": {"login": LOGIN, "type": "Bot"},
            }  # fmt: skip
            seen["n"] = seen.get("n", 0) + 1
            return 0, json.dumps([[row]]), ""
        return out

    monkeypatch.setattr(cli, "run_gh", with_marker)
    res, data = run(tmp_path, "--allow-approve")
    assert len(posts(calls)) == 1
    assert data["review_event"] == "APPROVE" and data["review_id"] == 77


def test_a_quiet_clean_round_approves_with_a_new_review_instead_of_the_put(monkeypatch, tmp_path):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    calls, res, data = approve_run(
        monkeypatch, tmp_path, payload, reviews=[review_row(9, ledger([finding("a")]))]
    )
    assert not [c for c in calls if "PUT" in c]
    (posted,) = posts(calls)
    assert posted["event"] == "APPROVE" and "Quiet." in posted["body"] and MARK in posted["body"]
    assert data["review_event"] == "APPROVE" and data["review_id"] == 555


def test_a_quiet_round_on_a_floor_file_keeps_the_put_and_adds_the_reason(monkeypatch, tmp_path):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    calls, res, data = approve_run(
        monkeypatch, tmp_path, payload, reviews=[review_row(9, ledger([finding("a")]))],
        files=[pf("pay/X.java")], globs=["pay/**"],
    )  # fmt: skip
    assert calls[0][:4] == ["api", "-X", "PUT", "repos/me/repo/pulls/11/reviews/9"]
    body = json.loads(calls.inputs[0])["body"]
    assert "Looks good" in body and "<!--" not in body and ledger_ok(body)
    assert data["signoff"]["reason"] == "match" and data["review_event"] == "COMMENT"


def test_an_approved_update_target_is_replaced_by_a_new_comment_review(monkeypatch, tmp_path):
    led = ledger([finding("a")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    calls, res, data = approve_run(
        monkeypatch, tmp_path, payload, extra=(),
        reviews=[review_row(9, ledger(), state="APPROVED")],
    )  # fmt: skip
    assert not [c for c in calls if "PUT" in c]
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and "Quiet." in posted["body"]
    assert res.exit_code == 0 and data["review_event"] == "COMMENT"


def test_prior_settled_needs_every_open_prior_finding_cleared_by_id():
    pol = policy()
    prior = ledger([finding("a"), finding("b", status="fixed")])
    assert skillreview.prior_settled([], CLEAN, pol)
    assert skillreview.prior_settled([prior], ledger([finding("a", status="wontfix")]), pol)
    assert not skillreview.prior_settled([prior], ledger([]), pol)
    assert not skillreview.prior_settled([prior], ledger([finding("a")]), pol)
    # a finding that an intermediate round dropped is still owed; a cleared one is not
    assert not skillreview.prior_settled([prior, ledger([])], ledger([]), pol)
    assert skillreview.prior_settled([prior, ledger([finding("a", status="fixed")])], CLEAN, pol)


def test_a_clean_round_with_no_body_and_no_update_posts_nothing(monkeypatch, tmp_path):
    calls, res, data = approve_run(monkeypatch, tmp_path, review_payload(CLEAN, review=None))
    assert res.exit_code == 0 and not calls
    assert data["status"] == "review_complete" and "review_event" not in data


def test_a_finding_dropped_by_an_intermediate_round_still_blocks_approval(monkeypatch, tmp_path):
    rows = [
        review_row(5, ledger([finding("a")]), at="2026-10-01T00:00:00Z"),
        review_row(6, ledger([]), at="2026-10-02T00:00:00Z"),  # round 2 silently dropped it
    ]
    calls, res, data = approve_run(monkeypatch, tmp_path, reviews=rows)
    assert posts(calls)[0]["event"] == "COMMENT"
    rows[1] = review_row(6, ledger([finding("a", status="fixed")]), at="2026-10-02T00:00:00Z")
    calls, res, data = approve_run(monkeypatch, tmp_path, reviews=rows)
    assert posts(calls)[0]["event"] == "APPROVE"


OPEN = ledger([finding("a")])
APPROVED_ROW = review_row(5, OPEN, state="APPROVED")


DISMISS = ["api", "-X", "PUT", "repos/me/repo/pulls/11/reviews/5/dismissals", "--input", "-"]


def dismissals(calls):
    return [json.loads(i) for c, i in zip(calls, calls.inputs) if c == DISMISS]


def test_open_findings_after_an_approval_dismiss_it_then_comment(monkeypatch, tmp_path):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, review_payload(OPEN, comments=[inline()]), reviews=[APPROVED_ROW]
    )
    assert calls[0] == DISMISS  # before the review is posted
    assert dismissals(calls) == [{"message": cli.DISMISS_MESSAGE, "event": "DISMISS"}]
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and posted["comments"] == [inline()]
    assert data["review_event"] == "COMMENT" and data["dismissed_review_id"] == 5


def test_a_clean_round_on_a_floor_file_after_an_approval_dismisses_then_comments(
    monkeypatch, tmp_path
):
    rows = [review_row(5, ledger([]), state="APPROVED")]
    calls, res, data = approve_run(
        monkeypatch, tmp_path, reviews=rows, files=[pf("pay/X.java")], globs=["pay/**"]
    )
    assert calls[0] == DISMISS
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and "Looks good" in posted["body"]
    assert data["dismissed_review_id"] == 5 and data["signoff"]["reason"] == "match"


def test_a_round_that_approves_again_does_not_dismiss(monkeypatch, tmp_path):
    rows = [review_row(5, ledger([]), state="APPROVED")]
    calls, res, data = approve_run(monkeypatch, tmp_path, reviews=rows)
    assert not dismissals(calls) and posts(calls)[0]["event"] == "APPROVE"
    assert "dismissed_review_id" not in data


@pytest.mark.parametrize(
    "extra,rows",
    [
        ((), [APPROVED_ROW]),  # without --allow-approve nothing is dismissed
        (("--allow-approve",), []),  # no prior approval
        (("--allow-approve",), [review_row(5, OPEN)]),  # no approval stands
        (
            ("--allow-approve",),
            [
                APPROVED_ROW,
                review_row(6, OPEN, state="CHANGES_REQUESTED", at="2026-10-02T00:00:00Z"),
            ],
        ),
        (("--allow-approve",), [review_row(5, OPEN, login="someone", state="APPROVED")]),
    ],
)
def test_no_standing_bot_approval_means_no_dismissal(monkeypatch, tmp_path, extra, rows):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, review_payload(OPEN), extra=extra, reviews=rows
    )
    assert not [c for c in calls if "dismissals" in c[4]]
    assert posts(calls)[0]["event"] == "COMMENT" and "dismissed_review_id" not in data


def test_a_failed_dismissal_posts_request_changes_over_the_stale_approval(monkeypatch, tmp_path):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, review_payload(OPEN), reviews=[APPROVED_ROW], put=(1, "", "boom")
    )
    assert posts(calls)[0]["event"] == "REQUEST_CHANGES"
    assert data["skill_notes"] == ["dismiss_failed:1"] and "dismissed_review_id" not in data


def test_a_head_that_moved_before_the_dismissal_writes_nothing(monkeypatch, tmp_path):
    # pin, publish start, then the check right before the dismissal sees a moved head
    calls, res, data = approve_run(
        monkeypatch,
        tmp_path,
        review_payload(OPEN),
        reviews=[APPROVED_ROW],
        heads=[SHA, SHA, "c" * 40],
    )
    assert not calls and data["status"] == "publish_blocked_stale_head"


def test_unreadable_reviews_cannot_dismiss_and_say_so(monkeypatch, tmp_path):
    calls, res, data = approve_run(
        monkeypatch, tmp_path, review_payload(OPEN), review_reads=[(1, "", "boom")]
    )
    assert posts(calls)[0]["event"] == "COMMENT" and not dismissals(calls)
    assert data["skill_notes"] == ["approval_unverified:1"]


def test_a_quiet_round_after_an_approval_dismisses_and_posts_a_new_comment(monkeypatch, tmp_path):
    upd = {"review_id": 5, "body": "Quiet.\n\n" + block(OPEN)}
    payload = review_payload(OPEN, decision="delta", review=None, ledger_update=upd)
    calls, res, data = approve_run(monkeypatch, tmp_path, payload, reviews=[APPROVED_ROW])
    assert calls[0] == DISMISS and len(calls) == 2
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and "Quiet." in posted["body"]
    assert data["review_event"] == "COMMENT" and data["dismissed_review_id"] == 5


def test_a_quiet_round_whose_dismissal_fails_posts_request_changes(monkeypatch, tmp_path):
    upd = {"review_id": 5, "body": "Quiet.\n\n" + block(OPEN)}
    payload = review_payload(OPEN, decision="delta", review=None, ledger_update=upd)
    calls, res, data = approve_run(
        monkeypatch, tmp_path, payload, reviews=[APPROVED_ROW], put=(1, "", "boom")
    )
    (posted,) = posts(calls)
    assert posted["event"] == "REQUEST_CHANGES" and "Quiet." in posted["body"]
    assert data["skill_notes"] == ["dismiss_failed:1"] and "dismissed_review_id" not in data


def test_an_absent_review_after_an_unconfirmed_approve_is_uncertain_not_a_comment(
    monkeypatch, tmp_path
):
    calls, res, data = approve_run(monkeypatch, tmp_path, post=(1, "", "timed out"))
    assert len(posts(calls)) == 1 and not [c for c in calls if "PUT" in c]
    assert res.exit_code == 8 and data["status"] == "publish_uncertain"


def test_a_quiet_round_may_edit_only_the_newest_ledger_review(monkeypatch, tmp_path):
    led = ledger([finding("a", status="fixed")])
    upd = {"review_id": 9, "body": "Quiet.\n\n" + block(led)}
    payload = review_payload(led, decision="delta", review=None, ledger_update=upd)
    rows = [
        review_row(9, ledger([finding("a")]), at="2026-10-01T00:00:00Z"),
        review_row(10, ledger([finding("a")]), at="2026-10-02T00:00:00Z"),
    ]
    calls, res, data = approve_run(monkeypatch, tmp_path, payload, extra=(), reviews=rows)
    assert res.exit_code == 7 and not calls and "skill_update_target" in data["reason"]


def test_a_moved_base_voids_the_floor_and_blocks_the_approval(monkeypatch, tmp_path):
    # pin, publish start, after the floor reads, then the check right before the approval
    calls, res, data = approve_run(monkeypatch, tmp_path, bases=[BASE, BASE, BASE, "c" * 40])
    assert not calls and data["status"] == "publish_blocked_stale_head"


def long_body_payload(lead_chars=900):
    led = CLEAN
    return review_payload(led, "x" * lead_chars + "\n\n" + block(led)), (
        len("x" * lead_chars + "\n\n" + block(led)) + 2 + len(cli.review_footer())
    )


def test_the_sign_off_paragraph_falls_back_to_a_fixed_short_one_and_never_vanishes(
    monkeypatch, tmp_path
):
    payload, base_len = long_body_payload()
    cap = base_len + 2 + len(cli._SIGNOFF_FIXED_TEXT)
    calls, res, data = approve_run(
        monkeypatch, tmp_path, payload, files=[pf("pay/X.java")], globs=["pay/**"],
    )  # fmt: skip
    assert "matches `pay/**`" in posts(calls)[0]["body"]  # fits under the default cap
    calls, _ = setup_run(monkeypatch, tmp_path, payload, files=[pf("pay/X.java")], globs=["pay/**"])
    res, data = run(tmp_path, "--allow-approve", policy_over={"max_body_chars": cap})
    body = posts(calls)[0]["body"]
    assert cli._SIGNOFF_FIXED_TEXT in body and "pay/X.java" not in body
    assert data["signoff"]["reason"] == "match" and data["skill_notes"] == [
        "signoff_text_shortened:1"
    ]
    # not even the short paragraph fits: the payload is refused, nothing is posted
    calls, _ = setup_run(monkeypatch, tmp_path, payload, files=[pf("pay/X.java")], globs=["pay/**"])
    res, data = run(tmp_path, "--allow-approve", policy_over={"max_body_chars": cap - 1})
    assert res.exit_code == 7 and not posts(calls)
    assert data["status"] == "skill_payload_invalid"


def test_an_approve_body_that_does_not_fit_with_its_marker_is_posted_as_a_comment(
    monkeypatch, tmp_path
):
    payload, base_len = long_body_payload()
    calls, _ = setup_run(monkeypatch, tmp_path, payload)
    res, data = run(tmp_path, "--allow-approve", policy_over={"max_body_chars": base_len})
    (posted,) = posts(calls)
    assert posted["event"] == "COMMENT" and MARK not in posted["body"]
    assert data["skill_notes"] == ["approve_body_cap:1"]
