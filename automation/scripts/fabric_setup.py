#!/usr/bin/env python
"""Fabric IaC setup - entry point.

A thin wrapper over the `fabricops` package so existing pipelines keep working with the
arguments they already pass. Everything of substance lives in the package:
recipe resolution (E01/E02), planning and execution (E03), logging and redaction (E04).

    python automation/scripts/fabric_setup.py --environment dev
    python automation/scripts/fabric_setup.py --environment dev --dry-run --log-level debug
    python automation/scripts/fabric_setup.py --environment dev --solution my-data-platform
    python automation/scripts/fabric_setup.py --environment dev --action delete

The previous implementation is kept at automation/scripts/legacy/fabric_setup.py.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fabricops.cli import main as fabricops_main  # noqa: E402  (after sys.path setup)

DEFAULT_ENVIRONMENT = "dev"
DEFAULT_RESOURCES = str(pathlib.Path(__file__).resolve().parents[1] / "resources")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fabric IaC setup arguments")
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT, help="Environment to set up. Default: dev.")
    parser.add_argument("--action", default="create", help="create or delete. Default: create.")
    parser.add_argument("--solution", default=os.environ.get("FABOPS_SOLUTION"), help="Solution name. Optional.")
    parser.add_argument("--layers", default=None, help="Comma-separated subset of layers. Optional.")
    parser.add_argument("--resources", default=DEFAULT_RESOURCES, help="Recipe root.")
    parser.add_argument("--tenant_id", default=os.environ.get("TENANT_ID"))
    parser.add_argument("--client_id", default=os.environ.get("CLIENT_ID"))
    parser.add_argument("--client_secret", default=os.environ.get("CLIENT_SECRET"))
    parser.add_argument("--github_pat", default=os.environ.get("GITHUB_PAT"))
    parser.add_argument("--dry-run", action="store_true", help="Plan only: read, report writes, change nothing.")
    parser.add_argument("--log-level", default=os.environ.get("FABOPS_LOG_LEVEL"), help="info (default) … debug, trace.")
    parser.add_argument("--trace-file", default=os.environ.get("FABOPS_TRACE_FILE"), help="JSONL trace of every command.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first failing action.")
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

    action = str(args.action).lower()
    argv += ["setup", "--environment", args.environment, "--action", action]
    if args.layers:
        argv += ["--layers", args.layers]
    if args.stop_on_error:
        argv.append("--stop-on-error")
    if action == "delete" and not args.dry_run:
        # The package asks for --confirm interactively; a pipeline that was told to
        # delete has already made that decision, so pass it through.
        argv += ["--confirm", f"{args.solution or 'default'}/{args.environment}"]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if str(args.action).lower() not in ("create", "delete"):
        print(f"Invalid action specified: {args.action}. Supported values are create/delete.", file=sys.stderr)
        return 2
    return fabricops_main(build_argv(args))


if __name__ == "__main__":
    sys.exit(main())
