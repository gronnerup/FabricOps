"""The runner contract: argv lists, parsed output, real failures, no leaked secrets."""

import io
import json
import pathlib
import tempfile
import unittest

from fabricops.errors import AuthError, ExitCode, FabricApiError, FabricCliError, FabricOpsError
from fabricops.fabric.cli import NO_RETRY, FabricCli, RetryPolicy
from fabricops.fabric.paths import FabPath
from fabricops.obs.logging import Level, RunLog
from fabricops.obs.redaction import Secret
from support import FakeFab

SECRET_VALUE = "pipeline-client-secret-0001"


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.fab = FakeFab()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-test-"))
        self.trace = self.tmp / "trace.jsonl"
        self.console = _Buffer()
        self.slept: list[float] = []
        self.log = RunLog(
            level=Level.DEBUG,
            trace_file=self.trace,
            stream=self.console,
            _colour=False,
        )
        self.cli = FabricCli(
            self.log,
            executable=self.fab.executable,
            env=self.fab.env,
            retry=NO_RETRY,
            sleep=self.slept.append,
        )

    def tearDown(self):
        self.log.close()
        self.fab.cleanup()

    def trace_records(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines() if line.strip()]


class ExistsTests(RunnerTestCase):
    def test_true_and_false_are_parsed_not_string_compared(self):
        self.fab.add(["exists", "Store"], stdout="* true")
        self.fab.add(["exists", "Missing"], stdout="* false")
        self.assertTrue(self.cli.exists(FabPath.workspace("Sales - Store [dev]")))
        self.assertFalse(self.cli.exists(FabPath.workspace("Missing [dev]")))

    def test_unparseable_answer_raises_instead_of_guessing(self):
        self.fab.add(["exists"], stdout="¯\\_(ツ)_/¯")
        with self.assertRaises(FabricCliError):
            self.cli.exists(FabPath.workspace("Sales - Store [dev]"))

    def test_display_name_is_a_single_argv_element(self):
        self.fab.add(["exists"], stdout="true")
        self.cli.exists(FabPath.workspace("Sales, O'Brien & Co [dev]"))
        argv = self.fab.calls[-1]
        self.assertEqual(argv[0], "exists")
        self.assertEqual(argv[1], "Sales, O'Brien & Co [dev].Workspace")


class FailureTests(RunnerTestCase):
    def test_nonzero_exit_raises(self):
        self.fab.add(["get"], stderr="Workspace not found", returncode=1)
        with self.assertRaises(FabricCliError) as ctx:
            self.cli.get_value(FabPath.workspace("Nope [dev]"), "id")
        self.assertIn("Workspace not found", str(ctx.exception))
        self.assertEqual(ctx.exception.returncode, 1)

    def test_error_is_never_returned_as_a_value(self):
        """The bug this replaces: a failed id lookup used to be used as a workspace id."""
        self.fab.add(["get"], stdout="ERROR: not found", returncode=1)
        with self.assertRaises(FabricCliError):
            self.cli.get_value(FabPath.workspace("Nope [dev]"), "id")

    def test_empty_query_result_raises(self):
        self.fab.add(["get"], stdout="", returncode=0)
        with self.assertRaises(FabricCliError):
            self.cli.get_value(FabPath.workspace("WS [dev]"), "id")

    def test_check_false_returns_the_failed_result(self):
        self.fab.add(["rm"], stderr="boom", returncode=1)
        result = self.cli.rm(FabPath.workspace("WS [dev]"), check=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 1)

    def test_missing_executable_gives_an_actionable_hint(self):
        cli = FabricCli(self.log, executable="/nonexistent/fab", env=self.fab.env, retry=NO_RETRY)
        with self.assertRaises(FabricCliError) as ctx:
            cli.exists("WS.Workspace")
        self.assertIn("ms-fabric-cli", ctx.exception.hint or "")


class SecretTests(RunnerTestCase):
    def test_secret_is_masked_in_console_and_trace_but_sent_to_the_process(self):
        self.fab.add(["auth", "login"], stdout="logged in")
        self.cli.login_service_principal("client-id", Secret(SECRET_VALUE, "client_secret"), "tenant-id")

        self.assertIn(SECRET_VALUE, self.fab.calls[-1], "the real secret must reach the CLI")
        self.assertNotIn(SECRET_VALUE, self.console.text)
        self.assertNotIn(SECRET_VALUE, self.trace.read_text())
        self.assertIn("***", self.console.text)

    def test_secret_shaped_output_is_scrubbed_from_the_trace(self):
        self.fab.add(["mkdir"], stdout="created with credentialDetails.key=ghp_0123456789abcdefghijklmnopqrstuvwx")
        self.cli.mkdir(FabPath.connection("Sales-GitHub"))
        self.assertNotIn("ghp_0123456789abcdefghijklmnopqrstuvwx", self.trace.read_text())


