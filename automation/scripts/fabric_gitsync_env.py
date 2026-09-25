#!/usr/bin/env python
"""Bring an environment's workspaces back in step with its branch - entry point.

The step a merge into the branch that dev is connected to should run. Fabric never pulls on
its own: git integration needs an explicit `updateFromGit`, so a branch moving forward
leaves the workspaces behind until something asks them to catch up.

A thin wrapper over `fabricops setup --only git`, which converges the git state of every
layer and nothing else - no roles, no properties, no connections. Workspaces are included
because a git action needs their ids, and they are a read when the workspace exists.

    python automation/scripts/fabric_gitsync_env.py --environment dev
    python automation/scripts/fabric_gitsync_env.py --environment dev --dry-run

What it gains over the previous implementation, which called the Fabric CLI directly:
`gitConnectionState` is handled as the three states Fabric actually reports, an item whose
committed references cannot resolve defers with the command that fixes it instead of
failing on `DiscoverDependenciesFailed`, and conflicts follow the recipe's
`conflict_resolution` policy rather than always preferring the remote.

The previous implementation is kept at automation/scripts/legacy/fabric_gitsync_env.py.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fabricops.cli import main as fabricops_main  # noqa: E402  (after sys.path setup)

DEFAULT_RESOURCES = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronise an environment's workspaces with its branch")
    parser.add_argument("--environment", default=os.environ.get("FABOPS_ENVIRONMENT", "dev"))
    parser.add_argument("--solution", default=os.environ.get("FABOPS_SOLUTION"))
    parser.add_argument("--layers", default=None, help="Comma-separated subset of layers. Optional.")
    parser.add_argument(
        "--changed_since",
        default=os.environ.get("FABOPS_CHANGED_SINCE") or None,
        help="Only sync layers whose folder changed since this git ref, e.g. HEAD~1 on a merge. "
             "Falls back to all layers when the ref cannot be diffed.",
    )
    parser.add_argument("--resources", default=DEFAULT_RESOURCES)
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

    argv += ["setup", "--environment", args.environment, "--only", "git"]
    if args.layers:
        argv += ["--layers", args.layers]
    if args.changed_since:
        argv += ["--changed-since", args.changed_since]
    return argv


def main(argv: list[str] | None = None) -> int:
    return fabricops_main(build_argv(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
