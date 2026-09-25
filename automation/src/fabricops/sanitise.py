"""The gate that stands between the internal repository and the public one.

Two layers, in this order (documentation/specs/E11):

1. **Clean by construction** - public recipes carry placeholders, and tenant-specific
   values live in a solution folder that is not on the export allowlist. Nothing needs
   wiping.
2. **A gate that fails the sync** - this module scans the export tree for denied paths,
   denied literal values and secret-shaped strings, so a mistake is caught before it is
   published rather than after.

What automation cannot decide: whether a *new* file belongs in public, and whether
something sensitive hides in prose. So `include` is an allowlist and the export prints
what it copied for a human to eyeball.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import RecipeError

GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("shared access signature", re.compile(r"(?i)\bsig=[A-Za-z0-9%/+=]{16,}")),
    # A *literal* assigned to a secret-shaped name. Deliberately not matching
    # `client_secret = args.client_secret`, which is how the code passes credentials
    # around - only quoted values that are not obviously placeholders.
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(client_?secret|servicePrincipalSecret|credentialDetails\.key|password|pwd|api_?key|access_?token)\b"
            r"\s*[:=]\s*"
            r"[\"'](?!\*{2,})(?!<)(?![{$])(?!your[-_ ])(?!x{4,})(?!redacted)(?!changeme)(?!placeholder)"
            r"[^\"']{8,}[\"']"
        ),
    ),
    ("connection string password", re.compile(r"(?i)\b(?:pwd|password)=(?![{$*<])[^\s;\"']{6,}")),
)

TEXT_SUFFIXES = {
    ".py", ".md", ".yml", ".yaml", ".json", ".txt", ".ps1", ".sh", ".sql", ".tmdl", ".pbir",
    ".pbism", ".cfg", ".ini", ".toml", ".csproj", ".sqlproj", ".gitignore", "",
}


@dataclass
class Finding:
    path: pathlib.Path
    line: int
    kind: str
    detail: str
    #: Reported, but does not fail the gate. Used where the allowlist already prevents the
    #: leak, so the only thing left to say is "this exists on your machine".
    informational: bool = False

    def __str__(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{location}  {self.kind}: {self.detail}"


@dataclass
class ExportPolicy:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)
    deny_values: list[str] = field(default_factory=list)
    allow_guids: list[str] = field(default_factory=list)
    secret_exceptions: list[str] = field(default_factory=list)
    path: pathlib.Path | None = None

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "ExportPolicy":
        from .recipe import loader

        path = pathlib.Path(path)
        if not path.exists():
            raise RecipeError(
                f"export policy not found: {path}",
                hint="Create automation/resources/export.yml, or pass --policy.",
            )
        data = loader.load_file(path)
        return cls(
            include=list(data.get("include") or []),
            exclude=list(data.get("exclude") or []),
            deny_paths=list(data.get("deny_paths") or []),
            deny_values=list(data.get("deny_values") or []),
            allow_guids=[value.lower() for value in (data.get("allow_guids") or [])],
            secret_exceptions=list(data.get("secret_exceptions") or []),
            path=path,
        )

    def matches_include(self, relative: str) -> bool:
        return any(_match(relative, pattern) for pattern in self.include)

    def matches_exclude(self, relative: str) -> bool:
        return any(_match(relative, pattern) for pattern in self.exclude)

    def secrets_expected_in(self, relative: str) -> bool:
        """True where secret-shaped strings are expected (fixtures, prose, the patterns)."""
        return any(_match(relative, pattern) for pattern in self.secret_exceptions)


def _match(relative: str, pattern: str) -> bool:
    if fnmatch.fnmatch(relative, pattern):
        return True
    # `automation/src/**` should also match `automation/src/x/y.py`
    if pattern.endswith("/**") and relative.startswith(pattern[:-3] + "/"):
        return True
    return False


def exportable_files(root: str | pathlib.Path, policy: ExportPolicy) -> list[pathlib.Path]:
    """Every file the allowlist admits, relative to `root`."""
    root = pathlib.Path(root)
    found: list[pathlib.Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or "/.venv/" in f"/{relative}" or relative.startswith(".venv/"):
            continue
        if policy.matches_exclude(relative) or not policy.matches_include(relative):
            continue
        found.append(path)
    return found


def scan(root: str | pathlib.Path, policy: ExportPolicy, *, strict: bool = False) -> list[Finding]:
    """Findings for the exportable tree under `root`."""
    root = pathlib.Path(root)
    findings: list[Finding] = []

    exportable = {path.relative_to(root) for path in exportable_files(root, policy)}
    for denied in policy.deny_paths:
        for path in root.glob(denied):
            if not path.exists():
                continue
            relative = path.relative_to(root)
            if relative not in exportable:
                # The allowlist already keeps this out of the export, so its presence on
                # disk is not a finding. Saying otherwise makes the gate permanently red on
                # any machine that holds a real credentials file - and a check that is
                # always red is a check nobody reads.
                findings.append(Finding(relative, 0, "denied path (not exportable)", denied, informational=True))
                continue
            findings.append(Finding(relative, 0, "denied path", denied))

    for path in exportable_files(root, policy):
        relative = path.relative_to(root)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(
            _scan_text(
                relative,
                text,
                policy,
                strict=strict,
                check_patterns=not policy.secrets_expected_in(relative.as_posix()),
            )
        )

    return findings


ALLOW_MARKER = "sanitise: allow"


def _scan_text(
    relative: pathlib.Path,
    text: str,
    policy: ExportPolicy,
    *,
    strict: bool,
    check_patterns: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue  # an explicitly accepted line, e.g. a documented example
        for value in policy.deny_values:
            if value and value in line:
                findings.append(Finding(relative, number, "tenant value", value))
        if not check_patterns:
            continue
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(Finding(relative, number, "possible secret", f"{label}: {match.group(0)[:40]}…"))
        if strict:
            for guid in GUID.findall(line):
                if guid.lower() not in policy.allow_guids:
                    findings.append(Finding(relative, number, "unlisted guid", guid))
    return findings


def export(root: str | pathlib.Path, target: str | pathlib.Path, policy: ExportPolicy) -> list[pathlib.Path]:
    """Copy the exportable tree to `target`, returning what was copied."""
    root, target = pathlib.Path(root), pathlib.Path(target)
    copied: list[pathlib.Path] = []
    for path in exportable_files(root, policy):
        relative = path.relative_to(root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(relative)
    return copied


def summarise(findings: Iterable[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return counts
