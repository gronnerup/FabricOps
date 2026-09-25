"""Connection payloads and actions, including where secrets are allowed to appear."""

import json
import unittest

from fabricops.engine import Manifest, RunContext, build_plan, execute
from fabricops.engine.connections import (
    CreateConnection,
    Credentials,
    connection_roles,
    fabric_connection_payload,
    git_connection_payload,
    repository_url,
    sql_connection_payload,
)
from fabricops.errors import FabricOpsError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.obs.redaction import Secret
from fabricops.recipe import Recipe
from support import FakeFab

SECRET = "connection-client-secret-9911"
PAT = "ghp_connectiontokenabcdefghijklmnop"


def creds() -> Credentials:
    return Credentials(
        tenant_id="t-1",
        client_id="c-1",
        client_secret=Secret(SECRET, "client_secret"),
        github_pat=Secret(PAT, "github_pat"),
    )


class PayloadTests(unittest.TestCase):
    def test_sql_payload(self):
        payload = sql_connection_payload("srv.fabric.microsoft.com", "Curated", creds())
        self.assertEqual(payload["connectionDetails.type"], "SQL")
        self.assertEqual(payload["connectionDetails.parameters.database"], "Curated")
        self.assertIsInstance(payload["credentialDetails.servicePrincipalSecret"], Secret)

    def test_fabric_payload_maps_the_creation_method(self):
        payload = fabric_connection_payload("PowerBIDatasets", "ServicePrincipal", creds())
        self.assertEqual(payload["connectionDetails.creationMethod"], "PowerBIDatasets.Actions")
        self.assertIn("connectionDetails.parameters.options", payload)

    def test_unsupported_fabric_type_lists_the_known_ones(self):
        with self.assertRaises(FabricOpsError) as ctx:
            fabric_connection_payload("Nope", "ServicePrincipal", creds())
        self.assertIn("powerbidatasets", str(ctx.exception).lower())

    def test_missing_credentials_are_reported_before_any_call(self):
        with self.assertRaises(FabricOpsError) as ctx:
            sql_connection_payload("srv", "db", Credentials())
        self.assertIn("TENANT_ID", ctx.exception.hint or "")

    def test_github_payload_needs_a_pat(self):
        git = {"provider": "GitHub", "owner": "o", "repository": "r"}
        payload = git_connection_payload(git, creds())
        self.assertEqual(payload["credentialDetails.type"], "Key")
        self.assertIsInstance(payload["credentialDetails.key"], Secret)

        with self.assertRaises(FabricOpsError) as ctx:
            git_connection_payload(git, Credentials(tenant_id="t", client_id="c", client_secret=Secret("s")))
        self.assertIn("GITHUB_PAT", ctx.exception.hint or "")

    def test_azure_devops_payload_uses_the_service_principal(self):
        git = {"provider": "AzureDevOps", "organization": "org", "project": "proj", "repository": "repo"}
        payload = git_connection_payload(git, creds())
        self.assertEqual(payload["credentialDetails.type"], "ServicePrincipal")
        self.assertEqual(payload["connectionDetails.parameters.url"], "https://dev.azure.com/org/proj/_git/repo")

    def test_repository_url_per_provider(self):
        self.assertEqual(
            repository_url({"provider": "GitHub", "owner": "gronnerup", "repository": "FabricOps"}),
            "https://github.com/gronnerup/FabricOps",
        )
        with self.assertRaises(FabricOpsError):
            repository_url({"provider": "GitHub", "owner": "gronnerup"})
        with self.assertRaises(FabricOpsError):
            repository_url({"provider": "Gitea", "owner": "o", "repository": "r"})

    def test_admin_maps_to_owner(self):
        roles = connection_roles({"Admin": [{"type": "Group", "id": "g1"}], "Member": [{"type": "Group", "id": "g2"}]})
        self.assertEqual([(role["id"], role["role"]) for role in roles], [("g1", "Owner"), ("g2", "User")])


class ConnectionActionTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.console = _Buffer()
        self.log = RunLog(level=Level.DEBUG, stream=self.console, _colour=False)

    def tearDown(self):
        self.fab.cleanup()

    def run_plan(self, layers: dict, *, root: dict | None = None, dry_run: bool = False):
        data = {
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "defaults": {"capacity": "Trial-01"},
            "layers": layers,
        }
        data.update(root or {})
        recipe = Recipe(data=data, sources=(), environment="dev", solution="demo")
        cli = FabricCli(self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run)
        ctx = RunContext(cli=cli, log=self.log, recipe=recipe, dry_run=dry_run, credentials=creds(), sleep=lambda _s: None)
        report = execute(build_plan(recipe), ctx, manifest=Manifest(run_id="t"))
        return report, ctx

    def test_solution_connection_is_created_once_with_roles(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(["api", "roleAssignments"], {"status_code": 200, "text": {}, "headers": {}})
        self.fab.add(["get"], stdout="conn-1")

        root = {
            "connections": [{"name": "Demo-SemanticModel", "type": "PowerBIDatasets", "auth": "ServicePrincipal"}],
            "defaults": {"capacity": "Trial-01", "permissions": {"Admin": [{"type": "Group", "id": "g1"}]}},
        }
        report, ctx = self.run_plan({}, root=root)

        creations = [c for c in self.fab.commands if c.startswith("mkdir .connections/")]
        self.assertEqual(len(creations), 1)
        self.assertIn("connectionDetails.creationMethod=PowerBIDatasets.Actions", creations[0])
        self.assertEqual(ctx.output("connection:Demo-SemanticModel", "id"), "conn-1")
        self.assertTrue(any("roleAssignments" in c for c in self.fab.commands))

    def test_the_secret_reaches_the_cli_but_not_the_log(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add(["get"], stdout="conn-1")

        root = {"connections": [{"name": "Demo-SemanticModel", "type": "PowerBIDatasets", "auth": "ServicePrincipal"}]}
        self.run_plan({}, root=root)

        argv = next(call for call in self.fab.calls if call and call[0] == "mkdir")
        self.assertIn(SECRET, " ".join(argv), "the CLI must receive the real secret")
        self.assertNotIn(SECRET, self.console.text, "the console must not")

    def test_existing_connection_is_not_recreated(self):
        self.fab.add(["exists"], stdout="true")
        self.fab.add(["get"], stdout="conn-1")
        root = {"connections": [{"name": "Demo-SemanticModel", "type": "PowerBIDatasets"}]}
        report, _ctx = self.run_plan({}, root=root)
        self.assertEqual(report.counts.get("existed"), 1)
        self.assertFalse([c for c in self.fab.commands if c.startswith("mkdir")])

    def test_item_connection_uses_the_items_endpoint(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(
            ["get", "Curated.Lakehouse"],
            {
                "id": "lh-1",
                "properties": {
                    "sqlEndpointProperties": {
                        "provisioningStatus": "Success",
                        "connectionString": "abc.datawarehouse.fabric.microsoft.com",
                        "id": "ep-1",
                    }
                },
            },
        )
        self.fab.add(["get"], stdout="conn-or-ws-id")

        layers = {
            "Store": {
                "items": [
                    {"name": "Curated", "type": "Lakehouse", "connection": {"name": "Demo-Curated [dev]"}}
                ]
            }
        }
        self.run_plan(layers)

        connection = next(c for c in self.fab.commands if c.startswith("mkdir .connections/"))
        self.assertIn("connectionDetails.parameters.server=abc.datawarehouse.fabric.microsoft.com", connection)
        self.assertIn("connectionDetails.parameters.database=Curated", connection)

    def test_item_connection_without_an_endpoint_fails_with_a_hint(self):
        self.fab.add(["exists"], stdout="false")
        self.fab.add(["mkdir"], stdout="created")
        self.fab.add_json(["get", "Thing.Notebook"], {"id": "nb-1"})
        self.fab.add(["get"], stdout="ws-1")

        layers = {"Prepare": {"items": [{"name": "Thing", "type": "Notebook", "connection": {"name": "nope"}}]}}
        report, _ctx = self.run_plan(layers)

        self.assertEqual(report.exit_code, 1)
        self.assertIn("did not report an endpoint", str(report.failures[0][1]))

    def test_dry_run_creates_nothing(self):
        self.fab.add(["exists"], stdout="false")
        root = {"connections": [{"name": "Demo-SemanticModel", "type": "PowerBIDatasets"}]}
        self.run_plan({}, root=root, dry_run=True)
        self.assertFalse([c for c in self.fab.commands if c.startswith("mkdir")])


class _Buffer:
    def __init__(self):
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


if __name__ == "__main__":
    unittest.main()


class GitCredentialRefreshTests(unittest.TestCase):
    """A PAT expires; rotating it must not mean clicking through the portal."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def credentials(self, pat="ghp_freshtoken0123456789"):
        from fabricops.engine.connections import Credentials

        return Credentials(github_pat=Secret(pat, "github_pat"))

    def test_the_token_is_patched_onto_the_connection(self):
        from fabricops.engine.connections import refresh_git_credentials

        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add(["get"], stdout="conn-1", command="get")
        self.fab.add_json(["api", "connections/conn-1"], {"status_code": 200, "text": {}, "headers": {}})

        refresh_git_credentials(self.cli, connection_name="Creds", credentials=self.credentials())

        patch = next(c for c in self.fab.commands if "connections/conn-1" in c)
        self.assertIn("-X patch", patch)
        self.assertIn('"credentialType":"Key"', patch.replace(" ", ""))

    def test_the_token_never_reaches_the_log(self):
        from fabricops.engine.connections import refresh_git_credentials

        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add(["get"], stdout="conn-1", command="get")
        self.fab.add_json(["api", "connections/conn-1"], {"status_code": 200, "text": {}, "headers": {}})

        refresh_git_credentials(self.cli, connection_name="Creds", credentials=self.credentials())

        self.assertTrue(self.cli.log.redaction_enabled)
        # The process gets the real token; every rendered command must not.
        rendered = " ".join(
            record for record in getattr(self.cli, "skipped_writes", [])
        )
        self.assertNotIn("ghp_freshtoken0123456789", rendered)

    def test_no_token_is_a_clear_error(self):
        from fabricops.engine.connections import Credentials, refresh_git_credentials

        with self.assertRaises(FabricOpsError) as caught:
            refresh_git_credentials(self.cli, connection_name="Creds", credentials=Credentials())
        self.assertIn("--github-pat", str(caught.exception))

    def test_a_missing_connection_is_a_clear_error(self):
        from fabricops.engine.connections import refresh_git_credentials

        self.fab.add(["exists"], stdout="false", command="exists")
        with self.assertRaises(FabricOpsError) as caught:
            refresh_git_credentials(self.cli, connection_name="Gone", credentials=self.credentials())
        self.assertIn("does not exist", str(caught.exception))


class SolutionScopeTeardownTests(unittest.TestCase):
    """A solution-scoped object outlives any one environment."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def context(self):
        from fabricops.engine import RunContext
        from fabricops.recipe import Recipe

        return RunContext(
            cli=self.cli,
            log=self.log,
            recipe=Recipe(data={"display_name_pattern": "D - {layer} [{environment}]",
                                "layers": {"Store": {}}}, sources=(), environment="tst"),
            dry_run=False,
        )

    def action(self, scope):
        return CreateConnection(
            id="connection:Shared", kind="connection", label="Connection 'Shared'",
            connection_name="Shared", payload_kind="fabric", scope=scope,
        )

    def test_a_solution_scoped_connection_survives_an_environment_teardown(self):
        """is_primary defaults to true, so every environment thought it owned these."""
        self.fab.add(["exists"], stdout="true", command="exists")
        outcome = self.action("solution").destroy(self.context())
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("solution-scoped", outcome.message)
        self.assertFalse([c for c in self.fab.commands if c.startswith("rm")])

    def test_an_environment_scoped_connection_is_deleted(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        outcome = self.action("environment").destroy(self.context())
        self.assertEqual(outcome.status, "deleted")
        self.assertTrue([c for c in self.fab.commands if c.startswith("rm")])

    def test_the_plan_carries_the_declared_scope(self):
        from fabricops.engine import build_plan
        from fabricops.recipe import Recipe

        loaded = Recipe(
            data={
                "display_name_pattern": "D - {layer} [{environment}]",
                "defaults": {"connections": [
                    {"name": "Shared", "type": "PowerBIDatasets", "scope": "solution"},
                    {"name": "Local", "type": "PowerBIDatasets", "scope": "environment"},
                ]},
                "layers": {"Store": {}},
            },
            sources=(), environment="tst",
        )
        scopes = {
            a.connection_name: a.scope
            for a in build_plan(loaded) if isinstance(a, CreateConnection)
        }
        self.assertEqual(scopes["Shared"], "solution")
        self.assertEqual(scopes["Local"], "environment")
