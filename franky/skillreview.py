"""Skill-mode `review-pr` (pure - no I/O, no docker).

`review-pr --skill NAME --skill-policy FILE` runs a team skill instead of Franky's own review
method. The container holds a read token, so the skill cannot post. It puts the exact payloads in
the nonce-fenced sentinel and this module validates them; `cli.py` then posts through the same
host wrapper shapes as every other review.

Everything skill-specific comes from the caller's JSON policy (`SkillPolicy`); nothing here names
a team, a repository or a skill. The ledger convention Franky reads is deliberately small: a JSON
object with `schema`, `reviewed_sha`, `state` (`clean` or `findings`), an optional integer
`round`, and `findings[]` of objects with a string `id`, a `loc` (`path:line`), an optional
`status` (absent means `open`; any other value counts as cleared) and the policy's blocking field.

Every refusal is a fixed `SkillError.code`; the payload text never reaches an error message.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .config import redact
from .sentinel import scan_sentinel_json

REVIEW_DECISIONS = frozenset({"first-pass", "delta", "second-opinion"})
TERMINAL_DECISIONS = frozenset({"done", "skip", "halt", "author-mode", "not-reviewable"})
LEDGER_STATES = frozenset({"clean", "findings"})
SIDES = frozenset({"LEFT", "RIGHT"})
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
MAX_REASON_CHARS = 300
MAX_PATH_CHARS = 300
MAX_RESOLVE = 100
MAX_REPLIES = 50
# GitHub refuses a review body over 65,536 characters; the policy may only lower the cap.
GITHUB_BODY_LIMIT = 65000
MAX_REPLY_CHARS = 2000  # the host wrapper refuses a longer reply body


class SkillError(ValueError):
    """A refusal with a fixed machine code; nothing from the payload is in the message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SkillPolicy:
    ledger_schema: str
    ledger_summary_marker: str
    max_body_chars: int
    max_comments: int
    max_comment_chars: int
    bot_login: str
    allow_ledger_update: bool = False
    blocking_field: str = "sev"
    blocking_values: tuple[str, ...] = ("blocking",)

    @property
    def bare_login(self) -> str:
        return bare(self.bot_login)


def bare(login: object) -> str:
    """A login without a trailing `[bot]`, lowercased, for comparison."""
    text = str(login or "").strip().lower()
    return text[: -len("[bot]")] if text.endswith("[bot]") else text


def _int(raw: dict, key: str, low: int, high: int) -> int:
    value = raw.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"skill policy: {key} must be an integer from {low} to {high}")
    return value


def _str(raw: dict, key: str, cap: int = 200) -> str:
    value = raw.get(key)
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > cap
    ):
        raise ValueError(f"skill policy: {key} must be a non-empty trimmed string")
    return value


_REQUIRED = (
    "ledger_schema",
    "ledger_summary_marker",
    "max_body_chars",
    "max_comments",
    "max_comment_chars",
    "bot_login",
)
_OPTIONAL = ("allow_ledger_update", "blocking_field", "blocking_values")


def parse_policy(raw: object) -> SkillPolicy:
    """Validate the caller's policy object strictly: unknown, missing or mistyped keys refuse."""
    if not isinstance(raw, dict):
        raise ValueError("skill policy must be a JSON object")
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise ValueError(f"skill policy: unknown key(s): {', '.join(unknown)[:200]}")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"skill policy: missing key(s): {', '.join(missing)}")
    login = _str(raw, "bot_login", 60)
    if not _LOGIN_RE.match(login):
        raise ValueError("skill policy: bot_login is not a GitHub login")
    allow = raw.get("allow_ledger_update", False)
    if not isinstance(allow, bool):
        raise ValueError("skill policy: allow_ledger_update must be a boolean")
    field_name = _str(raw, "blocking_field", 40) if "blocking_field" in raw else "sev"
    values = raw.get("blocking_values", ["blocking"])
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 10
        or not all(isinstance(v, str) and v and len(v) <= 40 for v in values)
    ):
        raise ValueError("skill policy: blocking_values must be 1-10 short strings")
    return SkillPolicy(
        ledger_schema=_str(raw, "ledger_schema", 100),
        ledger_summary_marker=_str(raw, "ledger_summary_marker", 100),
        max_body_chars=_int(raw, "max_body_chars", 1000, GITHUB_BODY_LIMIT),
        max_comments=_int(raw, "max_comments", 0, 50),
        max_comment_chars=_int(raw, "max_comment_chars", 100, GITHUB_BODY_LIMIT),
        bot_login=login,
        allow_ledger_update=allow,
        blocking_field=field_name,
        blocking_values=tuple(values),
    )