class RetryTests(RunnerTestCase):
    def test_transient_failure_is_retried_then_succeeds(self):
        self.cli.retry = RetryPolicy(attempts=3, base_delay=0.01, jitter=0)
        self.fab.add_sequence(
            ["mkdir"],
            [
                {"returncode": 1, "stderr": "429 TooManyRequests, Retry-After: 2"},
                {"returncode": 0, "stdout": "created"},
            ],
        )
        result = self.cli.mkdir(FabPath.workspace("WS [dev]"))
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.slept, [2.0], "Retry-After must be honoured")

    def test_permanent_failure_is_not_retried(self):
        self.cli.retry = RetryPolicy(attempts=3, base_delay=0.01, jitter=0)
        self.fab.add(["mkdir"], stderr="InvalidInputs: bad display name", returncode=1)
        with self.assertRaises(FabricCliError):
            self.cli.mkdir(FabPath.workspace("WS [dev]"))
        self.assertEqual(len(self.fab.calls), 1)
        self.assertEqual(self.slept, [])

    def test_exhausted_retries_raise(self):
        self.cli.retry = RetryPolicy(attempts=2, base_delay=0.01, jitter=0)
        self.fab.add(["mkdir"], stderr="503 Service Unavailable", returncode=1)
        with self.assertRaises(FabricCliError):
            self.cli.mkdir(FabPath.workspace("WS [dev]"))
        self.assertEqual(len(self.fab.calls), 2)


class DryRunTests(RunnerTestCase):
    def test_writes_are_skipped_and_reported(self):
        cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY, dry_run=True
        )
        self.fab.add(["exists"], stdout="false")

        self.assertFalse(cli.exists(FabPath.workspace("WS [dev]")), "reads still run in dry-run")
        result = cli.mkdir(FabPath.workspace("WS [dev]"), params="capacityname=Trial-01")

        self.assertTrue(result.dry_run)
        self.assertTrue(result.ok)
        self.assertEqual(len(cli.skipped_writes), 1)
        self.assertIn("mkdir", cli.skipped_writes[0])
        self.assertEqual([c[0] for c in self.fab.calls], ["exists"], "no write reached the CLI")


