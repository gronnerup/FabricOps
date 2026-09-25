"""The feature storage resolver's decision table.

The resolver ships inside a Fabric notebook, because `%run` is the only way to share code
between notebooks in a workspace. That notebook is still valid Python, so this executes it
with the Fabric-only modules stubbed and exercises the pure `_decide_*` functions directly.

What is covered is the part that decides where data goes. What is not, and cannot be
without a tenant, is the Spark call in `fork_table` and the workspace lookup in
`feature_name` - so both are written to fail toward the shared schema.
"""

import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
NOTEBOOK = (
    REPO_ROOT / "solution" / "engineering" / "prepare" / "Utils" / "fabricops_util.Notebook" / "notebook-content.py"
)


def load_notebook() -> dict:
    """Execute the helper notebook in a namespace with the Fabric modules stubbed out."""
    stubs = {}
    for name in ("sempy", "sempy.fabric", "pyspark", "pyspark.sql", "requests",
                 "requests.adapters", "urllib3", "urllib3.util", "urllib3.util.retry"):
        stubs[name] = types.ModuleType(name)
    stubs["pyspark.sql"].DataFrame = type("DataFrame", (), {})
    stubs["requests.adapters"].HTTPAdapter = type("HTTPAdapter", (), {})
    stubs["urllib3.util.retry"].Retry = type("Retry", (), {})
    stubs["sempy"].fabric = stubs["sempy.fabric"]

    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        namespace: dict = {"__name__": "fabricops_util"}
        exec(compile(NOTEBOOK.read_text(), str(NOTEBOOK), "exec"), namespace)
        return namespace
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@unittest.skipUnless(NOTEBOOK.is_file(), "solution tree not present")
class ResolverDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_notebook()

    def config(self, **overrides):
        config = dict(self.ns["FEATURE_STORAGE"])
        config.update(overrides)
        return config

    def write(self, table=None, forked=(), feature="add-orders", lakehouse="Curated", **flags):
        return self.ns["_decide_write"](table, set(forked), self.config(**flags), feature, lakehouse)

    def read(self, table, forked=(), feature="add-orders", lakehouse="Curated", **flags):
        return self.ns["_decide_read"](table, set(forked), self.config(**flags), feature, lakehouse)

    # ------------------------------------------------------------------ the switch
    def test_disabled_is_shared_for_everything(self):
        self.assertEqual(self.write("orders", enabled=False), "dbo")
        self.assertEqual(self.read("orders", forked=["orders"], enabled=False), "dbo")


    def test_outside_a_feature_workspace_is_shared(self):
        self.assertEqual(self.write("orders", feature=None, enabled=True), "dbo")
        self.assertEqual(self.read("orders", forked=["orders"], feature=None, enabled=True), "dbo")

    # ------------------------------------------------------------------ writes
    def test_write_always_to_feature_forks_everything(self):
        self.assertEqual(self.write("orders", enabled=True), "dev_add_orders")

    def test_write_always_to_feature_does_not_need_the_table_name(self):
        self.assertEqual(self.write(None, enabled=True), "dev_add_orders")

    def test_opt_in_writes_shared_for_a_table_not_yet_forked(self):
        self.assertEqual(
            self.write("orders", enabled=True, write_always_to_feature=False), "dbo"
        )

    def test_opt_in_writes_to_the_feature_for_a_table_already_forked(self):
        self.assertEqual(
            self.write("orders", forked=["orders"], enabled=True, write_always_to_feature=False),
            "dev_add_orders",
        )

    def test_opt_in_without_a_table_name_cannot_know_and_stays_shared(self):
        self.assertEqual(
            self.write(None, forked=["orders"], enabled=True, write_always_to_feature=False), "dbo"
        )

    # ------------------------------------------------------------------ reads
    def test_the_overlay_prefers_a_forked_table(self):
        self.assertEqual(self.read("orders", forked=["orders"], enabled=True), "dev_add_orders")

    def test_the_overlay_falls_back_for_a_table_not_forked(self):
        self.assertEqual(self.read("customers", forked=["orders"], enabled=True), "dbo")

    def test_the_overlay_can_be_turned_off(self):
        self.assertEqual(
            self.read("orders", forked=["orders"], enabled=True, read_overlay=False), "dbo"
        )

    def test_turning_the_overlay_off_does_not_change_where_writes_go(self):
        self.assertEqual(self.write("orders", enabled=True, read_overlay=False), "dev_add_orders")

    # ------------------------------------------------------------------ forkable lakehouses
    def test_a_lakehouse_outside_the_list_is_never_forked(self):
        # The list is given explicitly: the shipped value is a platform decision that differs
        # between solutions, and a test that leaned on it broke the moment one chose "all".
        only_curated = dict(enabled=True, forkable_lakehouses=["Curated"])
        self.assertEqual(self.write("orders", lakehouse="Base", **only_curated), "dbo")
        self.assertEqual(self.read("orders", forked=["orders"], lakehouse="Base", **only_curated), "dbo")
        self.assertEqual(self.write("orders", lakehouse="Curated", **only_curated), "dev_add_orders")

    def test_the_forkable_list_is_a_list_or_none(self):
        allowed = self.ns["FEATURE_STORAGE"]["forkable_lakehouses"]
        self.assertTrue(allowed is None or isinstance(allowed, list), allowed)

    def test_none_means_every_lakehouse(self):
        self.assertEqual(
            self.write("orders", enabled=True, lakehouse="Base", forkable_lakehouses=None),
            "dev_add_orders",
        )

    def test_an_unnamed_lakehouse_is_treated_as_forkable(self):
        self.assertEqual(self.write("orders", enabled=True, lakehouse=None), "dev_add_orders")

    # ------------------------------------------------------------------ naming
    def test_a_branch_topic_becomes_a_legal_schema_name(self):
        sanitise = self.ns["_sanitise_feature"]
        self.assertEqual(sanitise("add-orders"), "add_orders")
        self.assertEqual(sanitise("PEER/Add Orders!"), "peer_add_orders")
        self.assertEqual(sanitise("_trim_"), "trim")

    def test_the_pattern_must_carry_the_feature(self):
        # Without {feature} every feature would share one schema, which is worse than shared.
        self.assertIn("{feature}", self.ns["FEATURE_STORAGE"]["pattern"])

    def test_the_workspace_pattern_must_capture_a_feature(self):
        self.assertIn("(?P<feature>", self.ns["FEATURE_STORAGE"]["workspace_pattern"])

    def test_the_pattern_is_applied(self):
        self.assertEqual(
            self.ns["_feature_schema_for"]("add-orders", self.config()), "dev_add_orders"
        )
        self.assertEqual(
            self.ns["_feature_schema_for"]("x", self.config(pattern="sandbox_{feature}")), "sandbox_x"
        )