def parse_policy_text(text: str) -> SkillPolicy:
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError("skill policy is not valid JSON") from exc
    return parse_policy(raw)


# --- ledger blocks -------------------------------------------------------------------------

_BLOCK_RE = re.compile(
    r"<details>\s*<summary>(?P<summary>[^<]*)</summary>\s*```json\s*(?P<json>.*?)\s*```\s*</details>",
    re.DOTALL,
)
_SUMMARY_RE = re.compile(r"<summary>(?P<summary>[^<]*)</summary>")


def ledger_block(body: str, marker: str) -> dict:
    """The one ledger object a body carries, or SkillError("skill_body_block").

    Exactly one `<summary>` may contain `marker`, and that `<details>` block must hold a json
    fence that parses to an object. Other `<details>` blocks are none of our business."""
    marked = [m for m in _SUMMARY_RE.finditer(body) if marker in m.group("summary")]
    blocks = [m for m in _BLOCK_RE.finditer(body) if marker in m.group("summary")]
    if len(marked) != 1 or len(blocks) != 1:
        raise SkillError("skill_body_block")
    try:
        obj = json.loads(blocks[0].group("json"))
    except (ValueError, RecursionError) as exc:
        raise SkillError("skill_body_block") from exc
    if not isinstance(obj, dict):
        raise SkillError("skill_body_block")
    return obj


def _is_open(finding: dict) -> bool:
    return finding.get("status", "open") == "open"


def validate_ledger(ledger: object, policy: SkillPolicy, pinned_sha: str) -> dict:
    """Schema, pinned sha, state and state/findings consistency; returns the ledger."""
    if not isinstance(ledger, dict) or ledger.get("schema") != policy.ledger_schema:
        raise SkillError("skill_ledger_schema")
    sha = ledger.get("reviewed_sha")
    if not isinstance(sha, str) or sha.lower() != pinned_sha.lower():
        raise SkillError("skill_ledger_sha")
    if "round" in ledger and (type(ledger["round"]) is not int or ledger["round"] < 1):
        raise SkillError("skill_ledger_round")
    findings = ledger.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        raise SkillError("skill_ledger_findings")
    if ledger.get("state") not in LEDGER_STATES:
        raise SkillError("skill_ledger_state")
    n_open = sum(_is_open(f) for f in findings)
    if (ledger["state"] == "clean") != (n_open == 0):
        raise SkillError("skill_ledger_state")
    return ledger


def ledger_counts(ledger: dict, policy: SkillPolicy) -> tuple[int, int]:
    """(open, blocking) finding counts; blocking = open findings whose policy field matches."""
    live = [f for f in ledger.get("findings") or [] if _is_open(f)]
    blocking = [f for f in live if f.get(policy.blocking_field) in policy.blocking_values]
    return len(live), len(blocking)


# --- sentinel payload ----------------------------------------------------------------------


@dataclass
class SkillPayload:
    kind: str  # "review" | "terminal"
    decision: str
    ledger: dict | None = None
    body: str | None = None  # the review body, final text (redacted, neutralized, footer)
    comments: list[dict] | None = None  # final text
    resolve: list[str] | None = None
    replies: list[dict] | None = None  # {comment_id, body} final text
    update: dict | None = None  # {review_id, body} final text
    reason: str = ""
    prior_review_id: int | None = None


def parse_skill_payload(output: str, nonce: str) -> dict | None:
    return scan_sentinel_json(output, "REVIEW", nonce)


def neutralize(text: str) -> str:
    """No HTML comment may open in anything we post: a forged marker could steer a reconcile."""
    return text.replace("<!--", "&lt;!--")


def _final(text: object, secrets: Iterable[str]) -> str:
    if not isinstance(text, str):
        raise SkillError("skill_payload_shape")
    return neutralize(redact(text, list(secrets)))


