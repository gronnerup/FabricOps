"""Which recipe files apply to this run (documentation/specs/E02).

Resolution is a documented, ordered search - first match wins - and the resolved paths are
printed in the run header, so "which file did it actually read?" is never a mystery.

Platform, for `--solution my-data-platform --environment dev`:

    1  resources/solutions/my-data-platform/platform.{yml,yaml,json}     (+ platform.dev.*)
    2  resources/environments/my-data-platform.{yml,yaml,json}           (+ .dev.*)
    3  resources/environments/infrastructure.{yml,yaml,json}             (+ .dev.*)   <- legacy default
    4  the only entry under resources/solutions/, when there is exactly one and
       no solution was named. Two or more is an error that lists them.

Feature, for `--solution my-data-platform --developer peer`, overlaid in order:

    feature.*              the shared feature recipe
    feature.<group>.*      optional, selected by the branch segment (feature/<group>/<topic>)
    feature.<developer>.*  optional, personal overrides

A *group* recipe lets one branch convention describe a whole slice of the platform:
`feature/backend/add-orders` picks up `feature.backend.yml`, which declares the layers
that slice needs. A segment that is not a group is matched against layer names instead
(the first-generation convention), and a segment that is neither provisions everything.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from dataclasses import dataclass, field

from ..errors import RecipeError
from .loader import SUPPORTED_SUFFIXES

DEFAULT_RESOURCES = pathlib.Path("automation/resources")
LEGACY_BASENAME = "infrastructure"


@dataclass(frozen=True)
class ResolvedRecipe:
    """The files that make up one recipe, in overlay order."""

    kind: str
    solution: str | None
    environment: str | None
    base: pathlib.Path
    overlays: tuple[pathlib.Path, ...] = ()
    order: int = 0
    searched: tuple[pathlib.Path, ...] = field(default=(), repr=False)

    @property
    def files(self) -> tuple[pathlib.Path, ...]:
        return (self.base, *self.overlays)

    def describe(self) -> str:
        return " + ".join(str(path) for path in self.files)


def _first_existing(candidates: list[pathlib.Path]) -> pathlib.Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _with_suffixes(directory: pathlib.Path, stem: str) -> list[pathlib.Path]:
    return [directory / f"{stem}{suffix}" for suffix in SUPPORTED_SUFFIXES]


def resolve_platform(
    resources: str | pathlib.Path = DEFAULT_RESOURCES,
    *,
    solution: str | None = None,
    environment: str | None = None,
) -> ResolvedRecipe:
    resources = pathlib.Path(resources)
    searched: list[pathlib.Path] = []
    attempts: list[tuple[int, pathlib.Path, str]] = []

    if solution:
        attempts.append((1, resources / "solutions" / solution, "platform"))
        attempts.append((2, resources / "environments", solution))
    else:
        attempts.append((1, resources / "solutions" / "default", "platform"))
    attempts.append((3, resources / "environments", LEGACY_BASENAME))

    for order, directory, stem in attempts:
        candidates = _with_suffixes(directory, stem)
        searched.extend(candidates)
        base = _first_existing(candidates)
        if not base:
            continue
        overlays: tuple[pathlib.Path, ...] = ()
        if environment:
            overlay_candidates = _with_suffixes(directory, f"{stem}.{environment}")
            searched.extend(overlay_candidates)
            overlay = _first_existing(overlay_candidates)
            overlays = (overlay,) if overlay else ()
        return ResolvedRecipe(
            kind="Platform",
            solution=solution,
            environment=environment,
            base=base,
            overlays=overlays,
            order=order,
            searched=tuple(searched),
        )

    # No --solution, no `default` folder, no legacy file. If the repository defines exactly
    # one solution, that is what "the recipe" means here - a fresh clone of a repository
    # that ships only `solutions/demo/` must work without every command naming it. Two or
    # more is a real ambiguity, and picking one silently would be worse than stopping.
    if not solution:
        only = _lone_solution(resources)
        if only:
            return resolve_platform(resources, solution=only, environment=environment)

    raise RecipeError(
        f"no platform recipe found for solution '{solution or 'default'}'",
        hint="Searched:\n    " + "\n    ".join(str(path) for path in searched),
    )


def _lone_solution(resources: pathlib.Path) -> str | None:
    """The one solution under resources/solutions/ when nothing was named, else None.

    Two or more with none named is an error that lists them: guessing would be worse
    than stopping.
    """
    defined = [entry for entry in list_solutions(resources) if entry["layout"] == "solutions"]
    if len(defined) == 1:
        return str(defined[0]["name"])
    if len(defined) > 1:
        names = ", ".join(str(entry["name"]) for entry in defined)
        raise RecipeError(
            f"this repository defines {len(defined)} solutions and none is named",
            hint=f"Pass --solution <name> (or set FABOPS_SOLUTION). Defined: {names}",
        )
    return None


def feature_directories(
    resources: str | pathlib.Path = DEFAULT_RESOURCES, solution: str | None = None
) -> list[pathlib.Path]:
    resources = pathlib.Path(resources)
    directories = [resources / "solutions" / (solution or "default")]
    directories.append(resources / "environments")
    if not solution:
        # Same last resort as the platform recipe: the one solution the repository defines.
        # After the legacy folder, so an existing clone keeps reading environments/feature.*.
        only = _lone_solution(resources)
        if only:
            directories.append(resources / "solutions" / only)
    return directories


def feature_group_exists(
    resources: str | pathlib.Path = DEFAULT_RESOURCES,
    *,
    solution: str | None = None,
    group: str | None = None,
) -> bool:
    """True when `feature.<group>.*` exists - i.e. the branch segment names a group."""
    if not group:
        return False
    return any(
        _first_existing(_with_suffixes(directory, f"feature.{group}"))
        for directory in feature_directories(resources, solution)
    )


def resolve_feature(
    resources: str | pathlib.Path = DEFAULT_RESOURCES,
    *,
    solution: str | None = None,
    developer: str | None = None,
    group: str | None = None,
) -> ResolvedRecipe:
    resources = pathlib.Path(resources)
    searched: list[pathlib.Path] = []
    directories = feature_directories(resources, solution)

    base: pathlib.Path | None = None
    overlays: list[pathlib.Path] = []
    order = 0

    for index, directory in enumerate(directories, start=1):
        candidates = _with_suffixes(directory, "feature")
        searched.extend(candidates)
        found = _first_existing(candidates)
        if not found:
            continue
        base, order = found, index
        for stem in filter(None, (f"feature.{group}" if group else None,
                                  f"feature.{developer}" if developer else None)):
            overlay_candidates = _with_suffixes(directory, stem)
            searched.extend(overlay_candidates)
            overlay = _first_existing(overlay_candidates)
            if overlay:
                overlays.append(overlay)
        break

    if base is None:
        raise RecipeError(
            f"no feature recipe found for solution '{solution or 'default'}'",
            hint="Searched:\n    " + "\n    ".join(str(path) for path in searched),
        )

    return ResolvedRecipe(
        kind="Feature",
        solution=solution,
        environment=None,
        base=base,
        overlays=tuple(overlays),
        order=order,
        searched=tuple(searched),
    )


def list_solutions(resources: str | pathlib.Path = DEFAULT_RESOURCES) -> list[dict[str, object]]:
    """Every solution the repo defines, in either layout."""
    resources = pathlib.Path(resources)
    found: dict[str, dict[str, object]] = {}

    solutions_dir = resources / "solutions"
    if solutions_dir.is_dir():
        for entry in sorted(solutions_dir.iterdir()):
            if entry.is_dir() and _first_existing(_with_suffixes(entry, "platform")):
                found[entry.name] = {"name": entry.name, "layout": "solutions", "path": entry, "environments": _environments(entry, "platform")}

    environments_dir = resources / "environments"
    if environments_dir.is_dir():
        for entry in sorted(environments_dir.iterdir()):
            if entry.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            stem = entry.stem
            if "." in stem:  # an environment overlay, e.g. infrastructure.dev
                continue
            name = "default" if stem == LEGACY_BASENAME else stem
            if name == "feature" or name in found:
                continue
            found[name] = {
                "name": name,
                "layout": "environments",
                "path": entry,
                "environments": _environments(environments_dir, stem),
            }

    return list(found.values())


def _environments(directory: pathlib.Path, stem: str) -> list[str]:
    pattern = re.compile(rf"^{re.escape(stem)}\.([A-Za-z0-9_-]+)$")
    environments: list[str] = []
    for entry in sorted(directory.glob(f"{stem}.*")):
        if entry.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        match = pattern.match(entry.stem)
        if match:
            environments.append(match.group(1))
    return environments


def resolve_developer(explicit: str | None = None) -> str | None:
    """Identify the developer for a feature run, then normalise it for use in names.

    Order: --developer, FABOPS_DEVELOPER, GITHUB_ACTOR, BUILD_REQUESTEDFOREMAIL,
    git config user.email.
    """
    candidates = [
        explicit,
        os.environ.get("FABOPS_DEVELOPER"),
        os.environ.get("GITHUB_ACTOR"),
        os.environ.get("BUILD_REQUESTEDFOREMAIL"),
    ]
    for candidate in candidates:
        if candidate:
            return sanitise_developer(candidate)

    try:
        email = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True, check=False, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git absent
        email = ""
    return sanitise_developer(email) if email else None


def sanitise_developer(value: str) -> str:
    """`Peer.Gronnerup@example.com` -> `peer-gronnerup`."""
    local = value.split("@")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")
    return slug or "developer"
