"""Item definitions (E03-S4) and drift detection (E03-S8)."""

import json
import pathlib
import tempfile
import unittest

from fabricops.engine import definitions
from fabricops.engine.actions import ImportDefinition
from fabricops.engine.context import RunContext
from fabricops.engine.drift import DIFFERS, IN_SYNC, MISSING, detect
from fabricops.engine.plan import build_plan
from fabricops.errors import ExitCode, RecipeError
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab

PATTERN = "Demo - {layer} [{environment}]"


def make_recipe(layers, environment="dev") -> Recipe:
    return Recipe(
        data={"display_name_pattern": PATTERN, "layers": layers},
        sources=(),
        solution="demo",
        environment=environment,
    )


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-defs-"))

    def test_a_repository_directory_is_used_as_is(self):
        source_dir = self.root / "solution" / "store" / "Cfg.VariableLibrary"
        source_dir.mkdir(parents=True)
        (source_dir / "variables.json").write_text("{}")
        resolved = definitions.resolve(
            {"from": "solution/store/Cfg.VariableLibrary"}, root=self.root, context={}, where="x"
        )
        self.assertEqual(resolved.directory, source_dir.resolve())
        self.assertFalse(resolved.is_inline)

    def test_a_missing_directory_is_a_recipe_error(self):
        with self.assertRaises(RecipeError) as caught:
            definitions.resolve({"from": "nope"}, root=self.root, context={}, where="layers.Store.items.X")
        self.assertIn("layers.Store.items.X", str(caught.exception))

    def test_a_file_is_not_a_definition_directory(self):
        (self.root / "file.txt").write_text("x")
        with self.assertRaises(RecipeError):
            definitions.resolve({"from": "file.txt"}, root=self.root, context={}, where="x")

    def test_from_and_parts_together_are_rejected(self):
        with self.assertRaises(RecipeError):
            definitions.resolve({"from": "a", "parts": [{"path": "b"}]}, root=self.root, context={}, where="x")

    def test_neither_from_nor_parts_is_rejected(self):
        with self.assertRaises(RecipeError):
            definitions.resolve({}, root=self.root, context={}, where="x")

    def test_inline_parts_are_token_substituted(self):
        resolved = definitions.resolve(
            {"parts": [{"path": "content.py", "content": "layer = '{layer}'"}]},
            root=self.root,
            context={"layer": "Store"},
            where="x",
        )
        self.assertEqual(resolved.parts, (("content.py", "layer = 'Store'"),))

    def test_braces_that_are_not_tokens_survive(self):
        """A notebook is full of braces; treating each as a token would be useless."""
        code = 'name = "x"\nprint(f"{name}")\ncfg = {"layer": "{layer}"}\n'
        resolved = definitions.resolve(
            {"parts": [{"path": "nb.py", "content": code}]},
            root=self.root,
            context={"layer": "Store"},
            where="x",
        )
        rendered = resolved.parts[0][1]
        self.assertIn('f"{name}"', rendered)
        self.assertIn('"layer": "Store"', rendered)

    def test_a_part_can_come_from_a_file(self):
        (self.root / "template.py").write_text("env = '{environment}'")
        resolved = definitions.resolve(
            {"parts": [{"path": "nb.py", "from": "template.py"}]},
            root=self.root,
            context={"environment": "dev"},
            where="x",
        )
        self.assertEqual(resolved.parts[0][1], "env = 'dev'")

    def test_a_part_needs_a_path(self):
        with self.assertRaises(RecipeError):
            definitions.resolve({"parts": [{"content": "x"}]}, root=self.root, context={}, where="x")


class ContentHashTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-hash-"))

    def write(self, name: str, body: bytes) -> pathlib.Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "content.py").write_bytes(body)
        return directory

    def test_identical_content_hashes_the_same(self):
        a, b = self.write("a", b"print(1)\n"), self.write("b", b"print(1)\n")
        self.assertEqual(definitions.content_hash(a), definitions.content_hash(b))

    def test_different_content_hashes_differently(self):
        a, b = self.write("a", b"print(1)\n"), self.write("b", b"print(2)\n")
        self.assertNotEqual(definitions.content_hash(a), definitions.content_hash(b))

    def test_line_endings_are_normalised(self):
        """A CRLF checkout must not read as permanent drift."""
        a, b = self.write("a", b"print(1)\n"), self.write("b", b"print(1)\r\n")
        self.assertEqual(definitions.content_hash(a), definitions.content_hash(b))

    def test_the_platform_file_is_ignored(self):
        a = self.write("a", b"print(1)\n")
        before = definitions.content_hash(a)
        (a / ".platform").write_text(json.dumps({"config": {"logicalId": "abc"}}))
        self.assertEqual(definitions.content_hash(a), before)

    def test_the_exported_item_folder_is_unwrapped(self):
        exported = self.root / "export"
        item = exported / "Boot.Notebook"
        item.mkdir(parents=True)
        (item / "content.py").write_bytes(b"print(1)\n")
        plain = self.write("plain", b"print(1)\n")
        self.assertEqual(
            definitions.exported_hash(exported, "Boot", "Notebook"), definitions.content_hash(plain)
        )

    def test_nothing_exported_means_no_hash(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(definitions.exported_hash(empty, "Boot", "Notebook"))

    def test_inline_parts_hash_without_being_kept(self):
        source = definitions.DefinitionSource(parts=(("content.py", "print(1)\n"),))
        self.assertEqual(definitions.source_hash(source), definitions.source_hash(source))


class PlanWiringTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-plan-defs-"))
        source = self.root / "bootstrap"
        source.mkdir()
        (source / "content.py").write_text("print(1)")

    def test_an_item_with_a_definition_gets_an_import_action(self):
        loaded = make_recipe({"Store": {"items": [
            {"name": "Boot", "type": "Notebook", "definition": {"from": "bootstrap"}},
        ]}})
        plan = build_plan(loaded, repository_root=self.root)
        imports = [action for action in plan if isinstance(action, ImportDefinition)]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0].depends_on, ("item:Store:Notebook:Boot",))

    def test_properties_land_after_the_definition(self):
        """An import replaces the whole definition, so a property set before it is lost."""
        loaded = make_recipe({"Store": {"items": [
            {"name": "Boot", "type": "Notebook", "definition": {"from": "bootstrap"},
             "properties": {"displayName": "Boot"}},
        ]}})
        plan = build_plan(loaded, repository_root=self.root)
        properties = next(action for action in plan if action.kind == "properties")
        self.assertEqual(properties.depends_on, ("item:Store:Notebook:Boot:definition",))

    def test_an_item_without_a_definition_gets_no_import(self):
        loaded = make_recipe({"Store": {"items": [{"name": "Curated", "type": "Lakehouse"}]}})
        plan = build_plan(loaded, repository_root=self.root)
        self.assertFalse([action for action in plan if isinstance(action, ImportDefinition)])


class ImportIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-import-"))
        self.source = self.root / "bootstrap"
        self.source.mkdir()
        (self.source / "content.py").write_text("print(1)")
        self.log = RunLog(level=Level.OFF)

    def context(self, dry_run=False) -> RunContext:
        cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run
        )
        return RunContext(cli=cli, log=self.log, recipe=make_recipe({"Store": {}}), dry_run=dry_run)

    def action(self) -> ImportDefinition:
        return ImportDefinition(
            id="item:Store:Notebook:Boot:definition",
            kind="definition",
            layer="Store",
            target_workspace="Demo - Store [dev]",
            item={"name": "Boot", "type": "Notebook"},
            source=definitions.resolve({"from": "bootstrap"}, root=self.root, context={}, where="x"),
            repository_root=self.root,
        )

    def test_a_missing_item_is_imported(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        result = self.action().apply(self.context())
        self.assertEqual(result.status, "created")
        self.assertTrue([c for c in self.fab.commands if c.startswith("import ")])

    def test_matching_content_is_not_reimported(self):
        self.fab.add(["exists"], stdout="true", command="exists")

        action = self.action()
        expected = definitions.source_hash(action.source)
        original = action._current_hash
        action._current_hash = lambda ctx: expected      # stand in for a real export

        result = action.apply(self.context())
        action._current_hash = original
        self.assertEqual(result.status, "existed")
        self.assertFalse([c for c in self.fab.commands if c.startswith("import ")])

    def test_different_content_is_reimported_as_an_update(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        action = self.action()
        action._current_hash = lambda ctx: "a-different-hash"
        result = action.apply(self.context())
        self.assertEqual(result.status, "updated")


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)

    def context(self, recipe_obj, dry_run=True) -> RunContext:
        cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=dry_run
        )
        return RunContext(cli=cli, log=self.log, recipe=recipe_obj, dry_run=dry_run)

    def test_an_empty_tenant_is_all_drift(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        loaded = make_recipe({"Store": {}})
        report = detect(build_plan(loaded), self.context(loaded))
        self.assertEqual(report.exit_code, ExitCode.DRIFT)
        self.assertTrue(all(finding.state == MISSING for finding in report.drifted))

    def test_a_matching_tenant_reports_no_drift(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add_json(["get"], {"id": "ws-1"}, command="get")
        loaded = make_recipe({"Store": {}})
        report = detect(build_plan(loaded), self.context(loaded))
        self.assertEqual(report.exit_code, ExitCode.SUCCESS)
        self.assertFalse(report.drifted)
        self.assertTrue(all(finding.state == IN_SYNC for finding in report.findings))

    def test_a_changed_property_reads_as_differs(self):
        self.fab.add(["exists"], stdout="true", command="exists")
        self.fab.add_json(["get"], {"id": "ws-1"}, command="get")
        loaded = make_recipe({"Store": {"properties": {"sparkSettings.pool.starterPool.maxNodeCount": 1}}})
        report = detect(build_plan(loaded), self.context(loaded))
        states = {finding.kind: finding.state for finding in report.findings}
        self.assertEqual(states.get("properties"), DIFFERS)
        self.assertEqual(report.exit_code, ExitCode.DRIFT)

    def test_a_live_context_is_refused(self):
        """A drift check that could write would not be a check."""
        loaded = make_recipe({"Store": {}})
        with self.assertRaises(ValueError):
            detect(build_plan(loaded), self.context(loaded, dry_run=False))


if __name__ == "__main__":
    unittest.main()