def _pos(v: object) -> int | None:
    return v if type(v) is int and v >= 1 else None


def build_payload(
    raw: object,
    policy: SkillPolicy,
    pinned_sha: str,
    secrets: Iterable[str],
    footer: str = "",
) -> SkillPayload:
    """Validate the agent's payload; fail closed with a SkillError and post nothing."""
    secrets = list(secrets)
    if not isinstance(raw, dict):
        raise SkillError("skill_payload_missing")
    kind = raw.get("kind")
    decision = raw.get("decision")
    if kind == "terminal":
        if decision not in TERMINAL_DECISIONS:
            raise SkillError("skill_payload_shape")
        reason = raw.get("reason")
        prior = raw.get("prior_ledger_review_id")
        if not isinstance(reason, str) or (prior is not None and _pos(prior) is None):
            raise SkillError("skill_payload_shape")
        return SkillPayload(
            "terminal",
            decision,
            reason=redact(reason.strip(), secrets)[:MAX_REASON_CHARS],
            prior_review_id=prior,
        )
    if kind != "review" or decision not in REVIEW_DECISIONS:
        raise SkillError("skill_payload_shape")

    ledger = validate_ledger(raw.get("ledger"), policy, pinned_sha)
    review, update = raw.get("review"), raw.get("ledger_update")
    if review is not None and update is not None:
        raise SkillError("skill_update_with_review")
    if update is not None and not policy.allow_ledger_update:
        raise SkillError("skill_update_refused")

    def checked_body(text: object) -> str:
        # The agent's own text must carry exactly the structured ledger; the posted text is
        # re-checked after redaction and neutralization, then capped.
        if not isinstance(text, str) or ledger_block(text, policy.ledger_summary_marker) != ledger:
            raise SkillError("skill_body_block")
        final = _final(text, secrets)
        if footer:
            final = f"{final}\n\n{footer}"
        post = ledger_block(final, policy.ledger_summary_marker)
        if post.get("schema") != policy.ledger_schema:
            raise SkillError("skill_body_block")
        if len(final) > policy.max_body_chars:
            raise SkillError("skill_body_cap")
        return final

    payload = SkillPayload("review", decision, ledger=ledger, resolve=[], replies=[])
    if review is not None:
        if not isinstance(review, dict):
            raise SkillError("skill_payload_shape")
        payload.body = checked_body(review.get("body"))
        comments = review.get("comments", [])
        if not isinstance(comments, list):
            raise SkillError("skill_payload_shape")
        if len(comments) > policy.max_comments:
            raise SkillError("skill_comment_cap")
        payload.comments = []
        for c in comments:
            if not isinstance(c, dict):
                raise SkillError("skill_payload_shape")
            path, line = c.get("path"), _pos(c.get("line"))
            side = c.get("side", "RIGHT")
            if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
                raise SkillError("skill_payload_shape")
            if line is None or side not in SIDES or not isinstance(c.get("body"), str):
                raise SkillError("skill_payload_shape")
            text = _final(c["body"], secrets)
            if not text.strip():
                raise SkillError("skill_payload_shape")
            if len(text) > policy.max_comment_chars:
                raise SkillError("skill_comment_cap")
            payload.comments.append({"path": path, "line": line, "side": side, "body": text})
    if update is not None:
        if not isinstance(update, dict) or _pos(update.get("review_id")) is None:
            raise SkillError("skill_payload_shape")
        body = checked_body(update.get("body"))
        payload.update = {"review_id": update["review_id"], "body": body}

    resolve = raw.get("resolve", [])
    if (
        not isinstance(resolve, list)
        or len(resolve) > MAX_RESOLVE
        or not all(isinstance(i, str) and i and len(i) <= 100 for i in resolve)
    ):
        raise SkillError("skill_payload_shape")
    payload.resolve = list(dict.fromkeys(resolve))
    replies = raw.get("replies", [])
    if not isinstance(replies, list) or len(replies) > MAX_REPLIES:
        raise SkillError("skill_payload_shape")
    for r in replies:
        if not isinstance(r, dict) or _pos(r.get("comment_id")) is None:
            raise SkillError("skill_payload_shape")
        payload.replies.append(
            {"comment_id": r["comment_id"], "body": _final(r.get("body"), secrets)}
        )
    return payload


