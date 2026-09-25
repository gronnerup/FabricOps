"""Connection payloads and connection actions.

The Fabric CLI creates connections through `mkdir .connections/<name>.Connection -P k=v`,
so each connection kind is a payload builder rather than a bespoke function. Credentials
are wrapped in `Secret`, so they are masked in every log sink.

Residual risk worth stating plainly: the CLI's only documented surface for connection
creation puts parameters on the command line, so a secret is briefly visible in the
process table of the machine running the deployment. Wrapping it keeps it out of logs,
traces and error messages; it does not hide it from `ps` on that host.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import FabricOpsError
from ..fabric.paths import FabPath, params as render_params
from ..obs.redaction import Secret
from .actions import Action, ActionResult

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext

# Fabric connection type -> creation method.
FABRIC_CREATION_METHODS: dict[str, str] = {
    "fabricsql": "FabricSql.Contents",
    "fabricdatapipelines": "FabricDataPipelines.Actions",
    "warehouse": "Fabric.Warehouse",
    "powerbidatasets": "PowerBIDatasets.Actions",
    "fabriclakehouse": "FabricLakehouse.Contents",
}

GIT_PROVIDER_TYPES: dict[str, tuple[str, str]] = {
    "github": ("GitHubSourceControl", "GitHubSourceControl.Contents"),
    "azuredevops": ("AzureDevOpsSourceControl", "AzureDevOpsSourceControl.Contents"),
}


@dataclass
class Credentials:
    """Whatever identity material this run was given."""

    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: Secret = field(default_factory=lambda: Secret(None, "client_secret"))
    github_pat: Secret = field(default_factory=lambda: Secret(None, "github_pat"))

    @property
    def has_service_principal(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    def require_service_principal(self, purpose: str) -> None:
        if not self.has_service_principal:
            raise FabricOpsError(
                f"{purpose} needs service principal credentials",
                hint="Pass --tenant-id/--client-id/--client-secret, or set TENANT_ID/CLIENT_ID/CLIENT_SECRET.",
            )


def sql_connection_payload(server: str, database: str, credentials: Credentials) -> dict[str, Any]:
    credentials.require_service_principal("Creating a SQL connection")
    return {
        "privacyLevel": "Organizational",
        "connectionDetails.type": "SQL",
        "connectionDetails.creationMethod": "SQL",
        "connectionDetails.parameters.server": server,
        "connectionDetails.parameters.database": database,
        "credentialDetails.connectionEncryption": "Encrypted",
        "credentialDetails.type": "ServicePrincipal",
        "credentialDetails.tenantId": credentials.tenant_id,
        "credentialDetails.servicePrincipalClientId": credentials.client_id,
        "credentialDetails.servicePrincipalSecret": credentials.client_secret,
    }


def fabric_connection_payload(connection_type: str, auth: str, credentials: Credentials) -> dict[str, Any]:
    creation_method = FABRIC_CREATION_METHODS.get(connection_type.lower())
    if not creation_method:
        raise FabricOpsError(
            f"unsupported Fabric connection type '{connection_type}'",
            hint=f"Known types: {', '.join(sorted(FABRIC_CREATION_METHODS))}",
        )
    payload: dict[str, Any] = {
        "privacyLevel": "Organizational",
        "connectionDetails.type": connection_type,
        "connectionDetails.creationMethod": creation_method,
        # The API rejects a duplicate connection definition, so vary a benign option.
        "connectionDetails.parameters.options": str(int(time.time())),
        "credentialDetails.connectionEncryption": "NotEncrypted",
        "credentialDetails.type": auth,
    }
    if auth == "ServicePrincipal":
        credentials.require_service_principal(f"Creating a {connection_type} connection")
        payload.update(
            {
                "credentialDetails.tenantId": credentials.tenant_id,
                "credentialDetails.servicePrincipalClientId": credentials.client_id,
                "credentialDetails.servicePrincipalSecret": credentials.client_secret,
            }
        )
    return payload


def git_connection_payload(git: dict[str, Any], credentials: Credentials) -> dict[str, Any]:
    provider = str(git.get("provider") or "").lower()
    if provider not in GIT_PROVIDER_TYPES:
        raise FabricOpsError(
            f"unsupported git provider '{git.get('provider')}'",
            hint="Use GitHub or AzureDevOps.",
        )
    connection_type, creation_method = GIT_PROVIDER_TYPES[provider]
    payload: dict[str, Any] = {
        "privacyLevel": "Organizational",
        "connectionDetails.type": connection_type,
        "connectionDetails.creationMethod": creation_method,
        "connectionDetails.parameters.url": repository_url(git),
    }
    if provider == "github":
        if not credentials.github_pat:
            raise FabricOpsError(
                "a GitHub source control connection needs a personal access token",
                hint="Pass --github-pat or set GITHUB_PAT.",
            )
        payload.update(
            {
                "credentialDetails.connectionEncryption": "Encrypted",
                "credentialDetails.type": "Key",
                "credentialDetails.key": credentials.github_pat,
            }
        )
    else:
        credentials.require_service_principal("An Azure DevOps source control connection")
        payload.update(
            {
                "credentialDetails.connectionEncryption": "NotEncrypted",
                "credentialDetails.type": "ServicePrincipal",
                "credentialDetails.tenantId": credentials.tenant_id,
                "credentialDetails.servicePrincipalClientId": credentials.client_id,
                "credentialDetails.servicePrincipalSecret": credentials.client_secret,
            }
        )
    return payload


def repository_url(git: dict[str, Any]) -> str:
    """The repository URL for a git node - one place, validated per provider."""
    provider = str(git.get("provider") or "").lower()
    if provider == "github":
        owner, repository = git.get("owner"), git.get("repository")
        if not owner or not repository:
            raise FabricOpsError("a GitHub git node needs 'owner' and 'repository'")
        return f"https://github.com/{owner}/{repository}"
    if provider == "azuredevops":
        organization, project, repository = git.get("organization"), git.get("project"), git.get("repository")
        if not organization or not project or not repository:
            raise FabricOpsError("an Azure DevOps git node needs 'organization', 'project' and 'repository'")
        return f"https://dev.azure.com/{organization}/{project}/_git/{repository}"
    raise FabricOpsError(f"unsupported git provider '{git.get('provider')}'")


# ------------------------------------------------------------------------ actions
@dataclass
class CreateConnection(Action):
    connection_name: str = ""
    payload_kind: str = "fabric"          # fabric | git | sql
    connection_type: str | None = None
    auth: str = "ServicePrincipal"
    git: dict[str, Any] = field(default_factory=dict)
    source_action: str | None = None      # item action supplying server/database
    source_item: dict[str, Any] | None = None   # {workspace, name, type} resolved live
    scope: str = "environment"                  # solution-scoped objects outlive one environment
    roles: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        return f"Connection '{self.connection_name}'"

    def detail(self) -> str:
        if self.payload_kind == "git":
            return f"{self.git.get('provider')} source control"
        if self.payload_kind == "sql":
            if self.source_item:
                return f"SQL \u00b7 from {self.source_item['type']} {self.source_item['name']}"
            source = (self.source_action or "").split(":")[-1]
            return f"SQL \u00b7 from item {source}" if source else "SQL"
        return f"{self.connection_type} \u00b7 {self.auth}"

    @property
    def path(self) -> FabPath:
        return FabPath.connection(self.connection_name)

    def apply(self, ctx: "RunContext") -> ActionResult:
        existed = ctx.cli.exists(self.path)
        if not existed:
            waiting = self._waiting_for_item(ctx)
            if waiting:
                return ActionResult("skipped", message=waiting)
            try:
                payload = self._payload(ctx)
            except FabricOpsError as error:
                if ctx.dry_run:
                    # A plan must be inspectable without secrets: report what is missing
                    # rather than failing the run.
                    return ActionResult("skipped", message=f"needs credentials - {error.message}")
                raise
            ctx.cli.mkdir(self.path, params=render_params(payload))

        connection_id = None
        if not ctx.dry_run:
            connection_id = ctx.cli.get_value(self.path, "id")
        self._assign_roles(ctx, connection_id)

        outputs = {"name": self.connection_name}
        if connection_id:
            outputs["id"] = connection_id
        return ActionResult("existed" if existed else "created", outputs)

    def destroy(self, ctx: "RunContext") -> ActionResult | None:
        if self.scope == "solution":
            # Solution-scoped objects exist once for the whole solution, so tearing down one
            # environment must not take them. `is_primary` defaults to true when a recipe
            # does not say otherwise, which meant every environment believed it owned them -
            # and a tst teardown would have deleted the connection dev depends on.
            return ActionResult(
                "skipped", message="solution-scoped; not owned by this environment"
            )
        if not ctx.cli.exists(self.path):
            return ActionResult("skipped", message="does not exist")
        ctx.cli.rm(self.path)
        return ActionResult("deleted")

    # ------------------------------------------------------------------ internals
    def _payload(self, ctx: "RunContext") -> dict[str, Any]:
        if self.payload_kind == "git":
            return git_connection_payload(self.git, ctx.credentials)
        if self.payload_kind == "sql":
            server, database = self._sql_target(ctx)
            return sql_connection_payload(server, database, ctx.credentials)
        return fabric_connection_payload(str(self.connection_type), self.auth, ctx.credentials)

    def _waiting_for_item(self, ctx: "RunContext") -> str | None:
        """Why this connection cannot be made yet, or None if it can.

        A connection built `from_item` points at something the *repository* owns and
        `fabricops release` deploys, so on a fresh environment it does not exist during
        setup. That is an ordinary state, not a failure: setup says what it is waiting for
        and moves on, and the connection appears when release syncs it (or on the next
        setup run).
        """
        if not self.source_item:
            return None
        # Checked in a dry run too. It is a read, and a dry run that reports a different
        # reason from the real run is worse than no dry run - it was saying "needs
        # credentials" for a connection whose actual problem was a missing item.
        if ctx.cli.exists(self._item_path()):
            return None
        if ctx.dry_run and not ctx.cli.exists(FabPath.workspace(str(self.source_item["workspace"]))):
            # A dry run cannot see the effect of its own earlier steps. The workspace is
            # missing because this very run would have created it, so "would wait" would
            # be pessimistic rather than accurate.
            return (
                f"would resolve {self.source_item['type']} '{self.source_item['name']}' once "
                f"{self.source_item['workspace']} exists"
            )
        prefix = "would wait for" if ctx.dry_run else "waiting for"
        return (
            f"{prefix} {self.source_item['type']} '{self.source_item['name']}' in "
            f"{self.source_item['workspace']} - it is deployed by `fabricops release`"
        )

    def _item_path(self) -> FabPath:
        item = self.source_item or {}
        return FabPath.item(str(item["workspace"]), str(item["name"]), str(item["type"]))

    def _sql_target(self, ctx: "RunContext") -> tuple[str, str]:
        if ctx.dry_run:
            return ("<sqlendpoint>", "<database>")
        if self.source_item:
            return self._resolve_from_item(ctx)
        outputs = ctx.outputs.get(self.source_action or "", {})
        server = outputs.get("sqlendpoint")
        database = outputs.get("database_name") or outputs.get("name")
        if not server or not database:
            raise FabricOpsError(
                f"cannot build the SQL connection '{self.connection_name}': "
                f"the source item did not report an endpoint",
                hint="Only SQL-backed item types (Lakehouse, Warehouse, SQLDatabase) expose one.",
            )
        return (str(server), str(database))

    def _resolve_from_item(self, ctx: "RunContext") -> tuple[str, str]:
        """Read the endpoint off the deployed item, waiting for it to be usable.

        The handler for the item type knows both where the endpoint lives and when it is
        ready, so this asks it rather than repeating that knowledge - and waits, because a
        lakehouse that has only just arrived from a git sync has an endpoint that is still
        provisioning. Reading once here would produce a connection to nothing.
        """
        from .actions import CreateItem
        from .handlers import handler_for

        item = self.source_item or {}
        handler = handler_for(str(item["type"]))
        probe = CreateItem(
            id=f"probe:{item['name']}",
            kind="item",
            workspace=str(item["workspace"]),
            item={"name": item["name"], "type": item["type"]},
        )
        metadata = handler.wait_ready(ctx, probe)
        outputs = handler.outputs(metadata)
        server = outputs.get("sqlendpoint")
        database = outputs.get("database_name") or item["name"]
        if not server:
            raise FabricOpsError(
                f"cannot build the SQL connection '{self.connection_name}': "
                f"{item['type']} '{item['name']}' reports no SQL endpoint",
                hint="Only SQL-backed item types (Lakehouse, Warehouse, SQLDatabase) expose one.",
            )
        return (str(server), str(database))

    def _assign_roles(self, ctx: "RunContext", connection_id: str | None) -> None:
        if not self.roles or not connection_id:
            return
        for principal in self.roles:
            body = {
                "principal": {"id": principal.get("id"), "type": principal.get("type", "Group")},
                "role": principal.get("role", "Owner"),
            }
            ctx.cli.api(f"connections/{connection_id}/roleAssignments", method="post", body=body,
                        expect=(200, 201, 409), check=False)


def connection_roles(permissions: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Workspace-style permissions mapped onto connection roles (Admin -> Owner)."""
    roles: list[dict[str, Any]] = []
    for role, principals in (permissions or {}).items():
        connection_role = "Owner" if str(role).lower() == "admin" else "User"
        for principal in principals or []:
            if principal.get("id"):
                roles.append({**principal, "role": connection_role})
    return roles


