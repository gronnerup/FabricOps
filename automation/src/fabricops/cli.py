"""The `fabricops` command line.

Today this exposes the recipe layer; provisioning verbs (`setup`, `plan`, `feature`,
`release`) land as the engine specs are implemented. Every command shares the same global
flags, exit codes and logging behaviour.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Sequence

from . import __version__, recipe, release as release_module, sanitise as sanitise_module
from .engine import Manifest, RunContext, build_plan, execute, summarise
from .engine.connections import Credentials
from .engine.feature import build_feature_plan
from .engine.tags import TagRegistry, sync_registry
from .errors import ExitCode, FabricOpsError
from .fabric.cli import FabricCli
from .obs.logging import Level, RunLog
from .obs.redaction import Secret

DEFAULT_RESOURCES = "automation/resources"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fabricops", description="Declarative automation for Microsoft Fabric.")
    parser.add_argument("--version", action="version", version=f"fabricops {__version__}")
    parser.add_argument("--resources", default=DEFAULT_RESOURCES, help=f"Recipe root. Default: {DEFAULT_RESOURCES}")
    parser.add_argument("--solution", default=None, help="Solution name (see documentation/specs/E02).")
    parser.add_argument("--log-level", default=None, help="off, error, warn, info, debug or trace.")
    parser.add_argument("--trace-file", default=None, help="Write a JSONL trace of every command to this path.")
    parser.add_argument("--no-redact", action="store_true", help="Disable secret masking (never use in CI).")
    parser.add_argument("--dry-run", action="store_true", help="Plan only: perform reads, report writes, change nothing.")
    parser.add_argument("--tenant-id", default=os.environ.get("TENANT_ID"), help="Entra tenant id. Default: $TENANT_ID.")
    parser.add_argument("--client-id", default=os.environ.get("CLIENT_ID"), help="Service principal client id. Default: $CLIENT_ID.")
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("CLIENT_SECRET"),
        help="Service principal secret. Default: $CLIENT_SECRET. Prefer FAB_SPN_* env vars in pipelines.",
    )
    parser.add_argument(
        "--github-pat",
        default=os.environ.get("GITHUB_PAT"),
        help="GitHub personal access token, for a GitHub source control connection. Default: $GITHUB_PAT.",
    )
    parser.add_argument("--manifest-root", default=".fabricops/runs", help="Where run manifests are written.")
    parser.add_argument(
        "--credentials-dir",
        default="automation/credentials",
        help="Local credentials folder, read only when a value is not already supplied. Local runs only.",
    )
    parser.add_argument(
        "--no-credentials-file",
        action="store_true",
        help="Ignore the local credentials folder entirely.",
    )

    # The same flags are attached to every subcommand, so both orders work:
    #   fabricops --dry-run setup --environment dev
    #   fabricops setup --environment dev --dry-run
    # SUPPRESS keeps an unspecified subcommand copy from overwriting the global value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--resources", default=argparse.SUPPRESS)
    common.add_argument("--solution", default=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS)
    common.add_argument("--trace-file", default=argparse.SUPPRESS)
    common.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--tenant-id", default=argparse.SUPPRESS)
    common.add_argument("--client-id", default=argparse.SUPPRESS)
    common.add_argument("--client-secret", default=argparse.SUPPRESS)
    common.add_argument("--github-pat", default=argparse.SUPPRESS)
    common.add_argument("--manifest-root", default=argparse.SUPPRESS)
    common.add_argument("--credentials-dir", default=argparse.SUPPRESS)
    common.add_argument("--no-credentials-file", action="store_true", default=argparse.SUPPRESS)

    # `dest` here must not collide with any subcommand's own flag: `feature --group` used
    # to write over it, so `fabricops feature create` dispatched to nothing at all.
    subparsers = parser.add_subparsers(dest="group", required=True)

    recipe_parser = subparsers.add_parser("recipe", help="Work with recipe files.", parents=[common])
    recipe_sub = recipe_parser.add_subparsers(dest="command", required=True)

    validate_parser = recipe_sub.add_parser("validate", help="Validate a recipe (or every recipe in the repo).", parents=[common])
    validate_parser.add_argument("--environment", default=None)
    validate_parser.add_argument("--all", action="store_true", help="Validate every solution and environment found.")

    render_parser = recipe_sub.add_parser("render", help="Print the merged, token-substituted recipe.", parents=[common])
    render_parser.add_argument("--environment", default=None)
    render_parser.add_argument("--format", default="yaml", choices=("yaml", "json"))
    render_parser.add_argument("--kind", default="platform", choices=("platform", "feature"))
    render_parser.add_argument("--developer", default=None, help="Feature recipes only.")

    plan_parser = subparsers.add_parser("plan", help="Show the ordered actions a setup would run.", parents=[common])
    plan_parser.add_argument("--environment", default=None)
    plan_parser.add_argument("--layers", default=None, help="Comma-separated subset of layers.")
    plan_parser.add_argument(
        "--sequence",
        action="store_true",
        help="Show the literal execution order with dependencies, instead of grouping by layer.",
    )
    plan_parser.add_argument(
        "--check",
        action="store_true",
        help="Also probe the tenant and report drift. Exits 3 when the tenant differs from the recipe.",
    )
    plan_parser.add_argument("--repo-path", default=".", help="Repository root, for `definition.from` paths.")

    setup_parser = subparsers.add_parser("setup", help="Provision (or tear down) a solution environment.", parents=[common])
    setup_parser.add_argument("--environment", required=True)
    setup_parser.add_argument("--action", default="create", choices=("create", "delete"))
    setup_parser.add_argument("--layers", default=None, help="Comma-separated subset of layers.")
    setup_parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first failing action.")
    setup_parser.add_argument("--confirm", default=None, help="Required for delete: <solution>/<environment>.")
    setup_parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated action kinds to converge, e.g. `git`. Everything else is left "
             "alone. Workspaces are always included, because the rest needs their ids.",
    )
    setup_parser.add_argument(
        "--changed-since",
        default=None,
        metavar="REF",
        help="Only layers whose git directory changed between REF and HEAD, e.g. HEAD~1 on a "
             "merge. Falls back to every layer when git cannot diff against REF. Layers left "
             "out are named in the log.",
    )
    setup_parser.add_argument("--repo-path", default=".", help="Repository root, for --changed-since.")

    feature_parser = subparsers.add_parser("feature", help="Feature workspaces for a branch (see E08).", parents=[common])
    feature_parser.add_argument("action", choices=("create", "update", "delete", "list", "reap"))
    feature_parser.add_argument("--branch", default=None, help="Branch name. Defaults to the CI branch variables.")
    feature_parser.add_argument("--developer", default=None, help="Owner slug. Defaults to the CI actor or git config.")
    feature_parser.add_argument(
        "--group",
        dest="feature_group",          # not `group`: that is the subcommand name
        default=None,
        help="Feature group recipe to use. Defaults to the branch segment.",
    )
    feature_parser.add_argument(
        "--developer-object-id",
        default=None,
        help="Entra object id, to make the developer admin of their own workspace. Optional, and "
             "not derivable from CI: without it only the recipe's permissions are applied.",
    )
    feature_parser.add_argument("--layers", default=None, help="Comma-separated subset of layers.")
    feature_parser.add_argument("--base-environment", default="dev", help="Environment the branch relates to. Default: dev.")
    feature_parser.add_argument("--no-relation", action="store_true", help="Skip registering the branched-workspace relation.")
    feature_parser.add_argument("--stop-on-error", action="store_true")
    feature_parser.add_argument("--ttl-days", type=int, default=None, help="reap: age after which a feature workspace is stale. Default: 14.")
    feature_parser.add_argument("--apply", action="store_true", help="reap: actually delete. Without it, reap only reports.")
    feature_parser.add_argument(
        "--apply-when",
        choices=("branch-gone", "both"),
        default="both",
        help="reap: with --apply, delete on every signal (both) or only where the branch no "
             "longer exists (branch-gone), leaving age-based candidates in the report.",
    )
    feature_parser.add_argument(
        "--azure-devops-token",
        default=os.environ.get("SYSTEM_ACCESSTOKEN") or os.environ.get("AZURE_DEVOPS_TOKEN"),
        help="reap: token for the Azure DevOps refs API. Default: $SYSTEM_ACCESSTOKEN (the pipeline's "
             "own) or $AZURE_DEVOPS_TOKEN (a PAT).",
    )
    feature_parser.add_argument("--no-branch-check", action="store_true", help="reap: age only; do not ask the git provider whether the branch still exists.")

    manifest_parser = subparsers.add_parser("manifest", help="Inspect run manifests.", parents=[common])
    manifest_sub = manifest_parser.add_subparsers(dest="command", required=True)
    show_parser = manifest_sub.add_parser("show", help="Print the outputs of a run.", parents=[common])
    show_parser.add_argument("--path", default=None, help="Manifest path. Default: the most recent run.")

    tags_parser = subparsers.add_parser("tags", help="Work with Fabric tags (see documentation/specs/E05).", parents=[common])
    tags_sub = tags_parser.add_subparsers(dest="command", required=True)
    tags_sync = tags_sub.add_parser("sync", help="Create missing tenant/domain tags and record their ids [admin].", parents=[common])
    tags_sync.add_argument("--environment", default=None)
    tags_sync.add_argument("--registry", default=None, help="Registry path. Default: the solution's tags file.")
    tags_list = tags_sub.add_parser("list", help="Show the tags a recipe declares and whether they are registered.", parents=[common])
    tags_list.add_argument("--environment", default=None)
    tags_list.add_argument("--registry", default=None)

    varlib_parser = subparsers.add_parser("varlib", help="Variable libraries (see documentation/specs/E06).", parents=[common])
    varlib_sub = varlib_parser.add_subparsers(dest="command", required=True)
    varlib_render = varlib_sub.add_parser("render", help="Generate variable library definitions from the recipe.", parents=[common])
    varlib_render.add_argument("--environments", default=None, help="Comma-separated value sets. Default: the solution's environments.")
    varlib_render.add_argument("--out", default="automation/generated", help="Where to write generated definitions.")
    varlib_activate = varlib_sub.add_parser("activate", help="Set the active value set on the target workspaces.", parents=[common])
    varlib_activate.add_argument("--environment", required=True, help="Value set to activate.")

    storage_parser = subparsers.add_parser("storage", help="Feature storage (see documentation/specs/E07).", parents=[common])
    storage_sub = storage_parser.add_subparsers(dest="command", required=True)
    storage_report = storage_sub.add_parser("report", help="List feature schemas left behind in shared storage.", parents=[common])
    storage_report.add_argument("--environment", default="dev")

    sanitise_parser = subparsers.add_parser("sanitise", help="Check the exportable tree for values that must not be published.", parents=[common])
    sanitise_parser.add_argument("--policy", default="automation/resources/export.yml")
    sanitise_parser.add_argument("--root", default=".")
    sanitise_parser.add_argument("--report-only", action="store_true", help="Print findings and still exit 0.")
    sanitise_parser.add_argument("--strict", action="store_true", help="Also flag GUIDs that are not allow-listed.")
    sanitise_parser.add_argument("--list-files", action="store_true", help="Print what the allowlist admits.")

    export_parser = subparsers.add_parser("export", help="Copy the exportable tree, gated by sanitise.", parents=[common])
    export_parser.add_argument("--to", required=True, help="Target directory.")
    export_parser.add_argument("--policy", default="automation/resources/export.yml")
    export_parser.add_argument("--root", default=".")
    export_parser.add_argument("--strict", action="store_true")
    export_parser.add_argument("--force", action="store_true", help="Export even with findings (records them).")

    release_parser = subparsers.add_parser("release", help="Deploy items with fabric-cicd (see E09).", parents=[common])
    release_parser.add_argument("--environment", required=True, help="Target environment, e.g. tst.")
    release_parser.add_argument("--layers", default=None, help="Comma-separated subset of layers. Default: all, in dependency order.")
    release_parser.add_argument("--repo-path", default=".", help="Repository root the layer directories are relative to.")
    release_parser.add_argument("--item-types", default=None, help="Comma-separated Fabric item types. Overrides the recipe.")
    release_parser.add_argument("--parameter-file", default=None, help=f"Committed parameter file. Default: {release_module.parameters.DEFAULT_PARAMETER_FILE}.")
    release_parser.add_argument(
        "--extend-parameters",
        default="true",
        choices=("true", "false"),
        help="Render the generated parameter overlay. Default: true.",
    )
    release_parser.add_argument("--no-accumulate-ids", action="store_true", help="Release each layer independently, without carrying item ids forward.")
    release_parser.add_argument(
        "--no-sync-connections",
        action="store_true",
        help="Skip creating connections that were waiting for their items to be published.",
    )

    references_parser = subparsers.add_parser(
        "references", help="Keep committed item ids in step with a deployed environment.", parents=[common]
    )
    references_sub = references_parser.add_subparsers(dest="command", required=True)
    references_sync = references_sub.add_parser(
        "sync", help="Rewrite committed references to the ids this environment actually has.", parents=[common]
    )
    references_sync.add_argument("--environment", default="dev", help="Environment to read ids from. Default: dev.")
    references_sync.add_argument("--repo-path", default=".", help="Repository root the declared files are relative to.")
    references_sync.add_argument("--apply", action="store_true", help="Write the changes. Without it, report only.")
    references_sync.add_argument(
        "--force",
        action="store_true",
        help="Write ids from an environment that is not the git-connected one. Rarely what you want.",
    )

    connection_parser = subparsers.add_parser("connection", help="Work with Fabric connections.", parents=[common])
    connection_sub = connection_parser.add_subparsers(dest="command", required=True)
    connection_refresh = connection_sub.add_parser(
        "refresh", help="Replace the token stored in the git credentials connection.", parents=[common]
    )
    connection_refresh.add_argument("--name", default=None, help="Connection name. Default: the recipe's git credentials connection.")
    connection_refresh.add_argument("--environment", default="dev", help="Environment whose recipe names the connection.")

    solution_parser = subparsers.add_parser("solution", help="Work with solutions.", parents=[common])
    solution_sub = solution_parser.add_subparsers(dest="command", required=True)
    solution_sub.add_parser("list", help="List the solutions this repo defines.", parents=[common])

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    log = RunLog.from_env(
        **{
            key: value
            for key, value in {
                "level": Level.parse(args.log_level) if args.log_level else None,
                "trace_file": pathlib.Path(args.trace_file) if args.trace_file else None,
                "redaction_enabled": False if args.no_redact else None,
            }.items()
            if value is not None
        }
    )

    try:
        if args.group == "recipe" and args.command == "validate":
            return _recipe_validate(args, log)
        if args.group == "recipe" and args.command == "render":
            return _recipe_render(args, log)
        if args.group == "solution" and args.command == "list":
            return _solution_list(args, log)
        if args.group == "plan":
            return _plan(args, log)
        if args.group == "setup":
            return _setup(args, log)
        if args.group == "manifest" and args.command == "show":
            return _manifest_show(args, log)
        if args.group == "varlib" and args.command == "render":
            return _varlib_render(args, log)
        if args.group == "varlib" and args.command == "activate":
            return _varlib_activate(args, log)
        if args.group == "storage" and args.command == "report":
            return _storage_report(args, log)
        if args.group == "sanitise":
            return _sanitise(args, log)
        if args.group == "export":
            return _export(args, log)
        if args.group == "feature" and args.action in ("list", "reap"):
            return _feature_inventory(args, log)
        if args.group == "feature":
            return _feature(args, log)
        if args.group == "release":
            return _release(args, log)
        if args.group == "connection" and args.command == "refresh":
            return _connection_refresh(args, log)
        if args.group == "references" and args.command == "sync":
            return _references_sync(args, log)
        if args.group == "tags" and args.command == "sync":
            return _tags_sync(args, log)
        if args.group == "tags" and args.command == "list":
            return _tags_list(args, log)
        parser.error(f"unknown command: {args.group} {getattr(args, 'command', '')}")
        return ExitCode.RECIPE_ERROR
    except FabricOpsError as error:
        log.error(f"✖ {error}")
        return error.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted")
        return ExitCode.ACTION_FAILED
    finally:
        log.close()


# --------------------------------------------------------------------- commands
def _recipe_validate(args: argparse.Namespace, log: RunLog) -> int:
    targets: list[tuple[str | None, str | None]] = []
    # A bare `recipe validate` used to check the base recipe on its own, where a
    # `display_name_pattern` of "... [{environment}]" cannot resolve - so a perfectly good
    # repo was told "1 of 1 recipes failed validation". The base recipe is never deployed
    # alone; every environment is. With no environment named, validate them all.
    if args.all or not args.environment:
        for entry in recipe.resolver.list_solutions(args.resources):
            name = None if entry["name"] == "default" else str(entry["name"])
            if args.solution and not args.all and name != args.solution:
                continue
            environments = list(entry["environments"]) or [None]
            targets += [(name, environment) for environment in environments]
    else:
        targets = [(args.solution, args.environment)]

    if not targets:
        log.warning("no recipes found to validate")
        return ExitCode.SUCCESS

    failures = 0
    for solution, environment in targets:
        label = f"{solution or 'default'}" + (f" [{environment}]" if environment else "")
        try:
            loaded = recipe.load_platform(args.resources, solution=solution, environment=environment)
        except FabricOpsError as error:
            failures += 1
            log.error(f"✖ {label}: {error}")
            continue
        log.info(f"✔ {label}  ({' + '.join(path.name for path in loaded.sources)})")
        for note in loaded.notes:
            log.debug(f"    {note}")
        for warning in loaded.warnings:
            log.warning(f"    ⚠ {warning}")

    if failures:
        log.error(f"\n{failures} of {len(targets)} recipes failed validation")
        return ExitCode.RECIPE_ERROR
    log.success(f"\n{len(targets)} recipe(s) valid")
    return ExitCode.SUCCESS


def _recipe_render(args: argparse.Namespace, log: RunLog) -> int:
    if args.kind == "feature":
        loaded = recipe.load_feature(args.resources, solution=args.solution, developer=args.developer)
    else:
        loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)

    log.debug(f"sources: {', '.join(str(path) for path in loaded.sources)}")
    for note in loaded.notes:
        log.debug(f"  {note}")
    sys.stdout.write(loaded.render(args.format))
    return ExitCode.SUCCESS


def _solution_list(args: argparse.Namespace, log: RunLog) -> int:
    entries = recipe.resolver.list_solutions(args.resources)
    if not entries:
        log.warning(f"no solutions found under {args.resources}")
        return ExitCode.SUCCESS

    width = max(len(str(entry["name"])) for entry in entries)
    log.info(f"{'SOLUTION'.ljust(width)}  {'LAYOUT'.ljust(12)}  ENVIRONMENTS  PATH")
    for entry in sorted(entries, key=lambda item: str(item["name"])):
        environments = ", ".join(entry["environments"]) or "-"
        log.info(
            f"{str(entry['name']).ljust(width)}  {str(entry['layout']).ljust(12)}  "
            f"{environments.ljust(12)}  {entry['path']}"
        )
    return ExitCode.SUCCESS


# ----------------------------------------------------------------------- engine
def _authenticate(args: argparse.Namespace, cli: FabricCli, log: RunLog) -> None:
    """Log in only when we must: FAB_SPN_* env vars are the preferred path."""
    if os.environ.get("FAB_SPN_CLIENT_ID") or os.environ.get("FAB_MANAGED_IDENTITY"):
        log.debug("using FAB_* environment credentials")
        return
    credentials = _credentials(args, log)
    if credentials.has_service_principal:
        cli.config_set("encryption_fallback_enabled", "true")
        cli.login_service_principal(credentials.client_id, credentials.client_secret, credentials.tenant_id)
        log.debug(f"authenticated as {credentials.client_id}")
        return
    log.debug("no credentials supplied; relying on an existing `fab auth login` session")


def _credentials(args: argparse.Namespace, log: RunLog | None = None) -> Credentials:
    """Identity for this run: flags and environment first, the local file only for gaps.

    A flag or an environment variable is a deliberate act. The credentials file is a local
    convenience, so it fills gaps and never overrides.
    """
    from .engine import credentials_file

    explicit = {
        "tenant_id": args.tenant_id,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "github_pat": args.github_pat,
    }
    values = {name: value for name, value in explicit.items() if value}

    if not getattr(args, "no_credentials_file", False):
        local = credentials_file.load(
            getattr(args, "credentials_dir", credentials_file.DEFAULT_DIRECTORY),
            environment=getattr(args, "environment", None),
        )
        if local:
            filled = [name for name in local.values if name not in values]
            values = credentials_file.merge(values, local)
            if log and filled:
                log.debug(f"credentials: {', '.join(sorted(filled))} from {local.path}")
            for warning in (local.warnings if log else []):
                log.warning(f"⚠ {warning}")

    return Credentials(
        tenant_id=values.get("tenant_id"),
        client_id=values.get("client_id"),
        client_secret=Secret(values.get("client_secret"), "client_secret"),
        github_pat=Secret(values.get("github_pat"), "github_pat"),
    )


def _filter_layers(loaded: recipe.Recipe, layers: str | None) -> recipe.Recipe:
    if not layers:
        return loaded
    wanted = {name.strip().lower() for name in layers.split(",") if name.strip()}
    kept = {name: definition for name, definition in loaded.layers.items() if name.lower() in wanted}
    if not kept:
        raise FabricOpsError(
            f"no layers matched '{layers}'",
            hint=f"Available layers: {', '.join(loaded.layers) or 'none'}",
        )
    loaded.data = {**loaded.data, "layers": kept}
    return loaded


def _run_header(
    log: RunLog, loaded: recipe.Recipe, action: str, dry_run: bool, *, layers: set[str] | None = None
) -> None:
    log.header(f"{action} {loaded.solution or 'default'} [{loaded.environment or '-'}]")
    log.info(f"fabricops {__version__}  run {log.run_id}" + ("  [dry-run]" if dry_run else ""))
    log.info(f"recipe   {', '.join(str(path) for path in loaded.sources)}")
    shown = [name for name in loaded.layers if layers is None or name in layers]
    log.info(f"layers   {', '.join(shown) or 'none'}")
    for note in loaded.notes:
        log.debug(f"  {note}")


def _references_sync(args: argparse.Namespace, log: RunLog) -> int:
    """Point committed ids at the items this environment actually has.

    Some Fabric references can only be item ids - a report's semantic model, a Direct Lake
    expression, a pipeline's notebooks - and item ids are per-tenant. A repository that
    ships working example content therefore cannot ship ids that resolve for anybody else.
    This is the one command that fixes that, rather than a hand edit per reference.
    """
    from .engine import references as references_module

    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    root = pathlib.Path(args.repo_path)
    declared = references_module.declared(loaded, root)

    log.header(f"References [{args.environment}]")

    # Committed ids belong to the environment git syncs into. Writing another environment's
    # ids into the repository would point the git-connected workspaces at the wrong items -
    # and it would look like it worked, because every id involved is real.
    if args.apply and not loaded.defaults.get("git") and not args.force:
        raise FabricOpsError(
            f"'{args.environment}' is not git-connected, so its ids do not belong in the repository",
            hint="Committed ids are the git-connected environment's - usually dev. Other environments "
                 "get theirs from parameter.yml at release time. Pass --force only if you are certain.",
        )
    if not declared:
        log.info("  the recipe declares no references")
        return ExitCode.SUCCESS

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    report = references_module.sync(
        declared,
        resolve=references_module.tenant_resolver(cli, loaded),
        apply=args.apply and not args.dry_run,
    )
    for line in report.describe():
        log.info(line)
    for warning in report.warnings:
        log.warning(f"  {warning}")
    log.info("")

    if not report.changed:
        log.success(f"\u2714 {len(report.rewrites)} reference(s) already correct")
    elif report.applied:
        log.success(f"\u2714 {len(report.changed)} reference(s) rewritten - commit the change")
    else:
        log.warning(f"\u26a0 {len(report.changed)} reference(s) differ - re-run with --apply")
    return ExitCode.ACTION_FAILED if report.warnings and not report.applied else ExitCode.SUCCESS


def _connection_refresh(args: argparse.Namespace, log: RunLog) -> int:
    """Put a fresh token into the git credentials connection (E03-S5).

    A personal access token expires, and when it does every `git/connect` fails. This is
    the one-command remedy, so rotating a PAT does not mean clicking through the portal.
    """
    from .engine.connections import refresh_git_credentials

    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    git = loaded.defaults.get("git") or {}
    name = args.name or (git.get("credentials") or {}).get("connection")
    if not name:
        raise FabricOpsError(
            "no git credentials connection to refresh",
            hint="Pass --name, or set `defaults.git.credentials.connection` in the recipe.",
        )

    log.header(f"Refresh connection '{name}'")
    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    if args.dry_run:
        log.info(f"  would replace the token stored in '{name}'")
        return ExitCode.SUCCESS

    refresh_git_credentials(
        cli,
        connection_name=str(name),
        credentials=_credentials(args, log),
        provider=str(git.get("provider") or "GitHub"),
        log=log,
    )
    log.success(f"\u2714 '{name}' now holds the current token")
    return ExitCode.SUCCESS


def _release(args: argparse.Namespace, log: RunLog) -> int:
    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    options = release_module.ReleaseOptions(
        environment=args.environment,
        repository_root=pathlib.Path(args.repo_path),
        layers=tuple(name.strip() for name in args.layers.split(",") if name.strip()) if args.layers else None,
        parameter_file=pathlib.Path(args.parameter_file) if args.parameter_file
        else release_module.parameters.DEFAULT_PARAMETER_FILE,
        extend_parameters=args.extend_parameters == "true",
        accumulate_ids=not args.no_accumulate_ids,
        sync_connections=not args.no_sync_connections,
        dry_run=args.dry_run,
        item_types=tuple(name.strip() for name in args.item_types.split(",") if name.strip())
        if args.item_types
        else release_module.runner.DEFAULT_ITEM_TYPES,
    )

    _run_header(log, loaded, "Release", args.dry_run)
    log.info(f"order    {', '.join(layer for layer, _, _ in release_module.plan(loaded, options))}")

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    result = release_module.run(
        loaded,
        options,
        cli=cli,
        log=log,
        credential=_token_credential(args, log),   # for fabric-cicd, which calls the REST API directly
        credentials=_credentials(args, log),       # for the connections FabricOps builds itself
    )

    log.info("")
    for layer in result.layers:
        detail = f"{layer.published} item(s)" if layer.status == release_module.Status.COMPLETED else layer.message
        log.info(f"  {layer.status:<9} {layer.layer:<12} {detail}")
    synced = result.connections
    if synced and (synced.created or synced.waiting):
        log.info(
            f"  connections  {len(synced.created)} created, {len(synced.existed)} already there"
            + (f", {len(synced.waiting)} still waiting" if synced.waiting else "")
        )
    if result.status == release_module.Status.FAILED:
        log.error(f"✖ {result.message}")
    else:
        log.success(f"✔ {result.message}")
    return result.exit_code


def _token_credential(args: argparse.Namespace, log: RunLog):  # noqa: ANN202 - azure type, imported lazily
    """The credential fabric-cicd needs. It talks to the REST API directly, not through `fab`."""
    if args.dry_run:
        return None
    resolved = _credentials(args, log)
    tenant_id = resolved.tenant_id or os.environ.get("FAB_SPN_TENANT_ID")
    client_id = resolved.client_id or os.environ.get("FAB_SPN_CLIENT_ID")
    client_secret = resolved.client_secret.reveal() or os.environ.get("FAB_SPN_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    from azure.identity import AzureCliCredential

    log.debug("no service principal supplied; using the Azure CLI credential")
    return AzureCliCredential()


def _plan_check(args: argparse.Namespace, loaded: recipe.Recipe, plan, log: RunLog) -> int:
    """Probe the tenant and report where it differs from the recipe (E03-S8).

    A separate flag rather than the default, because a bare `plan` is useful offline -
    reviewing a recipe change in a PR should not need credentials. Nightly drift jobs pass
    `--check`.

    The probe runs against a silent log: every action would otherwise narrate itself a
    second time, right after the plan listing that just described it.
    """
    from .engine.drift import detect

    quiet = RunLog(level=Level.OFF, redaction_enabled=log.redaction_enabled)
    cli = FabricCli(quiet, dry_run=True)
    _authenticate(args, cli, log)
    try:
        report = detect(
            plan,
            RunContext(cli=cli, log=quiet, recipe=loaded, dry_run=True, credentials=_credentials(args, log)),
            log=log,
        )
    finally:
        quiet.close()

    log.header("Drift")
    for line in report.describe():
        log.info(line)
    log.info("")
    if report.failures:
        log.error(f"\u2716 drift check incomplete: {len(report.failures)} action(s) could not be read")
    elif report.drifted:
        log.warning(f"\u26a0 {len(report.drifted)} of {len(report.findings)} resource(s) differ from the recipe")
    else:
        log.success(f"\u2714 {len(report.findings)} resource(s) checked, no drift")
    return report.exit_code


def _plan(args: argparse.Namespace, log: RunLog) -> int:
    loaded = _filter_layers(
        recipe.load_platform(args.resources, solution=args.solution, environment=args.environment), args.layers
    )
    plan = build_plan(loaded, repository_root=args.repo_path)
    _run_header(log, loaded, "Drift check" if args.check else "Plan", True)
    for line in plan.describe(sequence=args.sequence):
        log.info(line)
    log.info("")

    if args.check:
        return _plan_check(args, loaded, plan, log)

    log.success(f"✔ {len(plan)} action(s) planned")
    return ExitCode.SUCCESS


def _only_kinds(args: argparse.Namespace) -> set[str] | None:
    """The action kinds `--only` asked for, exactly as asked."""
    if not getattr(args, "only", None):
        return None
    kinds = {kind.strip().lower() for kind in str(args.only).split(",") if kind.strip()}
    return kinds or None


def _closure(plan: Any, asked: set[str]) -> list[Any]:
    """The asked actions plus everything they depend on, in plan order.

    Naming what you want is not enough: a git action reads the workspace id from the
    workspace action's output *and* the source control connection id from the connection
    action's. The plan already records what each action needs, so use that rather than a
    hand-maintained list of what must come along - which is what the first version was,
    and it named workspaces and forgot connections. Whatever comes along only as a
    dependency is marked incidental: read, reported at debug, never created.
    """
    by_id = {action.id: action for action in plan}
    keep = set(asked)
    stack = list(keep)
    while stack:
        action = by_id.get(stack.pop())
        for dependency in getattr(action, "depends_on", ()) or ():
            if dependency in by_id and dependency not in keep:
                keep.add(dependency)
                stack.append(dependency)
    for action in plan:
        if action.id in keep and action.id not in asked:
            action.incidental = True
    return [action for action in plan if action.id in keep]


def _keep_with_dependencies(plan: Any, kinds: set[str]) -> list[Any]:
    """`--only`: the actions of those kinds, plus what they depend on."""
    return _closure(plan, {action.id for action in plan if action.kind in kinds})


def _keep_layers_in_plan(plan: Any, layers: set[str]) -> list[Any]:
    """`--layers` and `--changed-since`: the actions of those layers, plus what they depend on.

    This filters the *plan*, never the recipe. Narrowing the recipe to one layer broke
    everything that referred to another: a solution connection built `from_item` on Store
    failed with "'Store' is not a layer" the moment only Prepare was selected. The full
    recipe stays whole; the plan is what gets smaller.
    """
    return _closure(plan, {action.id for action in plan if action.layer in layers})
    return [action for action in plan if action.id in keep]


def _selected_layers(loaded: recipe.Recipe, layers: str | None) -> set[str] | None:
    """The layers `--layers` names, or None for all. Same message as before when none match."""
    if not layers:
        return None
    wanted = {name.strip().lower() for name in layers.split(",") if name.strip()}
    selected = {name for name in loaded.layers if name.lower() in wanted}
    if not selected:
        raise FabricOpsError(
            f"no layers matched '{layers}'",
            hint=f"Available layers: {', '.join(loaded.layers) or 'none'}",
        )
    return selected


def _setup(args: argparse.Namespace, log: RunLog) -> int:
    destroy = args.action == "delete"
    # Loaded whole and kept whole. Layer selection filters the plan further down, because a
    # recipe narrowed to Prepare no longer knows Store, and the connection built from
    # Store's lakehouse then fails as "'Store' is not a layer".
    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    selected = _selected_layers(loaded, args.layers)

    unchanged: list[str] = []
    ref = getattr(args, "changed_since", None)
    if ref and not destroy:
        from .engine.changed import changed_paths_since, layers_touched

        paths = changed_paths_since(ref, getattr(args, "repo_path", ".") or ".")
        if paths is None:
            # Not a reason to do less. A ref that cannot be diffed - shallow clone, force
            # push, first run - means "check everything", never "check nothing".
            log.warning(f"could not diff against {ref}; checking every layer instead")
        else:
            base = selected if selected is not None else set(loaded.layers)
            touched = layers_touched(loaded, paths) & base
            unchanged = sorted(base - touched)
            if not touched:
                log.info(f"no layer's git directory changed since {ref} - nothing to sync")
                log.info(f"  ({len(paths)} path(s) changed, none inside a layer directory)")
                return ExitCode.SUCCESS
            selected = touched

    if destroy and not args.dry_run:
        expected = f"{loaded.solution or 'default'}/{args.environment}"
        if args.confirm != expected:
            raise FabricOpsError(
                f"delete needs --confirm {expected}",
                hint="This deletes workspaces and their contents. Pass the exact value to proceed.",
            )

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    plan = build_plan(loaded)
    if selected is not None and selected != set(loaded.layers):
        plan.actions = _keep_layers_in_plan(plan, selected)
    only = _only_kinds(args)
    if only:
        plan.actions = _keep_with_dependencies(plan, only)
        if not plan.actions:
            raise FabricOpsError(
                f"--only {args.only} matched no actions",
                hint="Kinds in this plan: " + ", ".join(sorted({a.kind for a in build_plan(loaded)})),
            )
    # A git-only run is not a setup, and calling it one made a routine sync on every merge
    # look like it was about to provision seven workspaces.
    verb = "Delete" if destroy else ("Sync" if only == {"git"} else "Converge" if only else "Setup")
    _run_header(log, loaded, verb, args.dry_run, layers=selected)
    if only:
        log.info(f"only     {', '.join(sorted(only))}")
    if unchanged:
        # Said out loud so a short log reads as a choice, not as layers forgotten.
        log.info(f"skipped  {', '.join(unchanged)} (unchanged since {ref})")

    manifest = Manifest(
        run_id=log.run_id,
        command=f"setup --action {args.action}" + (f" --only {args.only}" if args.only else ""),
        solution=loaded.solution,
        environment=loaded.environment,
        kind=loaded.kind,
        dry_run=args.dry_run,
        sources=[str(path) for path in loaded.sources],
    )
    ctx = RunContext(
        cli=cli,
        log=log,
        recipe=loaded,
        dry_run=args.dry_run,
        credentials=_credentials(args, log),
        tag_registry=TagRegistry.discover(args.resources, args.solution),
    )
    report = execute(plan, ctx, manifest=manifest, destroy=destroy, stop_on_error=args.stop_on_error)
    summarise(log, report, dry_run=args.dry_run)

    path = manifest.write(args.manifest_root)
    log.info(f"manifest {path}")
    if cli.skipped_writes:
        log.info(f"{len(cli.skipped_writes)} write(s) would have run; use --log-level debug to list them")
    return report.exit_code


def _manifest_show(args: argparse.Namespace, log: RunLog) -> int:
    path = pathlib.Path(args.path) if args.path else Manifest.latest(args.manifest_root)
    if not path or not pathlib.Path(path).exists():
        log.warning("no manifest found")
        return ExitCode.SUCCESS

    data = Manifest.read(path)
    log.info(f"run {data['run_id']}  {data.get('command', '')}  {data.get('started', '')}")
    log.info(f"recipe {', '.join(data.get('sources') or [])}")
    log.info(f"counts {data.get('counts')}")
    log.info("")
    for action_id, outputs in (data.get("outputs") or {}).items():
        rendered = ", ".join(f"{key}={value}" for key, value in outputs.items())
        log.info(f"  {action_id}: {rendered}")
    return ExitCode.SUCCESS


# ------------------------------------------------------------------------- tags
def _declared_tags(loaded: recipe.Recipe) -> list[str]:
    declared: list[str] = list(loaded.defaults.get("tags") or [])
    for layer in loaded.layers:
        for tag in loaded.tags_for(layer):
            if tag not in declared:
                declared.append(tag)
        for item in loaded.items(layer):
            for tag in loaded.tags_for(layer, item):
                if tag not in declared:
                    declared.append(tag)
    return declared


def _tags_sync(args: argparse.Namespace, log: RunLog) -> int:
    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    declared = _declared_tags(loaded)
    if not declared:
        log.warning("the recipe declares no tags")
        return ExitCode.SUCCESS

    registry = (
        TagRegistry.load(args.registry) if args.registry else TagRegistry.discover(args.resources, args.solution)
    )
    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    log.header(f"Tag sync ({len(declared)} declared)")
    if args.dry_run:
        for tag in declared:
            state = "registered" if registry.tags.get(tag) else "would create"
            log.info(f"  {tag:<42} {state}")
        return ExitCode.SUCCESS

    result = sync_registry(cli, registry, declared)
    path = registry.save(args.registry)
    for tag in declared:
        log.info(f"  {tag:<42} {'created' if tag in result['created'] else 'registered'}")
    log.success(f"\n✔ {len(result['created'])} created, {len(declared) - len(result['created'])} already registered")
    log.info(f"registry {path}  (commit this file)")
    return ExitCode.SUCCESS


def _tags_list(args: argparse.Namespace, log: RunLog) -> int:
    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    registry = (
        TagRegistry.load(args.registry) if args.registry else TagRegistry.discover(args.resources, args.solution)
    )
    declared = _declared_tags(loaded)
    if not declared:
        log.warning("the recipe declares no tags")
        return ExitCode.SUCCESS

    missing = 0
    log.info(f"{'TAG'.ljust(42)}  ID")
    for tag in declared:
        tag_id = registry.tags.get(tag)
        missing += 0 if tag_id else 1
        log.info(f"{tag.ljust(42)}  {tag_id or '- not registered -'}")
    if missing:
        log.warning(f"\n{missing} tag(s) are not registered; run `fabricops tags sync` with an admin identity")
        return ExitCode.DRIFT
    log.success(f"\n✔ all {len(declared)} declared tags are registered")
    return ExitCode.SUCCESS


# ---------------------------------------------------------------------- feature
def _current_branch() -> str | None:
    return (
        os.environ.get("GITHUB_REF_NAME")
        or (os.environ.get("BUILD_SOURCEBRANCH") or "").removeprefix("refs/heads/")
        or os.environ.get("BUILD_SOURCEBRANCHNAME")
        or None
    )


def _feature_inventory(args: argparse.Namespace, log: RunLog) -> int:
    """`feature list` and `feature reap` (E08-S2, E08-S3).

    Both read the same inventory: workspaces that identify themselves through their
    description. Nothing is deleted without `--apply`, and nothing that does not carry a
    FabricOps stamp is ever a candidate.
    """
    from datetime import datetime, timezone

    from .engine import inventory

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    log.header(f"Feature workspaces{' - reap' if args.action == 'reap' else ''}")
    workspaces = inventory.list_feature_workspaces(cli, solution=args.solution, log=log)
    if not workspaces:
        log.info("  none found")
        return ExitCode.SUCCESS

    if args.action == "list":
        width = max(len(workspace.name) for workspace in workspaces)
        now = datetime.now(timezone.utc)
        for workspace in sorted(workspaces, key=lambda w: (w.branch or "", w.layer or "")):
            age = workspace.age_days(now)
            log.info(
                f"  {workspace.name.ljust(width)}  {workspace.layer or '-':<12} "
                f"{workspace.branch or '-':<32} {workspace.developer or '-':<20} "
                + (f"{age:.0f}d" if age is not None else "-")
            )
        log.info("")
        log.success(f"\u2714 {len(workspaces)} feature workspace(s)")
        return ExitCode.SUCCESS

    ttl_days = args.ttl_days if args.ttl_days is not None else inventory.DEFAULT_TTL_DAYS
    branch_exists = None if args.no_branch_check else _branch_checker(args, log)
    decisions = inventory.decide(
        workspaces, now=datetime.now(timezone.utc), ttl_days=ttl_days, branch_exists=branch_exists
    )
    report = inventory.reap(
        cli, decisions, log=log, apply=args.apply and not args.dry_run, apply_when=args.apply_when
    )

    for line in report.describe():
        log.info(line)
    if report.deleted:
        # The workspace is gone; the schema it wrote into shared storage is not. Same
        # teardown a `feature delete` runs, so a reaped feature leaves nothing behind.
        branches = sorted({
            d.workspace.branch for d in decisions
            if d.workspace.name in report.deleted and d.workspace.branch
        })
        developers = {d.workspace.branch: d.workspace.developer for d in decisions}
        _drop_feature_schemas(args, log, cli, branches, developers)
    log.info("")
    if report.failures:
        log.error(f"\u2716 {len(report.failures)} deletion(s) failed")
        return ExitCode.ACTION_FAILED
    if report.applied:
        log.success(f"\u2714 {len(report.deleted)} workspace(s) deleted")
        if report.deferred:
            log.warning(f"\u26a0 {len(report.deferred)} stale workspace(s) left for review (age only); "
                        f"re-run with --apply-when both to delete them")
    elif report.doomed:
        log.warning(f"\u26a0 {len(report.doomed)} workspace(s) would be deleted - re-run with --apply")
    else:
        log.success("\u2714 nothing to reap")
    return ExitCode.SUCCESS


def _drop_feature_schemas(args: argparse.Namespace, log: RunLog, cli: FabricCli, branches, developers) -> None:
    """Drop the feature schemas of reaped branches, the way `feature delete` does."""
    from .engine.feature import _feature_schema_actions, parse_branch

    if not branches:
        return
    try:
        platform = recipe.load_platform(args.resources, solution=args.solution, environment=args.base_environment)
    except FabricOpsError as error:
        log.warning(f"  feature schemas not dropped: no platform recipe ({error})")
        return
    ctx = RunContext(cli=cli, log=log, recipe=platform, dry_run=args.dry_run, credentials=_credentials(args, log))
    for branch in branches:
        segment = branch.removeprefix("refs/heads/").split("/")[1] if branch.count("/") >= 2 else None
        group = segment if segment and recipe.resolver.feature_group_exists(args.resources, solution=args.solution, group=segment) else None
        try:
            feature_recipe = recipe.load_feature(args.resources, solution=args.solution, developer=developers.get(branch), branch=branch, group=group)
            info = parse_branch(branch, list(feature_recipe.layers), developer=developers.get(branch), group=group)
            actions = _feature_schema_actions(feature_recipe, info, platform.display_name_pattern, args.base_environment, platform)
        except FabricOpsError as error:
            log.warning(f"  {branch}: feature schema not dropped ({error})")
            continue
        for action in actions:
            try:
                result = action.destroy(ctx)
                log.info(f"  {branch}: {action.describe()} {result.status if result else 'skipped'}"
                         + (f" ({result.message})" if result and result.message else ""))
            except FabricOpsError as error:
                log.warning(f"  {branch}: {action.describe()} failed: {error}")


def _branch_checker(args: argparse.Namespace, log: RunLog):  # noqa: ANN202
    """A branch-existence probe for the recipe's git provider, or None when there is none.

    Returning None disables the signal entirely rather than guessing, because "I could not
    reach GitHub" must never be read as "the branch is gone".
    """
    from .engine import inventory

    try:
        platform = recipe.load_platform(
            args.resources, solution=args.solution, environment=args.base_environment
        )
    except FabricOpsError as error:
        log.debug(f"no platform recipe, so no branch check: {error}")
        return None

    git = (platform.defaults.get("git") or {})
    provider = str(git.get("provider") or "")
    if provider.lower() == "azuredevops":
        organization, project, repository = git.get("organization"), git.get("project"), git.get("repository")
        if not (organization and project and repository):
            log.info("  branch check skipped: the recipe names no Azure DevOps organization/project/repository")
            return None
        if not args.azure_devops_token:
            log.info("  branch check skipped: no token for Azure DevOps (set SYSTEM_ACCESSTOKEN or --azure-devops-token)")
            return None
        return inventory.azure_devops_branch_checker(str(organization), str(project), str(repository), args.azure_devops_token)
    if provider.lower() != "github":
        log.info(f"  branch check skipped: no probe implemented for provider '{provider or 'none'}'")
        return None
    owner, repository = git.get("owner"), git.get("repository")
    if not owner or not repository:
        log.info("  branch check skipped: the recipe names no GitHub owner/repository")
        return None
    if not args.github_pat:
        log.info("  branch check limited: no --github-pat, so a private repo cannot be read")
    return inventory.github_branch_checker(str(owner), str(repository), args.github_pat)


def _feature(args: argparse.Namespace, log: RunLog) -> int:
    branch = args.branch or _current_branch()
    if not branch:
        raise FabricOpsError(
            "no branch name available",
            hint="Pass --branch, or run where GITHUB_REF_NAME / BUILD_SOURCEBRANCH is set.",
        )

    developer = args.developer or recipe.resolver.resolve_developer()

    # The branch segment decides which feature recipe to load: a group recipe if one
    # exists (feature/<group>/<topic>), otherwise the segment is matched against layer
    # names by the planner, and failing that every layer participates.
    from .engine.feature import branch_segment

    segment = args.feature_group or branch_segment(branch)
    group = segment if recipe.resolver.feature_group_exists(
        args.resources, solution=args.solution, group=segment
    ) else None

    loaded = recipe.load_feature(
        args.resources, solution=args.solution, developer=developer, branch=branch, group=group
    )

    base_pattern = None
    storage_recipe = None
    platform_recipe = None
    try:
        platform = recipe.load_platform(
            args.resources, solution=args.solution, environment=args.base_environment
        )
        storage_recipe = platform            # Store, and therefore the storage block, is platform scope
        platform_recipe = platform           # and it carries the `references` declarations
        if not args.no_relation:
            base_pattern = platform.display_name_pattern
    except FabricOpsError as error:
        log.debug(f"no platform recipe available; relations and schema teardown skipped: {error}")

    plan = build_feature_plan(
        loaded,
        branch=branch,
        group=group,
        developer=developer,
        developer_object_id=args.developer_object_id,
        layers=args.layers.split(",") if args.layers else None,
        base_pattern=base_pattern,
        base_environment=args.base_environment,
        register_relation=not args.no_relation,
        storage_recipe=storage_recipe,
        base_recipe=platform_recipe,
        repository_root=getattr(args, "repo_path", ".") or ".",
    )

    if args.action == "update":
        # On every commit, converge only what a commit can change: the workspace must
        # exist, and git must be in sync. Roles and properties are left alone.
        plan.actions = [action for action in plan if action.kind in ("workspace", "git")]

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    log.header(f"Feature {args.action}: {branch}")
    log.info(f"fabricops {__version__}  run {log.run_id}" + ("  [dry-run]" if args.dry_run else ""))
    log.info(f"recipe   {', '.join(str(path) for path in loaded.sources)}")
    log.info(f"developer {developer or '-'}")
    for warning in plan.warnings:
        log.warning(f"⚠ {warning}")
    if group:
        log.info(f"group    {group} (feature.{group}.*)")
    elif segment:
        log.debug(f"branch segment '{segment}' is not a group recipe; matched against layer names")

    manifest = Manifest(
        run_id=log.run_id,
        command=f"feature {args.action}",
        solution=loaded.solution,
        environment=f"feature/{branch}",
        kind="Feature",
        dry_run=args.dry_run,
        sources=[str(path) for path in loaded.sources],
    )
    ctx = RunContext(
        cli=cli,
        log=log,
        recipe=loaded,
        dry_run=args.dry_run,
        credentials=_credentials(args, log),
        tag_registry=TagRegistry.discover(args.resources, args.solution),
    )
    report = execute(
        plan, ctx, manifest=manifest, destroy=args.action == "delete", stop_on_error=args.stop_on_error
    )
    summarise(log, report, dry_run=args.dry_run)
    log.info(f"manifest {manifest.write(args.manifest_root)}")
    return report.exit_code


# --------------------------------------------------------------- sanitise / export
def _sanitise(args: argparse.Namespace, log: RunLog) -> int:
    policy = sanitise_module.ExportPolicy.load(args.policy)
    files = sanitise_module.exportable_files(args.root, policy)

    if args.list_files:
        for path in files:
            log.info(str(pathlib.Path(path).relative_to(pathlib.Path(args.root).resolve())
                         if pathlib.Path(path).is_absolute() else path))
        return ExitCode.SUCCESS

    findings = sanitise_module.scan(args.root, policy, strict=args.strict)
    blocking = [finding for finding in findings if not finding.informational]
    notes = [finding for finding in findings if finding.informational]
    log.info(f"scanned {len(files)} exportable file(s) under {args.root}")

    for finding in notes:
        log.info(f"  note  {finding}")

    if not blocking:
        log.success("✔ no findings" + (f" ({len(notes)} note(s))" if notes else ""))
        return ExitCode.SUCCESS

    for finding in blocking:
        log.error(f"  {finding}")
    counts = ", ".join(f"{count} {kind}" for kind, count in sanitise_module.summarise(blocking).items())
    log.error(f"\n✖ {len(blocking)} finding(s): {counts}")
    log.info("Fix them, or record an accepted exception in the policy file.")
    return ExitCode.SUCCESS if args.report_only else ExitCode.ACTION_FAILED


def _export(args: argparse.Namespace, log: RunLog) -> int:
    policy = sanitise_module.ExportPolicy.load(args.policy)
    findings = [f for f in sanitise_module.scan(args.root, policy, strict=args.strict) if not f.informational]

    if findings and not args.force:
        for finding in findings:
            log.error(f"  {finding}")
        log.error(f"\n✖ export refused: {len(findings)} finding(s)")
        return ExitCode.ACTION_FAILED

    copied = sanitise_module.export(args.root, args.to, policy)
    log.success(f"✔ exported {len(copied)} file(s) to {args.to}")
    if findings:
        log.warning(f"⚠ exported with {len(findings)} unresolved finding(s) because --force was passed")
    log.info("Review the diff in the target repository before committing.")
    return ExitCode.SUCCESS


# --------------------------------------------------------------------- storage
def _storage_report(args: argparse.Namespace, log: RunLog) -> int:
    """Feature schemas sitting in shared storage - the mess `drop_on_teardown` prevents."""
    from .engine.storage import configured_lakehouses, list_feature_schemas, managed_prefix

    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    settings = loaded.storage["feature_schema"]
    references = configured_lakehouses(loaded)

    if not references:
        log.warning(
            "no lakehouses configured under storage.feature_schema.lakehouses - nothing to report"
        )
        return ExitCode.SUCCESS

    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)
    ctx = RunContext(cli=cli, log=log, recipe=loaded, dry_run=args.dry_run, credentials=_credentials(args, log))

    prefix = managed_prefix(str(settings["pattern"]))
    total = 0
    log.header(f"Feature schemas in {loaded.solution or 'default'} [{args.environment}]")
    for reference in references:
        workspace = loaded.workspace_name(reference.layer)
        schemas = list_feature_schemas(ctx, workspace, reference.item, prefix)
        total += len(schemas)
        log.info(f"\n{reference}  ({workspace})")
        if not schemas:
            log.success("  none")
        for schema in schemas:
            log.warning(f"  {schema}")

    log.info("")
    if total:
        log.warning(
            f"{total} feature schema(s) matching '{prefix}*'. "
            + ("Teardown drops them automatically." if settings["drop_on_teardown"]
               else "drop_on_teardown is off, so these accumulate until dropped by hand.")
        )
    else:
        log.success("✔ no feature schemas left behind")
    return ExitCode.SUCCESS


# ------------------------------------------------------------- variable libraries
def _solution_environments(args: argparse.Namespace) -> list[str]:
    """Environments this solution defines, which are also its value set names."""
    for entry in recipe.resolver.list_solutions(args.resources):
        name = None if entry["name"] == "default" else str(entry["name"])
        if name == args.solution:
            return list(entry["environments"])
    return []


def _varlib_render(args: argparse.Namespace, log: RunLog) -> int:
    from .generate import render_all, write

    environments = (
        [value.strip() for value in args.environments.split(",") if value.strip()]
        if args.environments
        else _solution_environments(args)
    )
    if not environments:
        raise FabricOpsError(
            "no environments to render value sets for",
            hint="Pass --environments dev,tst,prd, or add environment overlays to the solution.",
        )

    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=environments[0])
    plans = render_all(loaded, environments=environments, manifest_root=args.manifest_root)
    if not plans:
        log.warning("the recipe declares no variable libraries")
        return ExitCode.SUCCESS

    log.header(f"Variable libraries · value sets: {', '.join(environments)}")
    written = write(plans, args.out, loaded.solution)
    for plan, folder in zip(plans, written):
        log.info(f"  {plan.layer:<14} {plan.folder}  →  {folder}")
        for missing in plan.unresolved:
            log.warning(
                f"      ⚠ {missing}: no manifest for that environment yet, so the override was left out"
            )
    log.success(f"\n✔ {len(plans)} library definition(s) written")
    log.info("Commit them, or point the release at the generated folder.")
    return ExitCode.SUCCESS


def _varlib_activate(args: argparse.Namespace, log: RunLog) -> int:
    """Set the active value set. It is workspace state, so it survives no redeploy."""
    from .generate import declarations, target_layers

    loaded = recipe.load_platform(args.resources, solution=args.solution, environment=args.environment)
    cli = FabricCli(log, dry_run=args.dry_run)
    _authenticate(args, cli, log)

    log.header(f"Activate value set '{args.environment}'")
    activated = failed = 0
    for layer, declaration in declarations(loaded):
        for target in target_layers(loaded, layer, declaration):
            workspace = loaded.workspace_name(target)
            path = f"{workspace}.Workspace/{declaration['name']}.VariableLibrary"
            log.step(f"  {target} · {declaration['name']}")
            if args.dry_run:
                log.ok(" → would activate")
                continue
            try:
                workspace_id = cli.get_value(f"{workspace}.Workspace", "id")
                library_id = cli.get_value(path, "id")
                cli.api(
                    f"workspaces/{workspace_id}/variableLibraries/{library_id}",
                    method="patch",
                    body={"properties": {"activeValueSetName": args.environment}},
                    expect=(200,),
                )
                log.ok()
                activated += 1
            except FabricOpsError as error:
                log.fail()
                log.error(f"      {error}")
                failed += 1

    log.info("")
    if failed:
        log.error(f"✖ {activated} activated, {failed} failed")
        return ExitCode.ACTION_FAILED
    log.success(f"✔ {activated} library/libraries set to '{args.environment}'")
    return ExitCode.SUCCESS
