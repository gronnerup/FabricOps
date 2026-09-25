#!/usr/bin/env python
"""Fabric feature workspace maintenance - entry point.

A thin wrapper over the `fabricops` package, keeping the arguments the pipelines already
pass. See documentation/specs/E08 for what the new implementation adds: the
branched-workspace relation, ownership tags, the developer as admin of their own
workspace, and the shared git module.

    python automation/scripts/fabric_feature_maintainance.py --branch_name feature/peer/x
    python automation/scripts/fabric_feature_maintainance.py --action update
    python automation/scripts/fabric_feature_maintainance.py --action delete --branch_name $BRANCH

The previous implementation is kept at
automation/scripts/legacy/fabric_feature_maintainance.py.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fabricops.cli import main as fabricops_main  # noqa: E402  (after sys.path setup)

DEFAULT_RESOURCES = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


def default_branch() -> str | None:
    if os.environ.get("GITHUB_REF_NAME"):
        return os.environ["GITHUB_REF_NAME"]
    source_branch = os.environ.get("BUILD_SOURCEBRANCH")
    return source_branch.removeprefix("refs/heads/") if source_branch else None


def default_developer() -> str | None:
    actor = os.environ.get("GITHUB_ACTOR") or os.environ.get("BUILD_REQUESTEDFOREMAIL")
    return actor or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fabric feature maintenance arguments")
    parser.add_argument("--action", default="create", help="create, update or delete. Default: create.")
    parser.add_argument("--branch_name", default=default_branch(), help="Feature branch to operate on.")
    parser.add_argument("--solution", default=os.environ.get("FABOPS_SOLUTION"))
    parser.add_argument("--layers", default=None, help="Comma-separated subset of layers. Optional.")
    parser.add_argument("--resources", default=DEFAULT_RESOURCES)
    parser.add_argument("--developer", default=default_developer(), help="Owner slug for tags and recipe overlays.")
    parser.add_argument(
        "--developer_object_id",
        # Only ever an explicit value. Neither CI system has an Entra object id to offer:
        # GitHub's GITHUB_ACTOR_ID is GitHub's own numeric user id, and Azure DevOps'
        # BUILD_REQUESTEDFORID is an *Azure DevOps* identity id - GUID-shaped, so it passes a
        # shape check, and then rejected by Fabric. Defaulting to either meant every feature
        # run attempted an ACL we already knew would fail, once per layer. Unset, the recipe's
        # own permissions are all that is applied, which is what they are for.
        default=os.environ.get("FABOPS_DEVELOPER_OBJECT_ID"),
        help="Entra object id of the developer, to grant them admin on their own workspace. "
             "Optional: without it, only the recipe's permissions are applied.",
    )
    parser.add_argument("--base_environment", default="dev", help="Environment the branch relates to. Default: dev.")
    parser.add_argument("--no_relation", action="store_true", help="Skip the branched-workspace relation.")
    parser.add_argument("--tenant_id", default=os.environ.get("TENANT_ID"))
    parser.add_argument("--client_id", default=os.environ.get("CLIENT_ID"))
    parser.add_argument("--client_secret", default=os.environ.get("CLIENT_SECRET"))
    parser.add_argument("--github_pat", default=os.environ.get("GITHUB_PAT"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=os.environ.get("FABOPS_LOG_LEVEL"))
    parser.add_argument("--trace-file", default=os.environ.get("FABOPS_TRACE_FILE"))
    return parser.parse_args(argv)


def build_argv(args: argparse.Namespace) -> list[str]:
    argv: list[str] = ["--resources", args.resources]
    if args.solution:
        argv += ["--solution", args.solution]
    if args.log_level:
        argv += ["--log-level", args.log_level]
    if args.trace_file:
        argv += ["--trace-file", args.trace_file]
    if args.dry_run:
        argv.append("--dry-run")
    for flag, value in (
        ("--tenant-id", args.tenant_id),
        ("--client-id", args.client_id),
        ("--client-secret", args.client_secret),
        ("--github-pat", args.github_pat),
    ):
        if value:
            argv += [flag, value]

    argv += ["feature", str(args.action).lower()]
    if args.branch_name:
        argv += ["--branch", args.branch_name]
    if args.developer:
        argv += ["--developer", args.developer]
    if args.developer_object_id:
        argv += ["--developer-object-id", args.developer_object_id]
    if args.layers:
        argv += ["--layers", args.layers]
    if args.base_environment:
        argv += ["--base-environment", args.base_environment]
    if args.no_relation:
        argv.append("--no-relation")
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if str(args.action).lower() not in ("create", "update", "delete"):
        print(f"Unknown action '{args.action}'. Please use create, update or delete.", file=sys.stderr)
        return 2
    return fabricops_main(build_argv(args))


if __name__ == "__main__":
    sys.exit(main())
