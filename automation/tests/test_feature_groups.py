"""A group recipe names its layers; it does not add to them."""

import json
import pathlib
import tempfile
import unittest

from fabricops import recipe
from fabricops.engine.feature import build_feature_plan
from fabricops.errors import RecipeError

GIT = {"gitProviderType": "AzureDevOps", "organizationName": "o", "projectName": "p", "repositoryName": "r"}

BASE = {
    "feature_name": "*Demo {feature_name} ({layer_name})",
    "capacity_name": "cap",
    "git_settings": {"gitProviderDetails": GIT, "myGitCredentials": {"source": "ConfiguredConnection", "connection_name": "Demo-AzureDevOps"}},
    "layers": {
        "Ingest": {"git_directoryName": "solution/engineering/ingest", "git_synchronize_on_commit": True},
        "Prepare": {"git_directoryName": "solution/engineering/prepare", "git_synchronize_on_commit": True,
                    "spark_settings": {"pool": {"starterPool": {"maxNodeCount": 1}}}},
        "Orchestrate": {"git_directoryName": "solution/engineering/orchestrate", "git_synchronize_on_commit": True},
        "Model": {"git_directoryName": "solution/analytics/model", "always": True,
                  "git_synchronize_on_commit": False, "git_disconnect_after_initialize": True},
    },
}


class FeatureGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = pathlib.Path(self.tmp.name) / "environments"; self.env.mkdir()
        self.write("feature.json", BASE)

    def write(self, name, data):
        (self.env / name).write_text(json.dumps(data))

    def load(self, **kw):
        return recipe.load_feature(self.tmp.name, developer=kw.pop("developer", "nobody"), **kw)

    # ---------------------------------------------------------------- selection
    def test_a_group_yields_exactly_the_layers_it_names(self):
        self.write("feature.engineering.json", {"layers": {"Ingest": {}, "Prepare": {}}})
        self.assertEqual(list(self.load(group="engineering").layers), ["Ingest", "Prepare"])

    def test_the_group_inherits_the_base_definitions(self):
        # Two empty braces per layer is all a group has to say.
        self.write("feature.engineering.json", {"layers": {"Ingest": {}, "Prepare": {}}})
        prepare = self.load(group="engineering").layer("Prepare")
        self.assertEqual(prepare["git"]["directory"], "solution/engineering/prepare")
        self.assertEqual(prepare["properties"]["sparkSettings.pool.starterPool.maxNodeCount"], 1)

    def test_always_true_does_not_survive_a_group(self):
        # Model is `always: true`; an engineering group is explicit, and explicit wins.
        self.write("feature.engineering.json", {"layers": {"Ingest": {}, "Prepare": {}}})
        self.assertNotIn("Model", self.load(group="engineering").layers)

    def test_a_group_can_override_a_setting_on_a_layer_it_names(self):
        self.write("feature.engineering.json", {"layers": {"Prepare": {"spark_settings": {"pool": {"starterPool": {"maxNodeCount": 4}}}}}})
        prepare = self.load(group="engineering").layer("Prepare")
        self.assertEqual(prepare["properties"]["sparkSettings.pool.starterPool.maxNodeCount"], 4)
        self.assertEqual(prepare["git"]["directory"], "solution/engineering/prepare", "the rest is inherited")

    def test_a_group_naming_an_unknown_layer_is_an_error(self):
        self.write("feature.bogus.json", {"layers": {"Warehouse": {}}})
        with self.assertRaises(RecipeError) as raised:
            self.load(group="bogus")
        self.assertIn("Warehouse", str(raised.exception))
        self.assertIn("cannot introduce", str(raised.exception))

    def test_a_group_that_names_no_layers_selects_nothing(self):
        self.write("feature.tuned.json", {"capacity_name": "other"})
        loaded = self.load(group="tuned")
        self.assertEqual(len(loaded.layers), 4)
        self.assertEqual(loaded.defaults["capacity"], "other")

    # ---------------------------------------------------------------- what is not a group
    def test_no_group_means_every_layer(self):
        self.assertEqual(len(self.load().layers), 4)

    def test_a_developer_overlay_tunes_but_never_selects(self):
        self.write("feature.peer.json", {"layers": {"Model": {"spark_settings": {"pool": {"starterPool": {"maxNodeCount": 2}}}}}})
        loaded = self.load(developer="peer")
        self.assertEqual(len(loaded.layers), 4, "a developer file listing one layer must not drop the others")

    def test_group_and_developer_compose(self):
        self.write("feature.engineering.json", {"layers": {"Ingest": {}, "Prepare": {}}})
        self.write("feature.peer.json", {"layers": {"Prepare": {"spark_settings": {"pool": {"starterPool": {"maxNodeCount": 8}}}}}})
        loaded = self.load(group="engineering", developer="peer")
        self.assertEqual(list(loaded.layers), ["Ingest", "Prepare"])
        self.assertEqual(loaded.layer("Prepare")["properties"]["sparkSettings.pool.starterPool.maxNodeCount"], 8)

    # ---------------------------------------------------------------- end to end: the plan
    def test_the_plan_for_a_group_branch_has_only_the_group_workspaces(self):
        self.write("feature.engineering.json", {"layers": {"Ingest": {"git_directoryName": "solution/engineering/ingest"}, "Prepare": {}}})
        loaded = self.load(group="engineering")
        plan = build_feature_plan(loaded, branch="feature/engineering/add-orders", group="engineering")
        workspaces = sorted(a.layer for a in plan if a.kind == "workspace")
        self.assertEqual(workspaces, ["Ingest", "Prepare"])