class ApiTests(RunnerTestCase):
    def test_response_is_parsed(self):
        self.fab.add_json(
            ["api", "workspaces"],
            {"status_code": 200, "text": {"value": [{"id": "abc"}]}, "headers": {"x-ms-request-id": "r1"}},
        )
        response = self.cli.api("workspaces")
        self.assertTrue(response.ok)
        self.assertEqual(response.body["value"][0]["id"], "abc")
        self.assertEqual(response.headers["x-ms-request-id"], "r1")

    def test_post_body_is_json_encoded_in_one_argv_element(self):
        self.fab.add_json(["api", "git/connect"], {"status_code": 200, "text": {}, "headers": {}})
        self.cli.api("workspaces/ws1/git/connect", method="post", body={"gitProviderDetails": {"branchName": "main"}})
        argv = self.fab.calls[-1]
        payload = argv[argv.index("-i") + 1]
        self.assertEqual(json.loads(payload)["gitProviderDetails"]["branchName"], "main")

    def test_202_is_polled_to_completion(self):
        self.fab.add_json(
            ["api", "git/updateFromGit"],
            {"status_code": 202, "text": None, "headers": {"x-ms-operation-id": "op-1"}},
        )
        self.fab.add_sequence(
            ["operations/op-1"],
            [
                {"stdout": json.dumps({"status_code": 200, "text": {"status": "Running"}, "headers": {}})},
                {"stdout": json.dumps({"status_code": 200, "text": {"status": "Succeeded"}, "headers": {}})},
            ],
        )
        response = self.cli.api("workspaces/ws1/git/updateFromGit", method="post", body={})
        self.assertEqual(response.body["status"], "Succeeded")
        self.assertEqual(len(self.slept), 1)

    def test_failed_operation_raises(self):
        self.fab.add_json(["api", "long"], {"status_code": 202, "text": None, "headers": {"x-ms-operation-id": "op-2"}})
        self.fab.add_json(["operations/op-2"], {"status_code": 200, "text": {"status": "Failed"}, "headers": {}})
        with self.assertRaises(FabricApiError):
            self.cli.api("workspaces/ws1/long", method="post", body={})

    def test_unexpected_status_raises(self):
        self.fab.add_json(["api", "workspaces/ws1"], {"status_code": 404, "text": {"errorCode": "WorkspaceNotFound"}, "headers": {}})
        with self.assertRaises(FabricApiError) as ctx:
            self.cli.api("workspaces/ws1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_403_maps_to_the_auth_exit_code(self):
        self.fab.add_json(["api", "admin/tags"], {"status_code": 403, "text": {"errorCode": "InsufficientPrivileges"}, "headers": {}})
        with self.assertRaises(FabricApiError) as ctx:
            self.cli.api("admin/tags")
        self.assertEqual(ctx.exception.exit_code, 4)


class TraceTests(RunnerTestCase):
    def test_every_invocation_is_recorded_once(self):
        self.fab.add(["exists"], stdout="true")
        self.cli.exists(FabPath.workspace("WS [dev]"))
        self.cli.exists(FabPath.workspace("WS2 [dev]"))
        records = [r for r in self.trace_records() if r["kind"] == "cli"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["run_id"], self.log.run_id)
        for record in records:
            self.assertIn("duration_s", record)
            self.assertIn("command", record)
            self.assertEqual(record["returncode"], 0)

    def test_nothing_is_written_when_no_trace_file_is_requested(self):
        log = RunLog(level=Level.DEBUG, stream=_Buffer(), _colour=False)
        cli = FabricCli(log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        self.fab.add(["exists"], stdout="true")
        cli.exists(FabPath.workspace("WS [dev]"))
        self.assertEqual(log.counts.get("cli"), 1)

    def test_info_level_hides_command_lines(self):
        console = _Buffer()
        log = RunLog(level=Level.INFO, stream=console, _colour=False)
        cli = FabricCli(log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY)
        self.fab.add(["exists"], stdout="true")
        cli.exists(FabPath.workspace("WS [dev]"))
        self.assertEqual(console.text.strip(), "")


class _Buffer:
    def __init__(self):
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


if __name__ == "__main__":
    unittest.main()


class ErrorReportingTests(unittest.TestCase):
    """The Fabric CLI writes its errors to stdout, not stderr."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def test_an_error_on_stdout_reaches_the_message(self):
        self.fab.add(["exists"], returncode=1, stdout="x exists: [ItemNotFound] no such workspace")
        with self.assertRaises(FabricOpsError) as caught:
            self.cli.exists("Missing.Workspace")
        self.assertIn("ItemNotFound", str(caught.exception))
        self.assertNotIn("no error output", str(caught.exception))

    def test_the_cli_prefix_is_trimmed(self):
        self.fab.add(["exists"], returncode=1, stdout="x exists: [ItemNotFound] gone")
        with self.assertRaises(FabricOpsError) as caught:
            self.cli.exists("Missing.Workspace")
        self.assertNotIn("x exists:", str(caught.exception))

    def test_an_authentication_failure_is_an_auth_error(self):
        """Exit code 4, and worth stopping the whole run for."""
        self.fab.add(
            ["exists"],
            returncode=1,
            stdout="x exists: [AuthenticationFailed] Failed to get access token: Something went wrong",
        )
        with self.assertRaises(AuthError) as caught:
            self.cli.exists("Any.Workspace")
        self.assertEqual(caught.exception.exit_code, ExitCode.AUTH_ERROR)
        self.assertIn("fab auth login", str(caught.exception))
        self.assertIn("Something went wrong", str(caught.exception))

    def test_an_ordinary_failure_is_not_an_auth_error(self):
        self.fab.add(["exists"], returncode=1, stdout="x exists: [ItemNotFound] gone")
        with self.assertRaises(FabricOpsError) as caught:
            self.cli.exists("Missing.Workspace")
        self.assertNotIsInstance(caught.exception, AuthError)


class ExistsTests(unittest.TestCase):
    """`fab exists` answers only when it can resolve the whole path."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.log = RunLog(level=Level.OFF)
        self.cli = FabricCli(
            self.log, executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def test_true_and_false_are_parsed(self):
        self.fab.add(["Yes.Workspace"], stdout="* true", command="exists")
        self.fab.add(["No.Workspace"], stdout="* false", command="exists")
        self.assertTrue(self.cli.exists("Yes.Workspace"))
        self.assertFalse(self.cli.exists("No.Workspace"))

    def test_a_missing_parent_answers_false_rather_than_raising(self):
        """Asking about an item in a workspace that does not exist is not an error."""
        self.fab.add(
            ["exists"],
            returncode=1,
            stdout="x exists: [NotFound] The Workspace 'Gone.Workspace' could not be found",
        )
        self.assertFalse(self.cli.exists("Gone.Workspace/Thing.Lakehouse"))

    def test_a_genuine_failure_still_raises(self):
        self.fab.add(["exists"], returncode=1, stdout="x exists: [InvalidPath] nonsense")
        with self.assertRaises(FabricCliError):
            self.cli.exists("??")

    def test_an_auth_failure_raises_even_with_check_off(self):
        self.fab.add(
            ["exists"],
            returncode=1,
            stdout="x exists: [AuthenticationFailed] Failed to get access token",
        )
        with self.assertRaises(AuthError):
            self.cli.exists("Any.Workspace")

    def test_unparseable_output_is_reported_as_such(self):
        self.fab.add(["exists"], stdout="maybe?", command="exists")
        with self.assertRaises(FabricCliError) as caught:
            self.cli.exists("Odd.Workspace")
        self.assertIn("could not parse", str(caught.exception))


class ApiErrorSummaryTests(unittest.TestCase):
    """Fabric buries the real explanation; truncating the raw JSON cut it mid-word."""

    def summarise(self, payload):
        from fabricops.errors import summarise_api_error

        return summarise_api_error(json.dumps(payload))

    def test_the_nested_detail_message_is_lifted_out(self):
        summary = self.summarise({
            "status": "Failed",
            "error": {
                "errorCode": "GitSyncFailed",
                "moreDetails": [{
                    "errorCode": "Dataset_Import_FailedToImportDataset",
                    "message": "The Fabric artifact 'abc' is not found",
                }],
            },
        })
        self.assertIn("GitSyncFailed", summary)
        self.assertIn("The Fabric artifact 'abc' is not found", summary)

    def test_a_flat_error_still_reads_well(self):
        summary = self.summarise({
            "requestId": "req-1",
            "errorCode": "DiscoverDependenciesFailed",
            "message": "Dependency discovery failed for one or more items.",
        })
        self.assertIn("[DiscoverDependenciesFailed]", summary)
        self.assertIn("Dependency discovery failed", summary)
        self.assertIn("req-1", summary)

    def test_several_details_are_all_reported(self):
        summary = self.summarise({
            "error": {"errorCode": "X", "moreDetails": [
                {"message": "first thing"}, {"message": "second thing"},
            ]},
        })
        self.assertIn("first thing", summary)
        self.assertIn("second thing", summary)

    def test_a_body_that_is_not_json_is_passed_through(self):
        from fabricops.errors import summarise_api_error

        self.assertEqual(summarise_api_error("<html>gateway timeout</html>"), "<html>gateway timeout</html>")

    def test_an_empty_body_does_not_crash(self):
        from fabricops.errors import summarise_api_error

        self.assertEqual(summarise_api_error(""), "")


class AuthNeverSwallowedTests(unittest.TestCase):
    """check=False means "not found is an answer", not "I don't mind if you couldn't ask"."""

    def setUp(self):
        self.fab = FakeFab()
        self.addCleanup(self.fab.cleanup)
        self.cli = FabricCli(
            RunLog(level=Level.OFF), executable=self.fab.executable, env=self.fab.env, retry=NO_RETRY
        )

    def test_a_401_raises_even_with_check_off(self):
        self.fab.add_json(["api", "thing"], {"status_code": 401, "text": {}, "headers": {}})
        with self.assertRaises(FabricApiError) as caught:
            self.cli.api("thing", check=False)
        self.assertEqual(caught.exception.exit_code, ExitCode.AUTH_ERROR)

    def test_a_403_is_left_to_the_caller(self):
        """401 is "who are you"; 403 is "I know, and no" - only the first is a login problem,
        and the caller knows which privilege it was actually missing."""
        self.fab.add_json(["api", "thing"], {"status_code": 403, "text": {}, "headers": {}})
        self.assertFalse(self.cli.api("thing", check=False).ok)

    def test_an_expected_401_is_still_allowed_through(self):
        self.fab.add_json(["api", "thing"], {"status_code": 401, "text": {}, "headers": {}})
        self.assertEqual(self.cli.api("thing", expect=(401,), check=False).status_code, 401)

    def test_a_404_is_still_an_answer(self):
        self.fab.add_json(["api", "thing"], {"status_code": 404, "text": {}, "headers": {}})
        self.assertFalse(self.cli.api("thing", check=False).ok)


class ErrorLineSelectionTests(unittest.TestCase):
    """The CLI interleaves progress and errors on stdout."""

    def first_line(self, text):
        from fabricops.errors import _first_line

        return _first_line(text)

    def test_the_error_line_wins_over_progress(self):
        """"Creating a new Connection..." is the first line and says nothing."""
        text = "Creating a new Connection...\nx mkdir: [Unauthorized] no access to the repository\n"
        self.assertEqual(self.first_line(text), "[Unauthorized] no access to the repository")

    def test_a_warning_beats_progress_too(self):
        self.assertEqual(
            self.first_line("Working...\n! the item has no sensitivity label\n"),
            "the item has no sensitivity label",
        )

    def test_progress_alone_is_all_there_is(self):
        self.assertEqual(self.first_line("Creating a new Connection..."), "Creating a new Connection...")

    def test_an_error_without_a_colon_is_not_truncated(self):
        self.assertEqual(self.first_line("x something went wrong"), "something went wrong")

    def test_nothing_is_nothing(self):
        self.assertEqual(self.first_line(""), "")


class LroErrorSummaryTests(unittest.TestCase):
    """A failed long-running operation must summarise, not dump raw JSON.

    `poll_operation` used to truncate the body to 500 characters before handing it over.
    That slice is not valid JSON, so `summarise_api_error` could not parse it and fell back
    to the raw text - defeating the very thing it exists for, and cutting the one useful
    sentence mid-word ("...is not fou").
    """

    def test_the_nested_detail_is_lifted_out_of_an_operation_body(self):
        import json

        from fabricops.errors import summarise_api_error

        body = json.dumps({
            "status": "Failed",
            "createdTimeUtc": "2026-09-08T19:33:57.9599319",
            "lastUpdatedTimeUtc": "2026-09-08T19:33:59.3191554",
            "percentComplete": None,
            "error": {
                "errorCode": "GitSyncFailed",
                "moreDetails": [{
                    "errorCode": "Dataset_Import_FailedToImportDataset",
                    "message": "Dataset Workload failed to import the dataset. Error returned: "
                               "'The Fabric artifact '00000000-0000-0000-0000-00000000a002' is not found'",
                }],
            },
        })
        summary = summarise_api_error(body)
        self.assertIn("[GitSyncFailed]", summary)
        self.assertIn("is not found", summary)
        self.assertNotIn("percentComplete", summary)

    def test_a_truncated_body_is_what_used_to_break_it(self):
        import json

        from fabricops.errors import summarise_api_error

        body = json.dumps({"error": {"errorCode": "GitSyncFailed", "moreDetails": [
            {"message": "x" * 600 + " is not found"}]}})
        # Valid JSON in, useful summary out.
        self.assertIn("[GitSyncFailed]", summarise_api_error(body))
        # A pre-truncated slice cannot be parsed, so it comes back as raw JSON. That is the
        # bug this documents: the caller must hand over the whole body.
        raw = summarise_api_error(body[:500])
        self.assertTrue(raw.startswith('{"error"'), raw[:40])
        self.assertNotIn("[GitSyncFailed]", raw)


class TraceResponseTests(unittest.TestCase):
    """At trace level the response body is logged, not just the request."""

    def run_api(self, level):
        from fabricops.fabric.cli import NO_RETRY, FabricCli
        from fabricops.obs.logging import Level, RunLog
        from support import FakeFab

        buffer = io.StringIO()
        log = RunLog(level=Level.parse(level), stream=buffer)
        fab = FakeFab()
        fab.add_json(
            ["api", "workspaces/ws-1/git/workspaceRelations"],
            {"status_code": 400, "text": {"errorCode": "BadRequest", "message": "Nope."}, "headers": {}},
        )
        cli = FabricCli(log, executable=fab.executable, env=fab.env, retry=NO_RETRY)
        cli.api("workspaces/ws-1/git/workspaceRelations", method="post", expect=(200, 400), check=False)
        return buffer.getvalue()

    def test_trace_shows_the_response_body(self):
        output = self.run_api("trace")
        self.assertIn("400", output)
        self.assertIn("Nope.", output)

    def test_debug_does_not(self):
        self.assertNotIn("Nope.", self.run_api("debug"))