@unittest.skipUnless(NOTEBOOK.is_file(), "solution tree not present")
class WorkspacePatternTests(unittest.TestCase):
    """Detection reads the workspace name, so the pattern has to match what we create."""

    @classmethod
    def setUpClass(cls):
        cls.ns = load_notebook()

    def feature_in(self, workspace_name):
        import re

        match = re.match(self.ns["FEATURE_STORAGE"]["workspace_pattern"], workspace_name)
        return match.group("feature") if match else None

    def test_a_feature_workspace_with_a_solution_prefix(self):
        self.assertEqual(self.feature_in("*Demo sync03 (Prepare)"), "sync03")

    def test_a_feature_workspace_without_one(self):
        self.assertEqual(self.feature_in("*add-orders (Ingest)"), "add-orders")

    def test_an_environment_workspace_is_not_a_feature_workspace(self):
        for name in ("Demo - Prepare [dev]", "Confidence - Store [prd]", "Curated"):
            with self.subTest(workspace=name):
                self.assertIsNone(self.feature_in(name))

    def test_a_layer_name_with_spaces_still_parses(self):
        self.assertEqual(self.feature_in("*Demo add-orders (Data Prep)"), "add-orders")


@unittest.skipUnless(NOTEBOOK.is_file(), "solution tree not present")
class SwitchesAgreeTests(unittest.TestCase):
    """The recipe and the notebook each have an `enabled`, and they must not disagree.

    They do different jobs: the recipe's tells FabricOps whether feature teardown should
    look for schemas to drop, the notebook's tells the resolver whether to resolve. The
    mismatches are not symmetric. Recipe on with notebook off is harmless - teardown finds
    nothing. Recipe off with notebook on means notebooks create feature schemas that nothing
    ever drops, and they pile up in shared dev until someone notices.
    """

    def recipe_enabled(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT / "automation" / "src"))
        from fabricops import recipe as recipe_module

        loaded = recipe_module.load_platform(
            REPO_ROOT / "automation" / "resources", environment="dev"
        )
        return bool(loaded.storage["feature_schema"]["enabled"])

    def notebook_enabled(self):
        return bool(load_notebook()["FEATURE_STORAGE"]["enabled"])

    def test_the_notebook_is_not_on_while_the_recipe_is_off(self):
        if self.notebook_enabled() and not self.recipe_enabled():
            self.fail(
                "FEATURE_STORAGE['enabled'] is True in fabricops_util but "
                "storage.feature_schema.enabled is False in the recipe. Notebooks will create "
                "feature schemas that feature teardown will never drop. Turn the recipe flag "
                "on (and set drop_on_teardown), or turn the notebook flag off."
            )

    def test_they_agree(self):
        self.assertEqual(
            self.notebook_enabled(),
            self.recipe_enabled(),
            "The recipe and the notebook disagree about whether feature schemas are in use.",
        )