# ------------------------------------------------------------- credential refresh
#: Fabric's own name for the credential shape each provider uses.
CREDENTIAL_TYPES = {"GitHub": "Key", "AzureDevOps": "Key"}


def refresh_git_credentials(
    cli: "FabricCli",
    *,
    connection_name: str,
    credentials: Credentials,
    provider: str = "GitHub",
    log: Any = None,
) -> str:
    """Replace the token stored in a git credentials connection.

    A personal access token expires - GitHub's default is 30 to 90 days - and when it does,
    every `git/connect` in every workspace fails with GitProviderBadCredentials. Without
    this the only remedy is clicking through the portal, which is a poor answer from a tool
    whose whole point is that the platform is reproducible.

    Uses `PATCH /connections/{id}`, which is the documented way to set credentials on a
    connection (Fabric Connections API).
    """
    token = credentials.github_pat if provider.lower() == "github" else credentials.github_pat
    if not token or not token.reveal():
        raise FabricOpsError(
            f"no {provider} token available to refresh connection '{connection_name}'",
            hint="Pass --github-pat, set GITHUB_PAT, or put `github_pat` in the credentials file.",
        )

    path = FabPath.connection(connection_name)
    if not cli.exists(path):
        raise FabricOpsError(
            f"connection '{connection_name}' does not exist",
            hint="`fabricops setup` creates it from the recipe's `defaults.git.credentials.connection`.",
        )
    connection_id = cli.get_value(path, "id")

    body = {
        "connectivityType": "ShareableCloud",
        "credentialDetails": {
            "credentials": {
                "credentialType": CREDENTIAL_TYPES.get(provider, "Key"),
                "key": token.reveal(),
            }
        },
    }
    cli.api(f"connections/{connection_id}", method="patch", body=body, expect=(200, 201))
    if log:
        log.info(f"  refreshed the token stored in '{connection_name}'")
    return str(connection_id)
