"""Structural secret handling.

Redaction is *by construction*: a value that is secret is wrapped in `Secret` when it
enters the process, and only `reveal()` puts it on a command line. The logger renders the
same argv it executed, with every `Secret` shown as `***`.

The regex denylist is the second net, for output we do not control (stdout/stderr of
`fab`, error payloads, definitions).
"""

from __future__ import annotations

import re
from typing import Iterable

MASK = "***"

# Values shorter than this are not scrubbed from text: masking "s" everywhere would
# corrupt unrelated output (and no real credential is that short). The Secret object
# itself still renders as *** regardless of length.
MIN_SCRUB_LENGTH = 6

# Values registered here are scrubbed from any text we log, wherever they appear.
_known_values: set[str] = set()

# Second net: patterns for output we did not build ourselves.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_pat_fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("sas", re.compile(r"(?i)\bsig=[A-Za-z0-9%/+=]{10,}")),
    # key=value shapes, including the fab -P parameter style.
    #
    # Split in two on purpose. Some of these words only ever name a secret; others are
    # ordinary English. `Failed to get access token: Something went wrong` is a sentence,
    # and redacting it to `token: ***` destroyed the one error message that would have
    # told us the session had expired.
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b("
            r"client_?secret|servicePrincipalSecret|credentialDetails\.key|"
            r"password|passwd|pwd|access_?token|refresh_?token|api_?key"
            r")\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&)\]}]+)"
        ),
    ),
    (
        # Ambiguous words: only in an unmistakable assignment - `=`, or a quoted value as
        # JSON writes it - and only when what follows is long enough to be a credential.
        "assigned_secret_weak",
        re.compile(
            r"(?i)\b(token|pat|key|secret)\b(\s*=\s*|\"?\s*:\s*(?=[\"']))"
            r"(\"[^\"]{8,}\"|'[^']{8,}'|[^\s,;&)\]}]{8,})"
        ),
    ),
)


class MaskedValue:
    """A composite argv element that carries both a real and a masked rendering.

    The Fabric CLI takes `-P key=value,key=value` as a single argument, so a secret ends
    up *inside* a larger string. Stringifying the Secret at that point would send `***`
    to the CLI; revealing it would put the secret into whatever we log. Keeping both
    renderings side by side means the process gets the real value and every log sink gets
    the masked one - structurally, not by pattern matching.
    """

    __slots__ = ("_real", "_masked")

    def __init__(self, real: str, masked: str):
        self._real = real
        self._masked = masked

    def reveal(self) -> str:
        return self._real

    def __str__(self) -> str:
        return self._masked

    def __repr__(self) -> str:
        return f"MaskedValue({self._masked})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MaskedValue) and other._real == self._real

    def __hash__(self) -> int:
        return hash(("MaskedValue", self._real))


class Secret:
    """A value that must never be logged.

    Str/repr render as `***`, so an accidental f-string or print cannot leak it. Only
    `reveal()` returns the real value, and registering it also scrubs it from output.
    """

    __slots__ = ("_value", "_label")

    def __init__(self, value: str | None, label: str = "secret"):
        self._value = "" if value is None else str(value)
        self._label = label
        if len(self._value) >= MIN_SCRUB_LENGTH:
            _known_values.add(self._value)

    def reveal(self) -> str:
        return self._value

    @property
    def label(self) -> str:
        return self._label

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:  # so secrets can live in sets/dicts safely
        return hash(("Secret", self._value))

    def __str__(self) -> str:
        return MASK

    def __repr__(self) -> str:
        return f"Secret({self._label}={MASK})"


def register_secret(value: str | Secret | None) -> None:
    """Scrub `value` from anything we log, even when it arrives as a plain string."""
    if isinstance(value, Secret):
        value = value.reveal()
    if value and isinstance(value, str) and len(value) >= MIN_SCRUB_LENGTH:
        _known_values.add(value)


def registered_count() -> int:
    return len(_known_values)


def redact(text: str, *, enabled: bool = True) -> str:
    """Mask known secret values and secret-shaped substrings in `text`."""
    if not text or not enabled:
        return text
    out = text
    for value in sorted(_known_values, key=len, reverse=True):
        if value in out:
            out = out.replace(value, MASK)
    for _name, pattern in _PATTERNS:
        if pattern.groups >= 3:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
        else:
            out = pattern.sub(MASK, out)
    return out


def render_argv(argv: Iterable[object], *, enabled: bool = True) -> str:
    """Render an argv list as a copy-pasteable, redacted command string."""
    parts: list[str] = []
    for item in argv:
        if isinstance(item, Secret):
            parts.append(MASK)
            continue
        text = str(item)  # MaskedValue renders masked here, by design
        text = redact(text, enabled=enabled)
        parts.append(f"'{text}'" if (" " in text or "\t" in text) else text)
    return " ".join(parts)


def to_process_argv(argv: Iterable[object]) -> list[str]:
    """Materialise an argv list for `subprocess` (secrets revealed, nothing quoted)."""
    return [item.reveal() if hasattr(item, "reveal") else str(item) for item in argv]
