"""Reaping on a signal you can trust, and only that one unattended."""

import dataclasses
import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from fabricops.engine import inventory
from fabricops.obs.logging import Level, RunLog

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def workspace(**overrides):
    """A FeatureWorkspace with every required field filled, whatever they are."""
    values = {}
    for f in dataclasses.fields(inventory.FeatureWorkspace):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            values[f.name] = "" if f.type in ("str", str) else None
    values.update({"id": "ws-1", "name": "*Demo x (Prepare)", "branch": "feature/engineering/x"})
    values.update(overrides)
    return inventory.FeatureWorkspace(**values)


class SignalTests(unittest.TestCase):
    def decide(self, ws, branch_exists=None, ttl=14):
        return inventory.decide([ws], now=NOW, ttl_days=ttl, branch_exists=branch_exists)[0]

    def test_a_deleted_branch_is_branch_gone(self):
        d = self.decide(workspace(created=NOW - timedelta(days=1)), branch_exists=lambda b: False)
        self.assertTrue(d.delete); self.assertEqual(d.signal, "branch-gone")

    def test_an_old_workspace_with_a_live_branch_is_ttl(self):
        d = self.decide(workspace(created=NOW - timedelta(days=30)), branch_exists=lambda b: True)
        self.assertTrue(d.delete); self.assertEqual(d.signal, "ttl")

    def test_an_unreachable_provider_never_means_gone(self):
        d = self.decide(workspace(created=NOW - timedelta(days=1)), branch_exists=lambda b: None)
        self.assertFalse(d.delete); self.assertEqual(d.signal, "keep")

    def test_no_age_means_keep_with_its_own_signal(self):
        d = self.decide(workspace(created=None), branch_exists=lambda b: True)
        self.assertFalse(d.delete); self.assertEqual(d.signal, "unknown-age")


class ApplyWhenTests(unittest.TestCase):
    class Cli:
        def __init__(self): self.removed = []
        def rm(self, path): self.removed.append(str(path))

    def setUp(self):
        self.log = RunLog(level=Level.OFF, stream=io.StringIO(), _colour=False)
        gone = inventory.ReapDecision(workspace(name="*gone (Prepare)"), True, "branch 'x' no longer exists", signal="branch-gone")
        old = inventory.ReapDecision(workspace(name="*old (Prepare)"), True, "30 days since created", signal="ttl")
        keep = inventory.ReapDecision(workspace(name="*keep (Prepare)"), False, "fresh", signal="keep")
        self.decisions = [gone, old, keep]

    def test_branch_gone_only_deletes_the_certain_one(self):
        cli = self.Cli()
        report = inventory.reap(cli, self.decisions, log=self.log, apply=True, apply_when="branch-gone")
        self.assertEqual(report.deleted, ["*gone (Prepare)"])
        self.assertEqual(report.deferred, ["*old (Prepare)"])
        self.assertEqual(len(cli.removed), 1)

    def test_both_deletes_every_doomed_one(self):
        cli = self.Cli()
        report = inventory.reap(cli, self.decisions, log=self.log, apply=True, apply_when="both")
        self.assertEqual(sorted(report.deleted), ["*gone (Prepare)", "*old (Prepare)"])
        self.assertEqual(report.deferred, [])

    def test_without_apply_nothing_is_touched_either_way(self):
        cli = self.Cli()
        report = inventory.reap(cli, self.decisions, log=self.log, apply=False, apply_when="both")
        self.assertEqual(cli.removed, []); self.assertFalse(report.applied)


class AzureDevOpsProbeTests(unittest.TestCase):
    """Exact ref match, both auth schemes, and 'do not know' for everything else."""

    def probe(self, token="tok"):
        return inventory.azure_devops_branch_checker("org", "proj", "repo", token)

    @staticmethod
    def response(names):
        body = json.dumps({"value": [{"name": n} for n in names], "count": len(names)}).encode()
        r = mock.MagicMock(); r.read.return_value = body; r.__enter__.return_value = r
        return r

    def test_exact_ref_exists(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response(["refs/heads/feature/x"])):
            self.assertIs(self.probe()("feature/x"), True)

    def test_a_prefix_match_is_not_a_match(self):
        # the refs filter is a prefix: feature/x also returns feature/x-2
        with mock.patch("urllib.request.urlopen", return_value=self.response(["refs/heads/feature/x-2"])):
            self.assertIs(self.probe()("feature/x"), False)

    def test_no_token_is_do_not_know(self):
        self.assertIsNone(self.probe(token=None)("feature/x"))

    def test_unauthorised_on_both_schemes_is_do_not_know(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=[err, err]):
            self.assertIsNone(self.probe()("feature/x"))

    def test_bearer_rejected_then_basic_accepted(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=[err, self.response(["refs/heads/feature/x"])]) as m:
            self.assertIs(self.probe()("feature/x"), True)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(m.call_args_list[1].args[0].get_header("Authorization").startswith("Basic "))

    def test_network_failure_is_do_not_know(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(self.probe()("feature/x"))