@unittest.skipUnless(NOTEBOOK.is_file(), "solution tree not present")
class BaseWorkspaceLookupTests(unittest.TestCase):
    """How a notebook in a feature workspace finds the base environment's storage."""

    @classmethod
    def setUpClass(cls):
        cls.f = staticmethod(load_notebook()["_base_workspace_for"])

    STAMP = "FabricOps: solution=default layer=Prepare branch=feature/x developer=peer created=t base=Brickyard - {layer} [dev]"

    def test_the_stamp_wins(self):
        self.assertEqual(self.f("Store", self.STAMP, "*Brickyard x (Prepare)", "dev"), "Brickyard - Store [dev]")

    def test_the_stamp_wins_even_when_the_name_says_otherwise(self):
        self.assertEqual(self.f("Store", self.STAMP, "*Other x (Prepare)", "tst"), "Brickyard - Store [dev]")

    def test_the_name_prefix_is_the_fallback(self):
        self.assertEqual(self.f("Store", None, "*Brickyard spark2.0 (Prepare)", "dev"), "Brickyard - Store [dev]")

    def test_an_unprefixed_feature_name_cannot_be_resolved(self):
        self.assertIsNone(self.f("Store", None, "*add-orders (Prepare)", "dev"))

    def test_a_stamp_without_base_falls_back_to_the_name(self):
        self.assertEqual(self.f("Store", "FabricOps: solution=default layer=Prepare", "*Brickyard x (Prepare)", "dev"),
                         "Brickyard - Store [dev]")


@unittest.skipUnless(NOTEBOOK.is_file(), "solution tree not present")
class CrossWorkspaceLookupTests(unittest.TestCase):
    """The lakehouses live in Store; the notebook does not. The lookup must be told where."""

    @classmethod
    def setUpClass(cls):
        cls.ns = load_notebook()

    def test_read_and_write_accept_the_storage_workspace(self):
        import inspect

        for name in ("read_schema", "write_schema"):
            params = inspect.signature(self.ns[name]).parameters
            self.assertIn("workspace_id", params, f"{name} must take the workspace the lakehouse lives in")
            self.assertIn("lakehouse", params)

    def test_forked_tables_is_keyed_by_workspace_and_lakehouse(self):
        import inspect

        self.assertEqual(list(inspect.signature(self.ns["forked_tables"]).parameters), ["lakehouse", "workspace_id"])
