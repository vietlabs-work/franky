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
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\[bot\]$")
_LOC_RE = re.compile(r"^[^\s`<>]+:[1-9][0-9]*$")
# A repo-relative path that is inert inside a markdown code span and an HTML attribute.
_PATH_RE = re.compile(r"^[A-Za-z0-9._@+=~,\-][A-Za-z0-9._@+=~,/\-]*$")
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
    cleared_statuses: tuple[str, ...] = ("fixed", "resolved", "wontfix")
    open_statuses: tuple[str, ...] = ("open",)


def is_bot_login(login: object, policy: SkillPolicy) -> bool:
    """REST identity: the full `slug[bot]` login, case-insensitive, nothing stripped."""
    return isinstance(login, str) and login.lower() == policy.bot_login.lower()


def is_bot_user(user: object, policy: SkillPolicy) -> bool:
    """A REST `user` object: type Bot AND the exact full login."""
    return (
        isinstance(user, dict)
        and user.get("type") == "Bot"
        and is_bot_login(user.get("login"), policy)
    )


def is_bot_author(author: object, policy: SkillPolicy) -> bool:
    """A GraphQL author. GraphQL reports a bot's login without `[bot]`, so that suffix is the
    only difference allowed; the typename must be Bot."""
    if not isinstance(author, dict) or author.get("__typename") != "Bot":
        return False
    login = author.get("login")
    return isinstance(login, str) and login.lower() + "[bot]" == policy.bot_login.lower()


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
_OPTIONAL = (
    "allow_ledger_update",
    "blocking_field",
    "blocking_values",
    "cleared_statuses",
    "open_statuses",
)


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
        raise ValueError("skill policy: bot_login must be a full `slug[bot]` login")
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
    sets = {}
    for key, default in (
        ("cleared_statuses", ["fixed", "resolved", "wontfix"]),
        ("open_statuses", ["open"]),
    ):
        vals = raw.get(key, default)
        if (
            not isinstance(vals, list)
            or not 1 <= len(vals) <= 10
            or not all(isinstance(v, str) and v and len(v) <= 40 for v in vals)
        ):
            raise ValueError(f"skill policy: {key} must be 1-10 short strings")
        sets[key] = tuple(vals)
    if set(sets["cleared_statuses"]) & set(sets["open_statuses"]):
        raise ValueError("skill policy: cleared_statuses and open_statuses overlap")
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
        cleared_statuses=sets["cleared_statuses"],
        open_statuses=sets["open_statuses"],
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


def finding_state(finding: dict, policy: SkillPolicy) -> str | None:
    """ "open", "cleared", or None for a status in neither set (compared exactly, case matters).
    A missing status is open."""
    status = finding.get("status")
    if status is None or status in policy.open_statuses:
        return "open"
    return "cleared" if status in policy.cleared_statuses else None


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
    ids = [f.get("id") for f in findings]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        raise SkillError("skill_ledger_findings")
    for f in findings:
        if not isinstance(f.get("loc"), str) or not _LOC_RE.match(f["loc"]):
            raise SkillError("skill_ledger_findings")
        if finding_state(f, policy) is None:
            raise SkillError("skill_ledger_findings")
    if ledger.get("state") not in LEDGER_STATES:
        raise SkillError("skill_ledger_state")
    n_open = sum(finding_state(f, policy) == "open" for f in findings)
    if (ledger["state"] == "clean") != (n_open == 0):
        raise SkillError("skill_ledger_state")
    return ledger


def ledger_counts(ledger: dict, policy: SkillPolicy) -> tuple[int, int]:
    """(open, blocking) finding counts; blocking = open findings whose policy field matches."""
    live = [f for f in ledger.get("findings") or [] if finding_state(f, policy) == "open"]
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
            if (
                not isinstance(path, str)
                or not path
                or len(path) > MAX_PATH_CHARS
                or not _PATH_RE.match(path)
                or ".." in path.split("/")
                or redact(path, secrets) != path
            ):
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


def fold_comments(
    body: str, comments: list[dict], footer: str, policy: SkillPolicy, ledger: dict
) -> str:
    """Body-only fallback when GitHub rejects the inline anchors: the comments join the body,
    ahead of the footer. Raises SkillError when the result is not exactly one ledger block equal
    to `ledger`, or no longer fits its cap."""
    if footer and body.endswith(footer):
        body = body[: -len(footer)].rstrip()
    extra = "\n\n".join(
        f"- `{c['path']}:{int(c['line'])}`: " + neutralize(c["body"]).replace("<", "&lt;")
        for c in comments
    )
    final = f"{body}\n\n{extra}" + (f"\n\n{footer}" if footer else "")
    if ledger_block(final, policy.ledger_summary_marker) != ledger:
        raise SkillError("skill_body_block")
    if len(final) > policy.max_body_chars:
        raise SkillError("skill_body_cap")
    return final


# --- prior ledger, resolve and reply selection ---------------------------------------------


def _review_sort_key(r: dict) -> tuple:
    return (str(r.get("submitted_at") or ""), r.get("id") if type(r.get("id")) is int else 0)


def bot_ledger_reviews(reviews: list, policy: SkillPolicy) -> list[tuple[dict, dict]]:
    """The bot's own reviews that hold exactly one valid ledger block of the schema, oldest
    first, as (review, ledger). The author must be a REST Bot with the exact full login."""
    out = []
    for r in sorted((r for r in reviews if isinstance(r, dict)), key=_review_sort_key):
        if not is_bot_user(r.get("user"), policy):
            continue
        try:
            led = ledger_block(str(r.get("body") or ""), policy.ledger_summary_marker)
        except SkillError:
            continue
        if led.get("schema") == policy.ledger_schema:
            out.append((r, led))
    return out


def _loc(finding: dict) -> tuple[str, int] | None:
    loc = finding.get("loc")
    if not isinstance(loc, str) or not _LOC_RE.match(loc):
        return None
    path, _sep, line = loc.rpartition(":")
    return path, int(line)


def cleared_locs(prior: dict | None, new: dict, policy: SkillPolicy) -> set[tuple[str, int]]:
    """(path, line) anchors a thread may be resolved for.

    The anchor must be the `loc` of exactly ONE open finding of the prior ledger, and that
    finding must be present in the new ledger with a cleared status. A finding the new ledger
    drops is not cleared. Anything ambiguous is absent."""
    if prior is None:
        return set()
    now = {f.get("id"): f for f in new.get("findings") or [] if isinstance(f, dict)}
    seen: dict[tuple[str, int], list[bool]] = {}
    for f in prior.get("findings") or []:
        if not isinstance(f, dict) or finding_state(f, policy) != "open" or _loc(f) is None:
            continue
        cur = now.get(f.get("id"))
        seen.setdefault(_loc(f), []).append(
            cur is not None and finding_state(cur, policy) == "cleared"
        )
    return {loc for loc, flags in seen.items() if flags == [True]}


def own_thread(node: object, policy: SkillPolicy) -> bool:
    """An open review thread whose first comment is by the policy's bot."""
    if not isinstance(node, dict) or node.get("isResolved") is not False:
        return False
    comments = node.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    first = nodes[0] if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict) else {}
    return is_bot_author(first.get("author"), policy)


def thread_anchor(node: dict) -> tuple[str, int] | None:
    """(path, line) of a thread, from its line or, once outdated, its original line."""
    path = node.get("path")
    line = node.get("line") if type(node.get("line")) is int else node.get("originalLine")
    if not isinstance(path, str) or type(line) is not int or line < 1:
        return None
    return path, line


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
