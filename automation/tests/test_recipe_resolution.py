"""Resolution order (E02), including the legacy fallback that must keep working."""

import pathlib
import shutil
import tempfile
import unittest

from fabricops.errors import RecipeError
from fabricops.recipe import resolver

MINIMAL = 'display_name_pattern: "S - {layer} [{environment}]"\nlayers:\n  Store: {}\n'


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-resolve-"))
        self.resources = self.root / "automation" / "resources"
        (self.resources / "environments").mkdir(parents=True)
        (self.resources / "solutions").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative: str, text: str = MINIMAL) -> pathlib.Path:
        path = self.resources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # ------------------------------------------------------------- platform
    def test_order_1_solution_folder_wins(self):
        self.write("solutions/my-data-platform/platform.yml")
        self.write("solutions/my-data-platform/platform.dev.yml")
        self.write("environments/my-data-platform.yml")
        self.write("environments/infrastructure.json", "{}")
        resolved = resolver.resolve_platform(self.resources, solution="my-data-platform", environment="dev")
        self.assertEqual(resolved.order, 1)
        self.assertEqual(resolved.base.name, "platform.yml")
        self.assertEqual([p.name for p in resolved.overlays], ["platform.dev.yml"])

    def test_order_2_flat_naming(self):
        self.write("environments/my-data-platform.json", "{}")
        self.write("environments/my-data-platform.dev.json", "{}")
        resolved = resolver.resolve_platform(self.resources, solution="my-data-platform", environment="dev")
        self.assertEqual(resolved.order, 2)
        self.assertEqual(resolved.base.name, "my-data-platform.json")

    def test_order_3_legacy_fallback_for_an_unknown_solution(self):
        self.write("environments/infrastructure.json", "{}")
        self.write("environments/infrastructure.dev.json", "{}")
        resolved = resolver.resolve_platform(self.resources, solution="whatever", environment="dev")
        self.assertEqual(resolved.order, 3)
        self.assertEqual(resolved.base.name, "infrastructure.json")

    def test_no_solution_and_exactly_one_defined_uses_it(self):
        # What a fresh clone of the public repository looks like: solutions/demo/ and
        # nothing else. `plan --environment dev` has to work without --solution.
        self.write("solutions/demo/platform.yml")
        self.write("solutions/demo/platform.dev.yml")
        resolved = resolver.resolve_platform(self.resources, environment="dev")
        self.assertEqual(resolved.solution, "demo")
        self.assertEqual(resolved.base, self.resources / "solutions" / "demo" / "platform.yml")
        self.assertEqual([p.name for p in resolved.overlays], ["platform.dev.yml"])

    def test_the_legacy_file_still_beats_a_single_solution(self):
        # The internal repository has both; existing behaviour must not change.
        self.write("solutions/demo/platform.yml")
        self.write("environments/infrastructure.json", "{}")
        resolved = resolver.resolve_platform(self.resources, environment="dev")
        self.assertEqual(resolved.base.name, "infrastructure.json")

    def test_two_defined_solutions_and_none_named_is_an_error_that_lists_them(self):
        self.write("solutions/alpha/platform.yml")
        self.write("solutions/beta/platform.yml")
        with self.assertRaises(RecipeError) as raised:
            resolver.resolve_platform(self.resources, environment="dev")
        self.assertIn("alpha, beta", str(raised.exception.hint))
        self.assertIn("--solution", str(raised.exception.hint))

    def test_no_solution_uses_default_folder_then_legacy(self):
        self.write("environments/infrastructure.json", "{}")
        resolved = resolver.resolve_platform(self.resources, environment="dev")
        self.assertEqual(resolved.base.name, "infrastructure.json")

        self.write("solutions/default/platform.yml")
        resolved = resolver.resolve_platform(self.resources, environment="dev")
        self.assertEqual(resolved.base.name, "platform.yml")

    def test_missing_recipe_lists_what_was_searched(self):
        with self.assertRaises(RecipeError) as ctx:
            resolver.resolve_platform(self.resources, solution="ghost")
        hint = ctx.exception.hint or ""
        self.assertIn("solutions/ghost/platform.yml", hint)
        self.assertIn("environments/infrastructure.json", hint)

    def test_missing_environment_overlay_is_not_fatal(self):
        self.write("solutions/s/platform.yml")
        resolved = resolver.resolve_platform(self.resources, solution="s", environment="prd")
        self.assertEqual(resolved.overlays, ())

    # -------------------------------------------------------------- feature
    def test_feature_developer_overlay(self):
        self.write("solutions/s/feature.yml")
        self.write("solutions/s/feature.peer.yml")
        resolved = resolver.resolve_feature(self.resources, solution="s", developer="peer")
        self.assertEqual([p.name for p in resolved.files], ["feature.yml", "feature.peer.yml"])

    def test_group_recipe_overlays_the_shared_one(self):
        self.write("solutions/s/feature.yml")
        self.write("solutions/s/feature.backend.yml")
        self.write("solutions/s/feature.peer.yml")
        resolved = resolver.resolve_feature(self.resources, solution="s", developer="peer", group="backend")
        self.assertEqual(
            [p.name for p in resolved.files],
            ["feature.yml", "feature.backend.yml", "feature.peer.yml"],
            "shared, then group, then personal",
        )

    def test_group_detection(self):
        self.write("solutions/s/feature.yml")
        self.write("solutions/s/feature.backend.yml")
        self.assertTrue(resolver.feature_group_exists(self.resources, solution="s", group="backend"))
        self.assertFalse(resolver.feature_group_exists(self.resources, solution="s", group="prepare"))
        self.assertFalse(resolver.feature_group_exists(self.resources, solution="s", group=None))

    def test_a_missing_group_file_is_simply_not_an_overlay(self):
        self.write("solutions/s/feature.yml")
        resolved = resolver.resolve_feature(self.resources, solution="s", group="backend")
        self.assertEqual([p.name for p in resolved.files], ["feature.yml"])

    def test_feature_falls_back_to_environments(self):
        self.write("environments/feature.json", "{}")
        resolved = resolver.resolve_feature(self.resources, solution="s")
        self.assertEqual(resolved.base.name, "feature.json")
        self.assertEqual(resolved.order, 2)

    # ------------------------------------------------------------ discovery
    def test_list_solutions_covers_both_layouts(self):
        self.write("solutions/spaceparts/platform.yml")
        self.write("solutions/spaceparts/platform.tst.yml")
        self.write("environments/my-data-platform.json", "{}")
        self.write("environments/infrastructure.json", "{}")
        self.write("environments/infrastructure.dev.json", "{}")
        self.write("environments/feature.json", "{}")

        found = {entry["name"]: entry for entry in resolver.list_solutions(self.resources)}
        self.assertEqual(set(found), {"spaceparts", "my-data-platform", "default"})
        self.assertEqual(found["spaceparts"]["environments"], ["tst"])
        self.assertEqual(found["default"]["environments"], ["dev"])
        self.assertNotIn("feature", found)


class DeveloperTests(unittest.TestCase):
    def test_email_becomes_a_slug(self):
        self.assertEqual(resolver.sanitise_developer("Peer.Gronnerup@example.com"), "peer-gronnerup")

    def test_handles_odd_input(self):
        self.assertEqual(resolver.sanitise_developer("__"), "developer")
        self.assertEqual(resolver.sanitise_developer("peer_g"), "peer-g")


if __name__ == "__main__":
    unittest.main()
