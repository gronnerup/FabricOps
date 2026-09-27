"""Path building replaces the hand-rolled escaping in today's scripts."""

import unittest

from fabricops.fabric.paths import FabPath, escape_name, params
from fabricops.obs.redaction import MaskedValue, Secret, render_argv, to_process_argv


class PathTests(unittest.TestCase):
    def test_workspace_path(self):
        self.assertEqual(str(FabPath.workspace("Sales - Store [dev]")), "Sales - Store [dev].Workspace")

    def test_slash_in_display_name_is_escaped(self):
        self.assertEqual(escape_name("Sales/Ops"), "Sales\\/Ops")
        self.assertEqual(str(FabPath.workspace("Sales/Ops [dev]")), "Sales\\/Ops [dev].Workspace")

    def test_item_path(self):
        path = FabPath.item("Sales - Store [dev]", "Curated", "Lakehouse")
        self.assertEqual(str(path), "Sales - Store [dev].Workspace/Curated.Lakehouse")

    def test_awkward_display_names_survive(self):
        for name in ["O'Brien & Co", "Sales, Marketing", "Ops (EU) [dev]"]:
            with self.subTest(name=name):
                self.assertIn(name, str(FabPath.workspace(name)))

    def test_folder_path_normalisation(self):
        self.assertEqual(str(FabPath.folder("WS", "/Zones/Curated/")), "WS.Workspace/Zones/Curated")

    def test_virtual_containers(self):
        self.assertEqual(str(FabPath.connection("Sales-GitHub")), ".connections/Sales-GitHub.Connection")
        self.assertEqual(
            str(FabPath.managed_identity("Sales - Orchestrate [dev]")),
            "Sales - Orchestrate [dev].Workspace/.managedidentities/Sales - Orchestrate [dev].ManagedIdentity",
        )
        self.assertEqual(
            str(FabPath.managed_private_endpoint("WS", "kv-pe")),
            "WS.Workspace/.managedprivateendpoints/kv-pe.ManagedPrivateEndpoint",
        )

    def test_join_operator(self):
        self.assertEqual(str(FabPath.workspace("WS") / "Files/raw"), "WS.Workspace/Files/raw")


class ParamRenderingTests(unittest.TestCase):
    def test_flat_params(self):
        self.assertEqual(params({"capacityname": "Trial-01"}), "capacityname=Trial-01")

    def test_booleans_are_lowercased(self):
        self.assertEqual(params({"enableSchemas": True, "autoApproveEnabled": False}),
                         "enableSchemas=true,autoApproveEnabled=false")

    def test_a_secret_parameter_is_revealed_to_the_process_and_masked_in_logs(self):
        rendered = params({"credentialDetails.key": Secret("ghp_realtokenvalue123456", "pat"), "url": "https://x/y"})
        self.assertIsInstance(rendered, MaskedValue)
        self.assertIn("ghp_realtokenvalue123456", to_process_argv(["mkdir", rendered])[1])
        self.assertNotIn("ghp_realtokenvalue123456", render_argv(["mkdir", rendered]))
        self.assertIn("credentialDetails.key=***", render_argv(["mkdir", rendered]))
        self.assertIn("url=https://x/y", render_argv(["mkdir", rendered]))

    def test_params_without_secrets_stays_a_plain_string(self):
        self.assertIsInstance(params({"capacityname": "Trial-01"}), str)

    def test_nested_mappings_are_flattened(self):
        rendered = params({"pool": {"starterPool": {"maxNodeCount": 1, "maxExecutors": 1}}})
        self.assertEqual(rendered, "pool.starterPool.maxNodeCount=1,pool.starterPool.maxExecutors=1")


if __name__ == "__main__":
    unittest.main()
