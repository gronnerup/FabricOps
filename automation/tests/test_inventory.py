"""Feature workspace inventory and TTL reaping (E08-S2, E08-S3)."""

import unittest
from datetime import datetime, timedelta, timezone

from fabricops.engine import inventory
from fabricops.engine.feature import build_feature_plan
from fabricops.fabric.cli import NO_RETRY, FabricCli
from fabricops.obs.logging import Level, RunLog
from fabricops.recipe import Recipe
from support import FakeFab

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def workspace(name="Demo", *, branch="feature/peer/x", created_days=1, sync_days=None, tags=()):
    return inventory.FeatureWorkspace(
        id=f"id-{name}",
        name=name,
        solution="demo",
        layer="Prepare",
        branch=branch,
        developer="peer",
        created=NOW - timedelta(days=created_days),
        last_sync=None if sync_days is None else NOW - timedelta(days=sync_days),
        tags=list(tags),
    )


class StampTests(unittest.TestCase):
    def test_a_stamp_round_trips(self):
        text = inventory.stamp(
            solution="demo", layer="Prepare", branch="feature/peer/x",
            developer="peer", created="2026-09-01T10:00:00Z",
        )
        self.assertEqual(
            inventory.parse_stamp(text),
            {"solution": "demo", "layer": "Prepare", "branch": "feature/peer/x",
             "developer": "peer", "created": "2026-09-01T10:00:00Z"},
        )

    def test_someone_elses_description_is_not_ours(self):
        self.assertEqual(inventory.parse_stamp("A workspace for the sales team"), {})
        self.assertEqual(inventory.parse_stamp(None), {})

    def test_a_stamp_survives_surrounding_prose(self):
        text = "Created by hand. " + inventory.stamp(
            solution="demo", layer="Store", branch="b", developer="p", created="2026-01-01T00:00:00Z"
        )
        self.assertEqual(inventory.parse_stamp(text)["layer"], "Store")

    def test_missing_values_still_produce_a_readable_stamp(self):
        text = inventory.stamp(solution=None, layer="Store", branch="b", developer=None, created="x")
        fields = inventory.parse_stamp(text)
        self.assertEqual(fields["solution"], "default")
        self.assertEqual(fields["developer"], "unknown")


class FeaturePlanStampTests(unittest.TestCase):
    def test_a_feature_workspace_describes_itself(self):
        loaded = Recipe(
            data={"display_name_pattern": "*{feature} ({layer})", "layers": {"Prepare": {}}},
            sources=(), solution="demo",
        )
        plan = build_feature_plan(
            loaded, branch="feature/peer/add-orders", developer="peer",
            register_relation=False, created="2026-09-05T12:00:00Z",
        )
        properties = next(action for action in plan if action.kind == "properties")
        fields = inventory.parse_stamp(properties.properties["description"])
        self.assertEqual(fields["branch"], "feature/peer/add-orders")
        self.assertEqual(fields["layer"], "Prepare")
        self.assertEqual(fields["developer"], "peer")


class DecisionTests(unittest.TestCase):
    def decide(self, workspaces, **kwargs):
        return inventory.decide(workspaces, now=NOW, **kwargs)

    def test_a_young_workspace_is_kept(self):
        [decision] = self.decide([workspace(created_days=3)], ttl_days=14)
        self.assertFalse(decision.delete)

    def test_an_old_workspace_is_reaped(self):
        [decision] = self.decide([workspace(created_days=30)], ttl_days=14)
        self.assertTrue(decision.delete)
        self.assertIn("created", decision.reason)

    def test_last_sync_beats_creation_time(self):
        """An old workspace someone still pushes to is alive."""
        [decision] = self.decide([workspace(created_days=90, sync_days=2)], ttl_days=14)
        self.assertFalse(decision.delete)

    def test_a_missing_branch_reaps_regardless_of_age(self):
        [decision] = self.decide([workspace(created_days=1)], branch_exists=lambda b: False)
        self.assertTrue(decision.delete)
        self.assertIn("no longer exists", decision.reason)

    def test_an_unreachable_provider_never_deletes(self):
        """'I could not reach GitHub' must not read as 'the branch is gone'."""
        [decision] = self.decide([workspace(created_days=1)], branch_exists=lambda b: None)
        self.assertFalse(decision.delete)

    def test_retain_wins_over_everything(self):
        [decision] = self.decide(
            [workspace(created_days=900, tags=["Retain:true"])], branch_exists=lambda b: False
        )
        self.assertFalse(decision.delete)
        self.assertEqual(decision.reason, "Retain:true")

    def test_no_timestamps_means_no_decision(self):
        stale = workspace()
        stale.created = None
        [decision] = self.decide([stale], ttl_days=1)
        self.assertFalse(decision.delete)
        self.assertIn("no created or last-sync", decision.reason)


class ReapTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def test_a_report_without_apply_deletes_nothing(self):
        decisions = inventory.decide([workspace(created_days=90)], now=NOW, ttl_days=14)
        report = inventory.reap(self.cli, decisions, log=self.log)
        self.assertEqual(report.deleted, [])
        self.assertEqual(len(report.doomed), 1)
        self.assertFalse([c for c in self.fab.commands if c.startswith("rm")])
        self.assertIn("would delete", report.describe()[0])

    def test_apply_deletes_only_the_doomed(self):
        decisions = inventory.decide(
            [workspace("Old", created_days=90), workspace("New", created_days=1)], now=NOW, ttl_days=14
        )
        report = inventory.reap(self.cli, decisions, log=self.log, apply=True)
        self.assertEqual(report.deleted, ["Old"])
        removed = [c for c in self.fab.commands if c.startswith("rm")]
        self.assertEqual(len(removed), 1)
        self.assertIn("Old", removed[0])

    def test_one_failure_does_not_stop_the_sweep(self):
        self.fab.add(["rm", "Broken"], returncode=1, stderr="permission denied")
        decisions = inventory.decide(
            [workspace("Broken", created_days=90), workspace("Fine", created_days=90)], now=NOW, ttl_days=14
        )
        report = inventory.reap(self.cli, decisions, log=self.log, apply=True)
        self.assertEqual(report.deleted, ["Fine"])
        self.assertEqual(len(report.failures), 1)


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def test_only_stamped_workspaces_are_listed(self):
        self.fab.add_json(["api", "workspaces"], {"status_code": 200, "headers": {}, "text": {"value": [
            {"id": "1", "displayName": "*add-orders (Prepare)",
             "description": inventory.stamp(solution="demo", layer="Prepare", branch="feature/peer/x",
                                            developer="peer", created="2026-09-01T00:00:00Z")},
            {"id": "2", "displayName": "Confidence - Store [dev]", "description": "The store workspace"},
            {"id": "3", "displayName": "Someone's workspace"},
        ]}})
        found = inventory.list_feature_workspaces(self.cli, solution="demo")
        self.assertEqual([w.name for w in found], ["*add-orders (Prepare)"])
        self.assertEqual(found[0].branch, "feature/peer/x")

    def test_another_solutions_features_are_left_alone(self):
        self.fab.add_json(["api", "workspaces"], {"status_code": 200, "headers": {}, "text": {"value": [
            {"id": "1", "displayName": "theirs",
             "description": inventory.stamp(solution="other", layer="Prepare", branch="b",
                                            developer="p", created="2026-09-01T00:00:00Z")},
        ]}})
        self.assertEqual(inventory.list_feature_workspaces(self.cli, solution="demo"), [])


if __name__ == "__main__":
    unittest.main()


class BaseStampTests(unittest.TestCase):
    """The stamp carries the base workspace template, spaces and all."""

    def test_base_round_trips_through_stamp_and_parse(self):
        from fabricops.engine.inventory import parse_stamp, stamp

        text = stamp(solution="demo", layer="Prepare", branch="feature/x", developer="peer",
                     created="2026-09-23T00:00:00Z", base="Brickyard - {layer} [dev]")
        fields = parse_stamp(text)
        self.assertEqual(fields["base"], "Brickyard - {layer} [dev]")
        self.assertEqual(fields["layer"], "Prepare")
        self.assertEqual(fields["branch"], "feature/x")

    def test_a_stamp_without_base_still_parses(self):
        from fabricops.engine.inventory import parse_stamp, stamp

        fields = parse_stamp(stamp(solution=None, layer="Model", branch="feature/y", developer=None, created="t"))
        self.assertNotIn("base", fields)
        self.assertEqual(fields["solution"], "default")
