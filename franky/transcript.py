"""Bounded transcript storage and streaming redaction, using only redacted disk bytes."""

from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import warnings

from .config import REDACT_TOKEN

CHUNK_SIZE = 32768
# ponytail: JSON events/payloads support 1 MiB; use a streaming JSON parser if engines exceed it.
MAX_EVENT_CHARS = 1024 * 1024


class Transcript:
    """Own an anonymous 0600 spool until the CLI persists it as a run log."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._file = None if path else tempfile.TemporaryFile(mode="w+b")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._file is not None:
            self._file.close()

    def write(self, text: str):
        self._file.write(text.encode("utf-8"))

    @contextmanager
    def _reader(self):
        if self.path is not None:
            with self.path.open("rb") as source:
                yield source
        else:
            self._file.flush()
            position = self._file.tell()
            self._file.seek(0)
            try:
                yield self._file
            finally:
                self._file.seek(position)

    def chunks(self):
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with self._reader() as source:
            while chunk := source.read(CHUNK_SIZE):
                yield decoder.decode(chunk)
            yield decoder.decode(b"", final=True)

    def tail(self, count: int):
        with self._reader() as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - count * 4))
            return source.read(count * 4).decode("utf-8", errors="replace")[-count:]

    def persist(self, path: Path, suffix: str = ""):
        with open_secure(path) as target:
            for chunk in self.chunks():
                target.write(chunk)
            target.write(suffix)
        self.close()
        self._file = None
        self.path = path


def open_secure(path):
    return os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
    )


def chunks(output):
    if isinstance(output, Transcript):
        yield from output.chunks()
    else:
        for start in range(0, len(output), CHUNK_SIZE):
            yield output[start : start + CHUNK_SIZE]


def lines(output):
    """Yield bounded lines; None explicitly marks an unsupported oversized event."""
    pending = ""
    dropping = False
    for chunk in chunks(output):
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith(("\n", "\r"))
            if not dropping:
                pending += part
                if len(pending) > MAX_EVENT_CHARS:
                    warnings.warn(
                        "franky: event exceeds 1 MiB parser limit; full text remains in the log",
                        RuntimeWarning,
                    )
                    pending = ""
                    dropping = True
                    yield None
                elif ended:
                    yield pending
                    pending = ""
            if ended:
                dropping = False
    if pending:
        yield pending


class Redactor:
    """Incremental longest-first str.replace, including secrets spanning chunks or newlines."""

    def __init__(self, secrets):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.pending = [""] * len(self.secrets)

    def feed(self, text, *, final=False):
        for index, secret in enumerate(self.secrets):
            value = self.pending[index] + text
            cut = len(value) if final else max(0, len(value) - len(secret) + 1)
            position = 0
            parts = []
            while position < cut:
                match = value.find(secret, position)
                if match < 0 or match >= cut:
                    parts.append(value[position:cut])
                    position = cut
                    break
                parts.extend((value[position:match], REDACT_TOKEN))
                position = match + len(secret)
            self.pending[index] = value[position:]
            text = "".join(parts)
        return text
