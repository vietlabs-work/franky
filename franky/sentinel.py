"""Bounded nonce-fenced payload scanning for JSONL and raw engine transcripts."""

import json
import warnings

from .transcript import MAX_EVENT_CHARS, chunks, lines


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


class _Fence:
    def __init__(self, label, nonce):
        self.begin = f"FRANKY_{label}_{nonce}_BEGIN"
        self.end = f"FRANKY_{label}_{nonce}_END"
        self.pending = ""
        self.active = False
        self.result = None
        self.oversized = False

    def feed(self, text):
        self.pending += text
        while self.pending:
            if not self.active:
                start = self.pending.find(self.begin)
                if start < 0:
                    self.pending = self.pending[-len(self.begin) :]
                    return
                self.pending = self.pending[start + len(self.begin) :]
                self.active = True
            end = self.pending.find(self.end)
            if end < 0:
                if len(self.pending) > MAX_EVENT_CHARS + len(self.end):
                    self.oversized = True
                    self.active = False
                    self.pending = self.pending[-len(self.begin) :]
                return
            payload, self.pending = self.pending[:end], self.pending[end + len(self.end) :]
            self.active = False
            if len(payload) > MAX_EVENT_CHARS:
                self.oversized = True
                continue
            try:
                parsed = json.loads(payload)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(parsed, dict):
                self.result = parsed


def scan_sentinel_json(output, label: str, nonce: str) -> dict | None:
    """Keep the last valid decoded payload, with raw text as the final fallback.

    Payloads and JSON events over 1 MiB fail closed; the full transcript remains on disk.
    """
    decoded = _Fence(label, nonce)
    for line in lines(output):
        if line is None:
            return None
        try:
            event = json.loads(line)
            for leaf in _string_leaves(event):
                decoded.feed(leaf + "\n")
        except (ValueError, TypeError, RecursionError):
            continue
    raw = _Fence(label, nonce)
    for chunk in chunks(output):
        raw.feed(chunk)
    if decoded.oversized or raw.oversized:
        warnings.warn(
            "franky: sentinel payload exceeds 1 MiB; refusing truncated result", RuntimeWarning
        )
        return None
    return raw.result if raw.result is not None else decoded.result
