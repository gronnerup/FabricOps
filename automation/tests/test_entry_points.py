"""The pipeline entry points: same arguments in, the new engine underneath."""

import importlib.util
import os
import pathlib
import unittest

from support import FakeFab

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    """Import a script by path, the way a pipeline would run it."""
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ArgumentMappingTests(unittest.TestCase):
    def setUp(self):
        self.setup = load_script("fabric_setup")

    def test_defaults_match_the_previous_script(self):
        args = self.setup.parse_args([])
        self.assertEqual(args.environment, "dev")
        self.assertEqual(args.action, "create")

    def test_legacy_arguments_are_forwarded(self):
        argv = self.setup.build_argv(
            self.setup.parse_args(
                [
                    "--environment", "tst",
                    "--tenant_id", "t-1",
                    "--client_id", "c-1",
                    "--client_secret", "s-1",
                    "--github_pat", "p-1",
                ]
            )
        )
        self.assertIn("--tenant-id", argv)
        self.assertEqual(argv[argv.index("--client-secret") + 1], "s-1")
        self.assertEqual(argv[argv.index("--environment") + 1], "tst")
        self.assertEqual(argv[argv.index("--action") + 1], "create")

    def test_delete_is_auto_confirmed_for_pipelines(self):
        argv = self.setup.build_argv(self.setup.parse_args(["--environment", "tst", "--action", "delete"]))
        self.assertEqual(argv[argv.index("--confirm") + 1], "default/tst")

    def test_delete_with_a_solution_confirms_that_solution(self):
        argv = self.setup.build_argv(
            self.setup.parse_args(["--environment", "tst", "--action", "delete", "--solution", "spaceparts"])
        )
        self.assertEqual(argv[argv.index("--confirm") + 1], "spaceparts/tst")

    def test_dry_run_does_not_auto_confirm(self):
        argv = self.setup.build_argv(self.setup.parse_args(["--environment", "tst", "--action", "delete", "--dry-run"]))
        self.assertNotIn("--confirm", argv)

    def test_an_unknown_action_exits_with_a_recipe_error(self):
        self.assertEqual(self.setup.main(["--action", "merge"]), 2)


class FeatureEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.feature = load_script("fabric_feature_maintainance")

    def test_branch_and_action_are_forwarded(self):
        argv = self.feature.build_argv(
            self.feature.parse_args(["--action", "delete", "--branch_name", "feature/peer/add-orders"])
        )
        self.assertEqual(argv[argv.index("feature") + 1], "delete")
        self.assertEqual(argv[argv.index("--branch") + 1], "feature/peer/add-orders")

    def test_developer_identity_is_forwarded(self):
        argv = self.feature.build_argv(
            self.feature.parse_args(["--developer", "peer", "--developer_object_id", "user-1"])
        )
        self.assertEqual(argv[argv.index("--developer") + 1], "peer")
        self.assertEqual(argv[argv.index("--developer-object-id") + 1], "user-1")

    def test_an_unknown_action_is_rejected(self):
        self.assertEqual(self.feature.main(["--action", "merge"]), 2)

    def test_ci_branch_variables_are_used_by_default(self):
        original = dict(os.environ)
        try:
            os.environ["GITHUB_REF_NAME"] = "feature/peer/from-ci"
            self.assertEqual(self.feature.default_branch(), "feature/peer/from-ci")
            del os.environ["GITHUB_REF_NAME"]
            os.environ["BUILD_SOURCEBRANCH"] = "refs/heads/feature/peer/from-ado"
            self.assertEqual(self.feature.default_branch(), "feature/peer/from-ado")
        finally:
            os.environ.clear()
            os.environ.update(original)


class ReleaseEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.release = load_script("fabric_release")

    def test_environment_and_layers_are_forwarded(self):
        argv = self.release.build_argv(self.release.parse_args(["--environment", "tst", "--layers", "store,model"]))
        self.assertEqual(argv[argv.index("release") + 1], "--environment")
        self.assertEqual(argv[argv.index("--environment") + 1], "tst")
        self.assertEqual(argv[argv.index("--layers") + 1], "store,model")

    def test_is_debug_becomes_a_log_level(self):
        argv = self.release.build_argv(self.release.parse_args(["--environment", "tst", "--is_debug", "true"]))
        self.assertEqual(argv[argv.index("--log-level") + 1], "debug")

    def test_an_explicit_log_level_wins_over_is_debug(self):
        argv = self.release.build_argv(
            self.release.parse_args(["--environment", "tst", "--is_debug", "true", "--log-level", "trace"])
        )
        self.assertEqual(argv[argv.index("--log-level") + 1], "trace")

    def test_the_old_repo_path_meaning_is_translated(self):
        """It used to point at the solution folder; it now means the repository root."""
        root = pathlib.Path(__file__).resolve().parents[2]
        self.assertEqual(self.release.resolve_repo_path(str(root / "solution")), str(root))

    def test_a_repository_root_is_left_alone(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        self.assertEqual(self.release.resolve_repo_path(str(root)), str(root))

    def test_extend_parameters_is_always_explicit(self):
        argv = self.release.build_argv(self.release.parse_args(["--environment", "tst", "--extend_parameters", "false"]))
        self.assertEqual(argv[argv.index("--extend-parameters") + 1], "false")


class FlagPositionTests(unittest.TestCase):
    """Global flags must work on either side of the subcommand."""

    def parse(self, argv):
        from fabricops.cli import build_parser

        return build_parser().parse_args(argv)

    def test_after_the_subcommand(self):
        args = self.parse(["setup", "--environment", "dev", "--dry-run", "--log-level", "debug"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.log_level, "debug")

    def test_before_the_subcommand(self):
        args = self.parse(["--dry-run", "--log-level", "debug", "setup", "--environment", "dev"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.log_level, "debug")

    def test_a_global_value_is_not_clobbered_by_the_subcommand_default(self):
        args = self.parse(["--solution", "spaceparts", "plan", "--environment", "dev"])
        self.assertEqual(args.solution, "spaceparts")

    def test_leaf_subcommands_accept_them_too(self):
        """`varlib activate --dry-run` must work, not just `--dry-run varlib activate`."""
        for argv in (
            ["varlib", "activate", "--environment", "tst", "--dry-run"],
            ["tags", "list", "--dry-run"],
            ["storage", "report", "--dry-run"],
            ["recipe", "validate", "--dry-run"],
            ["manifest", "show", "--dry-run"],
            ["solution", "list", "--dry-run"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(self.parse(argv).dry_run)

    def test_the_subcommand_wins_when_both_are_given(self):
        args = self.parse(["--solution", "a", "plan", "--solution", "b"])
        self.assertEqual(args.solution, "b")


class EndToEndTests(unittest.TestCase):
    """Run the entry point against the fake `fab`, as a pipeline would."""

    def setUp(self):
        self.fab = FakeFab()
        self.setup = load_script("fabric_setup")
        self.original = dict(os.environ)
        os.environ["FABOPS_FAB_BIN"] = self.fab.executable
        os.environ["FAKE_FAB_SCRIPT"] = str(self.fab.script_path)
        os.environ["FAKE_FAB_LOG"] = str(self.fab.log_path)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original)
        self.fab.cleanup()

    def test_dry_run_against_the_repository_recipe_touches_nothing(self):
        self.fab.add(["exists"], stdout="false", command="exists")

        exit_code = self.setup.main(["--environment", "dev", "--dry-run", "--log-level", "off"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(self.fab.commands, "reads still happen in a dry run")
        writes = ("mkdir", "rm ", "set ", "acl set", "acl rm", "import ")
        self.assertFalse([c for c in self.fab.commands if c.startswith(writes)])
        self.assertTrue(
            [c for c in self.fab.commands if c.startswith("acl get")],
            "role assignment must read the current ACL before deciding to write",
        )

    def test_a_dry_run_without_secrets_reports_what_it_would_need(self):
        self.fab.add(["exists"], stdout="false", command="exists")
        exit_code = self.setup.main(["--environment", "dev", "--dry-run", "--log-level", "off"])
        self.assertEqual(exit_code, 0, "a plan must be inspectable without credentials")


if __name__ == "__main__":
    unittest.main()


class DispatchTests(unittest.TestCase):
    """Every subcommand must reach its handler.

    `fabricops feature create` never did: the feature parser has a `--group` flag and the
    subparsers use `dest="group"`, so omitting the flag wrote None over the subcommand name
    and the dispatcher fell through to "unknown command: None". A flag colliding with a
    dest is invisible until someone runs that exact command, so this checks all of them.
    """

    COMMANDS = [
        ["recipe", "validate"],
        ["recipe", "render"],
        ["plan", "--environment", "dev"],
        ["setup", "--environment", "dev"],
        ["feature", "create", "--branch", "feature/prepare/x"],
        ["feature", "update", "--branch", "feature/prepare/x"],
        ["feature", "delete", "--branch", "feature/prepare/x"],
        ["feature", "list"],
        ["feature", "reap"],
        ["manifest", "show"],
        ["tags", "sync"],
        ["tags", "list"],
        ["varlib", "render"],
        ["varlib", "activate", "--environment", "tst"],
        ["storage", "report"],
        ["sanitise"],
        ["export", "--to", "/tmp/x"],
        ["release", "--environment", "tst"],
        ["references", "sync"],
        ["connection", "refresh"],
        ["solution", "list"],
    ]

    def parse(self, argv):
        from fabricops.cli import build_parser

        return build_parser().parse_args(argv)

    def test_the_subcommand_name_survives_parsing(self):
        for argv in self.COMMANDS:
            with self.subTest(argv=" ".join(argv)):
                args = self.parse(argv)
                self.assertEqual(
                    args.group, argv[0],
                    f"a flag on `{argv[0]}` is overwriting the subcommand name",
                )

    def test_the_second_level_name_survives_too(self):
        for argv in self.COMMANDS:
            if len(argv) < 2 or argv[1].startswith("-"):
                continue
            with self.subTest(argv=" ".join(argv)):
                args = self.parse(argv)
                second = getattr(args, "command", None) or getattr(args, "action", None)
                self.assertEqual(second, argv[1])

    def test_no_flag_shares_a_dest_with_the_subcommand_names(self):
        """The general form of the bug, rather than the one instance of it."""
        from fabricops.cli import build_parser

        parser = build_parser()
        subparsers = next(
            a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
        )
        reserved = {subparsers.dest}
        for name, sub in subparsers.choices.items():
            for action in sub._actions:
                if action.option_strings and action.dest in reserved:
                    self.fail(f"`{name} {action.option_strings[0]}` writes over {action.dest!r}")


class DeveloperObjectIdTests(unittest.TestCase):
    """The developer's object id is never guessed from CI.

    Both CI systems have a user id, neither has an *Entra* one, and Fabric rejects the
    wrong kind. Defaulting to one meant every feature run attempted an ACL that could not
    work, once per layer.
    """

    def setUp(self):
        self.feature = load_script("fabric_feature_maintainance")
        self._original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._original)))
        for key in ("FABOPS_DEVELOPER_OBJECT_ID", "BUILD_REQUESTEDFORID", "GITHUB_ACTOR_ID"):
            os.environ.pop(key, None)

    def argv(self, *args):
        return self.feature.build_argv(self.feature.parse_args(list(args)))

    def test_the_azure_devops_identity_id_is_not_used(self):
        os.environ["BUILD_REQUESTEDFORID"] = "7a9ac92c-a581-602e-95d9-5d0feb2e865a"
        self.assertNotIn("--developer-object-id", self.argv("--branch_name", "feature/x"))

    def test_the_github_actor_id_is_not_used(self):
        os.environ["GITHUB_ACTOR_ID"] = "52330973"
        self.assertNotIn("--developer-object-id", self.argv("--branch_name", "feature/x"))

    def test_an_explicit_environment_variable_is_used(self):
        os.environ["FABOPS_DEVELOPER_OBJECT_ID"] = "11111111-1111-4111-8111-111111111111"
        argv = self.argv("--branch_name", "feature/x")
        self.assertEqual(
            argv[argv.index("--developer-object-id") + 1], "11111111-1111-4111-8111-111111111111"
        )

    def test_the_flag_wins_over_everything(self):
        os.environ["FABOPS_DEVELOPER_OBJECT_ID"] = "from-env"
        os.environ["BUILD_REQUESTEDFORID"] = "from-ado"
        argv = self.argv("--branch_name", "feature/x", "--developer_object_id", "explicit")
        self.assertEqual(argv[argv.index("--developer-object-id") + 1], "explicit")

    def test_nothing_set_means_the_flag_is_absent_entirely(self):
        self.assertNotIn("--developer-object-id", self.argv("--branch_name", "feature/x"))


class GitSyncEntryPointTests(unittest.TestCase):
    """The step a merge into dev's branch runs: converge git, nothing else."""

    def setUp(self):
        self.sync = load_script("fabric_gitsync_env")

    def argv(self, *args):
        return self.sync.build_argv(self.sync.parse_args(list(args)))

    def test_it_asks_for_git_only(self):
        argv = self.argv("--environment", "dev")
        self.assertEqual(argv[argv.index("setup") + 1 : argv.index("setup") + 3], ["--environment", "dev"])
        self.assertEqual(argv[argv.index("--only") + 1], "git")

    def test_dev_is_the_default_environment(self):
        self.assertEqual(self.argv()[self.argv().index("--environment") + 1], "dev")

    def test_a_layer_subset_is_forwarded(self):
        argv = self.argv("--environment", "tst", "--layers", "present,orchestrate")
        self.assertEqual(argv[argv.index("--layers") + 1], "present,orchestrate")

    def test_dry_run_is_forwarded(self):
        self.assertIn("--dry-run", self.argv("--environment", "dev", "--dry-run"))


class OnlyKindsTests(unittest.TestCase):
    """`--only` names kinds; the plan decides what has to come with them."""

    def kinds(self, value):
        import argparse as _argparse

        from fabricops.cli import _only_kinds

        return _only_kinds(_argparse.Namespace(only=value))

    def test_one_kind(self):
        self.assertEqual(self.kinds("git"), {"git"})

    def test_several_kinds(self):
        self.assertEqual(self.kinds("git,role"), {"git", "role"})

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(self.kinds(" Git , ROLE "), {"git", "role"})

    def test_nothing_asked_means_no_filter(self):
        self.assertIsNone(self.kinds(None))
        self.assertIsNone(self.kinds(""))


class KeepWithDependenciesTests(unittest.TestCase):
    """Naming kinds is not enough: a git action needs its workspace *and* its connection.

    The first version of this named workspaces explicitly and forgot connections, so the
    dev sync on merge failed every layer with "no git credentials connection is available".
    """

    def plan(self):
        from fabricops.engine.actions import Action

        return [
            Action(id="connection:git", kind="connection"),
            Action(id="workspace:Store", kind="workspace"),
            Action(id="role:Store:admin:0", kind="role", depends_on=("workspace:Store",)),
            Action(id="properties:Store", kind="properties", depends_on=("workspace:Store",)),
            Action(
                id="git:Store",
                kind="git",
                depends_on=("workspace:Store", "connection:git"),
            ),
        ]

    def keep(self, kinds):
        from fabricops.cli import _keep_with_dependencies

        return [action.id for action in _keep_with_dependencies(self.plan(), kinds)]

    def test_git_brings_its_workspace_and_its_connection(self):
        self.assertEqual(self.keep({"git"}), ["connection:git", "workspace:Store", "git:Store"])

    def test_it_leaves_out_what_git_does_not_need(self):
        kept = self.keep({"git"})
        self.assertNotIn("role:Store:admin:0", kept)
        self.assertNotIn("properties:Store", kept)

    def test_plan_order_is_preserved(self):
        kept = self.keep({"git"})
        self.assertLess(kept.index("connection:git"), kept.index("git:Store"))
        self.assertLess(kept.index("workspace:Store"), kept.index("git:Store"))

    def test_a_kind_with_no_dependencies_stands_alone(self):
        self.assertEqual(self.keep({"connection"}), ["connection:git"])

    def test_roles_bring_only_their_workspace(self):
        self.assertEqual(self.keep({"role"}), ["workspace:Store", "role:Store:admin:0"])

    def test_a_kind_that_matches_nothing_keeps_nothing(self):
        self.assertEqual(self.keep({"tags"}), [])


class GitSyncChangedSinceTests(unittest.TestCase):
    def setUp(self):
        self.sync = load_script("fabric_gitsync_env")
        self._env = dict(os.environ); self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop("FABOPS_CHANGED_SINCE", None)

    def argv(self, *a): return self.sync.build_argv(self.sync.parse_args(list(a)))

    def test_the_flag_is_forwarded(self):
        argv = self.argv("--environment", "dev", "--changed_since", "HEAD~1")
        self.assertEqual(argv[argv.index("--changed-since") + 1], "HEAD~1")

    def test_the_environment_variable_is_the_default(self):
        os.environ["FABOPS_CHANGED_SINCE"] = "abc123"
        argv = self.argv("--environment", "dev")
        self.assertEqual(argv[argv.index("--changed-since") + 1], "abc123")

    def test_absent_means_a_full_sync(self):
        self.assertNotIn("--changed-since", self.argv("--environment", "dev"))


class ReapFlagTests(unittest.TestCase):
    def test_apply_when_is_accepted_and_defaults_to_both(self):
        from fabricops.cli import build_parser

        args = build_parser().parse_args(["feature", "reap", "--apply", "--apply-when", "branch-gone"])
        self.assertEqual(args.apply_when, "branch-gone")
        self.assertEqual(build_parser().parse_args(["feature", "reap"]).apply_when, "both")
