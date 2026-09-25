"""Keeping committed item ids in step with a deployed environment."""

import json
import pathlib
import tempfile
import unittest

from fabricops.engine import references
from fabricops.errors import RecipeError
from fabricops.recipe import Recipe

WS_STORE = "11111111-1111-1111-1111-111111111111"
LH_CURATED = "22222222-2222-2222-2222-222222222222"
STALE = "99999999-9999-9999-9999-999999999999"


def make_recipe(entries):
    return Recipe(
        data={
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "layers": {"Store": {}, "Model": {}, "Present": {}},
            "references": entries,
        },
        sources=(),
        solution="demo",
        environment="dev",
    )


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-refs-"))

    def test_a_file_and_a_target_are_required(self):
        with self.assertRaises(RecipeError):
            references.declared(make_recipe([{"replace": [{"layer": "Store", "at": "a"}]}]), self.root)
        with self.assertRaises(RecipeError):
            references.declared(make_recipe([{"file": "x.json"}]), self.root)

    def test_a_target_needs_a_location(self):
        with self.assertRaises(RecipeError) as caught:
            references.declared(make_recipe([{"file": "x.json", "replace": [{"layer": "Store"}]}]), self.root)
        self.assertIn("`at`", str(caught.exception))

    def test_an_unknown_layer_is_rejected(self):
        with self.assertRaises(RecipeError):
            references.declared(
                make_recipe([{"file": "x.json", "replace": [{"layer": "Nope", "at": "a"}]}]), self.root
            )

    def test_each_pointer_becomes_its_own_reference(self):
        declared = references.declared(
            make_recipe([{"file": "x.json", "replace": [
                {"layer": "Store", "at": "a"},
                {"layer": "Model", "at": "b", "item": "M", "type": "SemanticModel"},
            ]}]),
            self.root,
        )
        self.assertEqual(len(declared), 2)
        self.assertEqual(declared[1].target.item, "M")


class JsonPathTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-refs-json-"))

    def reference(self, path, document):
        file = self.root / "doc.json"
        file.write_text(json.dumps(document))
        return references.Reference(
            file=file, target=references.Target("Store"), json_path=path, label="test"
        )

    def test_a_nested_value_is_read_and_written(self):
        ref = self.reference("a.b.c", {"a": {"b": {"c": STALE}}})
        self.assertEqual(references.read(ref), STALE)
        references.write(ref, STALE, WS_STORE)
        self.assertEqual(references.read(ref), WS_STORE)

    def test_a_list_index_is_a_path_segment(self):
        """A pipeline's activities are a list."""
        ref = self.reference("items.1.id", {"items": [{"id": "first"}, {"id": STALE}]})
        self.assertEqual(references.read(ref), STALE)
        references.write(ref, STALE, WS_STORE)
        self.assertEqual(json.loads(ref.file.read_text())["items"][0]["id"], "first")
        self.assertEqual(references.read(ref), WS_STORE)

    def test_a_missing_path_reads_as_nothing(self):
        self.assertIsNone(references.read(self.reference("a.missing", {"a": {}})))

    def test_an_index_out_of_range_reads_as_nothing(self):
        self.assertIsNone(references.read(self.reference("items.9.id", {"items": []})))

    def test_a_missing_file_reads_as_nothing(self):
        ref = references.Reference(
            file=self.root / "absent.json", target=references.Target("Store"), json_path="a", label="x"
        )
        self.assertIsNone(references.read(ref))


class PatternTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-refs-text-"))

    def reference(self, pattern, body, target=None):
        file = self.root / "expressions.tmdl"
        file.write_text(body)
        return references.Reference(
            file=file, target=target or references.Target("Store"), pattern=pattern, label="test"
        )

    def body(self, workspace=STALE, item=STALE):
        return f'Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{workspace}/{item}")\n'

    def test_the_capture_group_is_what_gets_replaced(self):
        ref = self.reference(r"onelake\.dfs\.fabric\.microsoft\.com/([0-9a-fA-F-]{36})", self.body())
        references.write(ref, STALE, WS_STORE)
        self.assertIn(f"/{WS_STORE}/{STALE}", ref.file.read_text())

    def test_only_the_matched_occurrence_changes(self):
        """The same id appearing elsewhere for another reason must be left alone."""
        ref = self.reference(
            r"onelake\.dfs\.fabric\.microsoft\.com/[0-9a-fA-F-]{36}/([0-9a-fA-F-]{36})",
            self.body() + f"// unrelated note about {STALE}\n",
        )
        references.write(ref, STALE, LH_CURATED)
        text = ref.file.read_text()
        self.assertIn(f"/{STALE}/{LH_CURATED}", text)
        self.assertIn(f"unrelated note about {STALE}", text)

    def test_no_match_reads_as_nothing(self):
        self.assertIsNone(references.read(self.reference(r"nothing-([0-9]+)", self.body())))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-refs-sync-"))
        self.file = self.root / "doc.json"
        self.file.write_text(json.dumps({"id": STALE}))
        self.ref = references.Reference(
            file=self.file, target=references.Target("Store"), json_path="id", label="store"
        )

    def test_a_report_without_apply_changes_nothing(self):
        report = references.sync([self.ref], resolve=lambda t: WS_STORE)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual(json.loads(self.file.read_text())["id"], STALE)
        self.assertIn("would be rewritten", report.describe()[0])

    def test_apply_writes_the_resolved_id(self):
        report = references.sync([self.ref], resolve=lambda t: WS_STORE, apply=True)
        self.assertEqual(json.loads(self.file.read_text())["id"], WS_STORE)
        self.assertTrue(report.applied)

    def test_a_matching_id_is_not_rewritten(self):
        self.file.write_text(json.dumps({"id": WS_STORE}))
        report = references.sync([self.ref], resolve=lambda t: WS_STORE, apply=True)
        self.assertEqual(report.changed, [])
        self.assertEqual(report.rewrites[0].state, "already correct")

    def test_case_differences_are_not_changes(self):
        self.file.write_text(json.dumps({"id": WS_STORE.upper()}))
        report = references.sync([self.ref], resolve=lambda t: WS_STORE, apply=True)
        self.assertEqual(report.changed, [])

    def test_an_undeployed_target_warns_and_leaves_the_file(self):
        report = references.sync([self.ref], resolve=lambda t: None, apply=True)
        self.assertEqual(json.loads(self.file.read_text())["id"], STALE)
        self.assertTrue(report.warnings)
        self.assertIn("not deployed", report.warnings[0])

    def test_a_missing_location_warns(self):
        ref = references.Reference(
            file=self.file, target=references.Target("Store"), json_path="nope", label="x"
        )
        report = references.sync([ref], resolve=lambda t: WS_STORE, apply=True)
        self.assertIn("nothing found", report.warnings[0])


if __name__ == "__main__":
    unittest.main()


class GitConnectedGuardTests(unittest.TestCase):
    """Committed ids belong to the environment git syncs into, and to no other."""

    def recipe_for(self, environment, git):
        return Recipe(
            data={
                "display_name_pattern": "Demo - {layer} [{environment}]",
                "defaults": {"git": {"provider": "GitHub"}} if git else {},
                "layers": {"Store": {}},
            },
            sources=(),
            solution="demo",
            environment=environment,
        )

    def test_a_git_connected_environment_owns_the_committed_ids(self):
        self.assertTrue(self.recipe_for("dev", git=True).defaults.get("git"))

    def test_a_deployed_environment_does_not(self):
        """Writing tst ids into the repository would point dev at the wrong items."""
        self.assertFalse(self.recipe_for("tst", git=False).defaults.get("git"))
