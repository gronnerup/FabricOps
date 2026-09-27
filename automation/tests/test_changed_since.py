"""Syncing only the layers a merge touched."""

import os
import pathlib
import subprocess
import tempfile
import unittest

from fabricops.engine.changed import changed_paths_since, layers_touched
from fabricops.recipe import Recipe


def recipe(**layers) -> Recipe:
    return Recipe(
        data={
            "display_name_pattern": "Demo - {layer} [{environment}]",
            "defaults": {"capacity": "Trial-01"},
            "layers": {name: {"git": {"directory": directory}} for name, directory in layers.items()},
        },
        sources=(), solution="demo", environment="dev",
    )


ENTERPRISE = dict(
    Store="solution/store",
    Ingest="solution/engineering/ingest",
    Prepare="solution/engineering/prepare",
    Orchestrate="solution/engineering/orchestrate",
    Model="solution/analytics/model",
    Present="solution/analytics/present",
)


class LayersTouchedTests(unittest.TestCase):
    def test_a_file_inside_a_layer_touches_that_layer_only(self):
        touched = layers_touched(recipe(**ENTERPRISE), ["solution/engineering/orchestrate/Load.DataPipeline/pipeline-content.json"])
        self.assertEqual(touched, {"Orchestrate"})

    def test_several_files_touch_several_layers(self):
        touched = layers_touched(recipe(**ENTERPRISE), [
            "solution/analytics/model/R.SemanticModel/definition/expressions.tmdl",
            "solution/analytics/present/R.Report/definition.pbir",
        ])
        self.assertEqual(touched, {"Model", "Present"})

    def test_changes_outside_every_layer_touch_nothing(self):
        touched = layers_touched(recipe(**ENTERPRISE), [
            "automation/src/fabricops/cli.py",
            ".azure-pipelines/feature_fabric_cleanup.yml",
            "documentation/reference/getting-started.md",
        ])
        self.assertEqual(touched, set())

    def test_a_combined_layer_at_a_smaller_tier_is_touched_by_its_leaves(self):
        touched = layers_touched(recipe(Store="solution/store", Engineering="solution/engineering"),
                                 ["solution/engineering/prepare/X.Notebook/notebook-content.py"])
        self.assertEqual(touched, {"Engineering"})

    def test_a_sibling_prefix_is_not_a_match(self):
        touched = layers_touched(recipe(Store="solution/store"), ["solution/storefront/X.Lakehouse/.platform"])
        self.assertEqual(touched, set())

    def test_windows_separators_and_leading_slashes_are_tolerated(self):
        touched = layers_touched(recipe(**ENTERPRISE), ["/solution\\store\\Curated.Lakehouse\\.platform"])
        self.assertEqual(touched, {"Store"})

    def test_a_layer_without_a_directory_is_never_touched(self):
        r = Recipe(data={"display_name_pattern": "x", "defaults": {}, "layers": {"Core": {}}},
                   sources=(), solution="demo", environment="dev")
        self.assertEqual(layers_touched(r, ["solution/engineering/core/x"]), set())


class ChangedPathsSinceTests(unittest.TestCase):
    """The git half: answers, or says it cannot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
        def git(*a):
            subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True, env=env, timeout=30)
        git("init", "-q"); (self.root / "a.txt").write_text("1"); git("add", "."); git("commit", "-q", "--no-gpg-sign", "-m", "one")
        (self.root / "solution").mkdir(); (self.root / "solution" / "b.txt").write_text("2"); git("add", "."); git("commit", "-q", "--no-gpg-sign", "-m", "two")

    def test_a_real_diff_lists_the_changed_paths(self):
        self.assertEqual(changed_paths_since("HEAD~1", self.root), ["solution/b.txt"])

    def test_an_unknown_ref_yields_none_not_an_empty_list(self):
        # None means "fall back to everything". An empty list would mean "sync nothing".
        self.assertIsNone(changed_paths_since("no-such-ref", self.root))

    def test_a_directory_that_is_not_a_repo_yields_none(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(changed_paths_since("HEAD~1", plain))
