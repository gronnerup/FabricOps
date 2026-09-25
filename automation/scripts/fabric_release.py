#!/usr/bin/env python
"""Fabric release - entry point.

A thin wrapper over the `fabricops` package, keeping the arguments the pipelines already
pass. See documentation/specs/E09 for what the new implementation adds: deployment policy
declared per layer in the recipe, a generated parameter overlay that leaves the committed
parameter file alone, native `semantic_model_binding` in place of the hand-written binding
step, dependency-ordered layers and a non-zero exit code when a layer fails.

    python automation/scripts/fabric_release.py --environment tst
    python automation/scripts/fabric_release.py --environment prd --layers store,model

The previous implementation is kept at automation/scripts/legacy/fabric_release.py.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fabricops.cli import main as fabricops_main  # noqa: E402  (after sys.path setup)

DEFAULT_RESOURCES = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


def _bool(value: object) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fabric release arguments")
    parser.add_argument("--environment", required=True, help="Environment to release, e.g. tst.")
    parser.add_argument("--layers", default=None, help="Comma-separated subset of layers. Default: all.")
    parser.add_argument("--item_types", default=None, help="Comma-separated Fabric item types. Overrides the recipe.")
    parser.add_argument("--repo_path", default=".", help="Repository root the layer directories are relative to.")
    parser.add_argument("--solution", default=os.environ.get("FABOPS_SOLUTION"))
    parser.add_argument("--resources", default=DEFAULT_RESOURCES)
    parser.add_argument("--parameter_file", default=None, help="Committed parameter file.")
    parser.add_argument("--extend_parameters", default="true", help="Render the generated parameter overlay.")
    parser.add_argument("--unpublish_items", default=None, help="Deprecated: declare `deploy.unpublish.skip` in the recipe.")
    parser.add_argument("--is_debug", default=False, help="Enable debug logging.")
    parser.add_argument("--tenant_id", default=os.environ.get("TENANT_ID"))
    parser.add_argument("--client_id", default=os.environ.get("CLIENT_ID"))
    parser.add_argument("--client_secret", default=os.environ.get("CLIENT_SECRET"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=os.environ.get("FABOPS_LOG_LEVEL"))
    parser.add_argument("--trace-file", default=os.environ.get("FABOPS_TRACE_FILE"))
    return parser.parse_args(argv)


def resolve_repo_path(repo_path: str) -> str:
    """Translate the old `--repo_path` meaning to the new one.

    It used to point at the solution folder, and the layer directory was that plus the
    lower-cased layer name. It now points at the repository root, and the layer directory
    comes from the recipe's `git.directory`, which already carries the `solution/` prefix.
    A pipeline still passing the old value would otherwise look in `solution/solution/...`.
    """
    path = pathlib.Path(repo_path)
    if path.name.lower() == "solution" and not (path / "solution").exists():
        print(
            f"note: --repo_path '{repo_path}' points at the solution folder. It now means the "
            f"repository root, so '{path.parent}' is being used instead.",
            file=sys.stderr,
        )
        return str(path.parent)
    return repo_path


def build_argv(args: argparse.Namespace) -> list[str]:
    argv: list[str] = ["--resources", args.resources]
    if args.solution:
        argv += ["--solution", args.solution]
    if args.log_level:
        argv += ["--log-level", args.log_level]
    elif _bool(args.is_debug):
        argv += ["--log-level", "debug"]
    if args.trace_file:
        argv += ["--trace-file", args.trace_file]
    if args.dry_run:
        argv.append("--dry-run")
    for flag, value in (
        ("--tenant-id", args.tenant_id),
        ("--client-id", args.client_id),
        ("--client-secret", args.client_secret),
    ):
        if value:
            argv += [flag, value]

    argv += ["release", "--environment", args.environment, "--repo-path", resolve_repo_path(args.repo_path)]
    if args.layers:
        argv += ["--layers", args.layers]
    if args.item_types:
        argv += ["--item-types", args.item_types]
    if args.parameter_file:
        argv += ["--parameter-file", args.parameter_file]
    argv += ["--extend-parameters", "true" if _bool(args.extend_parameters) else "false"]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.unpublish_items is not None:
        print(
            "note: --unpublish_items is deprecated. Declare `deploy.unpublish.skip` on the layer "
            "in the recipe instead, where it can also vary by environment.",
            file=sys.stderr,
        )
    return fabricops_main(build_argv(args))


if __name__ == "__main__":
    sys.exit(main())