def fold_comments(body: str, comments: list[dict], footer: str, policy: SkillPolicy) -> str:
    """Body-only fallback when GitHub rejects the inline anchors: the comments join the body,
    ahead of the footer. SkillError("skill_body_cap") when the result no longer fits."""
    if footer and body.endswith(footer):
        body = body[: -len(footer)].rstrip()
    extra = "\n\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in comments)
    final = f"{body}\n\n{extra}" + (f"\n\n{footer}" if footer else "")
    if len(final) > policy.max_body_chars:
        raise SkillError("skill_body_cap")
    return final


# --- prior ledger, resolve and reply selection ---------------------------------------------


def _review_sort_key(r: dict) -> tuple:
    return (str(r.get("submitted_at") or ""), r.get("id") if type(r.get("id")) is int else 0)


def bot_ledger_reviews(reviews: list, policy: SkillPolicy) -> list[tuple[dict, dict]]:
    """The bot's own reviews that hold exactly one valid ledger block of the schema, oldest
    first, as (review, ledger). A review whose block is missing, broken or another schema is
    skipped."""
    out = []
    for r in sorted((r for r in reviews if isinstance(r, dict)), key=_review_sort_key):
        user = r.get("user")
        if not isinstance(user, dict) or bare(user.get("login")) != policy.bare_login:
            continue
        try:
            led = ledger_block(str(r.get("body") or ""), policy.ledger_summary_marker)
        except SkillError:
            continue
        if led.get("schema") == policy.ledger_schema:
            out.append((r, led))
    return out


def _loc_path(finding: dict) -> str:
    loc = finding.get("loc")
    if not isinstance(loc, str):
        return ""
    head, _sep, tail = loc.rpartition(":")
    return head if head and tail.isdigit() else loc


def cleared_paths(prior: dict | None, new: dict) -> set[str]:
    """Paths whose prior open findings are ALL cleared in the new ledger.

    A thread may resolve only when its file has at least one open prior finding and every open
    prior finding on that file is present in the new ledger with a status other than `open`. A
    finding the new ledger drops counts as not cleared (fail closed)."""
    if prior is None:
        return set()
    now = {f.get("id"): f for f in new.get("findings") or [] if isinstance(f, dict)}
    by_path: dict[str, list[bool]] = {}
    for f in prior.get("findings") or []:
        if not isinstance(f, dict) or not _is_open(f) or not _loc_path(f):
            continue
        current = now.get(f.get("id"))
        by_path.setdefault(_loc_path(f), []).append(current is not None and not _is_open(current))
    return {p for p, flags in by_path.items() if all(flags)}


def own_thread(node: object, policy: SkillPolicy) -> bool:
    """An open review thread whose first comment is by the policy's bot."""
    if not isinstance(node, dict) or node.get("isResolved") is not False:
        return False
    nodes = (
        (node.get("comments") or {}).get("nodes")
        if isinstance(node.get("comments"), dict)
        else None
    )
    first = nodes[0] if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict) else {}
    author = first.get("author") if isinstance(first.get("author"), dict) else {}
    return author.get("__typename") == "Bot" and bare(author.get("login")) == policy.bare_login


def ledger_summary(
    payload: SkillPayload, policy: SkillPolicy, pinned_sha: str, review_url: str | None
) -> dict:
    """The result's `ledger` object."""
    if payload.kind == "terminal":
        return {
            "kind": "terminal",
            "decision": payload.decision,
            "state": None,
            "round": None,
            "reviewed_sha": pinned_sha,
            "open": None,
            "blocking": None,
            "review_url": review_url,
            "reason": payload.reason,
        }
    led = payload.ledger or {}
    n_open, n_blocking = ledger_counts(led, policy)
    rnd = led.get("round")
    return {
        "kind": "review",
        "decision": payload.decision,
        "state": led.get("state"),
        "round": rnd if type(rnd) is int else None,
        "reviewed_sha": pinned_sha,
        "open": n_open,
        "blocking": n_blocking,
        "review_url": review_url,
    }
